"""
api_service/app.py
------------------
Orchestrateur du pipeline AskYourData (architecture v3, voir documentation/ARCHITECTURE.md).

Pipeline actuel :
  1. schema_service   — identifie tables, colonnes, relations
  2. génération SQL   — rôle LLM `sql` (prompts/sql_generate.md), sans exécution pour l'instant

Endpoints :
  GET  /                   — interface web (api_service/static)
  POST /api/v1/transcribe  — question dictée (WAV) → texte (Muse Voice Transcribe)
  POST /api/v1/ask         — question → découpage → schéma → SQL → vérification → exécution → réponse
  POST /api/v1/translate   — question → découpage → schéma → SQL (sans exécution)
  POST /api/v1/schema/debug — diagnostic de la recherche de schéma
  POST /api/v1/schema/structured — recherche à partir d'une question découpée (sujet, mesures…)
  GET  /api/v1/health      — healthcheck des services
  GET  /api/v1/schemas     — liste les comptes disponibles
  POST /api/v1/index       — indexe les tables d'un compte (dry_run possible)
  GET  /api/v1/index/report — tables retenues / exclues d'un compte
  POST /api/v1/annotate/table   — annoter une table
  POST /api/v1/annotate/column  — annoter une colonne
  GET  /api/v1/schema/modules   — modules Odoo de l'index
  GET  /api/v1/schema/export    — fichier d'enrichissement d'un module (JSON)
  POST /api/v1/schema/import    — applique un fichier d'enrichissement (dry_run possible)
  GET  /api/v1/llm              — profils LLM choisis par rôle (sans secret)
  POST /api/v1/llm/test         — appel réel d'un rôle (vérifie la clé et le modèle)
  POST /api/v1/structure        — rôle PREP : question → découpage (et schéma si account_id)
"""

import logging
import os

import requests
from flask import Flask, jsonify, request, send_from_directory

from api_service import executor, pipeline, voice
from api_service.llm import ROLES, LLMClient, LLMError
from api_service.prep import structure_question

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("api_service")

# ── Config ─────────────────────────────────────────────────────────────────
SCHEMA_SERVICE_URL  = os.getenv("SCHEMA_SERVICE_URL",  "http://schema_service:8002")
SECRET_KEY          = os.getenv("SECRET_KEY",          "dev-secret")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.json.sort_keys = False   # garder l'ordre des champs (fichiers d'enrichissement lisibles)
app.config["SECRET_KEY"] = SECRET_KEY


# ── Client LLM (rôles prep et sql) ──────────────────────────────────────────
_llm: LLMClient | None = None
_llm_error: str | None = None


def get_llm() -> LLMClient:
    """Client créé au premier usage ; une configuration invalide est signalée, pas fatale."""
    global _llm, _llm_error
    if _llm is None:
        try:
            _llm = LLMClient.from_env()
            _llm_error = None
        except Exception as exc:  # noqa: BLE001
            _llm_error = str(exc)
            raise
    return _llm


def llm_status() -> dict:
    try:
        return get_llm().describe()
    except Exception:  # noqa: BLE001
        return {"error": _llm_error}


# ── Helpers ─────────────────────────────────────────────────────────────────

def call_extend_schema(account_id: int, question: str, structured: dict, missing: list) -> dict:
    """Recherche complémentaire des mots manquants signalés par le modèle SQL (schema_service)."""
    resp = requests.post(f"{SCHEMA_SERVICE_URL}/select/extend", timeout=60,
                         json={"account_id": account_id, "question": question, "structured": structured,
                               "missing": missing, "debug": True})
    return resp.json()


def call_structured_schema(account_id: int, question: str, structured: dict) -> dict:
    """Recherche structurée (schema_service), diagnostic compris (morceaux « non trouvés »)."""
    resp = requests.post(f"{SCHEMA_SERVICE_URL}/select/structured", timeout=60,
                         json={"account_id": account_id, "question": question, "structured": structured,
                               "debug": True})
    return resp.json()


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
def health():
    """Vérifie l'état de tous les services."""
    services = {
        "schema":  f"{SCHEMA_SERVICE_URL}/health",
    }
    statuses = {}
    for name, url in services.items():
        try:
            r = requests.get(url, timeout=5)
            statuses[name] = r.json()
        except Exception as exc:
            statuses[name] = {"error": str(exc)}

    all_ok = all(
        s.get("status") == "ok"
        for s in statuses.values()
        if isinstance(s, dict)
    )

    return jsonify({
        "api":      "ok",
        "services": statuses,
        "llm":      llm_status(),     # configuration seulement : aucun appel, aucun token
        "execution": executor.ping(),  # rôle PostgreSQL en lecture seule
        "voice":    {k: v for k, v in voice.voice_config().items() if k in ("model", "languages", "api_key_set")},
        "ready":    all_ok,
    })


