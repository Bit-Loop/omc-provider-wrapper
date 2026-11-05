# =========================================
# File: agent_chat_api/tests/test_service_manifest.py
# Purpose:
#   - Verifies `service.json` still describes the Agent Chat API HTTP surface,
#     provider defaults, runtime boundary, and generated Markdown policy.
# What This File Does:
#   - Loads the manifest through `load_service_manifest()` so the parser and the
#     JSON fixture are tested together.
#   - Confirms safe provider defaults such as Codex read-only sandbox, Claude
#     empty tools, and no shell allowance.
#   - Confirms generated test-result Markdown is marked non-authoritative and
#     unavailable for ingestion/retrieval.
# Main Classes:
#   - `ServiceManifestTests`: Manifest contract test for runtime metadata and
#     generated artifact policy.
# Main Methods:
#   - `test_manifest_preserves_http_surface_and_safe_defaults()`: Protects
#     endpoint list and provider default semantics.
#   - `test_manifest_marks_generated_markdown_reports_non_authoritative()`:
#     Protects generated Markdown policy flags.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Add or update assertions here when `service.json` grows new behavior that
#     callers should rely on.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import unittest

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT
except ModuleNotFoundError:
    from support import PACKAGE_ROOT

from agent_chat_api.manifest import load_service_manifest


class ServiceManifestTests(unittest.TestCase):
    def test_manifest_preserves_http_surface_and_safe_defaults(self) -> None:
        manifest = load_service_manifest(PACKAGE_ROOT / "service.json")

        self.assertEqual(manifest.service_id, "agent_chat_api")
        self.assertIn("standard library", manifest.runtime_boundary.lower())
        self.assertEqual(manifest.default_provider, "codex")
        self.assertEqual(manifest.fallback_providers, ("codex", "claude"))
        self.assertIn(("GET", "/health"), manifest.endpoints)
        self.assertIn(("POST", "/api/chat"), manifest.endpoints)
        self.assertIn(("POST", "/v1/chat/completions"), manifest.endpoints)
        self.assertIn("codex:gpt-5.5", manifest.model_prefixes)
        self.assertIn("codex:gpt-5.6", manifest.model_prefixes)
        self.assertEqual(manifest.provider_defaults["codex"]["sandbox"], "read-only")
        self.assertTrue(manifest.provider_defaults["codex"]["ephemeral"])
        self.assertEqual(manifest.provider_defaults["claude"]["tools"], "")
        self.assertFalse(manifest.provider_defaults["shell_allowed"])
        self.assertEqual(manifest.lifecycle_owner, "user_service_or_foreground")

    def test_manifest_marks_generated_markdown_reports_non_authoritative(self) -> None:
        manifest = load_service_manifest(PACKAGE_ROOT / "service.json")

        self.assertEqual(manifest.generated_markdown.path, "runtime/test-results")
        self.assertFalse(manifest.generated_markdown.ingest_allowed)
        self.assertFalse(manifest.generated_markdown.operational_retrieval_allowed)

    def test_manifest_documents_fail_closed_post_authentication(self) -> None:
        payload = json.loads((PACKAGE_ROOT / "service.json").read_text(encoding="utf-8"))

        controls = payload["request_controls"]
        environment = payload["environment"]
        self.assertTrue(controls["auth_required_for_post_by_default"])
        self.assertEqual(
            controls["unauthenticated_dev_override_env"],
            "CHAT_API_ALLOW_UNAUTHENTICATED_POSTS",
        )
        self.assertIn("required", environment["CHAT_API_KEY"])
        self.assertIn("development/test-only", environment["CHAT_API_ALLOW_UNAUTHENTICATED_POSTS"])
        self.assertIn("authentication-status", environment["CHAT_API_PROVIDER_PROBE_TIMEOUT_SECONDS"])

    def test_manifest_documents_bounded_http_resources(self) -> None:
        payload = json.loads((PACKAGE_ROOT / "service.json").read_text(encoding="utf-8"))

        controls = payload["request_controls"]
        environment = payload["environment"]
        self.assertEqual(controls["max_request_body_bytes_default"], 1_048_576)
        self.assertEqual(controls["max_concurrent_requests_default"], 8)
        self.assertEqual(controls["socket_io_timeout_seconds_default"], 15)
        self.assertIn("body", environment["CHAT_API_MAX_REQUEST_BODY_BYTES"])
        self.assertIn("concurrent", environment["CHAT_API_MAX_CONCURRENT_REQUESTS"])
        self.assertIn("socket", environment["CHAT_API_SOCKET_IO_TIMEOUT_SECONDS"])


if __name__ == "__main__":
    unittest.main()
