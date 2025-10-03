import os
import re
import json
from flask import Flask, render_template_string, request, redirect, url_for
from werkzeug.utils import secure_filename

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

from dotenv import load_dotenv
load_dotenv()

GENAI_AVAILABLE = False
try:
    from google import genai
    GENAI_AVAILABLE = True
except Exception:
    try:
        import google.generativeai as genai
        GENAI_AVAILABLE = True
    except Exception:
        GENAI_AVAILABLE = False

# ---------- CONFIG ----------
UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"txt", "pdf"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Lê chave API do .env
GENAI_API_KEY = os.getenv("GENAI_API_KEY")

# Configura o genai se disponível
if GENAI_AVAILABLE and GENAI_API_KEY:
    try:
        if hasattr(genai, "configure"):
            genai.configure(api_key=GENAI_API_KEY)
    except Exception as e:
        print("Aviso: falha ao configurar genai:", e)


# ---------- FUNÇÕES AUXILIARES ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(path):
    if PyPDF2 is None:
        raise RuntimeError("PyPDF2 não está instalado.")
    text = []
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for p in reader.pages:
            txt = p.extract_text()
            if txt:
                text.append(txt)
    return "\n".join(text)


def load_text_from_file(path):
    ext = path.rsplit(".", 1)[1].lower()
    if ext == "txt":
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    elif ext == "pdf":
        return extract_text_from_pdf(path)
    else:
        return ""


def clean_text(text):
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\S+@\S+", "", text)
    text = re.sub(r"[^0-9a-zA-ZÀ-ÿ\s\.,;:!?()\/-]", " ", text)
    return text.strip()


def preprocess(text):
    txt = clean_text(text).lower()
    if nltk is None:
        tokens = re.findall(r"\b\w+\b", txt)
        return " ".join(tokens)
    try:
        nltk.data.find("corpora/stopwords")
    except:
        nltk.download("stopwords", quiet=True)
    try:
        nltk.data.find("corpora/wordnet")
    except:
        nltk.download("wordnet", quiet=True)
    try:
        nltk.data.find("tokenizers/punkt")
    except:
        nltk.download("punkt", quiet=True)

    stop_words = set(stopwords.words("portuguese")) if "portuguese" in stopwords.fileids() else set(stopwords.words("english"))
    lemmatizer = WordNetLemmatizer()
    tokens = nltk.word_tokenize(txt)
    tokens = [t for t in tokens if t.isalpha() and t not in stop_words]
    lemmas = [lemmatizer.lemmatize(t) for t in tokens]
    return " ".join(lemmas)


# ---------- GENAI HELPERS ----------
def call_genai_classify(original_text):
    prompt = (
        "Classifique o texto do e-mail abaixo em 'Produtivo' (requer ação) ou 'Improdutivo' (sem ação). "
        "Responda em JSON com os campos: category e explain.\n\n"
        f"EMAIL:\n{original_text}\n\nJSON:"
    )

    if not GENAI_AVAILABLE or not GENAI_API_KEY:
        raise RuntimeError("genai não disponível")

    resp = genai.generate_text(model="models/text-bison-001", input=prompt)
    text = getattr(resp, "text", str(resp))

    try:
        m = re.search(r"(\{.*\})", text, re.DOTALL)
        parsed = json.loads(m.group(1) if m else text)
        category = parsed.get("category", "")
        explain = parsed.get("explain", "")
        if category.lower().startswith("p"):
            category = "Produtivo"
        else:
            category = "Improdutivo"
        return {"category": category, "explain": explain}
    except Exception:
        if "produt" in text.lower():
            return {"category": "Produtivo", "explain": text[:300]}
        else:
            return {"category": "Improdutivo", "explain": text[:300]}


def call_genai_generate_reply(category, original_text):
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


# ---------- FALLBACK ----------
FALLBACK_KEYWORDS_PROD = [
    "solicit", "pedido", "ajuda", "problema", "erro", "suporte", "status", "atualiza", "documento", "anexo",
    "fatura", "pagamento", "contrato", "reclama", "alterar", "cancelar", "informação", "dúvida", "duvida",
    "preciso", "favor", "urgente"
]


