"""
api_service/llm.py
------------------
Client LLM unique, par rôle (voir documentation/ARCHITECTURE.md § 7).

    client = LLMClient.from_env()
    result = client.complete("prep", [{"role": "system", "content": ...}, {"role": "user", "content": ...}])
    result.text, result.tokens_in, result.cost_usd, result.provider …

- Deux rôles : `prep` (découpage de la question) et `sql` (génération et correction du SQL).
  Chacun choisit un profil principal et un profil de secours dans .env (PREP_PROVIDER, PREP_FALLBACK…).
- Les profils (URL, modèle, prix, paramètre de raisonnement) sont dans config/llm_providers.yaml,
  sans clé : la clé est lue dans la variable nommée par `api_key_env`.
- Format chat completions (OpenAI), compris par l'API Meta, Baseten et les serveurs locaux.
- Erreur réseau, 429 ou 5xx : un nouvel essai sur le même profil, puis le profil de secours.
  Autre erreur (clé absente ou refusée, requête invalide) : directement le profil de secours.
- Réponse coupée sans contenu (plafond atteint pendant le raisonnement) : erreur explicite.
- La clé n'apparaît jamais dans les journaux, les erreurs ni les réponses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("api_service.llm")

ROLES = ("prep", "sql")
DEFAULT_PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "llm_providers.yaml")
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_DELAY_S = 1.0

# transport(url, headers, payload, timeout) → (status HTTP, corps JSON ou None, texte brut)
Transport = Callable[[str, dict, dict, float], tuple[int, Optional[dict], str]]


class LLMError(RuntimeError):
    """Échec d'un appel, après les nouveaux essais et le secours."""

    def __init__(self, message: str, attempts: Optional[list[dict]] = None):
        super().__init__(message)
        self.attempts = attempts or []


@dataclass
class Profile:
    name: str
    base_url: str
    model: str
    api_key_env: str
    reasoning_param: Optional[str] = None
    reasoning_default: Optional[str] = None
    max_tokens: int = 4000
    timeout_s: float = 90
    price_input: float = 0.0
    price_cached_input: float = 0.0
    price_output: float = 0.0

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Profile":
        for key in ("base_url", "model", "api_key_env"):
            if not data.get(key):
                raise ValueError(f"Profil LLM « {name} » : champ « {key} » manquant")
        reasoning = data.get("reasoning") or {}
        prices = data.get("price_per_mtok") or {}
        return cls(
            name=name,
            base_url=str(data["base_url"]).rstrip("/"),
            model=str(data["model"]),
            api_key_env=str(data["api_key_env"]),
            reasoning_param=reasoning.get("param"),
            reasoning_default=reasoning.get("default"),
            max_tokens=int(data.get("max_tokens") or 4000),
            timeout_s=float(data.get("timeout_s") or 90),
            price_input=float(prices.get("input") or 0),
            price_cached_input=float(prices.get("cached_input", prices.get("input")) or 0),
            price_output=float(prices.get("output") or 0),
        )

    def api_key(self, env: dict) -> str:
        return (env.get(self.api_key_env) or "").strip()

    def cost(self, tokens_in: int, tokens_cached: int, tokens_out: int) -> float:
        fresh = max(tokens_in - tokens_cached, 0)
        return round((fresh * self.price_input + tokens_cached * self.price_cached_input
                      + tokens_out * self.price_output) / 1_000_000, 6)


@dataclass
class RoleConfig:
    role: str
    primary: str
    fallback: Optional[str] = None
    reasoning: Optional[str] = None     # surcharge du raisonnement pour ce rôle


@dataclass
class LLMResult:
    text: str
    role: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    finish_reason: Optional[str] = None
    fallback_used: bool = False
    attempts: list[dict] = field(default_factory=list)

    def telemetry(self) -> dict:
        out = asdict(self)
        out.pop("text")
        return out


# ── Chargement ───────────────────────────────────────────────────────────────

def load_profiles(path: Optional[str] = None) -> dict[str, Profile]:
    path = path or os.getenv("LLM_PROVIDERS_FILE") or DEFAULT_PROFILES_PATH
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return {name: Profile.from_dict(name, data or {}) for name, data in raw.items()}


def roles_from_env(env: dict) -> dict[str, RoleConfig]:
    roles = {}
    for role in ROLES:
        prefix = role.upper()
        primary = (env.get(f"{prefix}_PROVIDER") or "").strip()
        if not primary:
            continue
        roles[role] = RoleConfig(
            role=role,
            primary=primary,
            fallback=(env.get(f"{prefix}_FALLBACK") or "").strip() or None,
            reasoning=(env.get(f"{prefix}_REASONING") or "").strip() or None,
        )
    return roles


def _requests_transport(url: str, headers: dict, payload: dict, timeout: float):
    import requests

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, resp.text[:500]


# ── Client ───────────────────────────────────────────────────────────────────

