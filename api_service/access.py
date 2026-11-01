"""
api_service/access.py
---------------------
Accès public par clé : chaque personne reçoit une clé avec un quota de questions (voix et texte),
toutes les questions sont journalisées, et un plafond global en dollars bloque tout s'il est atteint.

Stockage : base NL2SQL (tables access_keys et usage_log, créées au premier usage).
  - access_keys : nom, empreinte SHA-256 de la clé (jamais la clé en clair), quota, questions utilisées,
                  coût cumulé, révocation, expiration ;
  - usage_log   : une ligne par question posée (kind = ask) ou par transcription (kind = transcribe) :
                  clé, date, question posée et reformulée, statut, coût, temps, SQL exécuté.
                  Ni lignes de résultat, ni audio, ni adresse IP.

Réglages (.env) :
  PUBLIC_MODE=true                  clé obligatoire, routes d'administration fermées derrière le proxy
  ACCESS_DEFAULT_QUOTA=20           questions par clé (modifiable à la création de chaque clé)
  ACCESS_GLOBAL_BUDGET_USD=20       plafond global : au-delà, plus aucune question n'est acceptée
  ACCESS_MIN_INTERVAL_S=5           délai minimal entre deux questions d'une même clé
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

KEY_PREFIX = "ayd_"
MAX_QUESTION_CHARS = 500
FAILED_KEY_LIMIT = 5             # essais de clé fausse par adresse…
FAILED_KEY_BLOCK_S = 15 * 60     # … avant blocage de cette adresse (mémoire du processus seulement)
ACTIVE_WINDOW_S = 5 * 60         # « connecté » = clé utilisée dans les 5 dernières minutes

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS access_keys (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    key_hash    CHAR(64) NOT NULL UNIQUE,
    key_hint    VARCHAR(12) NOT NULL,
    quota       INT NOT NULL,
    used        INT NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(12, 6) NOT NULL DEFAULT 0,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at  TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    last_used   TIMESTAMP,
    last_ask    TIMESTAMP,
    last_voice  TIMESTAMP
);
CREATE TABLE IF NOT EXISTS usage_log (
    id           SERIAL PRIMARY KEY,
    key_id       INT REFERENCES access_keys(id) ON DELETE CASCADE,
    kind         VARCHAR(12) NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    question     TEXT,
    rewritten    TEXT,
    status       VARCHAR(30),
    cost_usd     NUMERIC(12, 6) NOT NULL DEFAULT 0,
    elapsed_ms   INT,
    audio_ms     INT,
    executed_sql TEXT
);
-- Groupes de clés (jury, public…) : un groupe entier se coupe et se rouvre sans rien effacer
ALTER TABLE access_keys ADD COLUMN IF NOT EXISTS grp      VARCHAR(30) NOT NULL DEFAULT 'public';
ALTER TABLE access_keys ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_usage_log_key  ON usage_log(key_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_time ON usage_log(created_at);
"""


class AccessError(Exception):
    """Refus d'accès : `status` HTTP et message pour la personne (en anglais, comme l'interface)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Settings:
    public: bool
    default_quota: int
    global_budget_usd: float
    min_interval_s: float

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        env = env if env is not None else os.environ

        def num(name: str, default: float) -> float:
            try:
                return float(str(env.get(name, "")).strip())
            except ValueError:
                return default

        return cls(public=str(env.get("PUBLIC_MODE", "")).strip().lower() in ("1", "true", "yes"),
                   default_quota=int(num("ACCESS_DEFAULT_QUOTA", 20)),
                   global_budget_usd=num("ACCESS_GLOBAL_BUDGET_USD", 20.0),
                   min_interval_s=num("ACCESS_MIN_INTERVAL_S", 5.0))


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def new_key() -> str:
    """ayd_ + 32 caractères aléatoires (192 bits) : impossible à deviner."""
    return KEY_PREFIX + secrets.token_urlsafe(24)


# ── Connexion et schéma ──────────────────────────────────────────────────────

def connect(env: Optional[dict] = None):
    import psycopg2

    env = env if env is not None else os.environ
    if env.get("ACCESS_DB_DSN"):
        return psycopg2.connect(env["ACCESS_DB_DSN"])
    return psycopg2.connect(host=env.get("NL2SQL_DB_HOST", "postgres_nl2sql"), port=env.get("NL2SQL_DB_PORT", "5432"),
                            dbname=env.get("NL2SQL_DB_NAME", "nl2sql"), user=env.get("NL2SQL_DB_USER", "nl2sql"),
                            password=env.get("NL2SQL_DB_PASSWORD", ""), connect_timeout=5)


_schema_ready = False


def ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


# ── Gestion des clés (outil tools/keys.py) ───────────────────────────────────

def create_key(conn, name: str, quota: int, expires_days: Optional[int] = None,
               group: str = "public") -> tuple[str, int]:
    """Crée une clé ; renvoie (clé en clair, affichée une seule fois, id)."""
    ensure_schema(conn)
    key = new_key()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO access_keys (name, key_hash, key_hint, quota, expires_at, grp)
            VALUES (%s, %s, %s, %s, CASE WHEN %s IS NULL THEN NULL ELSE NOW() + make_interval(days => %s) END, %s)
            RETURNING id
        """, (name.strip(), hash_key(key), key[-4:], int(quota), expires_days, expires_days or 0,
              (group or "public").strip().lower()))
        key_id = cur.fetchone()[0]
    conn.commit()
    return key, key_id


