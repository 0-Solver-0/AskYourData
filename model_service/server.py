"""
model_service/server.py
-----------------------
SQLCoder 7B-2 — float16 sur GPU sm_120 (RTX 5070 Ti Laptop)

Changements vs version précédente :
  - BitsAndBytesConfig supprimé (import + usage) — cause du crash
  - Chargement float16 uniquement (~7 Go VRAM sur 11.6 disponibles)
  - Fallback CPU si CUDA indisponible
"""

import os
import gc
import sys
import logging
import time
import threading

from flask import Flask, request, jsonify
from transformers import AutoTokenizer, AutoModelForCausalLM
import torch

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("model_service")

# ── Config ─────────────────────────────────────────────────────────────────
MODEL_PATH     = os.getenv("MODEL_PATH",     "/app/models/sqlcoder-7b-2")
DEVICE         = os.getenv("DEVICE",         "cuda")
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", 512))
TEMPERATURE    = float(os.getenv("TEMPERATURE",  0.1))

app         = Flask(__name__)
model       = None
tokenizer   = None
load_error  = None
model_ready = False


# ── Chargement ─────────────────────────────────────────────────────────────
def load_model():
    global model, tokenizer, load_error, model_ready

    try:
        logger.info("🚀 Thread de chargement démarré")

        logger.info("📂 Vérification du chemin : %s", MODEL_PATH)
        if not os.path.isdir(MODEL_PATH):
            raise RuntimeError(f"Dossier introuvable : {MODEL_PATH}")
        if not os.path.isfile(os.path.join(MODEL_PATH, "config.json")):
            raise RuntimeError(f"config.json absent dans {MODEL_PATH}")
        logger.info("✅ Dossier modèle OK")

        cuda_ok = DEVICE == "cuda" and torch.cuda.is_available()
        if cuda_ok:
            props = torch.cuda.get_device_properties(0)
            logger.info("GPU : %s | VRAM : %.1f Go | sm_%d%d",
                        props.name, props.total_memory / 1e9,
                        props.major, props.minor)
        else:
            logger.warning("⚠️  CUDA non disponible — mode CPU (lent)")

        logger.info("⏳ Chargement tokenizer…")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
        logger.info("✅ Tokenizer OK")

        logger.info("⏳ Chargement modèle float16 (~7 Go VRAM)…")
        t0    = time.time()
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_PATH,
            local_files_only=True,
            trust_remote_code=True,
            torch_dtype=torch.float16 if cuda_ok else torch.float32,
            device_map="auto" if cuda_ok else None,
        )
        model.eval()
        elapsed = round(time.time() - t0, 1)

        if cuda_ok:
            vram = torch.cuda.memory_allocated(0) / 1e9
            logger.info("✅ Modèle chargé en %ss | VRAM : %.2f Go", elapsed, vram)
        else:
            logger.info("✅ Modèle chargé en %ss (CPU)", elapsed)

        logger.info("🔥 Warmup…")
        dummy = tokenizer("SELECT 1", return_tensors="pt")
        if cuda_ok:
            dummy = {k: v.cuda() for k, v in dummy.items()}
        with torch.no_grad():
            model.generate(**dummy, max_new_tokens=8, do_sample=False)
        if cuda_ok:
            torch.cuda.synchronize()
        logger.info("✅ Warmup OK — service prêt !")

        model_ready = True

    except Exception as exc:
        load_error = str(exc)
        logger.exception("💥 Échec chargement : %s", exc)


logger.info("📌 Démarrage du thread model-loader…")
threading.Thread(target=load_model, daemon=True, name="model-loader").start()


# ── Prompt template SQLCoder ───────────────────────────────────────────────
PROMPT_TEMPLATE = """\
### Task
Generate a SQL query to answer [QUESTION]{question}[/QUESTION]

### Instructions
- Always use HAVING instead of WHERE for filtering on aggregated values
- Always use SUM(column) to calculate totals, not AVG(column)
- For "total spent", "total spending" → use SUM(o.total)
- For "average order value" → use AVG(o.total) per customer
- For month-over-month, running totals → use LAG() or SUM() OVER()
- For rankings within groups → use RANK() OVER (PARTITION BY ...)
- Always use date_trunc() for date grouping in PostgreSQL
- Prefer CTEs over subqueries for complex queries

### Database Schema
The query will run on a database with the following schema:
{schema}

### Answer
Given the database schema, here is the SQL query that [QUESTION]{question}[/QUESTION]
[SQL]
"""

def build_prompt(q, s):
    return PROMPT_TEMPLATE.format(question=q, schema=s)

def extract_sql(raw):
    if "[SQL]" in raw:
        raw = raw.split("[SQL]")[-1]
    for stop in ["[/SQL]", "###", "\n\n\n"]:
        if stop in raw:
            raw = raw.split(stop)[0]
    return raw.strip()


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    if load_error:
        return jsonify({"status": "error", "error": load_error}), 500
    if not model_ready:
        return jsonify({"status": "loading", "model_loaded": False}), 503

    cuda_ok = DEVICE == "cuda" and torch.cuda.is_available()
    info = {
        "status":       "ok",
        "model_loaded": True,
        "model_path":   MODEL_PATH,
        "device":       DEVICE,
        "dtype":        "float16",
    }
    if cuda_ok:
        info["gpu_name"]          = torch.cuda.get_device_name(0)
        info["vram_allocated_gb"] = round(torch.cuda.memory_allocated(0) / 1e9, 2)
        info["vram_reserved_gb"]  = round(torch.cuda.memory_reserved(0)  / 1e9, 2)
    return jsonify(info), 200


@app.post("/generate")
def generate():
    if load_error:
        return jsonify({"error": f"Échec chargement : {load_error}"}), 500
    if not model_ready:
        return jsonify({"error": "Modèle en cours de chargement"}), 503

    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    schema   = data.get("schema",   "").strip()

    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not schema:
        return jsonify({"error": "'schema' obligatoire"}), 400

    cuda_ok = DEVICE == "cuda" and torch.cuda.is_available()

    try:
        t0     = time.time()
        inputs = tokenizer(build_prompt(question, schema), return_tensors="pt")
        if cuda_ok:
            inputs = {k: v.cuda() for k, v in inputs.items()}

        with torch.no_grad():
            with torch.autocast("cuda", dtype=torch.float16) if cuda_ok else torch.no_grad():
                output_ids = model.generate(
                    **inputs,
                    max_new_tokens=MAX_NEW_TOKENS,
                    temperature=TEMPERATURE,
                    do_sample=False,
                    pad_token_id=tokenizer.eos_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                    repetition_penalty=1.1,
                )

        generated = output_ids[0][inputs["input_ids"].shape[1]:]
        sql       = extract_sql(tokenizer.decode(generated, skip_special_tokens=True))
        elapsed   = round(time.time() - t0, 2)

        logger.info("✅ SQL en %ss | %s", elapsed, sql[:80])
        return jsonify({"sql": sql, "elapsed": elapsed})

    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        gc.collect()
        logger.error("💥 OOM GPU")
        return jsonify({"error": "GPU out of memory"}), 500
    except Exception as exc:
        logger.exception("Erreur génération")
        return jsonify({"error": str(exc)}), 500


@app.post("/clear_cache")
def clear_cache():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        gc.collect()
        return jsonify({"vram_gb": round(torch.cuda.memory_allocated(0) / 1e9, 2)})
    return jsonify({"status": "pas de GPU"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)