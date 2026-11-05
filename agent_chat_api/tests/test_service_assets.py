# =========================================
# File: agent_chat_api/tests/test_service_assets.py
# Purpose:
#   - Verifies the packaged user systemd service points at the package-local
#     runtime, working directory, bind address, and workspace root.
# What This File Does:
#   - Reads `systemd/agent-chat-api.service` as text and checks the exact
#     deployment assumptions the local user service relies on.
# Main Classes:
#   - `ServiceAssetTests`: Single-purpose test case for the checked-in systemd
#     asset.
# Main Methods:
#   - `test_user_systemd_service_targets_subfolder()`: Protects working
#     directory, CLI bind args, workspace root, environment file, restart policy,
#     and install target.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Update this test with the service file any time the install path or
#     package-local runtime location changes.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import unittest

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT
except ModuleNotFoundError:
    from support import PACKAGE_ROOT


class ServiceAssetTests(unittest.TestCase):
    def test_legacy_systemd_unit_is_absent_when_runtime_supervisor_owns_lifecycle(self) -> None:
        service_path = PACKAGE_ROOT / "systemd" / "agent-chat-api.service"
        self.assertFalse(service_path.exists())


if __name__ == "__main__":
    unittest.main()