def list_keys(conn) -> list[dict]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, grp, key_hint, quota, used, cost_usd, revoked, disabled, expires_at, created_at, last_used
            FROM access_keys ORDER BY grp, id
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def revoke_key(conn, name: str) -> int:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE access_keys SET revoked = TRUE WHERE name = %s", (name,))
        n = cur.rowcount
    conn.commit()
    return n


def set_disabled(conn, disabled: bool, *, group: Optional[str] = None, name: Optional[str] = None) -> int:
    """Coupe (ou rouvre) toutes les clés d'un groupe, ou une clé par son nom. Rien n'est effacé."""
    ensure_schema(conn)
    if not group and not name:
        raise ValueError("préciser un groupe ou un nom")
    with conn.cursor() as cur:
        if group:
            cur.execute("UPDATE access_keys SET disabled = %s WHERE grp = %s", (disabled, group.strip().lower()))
        else:
            cur.execute("UPDATE access_keys SET disabled = %s WHERE name = %s", (disabled, name))
        n = cur.rowcount
    conn.commit()
    return n


def set_quota(conn, name: str, quota: int) -> int:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE access_keys SET quota = %s WHERE name = %s", (int(quota), name))
        n = cur.rowcount
    conn.commit()
    return n


def forget(conn, name: str) -> int:
    """Droit à l'effacement : supprime les questions journalisées d'une personne (la clé est révoquée)."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM usage_log WHERE key_id IN (SELECT id FROM access_keys WHERE name = %s)
        """, (name,))
        n = cur.rowcount
        cur.execute("UPDATE access_keys SET revoked = TRUE WHERE name = %s", (name,))
    conn.commit()
    return n


