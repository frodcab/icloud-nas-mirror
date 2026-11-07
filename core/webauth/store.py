# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Estado de ejecución del login en SQLite (WAL): sesiones, intentos, bloqueos de IP y pasos TOTP.

No guarda identidades (eso es usuarios.json). Los ids de sesión se guardan solo como hash SHA-256:
quien lea la base de datos no puede suplantar a nadie.
"""

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY, user TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL,
    expires REAL NOT NULL, remember INTEGER NOT NULL DEFAULT 0, mfa INTEGER NOT NULL DEFAULT 0,
    ip TEXT, ua TEXT, pending_totp TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    ts REAL NOT NULL, ip TEXT NOT NULL, user TEXT, ok INTEGER NOT NULL, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_ip ON attempts (ip, ts);
CREATE INDEX IF NOT EXISTS attempts_user ON attempts (user, ts);
CREATE TABLE IF NOT EXISTS ip_bans (
    ip TEXT PRIMARY KEY, until REAL NOT NULL, level INTEGER NOT NULL, reason TEXT,
    created REAL NOT NULL, manual INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS totp_steps (user TEXT PRIMARY KEY, step INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets (
    user TEXT PRIMARY KEY, code_hash TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS known_ips (user TEXT NOT NULL, ip TEXT NOT NULL, first_seen REAL NOT NULL,
    PRIMARY KEY (user, ip));
"""