@app.get("/api/v1/llm")
def llm_config():
    """Profil principal et de secours de chaque rôle, et présence des clés (jamais leur valeur)."""
    return jsonify(llm_status())


@app.post("/api/v1/llm/test")
def llm_test():
    """
    Appel réel d'un rôle, pour vérifier la clé, le modèle et le secours.
    Body : {"role": "prep" | "sql", "prompt": "Say OK", "max_tokens": 200}
    """
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "prep").lower()
    if role not in ROLES:
        return jsonify({"error": f"'role' doit valoir {' ou '.join(ROLES)}"}), 400
    prompt = (data.get("prompt") or "Reply with the single word OK.").strip()
    try:
        result = get_llm().complete(role, [{"role": "user", "content": prompt}],
                                    max_tokens=int(data.get("max_tokens") or 1000))
    except LLMError as exc:
        return jsonify({"error": str(exc), "attempts": exc.attempts}), 502
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Configuration LLM invalide : {exc}"}), 500
    return jsonify({"text": result.text, **result.telemetry()})


@app.get("/api/v1/schemas")
def list_schemas():
    """Comptes (bases sources) disponibles, depuis schema_service."""
    return _proxy("GET", "/accounts")


@app.post("/api/v1/index")
def index():
    """
    Indexe les tables d'un compte depuis sa base source.
    Body : { "account_id": 1, "dry_run": false, "embeddings": true }
      dry_run    : true → seulement la liste des tables retenues / exclues, rien n'est écrit
      embeddings : false → structure seulement (plus rapide)
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    payload = {"account_id": account_id}
    for key in ("dry_run", "embeddings"):
        if key in data:
            payload[key] = bool(data[key])

    try:
        resp = requests.post(f"{SCHEMA_SERVICE_URL}/index", json=payload, timeout=900)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout — indexation trop longue"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


def _proxy(method: str, path: str, timeout: int = 60, **kwargs):
    """Relaye une requête vers schema_service et renvoie sa réponse telle quelle."""
    try:
        resp = requests.request(method, f"{SCHEMA_SERVICE_URL}{path}", timeout=timeout, **kwargs)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


@app.get("/api/v1/schema/modules")
def schema_modules():
    """Modules Odoo de l'index, avec le nombre de tables enrichies. Query : ?account_id=1"""
    return _proxy("GET", "/modules", params=request.args)


@app.get("/api/v1/schema/export")
def schema_export():
    """
    Fichier d'enrichissement d'un module. Query : ?account_id=1&module=account
    (ou &tables=account_move,sale_order ; &include_technical=true pour toutes les colonnes)
    """
    return _proxy("GET", "/export", params=request.args)


@app.post("/api/v1/schema/import")
def schema_import():
    """
    Applique un fichier d'enrichissement rempli. Body : le fichier JSON.
    Query : ?account_id=1&dry_run=true (simulation : liste des changements sans rien écrire)
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "corps JSON attendu (fichier exporté par /api/v1/schema/export)"}), 400
    return _proxy("POST", "/import", timeout=300, params=request.args, json=payload)


@app.get("/api/v1/index/report")
def index_report():
    """Tables retenues et exclues (avec raison) d'un compte. Query : ?account_id=1"""
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        resp = requests.get(f"{SCHEMA_SERVICE_URL}/index/report",
                            params={"account_id": account_id}, timeout=30)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


@app.post("/api/v1/annotate/table")
def annotate_table():
    """Proxy vers schema_service /annotate/table"""
    data = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/annotate/table",
            json=data, timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/v1/annotate/column")
def annotate_column():
    """Proxy vers schema_service /annotate/column"""
    data = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/annotate/column",
            json=data, timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/v1/schema/debug")
def schema_debug():
    """
    Diagnostic de la recherche de schéma : tables retenues et pourquoi (recherche, jointure,
    référence), détail des scores, colonnes gardées, classement, taille en tokens.
    Body : {"question": "...", "account_id": 1}
    """
    data = request.get_json(silent=True) or {}
    if not data.get("question") or not data.get("account_id"):
        return jsonify({"error": "'question' et 'account_id' obligatoires"}), 400
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/select",
            json={"question": data["question"], "account_id": data["account_id"], "debug": True},
            timeout=30,
        )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


@app.post("/api/v1/schema/structured")
def schema_structured():
    """
    Recherche de schéma à partir d'une question déjà découpée (test manuel, puis rôle PREP).
    Body : {"account_id": 1, "question": "...", "structured": {"subject": "...", "measures": [...],
            "filters": [...], "group_by": [...], "period": "...", "order": "..."}}
    Le diagnostic (classement du sujet, colonnes trouvées par morceau) est toujours renvoyé.
    """
    data = request.get_json(silent=True) or {}
    if not data.get("account_id") or not data.get("structured"):
        return jsonify({"error": "'account_id' et 'structured' obligatoires"}), 400
    return _proxy("POST", "/select/structured", json={**data, "debug": True})


