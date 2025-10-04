import os
import re
import json
from flask import Flask, render_template, request, redirect, url_for
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# ---------- IMPORTS OPCIONAIS ----------
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    import nltk
    from nltk.corpus import stopwords
    from nltk.stem import WordNetLemmatizer
except Exception:
    nltk = None

# ---------- CONFIG GERAL ----------
load_dotenv()
UPLOAD_FOLDER = "/tmp" 
ALLOWED_EXTENSIONS = {"txt", "pdf"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ---------- GENAI CONFIG ----------
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
GENAI_AVAILABLE = False

try:
    from google import genai
    GENAI_AVAILABLE = True
except Exception:
    try:
        import google.generativeai as genai
        GENAI_AVAILABLE = True
    except Exception:
        pass

if GENAI_AVAILABLE and GENAI_API_KEY:
    try:
        if hasattr(genai, "configure"):
            genai.configure(api_key=GENAI_API_KEY)
    except Exception as e:
        print("Aviso: falha ao configurar genai:", e)

# ---------- FUNÇÕES AUXILIARES ----------
def allowed_file(filename: str) -> bool:
    """Verifica se a extensão do arquivo é permitida."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(path: str) -> str:
    """Extrai texto de um arquivo PDF usando PyPDF2."""
    if PyPDF2 is None:
        raise RuntimeError("PyPDF2 não está instalado.")
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        return "\n".join(p.extract_text() or "" for p in reader.pages)


def load_text_from_file(path: str) -> str:
    """Carrega conteúdo de arquivo TXT ou PDF."""
    ext = path.rsplit(".", 1)[1].lower()
    if ext == "txt":
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    if ext == "pdf":
        return extract_text_from_pdf(path)
    return ""


def clean_text(text: str) -> str:
    """Limpa texto removendo links, emails e caracteres inválidos."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\S+@\S+", "", text)
    text = re.sub(r"[^0-9a-zA-ZÀ-ÿ\s\.,;:!?()\/-]", " ", text)
    return text.strip()


def preprocess(text: str) -> str:
    """Preprocessa texto: limpeza, tokenização e lematização (se NLTK disponível)."""
    txt = clean_text(text).lower()
    if nltk is None:
        return " ".join(re.findall(r"\b\w+\b", txt))

    # Garantir downloads necessários
    for resource in ["stopwords", "wordnet", "punkt"]:
        try:
            nltk.data.find(f"corpora/{resource}")
        except:
            nltk.download(resource, quiet=True)

    stop_words = (
        set(stopwords.words("portuguese"))
        if "portuguese" in stopwords.fileids()
        else set(stopwords.words("english"))
    )

    tokens = [t for t in nltk.word_tokenize(txt) if t.isalpha() and t not in stop_words]
    return " ".join(WordNetLemmatizer().lemmatize(t) for t in tokens)

# ---------- GENAI FUNÇÕES ----------
def call_genai_classify(original_text: str) -> dict:
    """Classifica texto como Produtivo ou Improdutivo usando GenAI."""
    prompt = (
        "Classifique o texto do e-mail abaixo em 'Produtivo' (requer ação) ou 'Improdutivo' (sem ação). "
        "Responda em JSON com os campos: category e explain.\n\n"
        f"EMAIL:\n{original_text}\n\nJSON:"
    )

    if not (GENAI_AVAILABLE and GENAI_API_KEY):
        raise RuntimeError("genai não disponível")

    resp = genai.generate_text(model="models/text-bison-001", input=prompt)
    text = getattr(resp, "text", str(resp))

    try:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        parsed = json.loads(match.group(1) if match else text)
        category = parsed.get("category", "")
        explain = parsed.get("explain", "")
        category = "Produtivo" if category.lower().startswith("p") else "Improdutivo"
        return {"category": category, "explain": explain}
    except Exception:
        fallback_cat = "Produtivo" if "produt" in text.lower() else "Improdutivo"
        return {"category": fallback_cat, "explain": text[:300]}


def call_genai_generate_reply(category: str, original_text: str) -> str:
    """Gera resposta automática baseada na categoria do e-mail."""
    if category == "Produtivo":
        instruction = (
            "Redija uma resposta profissional em português (3-6 linhas) para o e-mail abaixo, "
            "educada e objetiva, pedindo detalhes se necessário."
        )
    else:
        instruction = (
            "Redija uma resposta curta e educada em português para um e-mail improdutivo "
            "(ex.: felicitações, agradecimentos)."
        )

    prompt = f"{instruction}\n\nEMAIL:\n{original_text}\n\nRESPOSTA:"
    resp = genai.generate_text(model="models/text-bison-001", input=prompt)
    return getattr(resp, "text", str(resp)).strip()

# ---------- FALLBACK RULE-BASED ----------
FALLBACK_KEYWORDS_PROD = [
    "solicit", "pedido", "ajuda", "problema", "erro", "suporte",
    "status", "atualiza", "documento", "anexo", "fatura", "pagamento",
    "contrato", "reclama", "alterar", "cancelar", "informação", "dúvida",
    "duvida", "preciso", "favor", "urgente"
]

def rule_based_classify(text: str) -> dict:
    """Classificação simples baseada em palavras-chave."""
    score = sum(1 for kw in FALLBACK_KEYWORDS_PROD if kw in text.lower())
    if score:
        return {"category": "Produtivo", "explain": f"Palavras-chave detectadas ({score})."}
    return {"category": "Improdutivo", "explain": "Nenhum indício de ação detectado."}


def rule_based_reply(category: str, original_text: str) -> str:
    """Resposta padrão baseada na categoria."""
    if category == "Produtivo":
        return (
            "Olá,\n\nObrigado pelo contato. Recebemos sua mensagem e iremos analisar. "
            "Poderia, por favor, enviar mais detalhes (nº protocolo, anexos, prints) para verificarmos? "
            "Retornaremos em até 2 dias úteis.\n\nAtenciosamente,\nEquipe de Suporte"
        )
    return "Olá,\n\nAgradecemos a sua mensagem! Desejamos tudo de bom.\n\nAtenciosamente."

# ---------- ROTAS ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    """Processa input do usuário: arquivo ou texto."""
    text_input = request.form.get("text", "").strip()
    file = request.files.get("file")

    if file and allowed_file(file.filename):
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(file.filename))
        file.save(save_path)
        original_text = load_text_from_file(save_path)
    elif text_input:
        original_text = text_input
    else:
        return redirect(url_for("index"))

    # Classificação
    try:
        result = (
            call_genai_classify(original_text)
            if (GENAI_AVAILABLE and GENAI_API_KEY)
            else rule_based_classify(original_text)
        )
    except Exception:
        result = rule_based_classify(original_text)

    # Geração de resposta
    try:
        reply = (
            call_genai_generate_reply(result["category"], original_text)
            if (GENAI_AVAILABLE and GENAI_API_KEY)
            else rule_based_reply(result["category"], original_text)
        )
    except Exception:
        reply = rule_based_reply(result["category"], original_text)

    return render_template("index.html", result=result, reply=reply)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