def rule_based_classify(text):
    t = text.lower()
    score = sum(1 for kw in FALLBACK_KEYWORDS_PROD if kw in t)
    if score >= 1:
        return {"category": "Produtivo", "explain": f"Palavras-chave produtivas detectadas ({score})."}
    else:
        return {"category": "Improdutivo", "explain": "Nenhum indício de ação detectado."}


def rule_based_reply(category, original_text):
    if category == "Produtivo":
        return (
            "Olá,\n\nObrigado pelo contato. Recebemos sua mensagem e iremos analisar. "
            "Poderia, por favor, enviar mais detalhes (nº protocolo, anexos, prints) para verificarmos? "
            "Retornaremos em até 2 dias úteis.\n\nAtenciosamente,\nEquipe de Suporte"
        )
    else:
        return "Olá,\n\nAgradecemos a sua mensagem! Desejamos tudo de bom.\n\nAtenciosamente."


# ---------- NOVO HTML ----------
INDEX_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Classificação de Emails</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center">
  <div class="bg-white shadow-xl rounded-2xl overflow-hidden flex w-11/12 max-w-5xl h-[80vh]">
    
    <!-- Lado esquerdo: formulário -->
    <div class="w-1/2 p-8 border-r overflow-y-auto">
      <h1 class="text-2xl font-bold text-gray-800 mb-6 text-center">📧 Classificador de Emails</h1>
      
      <!-- Upload -->
      <form action="/process" method="post" enctype="multipart/form-data" class="flex flex-col space-y-4">
        <input type="file" name="file" class="border border-gray-300 p-2 rounded-lg focus:ring-2 focus:ring-blue-500">
        <textarea name="text" rows="8" class="border border-gray-300 p-2 rounded-lg focus:ring-2 focus:ring-blue-500" placeholder="Ou cole o conteúdo do email aqui..."></textarea>
        <button type="submit" class="bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 px-4 rounded-lg shadow">Classificar</button>
      </form>
    </div>
    
    <!-- Lado direito: resultados -->
    <div class="w-1/2 p-8 overflow-y-auto">
      {% if result %}
        <h2 class="text-xl font-semibold mb-4">Resultado</h2>
        <p>Categoria: 
          {% if result.category == 'Produtivo' %}
            <span class="text-green-600 font-bold">{{result.category}}</span>
          {% else %}
            <span class="text-red-600 font-bold">{{result.category}}</span>
          {% endif %}
        </p>
        <p class="mt-2"><strong>Justificativa:</strong> {{result.explain}}</p>
        <hr class="my-4">
        <h3 class="text-lg font-semibold mb-2">Resposta sugerida:</h3>
        <pre class="bg-gray-100 p-3 rounded-lg whitespace-pre-wrap">{{reply}}</pre>
        <form action="/" method="get" class="mt-4">
          <button type="submit" class="bg-gray-500 hover:bg-gray-600 text-white px-4 py-2 rounded-lg">Limpar</button>
        </form>
      {% else %}
        <p class="text-gray-500">Preencha o formulário para ver a classificação.</p>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/process", methods=["POST"])
def process():
    text_input = request.form.get("text", "").strip()
    file = request.files.get("file")

    original_text = ""
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(save_path)
        original_text = load_text_from_file(save_path)
    elif text_input:
        original_text = text_input
    else:
        return redirect(url_for("index"))

    try:
        result = call_genai_classify(original_text) if (GENAI_AVAILABLE and GENAI_API_KEY) else rule_based_classify(original_text)
    except:
        result = rule_based_classify(original_text)

    try:
        reply = call_genai_generate_reply(result["category"], original_text) if (GENAI_AVAILABLE and GENAI_API_KEY) else rule_based_reply(result["category"], original_text)
    except:
        reply = rule_based_reply(result["category"], original_text)

    return render_template_string(INDEX_HTML, result=result, reply=reply)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
