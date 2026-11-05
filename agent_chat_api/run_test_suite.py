# =========================================
# File: agent_chat_api/run_test_suite.py
# Purpose:
#   - Provides a tiny executable shim for running the package-local test
#     reporter from either module mode or direct file execution.
# What This File Does:
#   - Adjusts `sys.path` when launched as a standalone file so the package import
#     still resolves correctly.
#   - Imports `agent_chat_api.test_reporter.main` and exits with its return code.
# Used By:
#   - Direct invocations such as `python agent_chat_api/run_test_suite.py`.
#   - Module invocations where package-relative imports are available.
# Reader Note:
#   - Keep this file as a shim only. The actual reporting behavior belongs in
#     `agent_chat_api/test_reporter.py`.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent_chat_api.test_reporter import main
else:
    from .test_reporter import main


if __name__ == "__main__":
    raise SystemExit(main())
