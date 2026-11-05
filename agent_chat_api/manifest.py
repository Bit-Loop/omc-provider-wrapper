# =========================================
# File: agent_chat_api/manifest.py
# Purpose:
#   - Loads and validates the JSON service manifest that describes this wrapper
#     as a runtime service.
# What This File Does:
#   - Reads `agent_chat_api/service.json` by default, requires it to be a JSON
#     object, and converts selected fields into typed dataclasses.
#   - Extracts endpoint method/path pairs, fallback provider defaults, provider
#     defaults, and generated-markdown safety policy.
#   - Uses strict helper functions so bad manifest shapes fail with clear
#     `ValueError` messages instead of leaking loose dictionaries.
# Main Classes:
#   - `GeneratedMarkdownPolicy`: Captures whether generated markdown is allowed
#     into ingestion and operational retrieval paths.
#   - `ServiceManifest`: Typed service description used by tests and runtime
#     consumers that need manifest metadata.
# Main Functions:
#   - `load_service_manifest()`: Parses the manifest file and returns a
#     `ServiceManifest` with endpoint and generated-markdown policy fields.
#   - `_string()`, `_bool()`, `_list()`, `_object()`: Small validators that make
#     manifest schema errors precise and easy to test.
# Used By:
#   - `agent_chat_api/tests/test_service_manifest.py` imports `load_service_manifest` from this file.
# Reader Note:
#   - Keep this module synchronized with `service.json`; adding a manifest field
#     should usually mean adding a dataclass field and a focused manifest test.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class GeneratedMarkdownPolicy:
    path: str
    ingest_allowed: bool
    operational_retrieval_allowed: bool


@dataclass(frozen=True, slots=True)
class ServiceManifest:
    service_id: str
    runtime_boundary: str
    default_provider: str
    fallback_providers: tuple[str, ...]
    model_prefixes: tuple[str, ...]
    endpoints: tuple[tuple[str, str], ...]
    provider_defaults: dict[str, Any]
    generated_markdown: GeneratedMarkdownPolicy
    lifecycle_owner: str


def load_service_manifest(path: Path | None = None) -> ServiceManifest:
    manifest_path = path or Path(__file__).with_name("service.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{manifest_path} must contain a JSON object")

    generated_markdown = _object(payload, "generated_markdown")
    endpoints = tuple(
        (str(endpoint["method"]).upper(), str(endpoint["path"]))
        for endpoint in _list(payload, "endpoints")
        if isinstance(endpoint, dict) and "method" in endpoint and "path" in endpoint
    )

    return ServiceManifest(
        service_id=_string(payload, "service_id"),
        runtime_boundary=_string(payload, "runtime_boundary"),
        default_provider=_string(payload, "default_provider"),
        fallback_providers=tuple(str(provider) for provider in _list(payload, "fallback_providers")),
        model_prefixes=tuple(str(prefix) for prefix in _list(payload, "model_prefixes")),
        endpoints=endpoints,
        provider_defaults=_object(payload, "provider_defaults"),
        generated_markdown=GeneratedMarkdownPolicy(
            path=_string(generated_markdown, "path"),
            ingest_allowed=_bool(generated_markdown, "ingest_allowed"),
            operational_retrieval_allowed=_bool(generated_markdown, "operational_retrieval_allowed"),
        ),
        lifecycle_owner=_string(payload, "lifecycle_owner"),
    )


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
