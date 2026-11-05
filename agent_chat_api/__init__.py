# =========================================
# File: agent_chat_api/__init__.py
# Purpose:
#   - Declares the `agent_chat_api` package and gives importers a stable package
#     boundary for the standalone Codex/Claude chat wrapper.
# What This File Does:
#   - Exposes only `__version__` through `__all__`, so package-level imports do
#     not accidentally pull in server, provider, or runtime dependencies.
#   - Stores the package version string used by callers that need to identify
#     this wrapper without importing the rest of the service.
# Used By:
#   - Python runs this file whenever anything imports `agent_chat_api` or one of
#     its child modules.
# Reader Note:
#   - There is intentionally no startup work here; service behavior begins in
#     `agent_chat_api/app.py` and request handling lives in `agent_chat_api/server.py`.
# - 2026-06-06 00:30:48 -0500
# =========================================
"""Standalone Codex/Claude chat API wrapper."""

__all__ = ["__version__"]

__version__ = "0.1.0"