# Bloqueo progresivo de IP: 15 min, 1 h, 24 h, 7 días
BAN_STEPS = [15 * 60, 60 * 60, 24 * 3600, 7 * 24 * 3600]
FAIL_WINDOW = 15 * 60
FAIL_LIMIT = 5
BAN_MEMORY = 30 * 24 * 3600   # una reincidencia dentro de 30 días sube de nivel


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    def __init__(self, path: Path | str, clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self.clock = clock
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def _q(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- sesiones -------------------------------------------------------------------------------

    def create_session(self, user: str, ip: str, ua: str, ttl: float, remember: bool, mfa: bool,
                       pending_totp: str | None = None) -> str:
        token = secrets.token_urlsafe(32)          # 256 bits
        now = self.clock()
        self._q("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_h(token), user, now, now, now + ttl, int(remember), int(mfa), ip, (ua or "")[:200], pending_totp))
        return token

    def get_session(self, token: str) -> dict | None:
        if not token:
            return None
        rows = self._q("SELECT id_hash, user, created, last_seen, expires, remember, mfa, ip, ua, pending_totp "
                       "FROM sessions WHERE id_hash = ?", (_h(token),))
        if not rows:
            return None
        r = rows[0]
        return {"id_hash": r[0], "user": r[1], "created": r[2], "last_seen": r[3], "expires": r[4],
                "remember": bool(r[5]), "mfa": bool(r[6]), "ip": r[7], "ua": r[8], "pending_totp": r[9]}

    def touch_session(self, id_hash: str, expires: float) -> None:
        self._q("UPDATE sessions SET last_seen = ?, expires = ? WHERE id_hash = ?", (self.clock(), expires, id_hash))

    def rotate_session(self, old_token: str, mfa: bool, ttl: float, pending_totp: str | None = None) -> str | None:
        """Id nuevo para la misma sesión (tras validar el 2º factor): evita la fijación de sesión."""
        s = self.get_session(old_token)
        if not s:
            return None
        self.delete_session(s["id_hash"])
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._q("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_h(token), s["user"], s["created"], now, now + ttl, int(s["remember"]), int(mfa),
                 s["ip"], s["ua"], pending_totp))
        return token

    def set_pending_totp(self, id_hash: str, enc_secret: str | None) -> None:
        self._q("UPDATE sessions SET pending_totp = ? WHERE id_hash = ?", (enc_secret, id_hash))

    def delete_session(self, id_hash: str) -> None:
        self._q("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))

    def delete_user_sessions(self, user: str, except_hash: str | None = None) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM sessions WHERE user = ? AND id_hash != ?",
                                      (user, except_hash or "")).rowcount

    def list_sessions(self, user: str) -> list[dict]:
        self._q("DELETE FROM sessions WHERE expires < ?", (self.clock(),))
        rows = self._q("SELECT id_hash, created, last_seen, expires, ip, ua, mfa FROM sessions "
                       "WHERE user = ? ORDER BY last_seen DESC", (user,))
        return [{"id": r[0][:16], "id_hash": r[0], "created": r[1], "last_seen": r[2], "expires": r[3],
                 "ip": r[4], "ua": r[5], "mfa": bool(r[6])} for r in rows]

    # --- intentos y bloqueos de IP --------------------------------------------------------------

    def record_attempt(self, ip: str, user: str | None, ok: bool, kind: str) -> None:
        now = self.clock()
        self._q("INSERT INTO attempts VALUES (?,?,?,?,?)", (now, ip, user, int(ok), kind))
        self._q("DELETE FROM attempts WHERE ts < ?", (now - 90 * 24 * 3600,))

    def recent_failures_ip(self, ip: str) -> int:
        return self._q("SELECT COUNT(*) FROM attempts WHERE ip = ? AND ok = 0 AND ts >= ?",
                       (ip, self.clock() - FAIL_WINDOW))[0][0]

    def user_failures(self, user: str) -> tuple[int, float | None]:
        """(fallos seguidos recientes, hora del último) para la espera por usuario."""
        rows = self._q("SELECT ts, ok FROM attempts WHERE user = ? AND kind IN ('password','totp') "
                       "AND ts >= ? ORDER BY ts DESC LIMIT 20", (user, self.clock() - FAIL_WINDOW))
        n = 0
        for _ts, ok in rows:
            if ok:
                break
            n += 1
        return n, (rows[0][0] if rows and not rows[0][1] else None)

    def recent_attempts(self, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT ts, ip, user, ok, kind FROM attempts ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": r[0], "ip": r[1], "user": r[2], "ok": bool(r[3]), "kind": r[4]} for r in rows]

    def ban_until(self, ip: str) -> float | None:
        rows = self._q("SELECT until FROM ip_bans WHERE ip = ?", (ip,))
        return rows[0][0] if rows and rows[0][0] > self.clock() else None

    def ban(self, ip: str, reason: str, manual: bool = False, seconds: float | None = None) -> dict:
        """Bloquea la IP subiendo de nivel si reincide. Devuelve el bloqueo."""
        now = self.clock()
        prev = self._q("SELECT level, created FROM ip_bans WHERE ip = ?", (ip,))
        level = 0
        if prev and now - prev[0][1] <= BAN_MEMORY:
            level = min(prev[0][0] + 1, len(BAN_STEPS) - 1)
        duration = seconds if seconds is not None else BAN_STEPS[level]
        attempts = self.recent_failures_ip(ip)
        self._q("INSERT OR REPLACE INTO ip_bans VALUES (?,?,?,?,?,?,?)",
                (ip, now + duration, level, reason, now, int(manual), attempts))
        return {"ip": ip, "until": now + duration, "level": level, "reason": reason, "attempts": attempts}

    def unban(self, ip: str) -> bool:
        with self._lock:
            # Se deja el registro caducado (sirve para la reincidencia) salvo si era manual
            return self._conn.execute("UPDATE ip_bans SET until = ? WHERE ip = ?", (self.clock(), ip)).rowcount > 0

    def list_bans(self) -> list[dict]:
        rows = self._q("SELECT ip, until, level, reason, created, manual, attempts FROM ip_bans "
                       "WHERE until > ? ORDER BY until DESC", (self.clock(),))
        return [{"ip": r[0], "until": r[1], "level": r[2], "reason": r[3], "created": r[4],
                 "manual": bool(r[5]), "attempts": r[6]} for r in rows]

    # --- restablecer contraseña por email -------------------------------------------------------

    def set_password_reset(self, user: str, code_hash: str, ttl: float) -> None:
        now = self.clock()
        self._q("INSERT OR REPLACE INTO password_resets VALUES (?,?,?,?,0)", (user, code_hash, now, now + ttl))

    def get_password_reset(self, user: str) -> dict | None:
        rows = self._q("SELECT code_hash, created, expires, attempts FROM password_resets WHERE user = ?", (user,))
        if not rows:
            return None
        r = rows[0]
        return {"code_hash": r[0], "created": r[1], "expires": r[2], "attempts": r[3]}

    def bump_password_reset(self, user: str) -> None:
        self._q("UPDATE password_resets SET attempts = attempts + 1 WHERE user = ?", (user,))

    def delete_password_reset(self, user: str) -> None:
        self._q("DELETE FROM password_resets WHERE user = ?", (user,))

    def reset_requests_last_hour(self, user: str) -> int:
        return self._q("SELECT COUNT(*) FROM attempts WHERE user = ? AND kind = 'reset_request' AND ts >= ?",
                       (user, self.clock() - 3600))[0][0]

    # --- TOTP e IP conocidas --------------------------------------------------------------------

    def last_totp_step(self, user: str) -> int:
        rows = self._q("SELECT step FROM totp_steps WHERE user = ?", (user,))
        return rows[0][0] if rows else -1

    def set_totp_step(self, user: str, step: int) -> None:
        self._q("INSERT OR REPLACE INTO totp_steps VALUES (?, ?)", (user, step))

    def remember_ip(self, user: str, ip: str) -> bool:
        """True si la IP es nueva para ese usuario (para avisar)."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO known_ips VALUES (?,?,?)", (user, ip, self.clock()))
            return cur.rowcount > 0
