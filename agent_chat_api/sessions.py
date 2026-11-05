# =========================================
# File: agent_chat_api/sessions.py
# Purpose:
#   - Persists provider conversation/session ids so follow-up chat requests can
#     resume the same Codex or Claude context safely.
# What This File Does:
#   - Stores each conversation id with provider, provider session id, cwd, Codex
#     sandbox, and timestamps in a JSON file.
#   - Rejects attempts to reuse a conversation id with a different provider,
#     cwd, or Codex sandbox.
#   - Writes updates atomically through a temporary file and `os.replace()`.
# Main Classes:
#   - `SessionStoreError`: Raised when session state is unreadable,
#     incompatible, or structurally invalid.
#   - `SessionRecord`: Typed in-memory representation of one persisted
#     conversation mapping.
#   - `SessionStore`: File-backed store that loads, saves, and compatibility
#     checks conversation records.
# Main Methods:
#   - `SessionStore.get()`: Reads a stored conversation record if it exists.
#   - `SessionStore.save()`: Creates or updates a record while preserving the
#     original creation timestamp.
#   - `SessionStore.assert_compatible()`: Protects against accidentally resuming
#     the wrong provider, cwd, or sandbox.
#   - `_load()` / `_save()`: Handle JSON file IO and validation.
# Used By:
#   - `agent_chat_api/tests/test_sessions_rate.py` imports `SessionStore` from this file.
# Reader Note:
#   - Session compatibility is part of request safety. Do not weaken these checks
#     without also updating provider resume tests and operator-facing behavior.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


class SessionStoreError(ValueError):
    pass


@dataclass(frozen=True)
class SessionRecord:
    conversation_id: str
    provider: str
    provider_session_id: str
    cwd: str
    codex_sandbox: str | None
    created_at: float
    updated_at: float

    @classmethod
    def from_dict(cls, conversation_id: str, data: dict[str, Any]) -> "SessionRecord":
        return cls(
            conversation_id=conversation_id,
            provider=str(data["provider"]),
            provider_session_id=str(data["provider_session_id"]),
            cwd=str(data["cwd"]),
            codex_sandbox=data.get("codex_sandbox"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "cwd": self.cwd,
            "codex_sandbox": self.codex_sandbox,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def get(self, conversation_id: str) -> SessionRecord | None:
        with self._lock:
            data = self._load()
            raw = data.get(conversation_id)
            if not isinstance(raw, dict):
                return None
            return SessionRecord.from_dict(conversation_id, raw)

    def save(
        self,
        *,
        conversation_id: str,
        provider: str,
        provider_session_id: str,
        cwd: str,
        codex_sandbox: str | None,
    ) -> SessionRecord:
        with self._lock:
            data = self._load()
            now = time.time()
            existing = data.get(conversation_id)
            created_at = float(existing.get("created_at", now)) if isinstance(existing, dict) else now
            record = SessionRecord(
                conversation_id=conversation_id,
                provider=provider,
                provider_session_id=provider_session_id,
                cwd=cwd,
                codex_sandbox=codex_sandbox,
                created_at=created_at,
                updated_at=now,
            )
            data[conversation_id] = record.to_dict()
            self._save(data)
            return record

    def assert_compatible(
        self,
        record: SessionRecord,
        *,
        provider: str,
        cwd: str,
        codex_sandbox: str | None,
    ) -> None:
        if record.provider != provider:
            raise SessionStoreError("conversation_id belongs to a different provider.")
        if record.cwd != cwd:
            raise SessionStoreError("conversation_id belongs to a different cwd.")
        if provider == "codex" and record.codex_sandbox != codex_sandbox:
            raise SessionStoreError("conversation_id belongs to a different Codex sandbox.")

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionStoreError(f"session store is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise SessionStoreError("session store must contain a JSON object.")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)