@app.post("/api/v1/structure")
def structure():
    """
    Rôle PREP : découpe la question (sujet, mesures, filtres, regroupements, période, tri).
    Body : {"question": "...", "account_id": 1 (facultatif), "reasoning": "low" (facultatif)}
    Avec account_id, la recherche structurée est lancée sur le découpage et le schéma est renvoyé.
    """
    import time
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    t0 = time.time()
    try:
        client = get_llm()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Configuration LLM invalide : {exc}"}), 500
    out = {"question": question, **structure_question(client, question, reasoning=data.get("reasoning"))}
    timings = {"structuring": int((time.time() - t0) * 1000)}

    account_id = data.get("account_id")
    if account_id and out["status"] in ("ok", "degraded"):
        t1 = time.time()
        try:
            resp = requests.post(f"{SCHEMA_SERVICE_URL}/select/structured", timeout=60,
                                 json={"account_id": account_id, "question": question,
                                       "structured": out["structured"], "debug": bool(data.get("debug"))})
            out["schema"] = resp.json()
        except Exception as exc:  # noqa: BLE001
            out["schema"] = {"error": f"schema_service : {exc}"}
        timings["schema"] = int((time.time() - t1) * 1000)
    timings["total"] = int((time.time() - t0) * 1000)
    out["timings_ms"] = timings
    return jsonify(out)


@app.post("/api/v1/translate")
def translate():
    """
    Question → SQL, sans exécution : découpage (PREP) → recherche structurée → génération (SQL) ;
    si le modèle signale un manque : recherche complémentaire puis une seule nouvelle génération.

    Body : {
      "question":   "Top 5 customers by revenue this year",
      "account_id": 1,
      "structured": {...}                       (facultatif : découpage fourni, PREP sauté)
      "reasoning":  {"prep": "low", "sql": "medium"}   (facultatif)
    }

    Retourne : {status: sql | missing | cannot_answer | refused | clarification_needed | error,
                sql, missing, reason, structured_request, prep_status, tables_used, schema_ddl,
                not_found, attempts (un par génération), missing_report, models, usage, cost_usd, timings_ms}
    """
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    account_id = data.get("account_id")
    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        client = get_llm()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Configuration LLM invalide : {exc}"}), 500
    logger.info("Translate — %s", question[:80])
    out = pipeline.translate(client, call_structured_schema, question, int(account_id),
                             structured=data.get("structured"), reasoning=data.get("reasoning"),
                             extend_fn=call_extend_schema)
    logger.info("Translate — %s en %s ms (%s $)", out["status"], out["timings_ms"].get("total"), out["cost_usd"])
    return jsonify(out)


@app.get("/")
def home():
    """Interface web : question écrite ou dictée → réponse."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.post("/api/v1/transcribe")
def transcribe():
    """
    Question dictée → texte (Muse Voice Transcribe). Body : multipart, champ « audio » (WAV mono
    16 bits, 16 ou 24 kHz) ou WAV brut. Le texte suit ensuite le chemin d'une question écrite.
    """
    upload = request.files.get("audio")
    wav = upload.read() if upload else request.get_data()
    try:
        out = voice.transcribe(wav)
    except voice.VoiceError as exc:
        return jsonify({"error": str(exc)}), 400
    logger.info("Transcribe — %s ms d'audio en %s ms", out["audio_ms"], out["elapsed_ms"])
    return jsonify(out)


@app.post("/api/v1/ask")
def ask():
    """
    Question → réponse tirée de la base : translate, puis vérification du SQL, exécution en lecture
    seule, correction par le modèle SQL avec l'erreur exacte (2 fois au plus), résumé par gabarit.

    Body : {"question": "...", "account_id": 1, "structured": {...} (facultatif),
            "reasoning": {"prep": "low", "sql": "low"} (facultatif)}

    Retourne les champs de translate, plus : status ok, answer {columns, rows, row_count, truncated,
    summary}, executed_sql, checks (une entrée par vérification / exécution).
    """
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    account_id = data.get("account_id")
    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        client = get_llm()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Configuration LLM invalide : {exc}"}), 500
    logger.info("Ask — %s", question[:80])
    out = pipeline.ask(client, call_structured_schema, executor.execute, question, int(account_id),
                       structured=data.get("structured"), reasoning=data.get("reasoning"),
                       extend_fn=call_extend_schema)
    logger.info("Ask — %s en %s ms (%s $)", out["status"], out["timings_ms"].get("total"), out["cost_usd"])
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
