# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in for the dashboard: one password, kept as a salted hash in data/auth.json.

A new installation starts with the default password and asks for a new one at the first sign-in.
RESET_PASSWORD=true in .env puts the default password back (and signs everyone out) each time the
dashboard starts, for when the password is forgotten; remove the line again afterwards.
Sessions are random tokens in an HttpOnly cookie; only their SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque

log = logging.getLogger(__name__)

DEFAULT_PASSWORD = "kamstrup"
MIN_LENGTH = 8
ITERATIONS = 240_000
LONG_SESSION = 30 * 86400   # "keep me signed in"
SHORT_SESSION = 12 * 3600   # otherwise
MAX_FAILURES, FAILURE_WINDOW = 5, 300  # 5 wrong passwords in 5 minutes: wait


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        assert algo == "pbkdf2_sha256"
        test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(test.hex(), digest)
    except Exception:  # noqa: BLE001
        return False


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, data_dir: str, reset: bool = False):
        self.path = os.path.join(data_dir, "auth.json")
        self.data: dict = {}
        self.failures: dict[str, deque] = defaultdict(deque)
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if reset or not self.data.get("hash"):
            if reset:
                log.warning("RESET_PASSWORD is set: the password is back to the default and everyone is signed out. "
                            "Remove RESET_PASSWORD from .env once you have chosen a new password.")
            self.data = {"hash": hash_password(DEFAULT_PASSWORD), "must_change": True, "sessions": {}}
            self._save()
        self.reset_active = reset

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def must_change(self) -> bool:
        return bool(self.data.get("must_change"))

    # --- signing in ---------------------------------------------------------------------
    def locked_for(self, ip: str) -> int:
        """Seconds this address has to wait after too many wrong passwords (0 = may try)."""
        q, now = self.failures[ip], time.time()
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        return int(FAILURE_WINDOW - (now - q[0])) + 1 if len(q) >= MAX_FAILURES else 0

    def login(self, password: str, ip: str, remember: bool = True) -> tuple[str, int] | None:
        if self.locked_for(ip):
            return None
        if not verify_password(password or "", self.data["hash"]):
            self.failures[ip].append(time.time())
            log.warning("wrong password from %s", ip)
            return None
        self.failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        ttl = LONG_SESSION if remember else SHORT_SESSION
        self._prune()
        self.data.setdefault("sessions", {})[_key(token)] = {"created": time.time(), "expires": time.time() + ttl}
        self._save()
        return token, ttl

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.data.get("sessions", {}).get(_key(token))
        if not s or s["expires"] < time.time():
            return None
        return s

    def logout(self, token: str | None) -> None:
        if token and self.data.get("sessions", {}).pop(_key(token), None):
            self._save()

    def logout_others(self, token: str | None) -> int:
        keep = _key(token) if token else None
        sessions = self.data.get("sessions", {})
        n = len([k for k in sessions if k != keep])
        self.data["sessions"] = {k: v for k, v in sessions.items() if k == keep}
        self._save()
        return n

    def _prune(self) -> None:
        now = time.time()
        self.data["sessions"] = {k: v for k, v in self.data.get("sessions", {}).items() if v["expires"] > now}

    def check(self, password: str | None) -> bool:
        return verify_password(password or "", self.data["hash"])

    # --- changing the password ----------------------------------------------------------
    def change(self, current: str | None, new: str, token: str | None) -> None:
        """current may be left out only while the default password must still be replaced."""
        if not self.must_change and not verify_password(current or "", self.data["hash"]):
            raise ValueError("the current password is not right")
        new = new or ""
        if len(new) < MIN_LENGTH:
            raise ValueError(f"use at least {MIN_LENGTH} characters")
        if new == DEFAULT_PASSWORD:
            raise ValueError("choose a password other than the default one")
        if verify_password(new, self.data["hash"]):
            raise ValueError("choose a password other than the current one")
        self.data["hash"] = hash_password(new)
        self.data["must_change"] = False
        self.logout_others(token)  # other devices sign in again with the new password
        log.info("password changed")
