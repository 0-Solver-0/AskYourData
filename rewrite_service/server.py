"""
rewrite_service/server.py
-------------------------
Qwen2.5 3B Instruct — traduction et clarification de questions NL2SQL.

Rôle unique : traduire la question en anglais clair et non ambigu.
  ❌ Ne génère pas de SQL
  ❌ Ne suggère pas de GROUP BY, JOIN, HAVING etc.
  ✅ Traduit français → anglais
  ✅ Lève les ambiguïtés métier ("panier moyen" → "average order value")
  ✅ Conserve l'intention sans ajouter de technique

Endpoints :
  GET  /health   — état du service
  POST /rewrite  — traduit et clarifie une question
"""

import os
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
logger = logging.getLogger("rewrite_service")

# ── Config ─────────────────────────────────────────────────────────────────
MODEL_PATH     = os.getenv("REWRITE_MODEL_PATH", "/app/models/qwen2.5-3b-instruct")
DEVICE         = os.getenv("DEVICE",             "cuda")
MAX_NEW_TOKENS = int(os.getenv("REWRITE_MAX_NEW_TOKENS", 256))

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

        if not os.path.isdir(MODEL_PATH):
            raise RuntimeError(f"Dossier introuvable : {MODEL_PATH}")
        if not os.path.isfile(os.path.join(MODEL_PATH, "config.json")):
            raise RuntimeError(f"config.json absent dans {MODEL_PATH}")
        logger.info("📂 Dossier modèle OK : %s", MODEL_PATH)

        cuda_ok = DEVICE == "cuda" and torch.cuda.is_available()
        if cuda_ok:
            props = torch.cuda.get_device_properties(0)
            logger.info("GPU : %s | VRAM : %.1f Go | sm_%d%d",
                        props.name, props.total_memory / 1e9,
                        props.major, props.minor)
        else:
            logger.warning("⚠️  CUDA non disponible — CPU")

        logger.info("⏳ Chargement tokenizer…")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
        logger.info("✅ Tokenizer OK")

        logger.info("⏳ Chargement modèle float16 (~3 Go VRAM)…")
        t0    = time.time()
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_PATH,
            local_files_only=True,
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
        _generate("Hello")
        logger.info("✅ Warmup OK — service prêt !")
        model_ready = True

    except Exception as exc:
        load_error = str(exc)
        logger.exception("💥 Échec chargement : %s", exc)


logger.info("📌 Démarrage du thread model-loader…")
threading.Thread(target=load_model, daemon=True, name="model-loader").start()


# ── Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a question rewriter for a SQL generation system.
Your only job is to translate and clarify the user's question into clear, unambiguous English.

Rules:
- Translate to English if the question is in another language
- Resolve ambiguous business terms:
    "panier moyen"        → "average order value"
    "chiffre d'affaires"  → "total revenue"
    "meilleurs clients"   → "customers with the highest total spending"
    "produits phares"     → "best selling products"
- Keep the original intent exactly — do not add or remove meaning
- Do NOT write SQL, mention tables, columns, GROUP BY, JOIN, or any technical hints
- Return only the rewritten question, nothing else
"""

def build_messages(question: str) -> list:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": question},
    ]

def _generate(question: str) -> str:
    cuda_ok  = DEVICE == "cuda" and torch.cuda.is_available()
    messages = build_messages(question)

    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = tokenizer(text, return_tensors="pt")
    if cuda_ok:
        inputs = {k: v.cuda() for k, v in inputs.items()}

    with torch.no_grad():
        with torch.autocast("cuda", dtype=torch.float16) if cuda_ok else torch.no_grad():
            output_ids = model.generate(
                **inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
                temperature=1.0,
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

    generated = output_ids[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(generated, skip_special_tokens=True).strip()


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


@app.post("/rewrite")
def rewrite():
    """
    Body    : { "question": "Quel est le panier moyen par client ?" }
    Retourne: {
      "question_original":  "Quel est le panier moyen par client ?",
      "question_rewritten": "What is the average order value per customer?",
      "elapsed_s": 1.2
    }
    """
    if load_error:
        return jsonify({"error": f"Échec chargement : {load_error}"}), 500
    if not model_ready:
        return jsonify({"error": "Modèle en cours de chargement"}), 503

    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400

    try:
        t0        = time.time()
        rewritten = _generate(question)
        elapsed   = round(time.time() - t0, 2)

        logger.info("✅ Rewrite en %ss | '%s' → '%s'",
                    elapsed, question[:50], rewritten[:50])

        return jsonify({
            "question_original":  question,
            "question_rewritten": rewritten,
            "elapsed_s":          elapsed,
        })

    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        return jsonify({"error": "GPU out of memory"}), 500
    except Exception as exc:
        logger.exception("Erreur rewrite")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8003, debug=False, threaded=True)