def export_questions(conn) -> list[dict]:
    """Questions sans le nom de leur auteur (pour un benchmark ou une analyse)."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT created_at, question, rewritten, status, cost_usd, elapsed_ms
            FROM usage_log WHERE kind = 'ask' ORDER BY created_at
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── Contrôle à chaque appel ──────────────────────────────────────────────────

class Guard:
    """Essais de clé fausse par adresse, en mémoire (par processus ; rien n'est écrit)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def check_blocked(self, ip: str) -> None:
        with self.lock:
            now = self.clock()
            recent = [t for t in self.failures.get(ip, []) if now - t < FAILED_KEY_BLOCK_S]
            self.failures[ip] = recent
            if len(recent) >= FAILED_KEY_LIMIT:
                raise AccessError(429, "blocked", "Too many invalid keys from this address. Try again in 15 minutes.")

    def failed(self, ip: str) -> None:
        with self.lock:
            self.failures.setdefault(ip, []).append(self.clock())


def global_spent(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log")
        return float(cur.fetchone()[0])


def find_key(conn, key: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, grp, quota, used, revoked, disabled,
                   (expires_at IS NOT NULL AND expires_at < NOW()) AS expired,
                   EXTRACT(EPOCH FROM (NOW() - last_used)) AS idle_s
            FROM access_keys WHERE key_hash = %s
        """, (hash_key(key),))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def check_key(conn, key: Optional[str], ip: str, guard: Guard, settings: Settings) -> dict:
    """Clé valide, non révoquée, non expirée ; adresse non bloquée ; plafond global non atteint."""
    guard.check_blocked(ip)
    key = (key or "").strip()
    found = find_key(conn, key) if key.startswith(KEY_PREFIX) and len(key) <= 80 else None
    if not found:
        guard.failed(ip)
        raise AccessError(401, "invalid_key", "This access key is not valid.")
    if found["revoked"] or found["expired"] or found["disabled"]:
        raise AccessError(403, "revoked", "This access key is no longer active.")
    if global_spent(conn) >= settings.global_budget_usd:
        raise AccessError(503, "budget", "The public test has reached its budget. Thank you for trying AskYourData!")
    return found


def remaining(found: dict) -> int:
    return max(0, int(found["quota"]) - int(found["used"]))


def reserve_question(conn, found: dict, settings: Settings) -> dict:
    """
    Réserve une question avant de la traiter (atomique : deux onglets ne dépassent pas le quota) et
    applique le délai minimal entre deux questions. Renvoie la clé à jour.
    """
    if remaining(found) <= 0:
        raise AccessError(429, "quota", f"You have used all {found['quota']} questions of this key. Thank you!")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE access_keys SET used = used + 1, last_used = NOW(), last_ask = NOW()
            WHERE id = %s AND used < quota AND NOT revoked
              AND (last_ask IS NULL OR last_ask < NOW() - make_interval(secs => %s))
            RETURNING quota, used
        """, (found["id"], settings.min_interval_s))
        row = cur.fetchone()
    conn.commit()
    if not row:
        raise AccessError(429, "too_fast", "Please wait a few seconds before asking again.")
    return {**found, "quota": row[0], "used": row[1]}


def touch_for_voice(conn, found: dict, settings: Settings) -> None:
    """
    Une transcription demande une question restante et respecte le délai minimal entre deux
    transcriptions ; elle ne consomme pas de question (la question dictée part ensuite par /ask).
    """
    if remaining(found) <= 0:
        raise AccessError(429, "quota", f"You have used all {found['quota']} questions of this key. Thank you!")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE access_keys SET last_used = NOW(), last_voice = NOW()
            WHERE id = %s AND (last_voice IS NULL OR last_voice < NOW() - make_interval(secs => %s))
            RETURNING id
        """, (found["id"], settings.min_interval_s))
        ok = cur.fetchone()
    conn.commit()
    if not ok:
        raise AccessError(429, "too_fast", "Please wait a few seconds before speaking again.")


def record(conn, key_id: int, kind: str, *, question: Optional[str] = None, rewritten: Optional[str] = None,
           status: Optional[str] = None, cost_usd: float = 0.0, elapsed_ms: Optional[int] = None,
           audio_ms: Optional[int] = None, executed_sql: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO usage_log (key_id, kind, question, rewritten, status, cost_usd, elapsed_ms, audio_ms,
                                   executed_sql)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (key_id, kind, question, rewritten, status, cost_usd or 0, elapsed_ms, audio_ms, executed_sql))
        cur.execute("UPDATE access_keys SET cost_usd = cost_usd + %s WHERE id = %s", (cost_usd or 0, key_id))
    conn.commit()


# ── Tableau de bord ──────────────────────────────────────────────────────────

def stats(conn, settings: Settings, recent: int = 200) -> dict:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT k.id, k.name, k.grp, k.key_hint, k.quota, k.used, k.cost_usd, k.revoked, k.disabled, k.last_used,
                   (k.last_used IS NOT NULL AND k.last_used > NOW() - make_interval(secs => %s)) AS active
            FROM access_keys k ORDER BY k.grp, k.last_used DESC NULLS LAST, k.id
        """, (ACTIVE_WINDOW_S,))
        cols = [d[0] for d in cur.description]
        keys = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("""
            SELECT l.created_at, k.name, l.kind, l.question, l.rewritten, l.status, l.cost_usd, l.elapsed_ms
            FROM usage_log l LEFT JOIN access_keys k ON k.id = l.key_id
            WHERE l.kind = 'ask' ORDER BY l.created_at DESC LIMIT %s
        """, (recent,))
        cols = [d[0] for d in cur.description]
        questions = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("""
            SELECT count(*) FILTER (WHERE kind = 'ask'), count(*) FILTER (WHERE kind = 'transcribe'),
                   COALESCE(SUM(cost_usd), 0) FROM usage_log
        """)
        n_ask, n_voice, spent = cur.fetchone()
    return {
        "active_now": sum(1 for k in keys if k["active"]),
        "keys": keys,
        "questions": questions,
        "totals": {"questions": n_ask, "transcriptions": n_voice, "spent_usd": float(spent),
                   "budget_usd": settings.global_budget_usd},
    }
