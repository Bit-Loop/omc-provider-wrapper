"""Explicit subprocess environment boundaries for the local provider wrapper."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path


_BASE_ENVIRONMENT_NAMES = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)
_PROVIDER_AUTH_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "OPENAI_API_KEY",
)


# Injected per request, so the advisor agent exists without editing any OpenCode config.
OPENCODE_ADVISOR_AGENT = "omc-advisor"
_OPENCODE_ADVISOR_CONFIG = json.dumps({"agents": {OPENCODE_ADVISOR_AGENT: {
    "mode": "primary",
    "description": "Read-only advisor for the OMC provider wrapper.",
    "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
}}})

_OPENCODE_CONFIG_NAMES = {"OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG"}
_WRAPPER_CONFIG_NAMES = {"OMC_WRAPPER_CONFIG_DIR", "OMC_WRAPPER_PROVIDER_CONFIG"}


def _opencode_environment_names(current: Mapping[str, str]) -> set[str]:
    """Forward auth explicitly declared by the user, without inheriting app secrets."""
    home = Path(current.get("HOME") or Path.home())
    root = Path(current.get("OMC_WRAPPER_CONFIG_DIR") or
                Path(current.get("XDG_CONFIG_HOME") or home / ".config") / "omc-provider-wrapper")
    filename = Path(current.get("OMC_WRAPPER_PROVIDER_CONFIG") or root / "providers.json")
    names = set(_OPENCODE_CONFIG_NAMES)
    try:
        document = json.loads(filename.read_text())
        apis = document.get("apis", {}) if isinstance(document, dict) else {}
        for api in apis.values() if isinstance(apis, dict) else ():
            name = api.get("key_env") if isinstance(api, dict) else None
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) and name != "CHAT_API_KEY":
                names.add(name)
    except (OSError, ValueError):
        # Configuration commands report bad settings; the environment boundary
        # must stay closed even while the file is absent or being repaired.
        pass
    return names


def provider_subprocess_environment(
    source: Mapping[str, str] | None = None,
    *,
    provider: str | None = None,
) -> dict[str, str]:
    """Return only OS basics and provider-owned authentication configuration."""

    current = source if source is not None else os.environ
    names = set(_BASE_ENVIRONMENT_NAMES) | set(_PROVIDER_AUTH_ENVIRONMENT_NAMES)
    if provider == "opencode":
        names.update(_opencode_environment_names(current))
    names.update(
        item.strip()
        for item in str(current.get("CHAT_API_PROVIDER_ENV_ALLOWLIST") or "").split(",")
        if item.strip()
    )
    environment = {
        name: str(current[name])
        for name in sorted(names)
        if name in current and str(current[name])
    }
    environment.setdefault("PATH", os.defpath)
    if provider == "opencode":
        environment["OPENCODE_CONFIG_CONTENT"] = _OPENCODE_ADVISOR_CONFIG
        # Launcher scripts may start a GPU model for bare `opencode`; a request must not.
        environment["OPENCODE_NO_AUTOLLAMA"] = "1"
    return environment


def wrapper_process_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the wrapper service environment without inheriting app secrets."""

    current = source if source is not None else os.environ
    environment = provider_subprocess_environment(current)
    for name in _opencode_environment_names(current) | _WRAPPER_CONFIG_NAMES:
        if current.get(name):
            environment[name] = str(current[name])
    for name, value in current.items():
        if name.startswith("CHAT_API_") and str(value):
            environment[name] = str(value)
    if overrides:
        environment.update({name: str(value) for name, value in overrides.items()})
    return environment


__all__ = ["OPENCODE_ADVISOR_AGENT", "provider_subprocess_environment", "wrapper_process_environment"]