class LLMClient:
    def __init__(self, profiles: dict[str, Profile], roles: dict[str, RoleConfig],
                 env: Optional[dict] = None, transport: Optional[Transport] = None,
                 retry_delay_s: float = RETRY_DELAY_S):
        self.profiles = profiles
        self.roles = roles
        self.env = env if env is not None else dict(os.environ)
        self.transport = transport or _requests_transport
        self.retry_delay_s = retry_delay_s
        for rc in roles.values():
            for name in (rc.primary, rc.fallback):
                if name and name not in profiles:
                    raise ValueError(f"Rôle {rc.role} : profil « {name} » absent de llm_providers.yaml")

    @classmethod
    def from_env(cls, path: Optional[str] = None, **kwargs) -> "LLMClient":
        env = dict(os.environ)
        return cls(load_profiles(path), roles_from_env(env), env=env, **kwargs)

    # Description sans secret (pour /health et /api/v1/llm)
    def describe(self) -> dict:
        out = {}
        for role in ROLES:
            rc = self.roles.get(role)
            if not rc:
                out[role] = {"configured": False}
                continue
            entry = {"configured": True}
            for kind, name in (("primary", rc.primary), ("fallback", rc.fallback)):
                if not name:
                    continue
                p = self.profiles[name]
                entry[kind] = {"profile": name, "model": p.model, "base_url": p.base_url,
                               "api_key_env": p.api_key_env, "api_key_set": bool(p.api_key(self.env)),
                               "reasoning": rc.reasoning or p.reasoning_default}
            out[role] = entry
        return out

    def complete(self, role: str, messages: list[dict], *, max_tokens: Optional[int] = None,
                 reasoning: Optional[str] = None, temperature: float = 0.0,
                 extra: Optional[dict] = None) -> LLMResult:
        rc = self.roles.get(role)
        if not rc:
            raise LLMError(f"Rôle « {role} » non configuré ({role.upper()}_PROVIDER absent de .env)")
        attempts: list[dict] = []
        chain = [(rc.primary, False)] + ([(rc.fallback, True)] if rc.fallback and rc.fallback != rc.primary else [])
        for name, is_fallback in chain:
            profile = self.profiles[name]
            key = profile.api_key(self.env)
            if not key:
                attempts.append({"provider": name, "error": f"clé absente ({profile.api_key_env})"})
                continue
            for attempt in (1, 2):
                outcome = self._call(profile, key, role, rc, messages, max_tokens, reasoning, temperature, extra)
                outcome["attempt_info"]["provider"] = name
                attempts.append(outcome["attempt_info"])
                if outcome.get("result"):
                    result = outcome["result"]
                    result.fallback_used = is_fallback
                    result.attempts = attempts
                    return result
                if not outcome.get("retryable") or attempt == 2:
                    break
                time.sleep(self.retry_delay_s)
        summary = "; ".join(f"{a['provider']}: {a.get('error')}" for a in attempts)
        raise LLMError(f"Appel {role} impossible — {summary}", attempts)

    def _call(self, profile: Profile, key: str, role: str, rc: RoleConfig, messages: list[dict],
              max_tokens: Optional[int], reasoning: Optional[str], temperature: float,
              extra: Optional[dict]) -> dict:
        payload: dict[str, Any] = {
            "model": profile.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": int(max_tokens or profile.max_tokens),
        }
        effort = reasoning or rc.reasoning or profile.reasoning_default
        if profile.reasoning_param and effort:
            payload[profile.reasoning_param] = effort
        payload.update(extra or {})
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        url = f"{profile.base_url}/chat/completions"
        t0 = time.time()
        try:
            status, body, raw = self.transport(url, headers, payload, profile.timeout_s)
        except Exception as exc:  # noqa: BLE001 — réseau, délai dépassé…
            return {"retryable": True,
                    "attempt_info": {"error": _redact(f"{type(exc).__name__}: {exc}", key)}}
        latency = int((time.time() - t0) * 1000)
        if status != 200 or not isinstance(body, dict):
            detail = (body or {}).get("error") if isinstance(body, dict) else raw
            return {"retryable": status in RETRYABLE_STATUS,
                    "attempt_info": {"status": status, "latency_ms": latency,
                                     "error": _redact(f"HTTP {status} {str(detail)[:300]}", key)}}

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        tokens_cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tokens_reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        info = {"status": status, "latency_ms": latency, "tokens_in": tokens_in, "tokens_out": tokens_out}
        if not text.strip():
            info["error"] = ("réponse coupée avant le contenu (plafond max_tokens atteint pendant le "
                             "raisonnement)" if finish == "length" else f"réponse vide (finish_reason={finish})")
            return {"retryable": False, "attempt_info": info}
        return {"attempt_info": info, "result": LLMResult(
            text=text, role=role, provider=profile.name, model=body.get("model") or profile.model,
            tokens_in=tokens_in, tokens_cached=tokens_cached, tokens_out=tokens_out,
            tokens_reasoning=tokens_reasoning, latency_ms=latency,
            cost_usd=profile.cost(tokens_in, tokens_cached, tokens_out), finish_reason=finish,
        )}


def _redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


# ── Extraction tolérante ─────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> Any:
    """Premier objet ou tableau JSON valide du texte (blocs ```json``` compris). ValueError sinon."""
    candidates = [m.group(1) for m in _FENCE.finditer(text or "")] + [text or ""]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        for i, ch in enumerate(chunk):
            if ch not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(chunk[i:])
                return value
            except ValueError:
                continue
    raise ValueError("aucun JSON valide dans la réponse")


def extract_tag(text: str, tag: str) -> Optional[str]:
    """
    Contenu de <tag>…</tag> (le dernier si plusieurs), sans espaces autour ; None si absent.
    Balise fermante oubliée par le modèle : le contenu va jusqu'à la balise suivante ou la fin.
    """
    found = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", text or "", re.S | re.I)
    if found:
        return found[-1]
    unclosed = re.search(rf"<{tag}>\s*(.*?)\s*(?=<[a-z_]+>|$)", text or "", re.S | re.I)
    return unclosed.group(1) if unclosed and unclosed.group(1) else None
