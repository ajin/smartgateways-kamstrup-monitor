# SPDX-License-Identifier: GPL-3.0-or-later
"""API keys for other applications (Home Assistant, Homey, scripts).

Every key can read: every GET of the data API, and ask for a fresh reading (POST /api/refresh).
No key can manage keys, passwords or settings: that needs a person signed in to the dashboard.
Keys are only needed while sign-in is on; with sign-in off, the API is open to the whole network.

Keys look like km_<43 random characters>. Only their SHA-256 is stored (data/apikeys.json), so a key is
shown once, when it is created. Send it as "Authorization: Bearer <key>" or "X-API-Key: <key>".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

PREFIX = "km_"
SCOPES = ("read",)
MAX_LABEL = 60

# never with a key, whatever its level: these are for a person signed in to the dashboard
HUMAN_ONLY = ("/api/auth/", "/api/keys", "/api/settings")


def key_allows(scope: str, method: str, path: str) -> bool:
    if any(path == p.rstrip("/") or path.startswith(p) for p in HUMAN_ONLY):
        return False
    if method in ("GET", "HEAD"):
        return path.startswith("/api/")
    return method == "POST" and path == "/api/refresh"  # a fresh reading changes nothing


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeys:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "apikeys.json")
        try:
            with open(self.path) as f:
                self.keys: list[dict] = json.load(f).get("keys", [])
        except (OSError, ValueError, AttributeError):
            self.keys = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"keys": self.keys}, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def create(self, label: str, scope: str) -> tuple[dict, str]:
        label = " ".join(str(label or "").split())[:MAX_LABEL]
        if not label:
            raise ValueError("give the key a name, e.g. Home Assistant")
        if scope not in SCOPES:
            raise ValueError("keys can only read")
        if any(k["label"].lower() == label.lower() for k in self.keys):
            raise ValueError(f"there is already a key called {label}")
        key = PREFIX + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(6), "label": label, "scope": scope, "hint": key[:8] + "…" + key[-4:],
                 "hash": _hash(key), "created": time.time(), "last_used": None}
        self.keys.append(entry)
        self._save()
        return self.view(entry), key

    def find(self, key: str | None) -> dict | None:
        if not key or not key.startswith(PREFIX):
            return None
        h = _hash(key)
        for k in self.keys:
            if hmac.compare_digest(k["hash"], h):
                return k
        return None

    def touch(self, entry: dict) -> None:
        """Remember when a key was last used (written at most once a minute)."""
        now = time.time()
        if not entry.get("last_used") or now - entry["last_used"] > 60:
            entry["last_used"] = now
            self._save()

    def revoke(self, key_id: str) -> bool:
        before = len(self.keys)
        self.keys = [k for k in self.keys if k["id"] != key_id]
        if len(self.keys) != before:
            self._save()
            return True
        return False

    @staticmethod
    def view(k: dict) -> dict:
        return {x: k.get(x) for x in ("id", "label", "scope", "hint", "created", "last_used")}

    def list(self) -> list[dict]:
        return [self.view(k) for k in sorted(self.keys, key=lambda k: k["created"])]
