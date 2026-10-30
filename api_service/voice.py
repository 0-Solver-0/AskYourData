"""
api_service/voice.py
--------------------
Transcription de la question dictée (Muse Voice Transcribe, API Meta). Le texte obtenu suit
ensuite exactement le chemin d'une question écrite (PREP → recherche → SQL → …).

Configuration : le profil (modèle, adresse, prix, nom de la variable de la clé) est défini
uniquement dans config/llm_providers.yaml ; .env choisit le profil, les langues et contient la clé.
  VOICE_PROVIDER=meta_voice              profil de config/llm_providers.yaml
  VOICE_LANGUAGES=English,French         langues attendues (oriente sans imposer)

Entrée : WAV mono 16 bits, 16 ou 24 kHz (le navigateur convertit lui-même l'enregistrement).
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests
import yaml

from api_service.llm import DEFAULT_PROFILES_PATH

REQUIRED_FIELDS = ("base_url", "model", "api_key_env")
MAX_BYTES = 32 * 1024 * 1024


class VoiceError(Exception):
    pass


def voice_config(env: Optional[dict] = None, profiles_path: Optional[str] = None) -> dict:
    """Profil choisi par VOICE_PROVIDER, lu dans llm_providers.yaml. VoiceError s'il manque ou est incomplet."""
    env = env if env is not None else os.environ
    name = (env.get("VOICE_PROVIDER") or "").strip()
    if not name:
        raise VoiceError("VOICE_PROVIDER manquant dans .env (ex. meta_voice)")
    path = profiles_path or env.get("LLM_PROVIDERS_FILE") or DEFAULT_PROFILES_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            raw = (yaml.safe_load(fh) or {}).get(name)
    except OSError as exc:
        raise VoiceError(f"fichier de profils illisible : {path}") from exc
    if not raw:
        raise VoiceError(f"profil « {name} » absent de config/llm_providers.yaml")
    missing = [k for k in REQUIRED_FIELDS if not raw.get(k)]
    if missing:
        raise VoiceError(f"profil « {name} » : champ(s) manquant(s) {', '.join(missing)}")
    languages = [x.strip() for x in (env.get("VOICE_LANGUAGES") or "").split(",") if x.strip()]
    return {"profile": name, "timeout_s": 60, "price_per_hour": 0.0, **raw, "languages": languages,
            "api_key_set": bool(env.get(raw["api_key_env"]))}


def voice_status(env: Optional[dict] = None) -> dict:
    """Pour /health : configuration sans secret, ou l'erreur de configuration."""
    try:
        cfg = voice_config(env)
    except VoiceError as exc:
        return {"error": str(exc)}
    return {k: cfg[k] for k in ("profile", "model", "languages", "api_key_set")}


def transcribe(wav: bytes, env: Optional[dict] = None, post=requests.post) -> dict:
    env = env if env is not None else os.environ
    cfg = voice_config(env)
    key = env.get(cfg["api_key_env"])
    if not key:
        raise VoiceError(f"{cfg['api_key_env']} manquant dans .env")
    if not wav or len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise VoiceError("audio attendu : fichier WAV (mono, 16 bits, 16 ou 24 kHz)")
    if len(wav) > MAX_BYTES:
        raise VoiceError("audio trop long (32 Mo au plus)")
    settings = {"mode": "PUSH_TO_TALK", "model": cfg["model"], "audioEncoding": "WAV"}
    if cfg["languages"]:
        settings["languageBias"] = cfg["languages"]
    t0 = time.time()
    try:
        resp = post(f"{cfg['base_url'].rstrip('/')}/asr/transcribe",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    files={"request": (None, json.dumps(settings), "application/json"),
                           "audio": ("question.wav", wav, "audio/wav")},
                    timeout=cfg.get("timeout_s") or 60)
    except requests.RequestException as exc:
        raise VoiceError(f"service de transcription injoignable : {exc.__class__.__name__}") from None
    elapsed = int((time.time() - t0) * 1000)
    if resp.status_code != 200:
        detail = (resp.text or "")[:300].replace(key, "***")
        raise VoiceError(f"transcription refusée (HTTP {resp.status_code}) : {detail}")
    data = resp.json()
    duration_ms = int(data.get("audioDurationMs") or 0)
    return {"text": " ".join((data.get("transcript") or "").split()),
            "audio_ms": duration_ms, "elapsed_ms": elapsed, "model": cfg["model"],
            "cost_usd": round(duration_ms // 1000 / 3600 * float(cfg.get("price_per_hour") or 0), 6)}
