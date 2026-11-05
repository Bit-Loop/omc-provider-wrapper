"""Explicit subprocess environment boundaries for the local provider wrapper."""

from __future__ import annotations

import os
from collections.abc import Mapping


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


def provider_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return only OS basics and provider-owned authentication configuration."""

    current = source or os.environ
    names = set(_BASE_ENVIRONMENT_NAMES) | set(_PROVIDER_AUTH_ENVIRONMENT_NAMES)
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
    return environment


def wrapper_process_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the wrapper service environment without inheriting app secrets."""

    current = source or os.environ
    environment = provider_subprocess_environment(current)
    for name, value in current.items():
        if name.startswith("CHAT_API_") and str(value):
            environment[name] = str(value)
    if overrides:
        environment.update({name: str(value) for name, value in overrides.items()})
    return environment


__all__ = ["provider_subprocess_environment", "wrapper_process_environment"]
