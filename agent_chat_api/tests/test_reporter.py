# =========================================
# File: agent_chat_api/tests/test_reporter.py
# Purpose:
#   - Verifies the generated Markdown test report path, content, and metadata
#     behavior in `agent_chat_api.test_reporter`.
# What This File Does:
#   - Builds a synthetic `MarkdownTestResult` containing one pass and one
#     failure so report rendering can be tested without running the full suite.
#   - Confirms the Markdown includes status metrics, per-test rows, and failure
#     details.
#   - Confirms timestamped reports, `latest.md`, and metadata sidecars are
#     written with generated-markdown ingestion disabled.
# Main Classes:
#   - `ReporterTests`: Covers report rendering, file writes, sidecar metadata,
#     and the default runtime output directory.
# Main Methods:
#   - `make_result()`: Creates the controlled fake test result used by the
#     rendering and file-write tests.
#   - Report tests assert both human-readable Markdown content and machine-readable
#     metadata flags.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Generated test reports are intentionally marked non-authoritative; keep
#     these tests aligned with any changes to generated artifact policy.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT
except ModuleNotFoundError:
    from support import PACKAGE_ROOT

from agent_chat_api.test_reporter import (
    MarkdownTestResult,
    TestRecord,
    default_output_dir,
    render_markdown_report,
    write_markdown_report,
)


class ReporterTests(unittest.TestCase):
    def make_result(self) -> MarkdownTestResult:
        result = MarkdownTestResult(io.StringIO(), descriptions=True, verbosity=2)
        result.testsRun = 2
        result.records = [
            TestRecord("tests.test_example.Passing.test_one", "", "passed", 0.001),
            TestRecord("tests.test_example.Failing.test_two", "", "failed", 0.002, "assertion failed"),
        ]
        result.failures = [(object(), "assertion failed")]
        return result

    def test_render_markdown_report_contains_summary_and_case_table(self) -> None:
        now = datetime(2026, 5, 14, tzinfo=timezone.utc)
        markdown = render_markdown_report(root=PACKAGE_ROOT, result=self.make_result(), started_at=now, finished_at=now)
        self.assertIn("# Agent Chat API Test Results", markdown)
        self.assertIn("| Tests run | 2 |", markdown)
        self.assertIn("`tests.test_example.Passing.test_one`", markdown)
        self.assertIn("## Details", markdown)
        self.assertIn("assertion failed", markdown)

    def test_write_markdown_report_writes_timestamp_and_latest_files(self) -> None:
        now = datetime(2026, 5, 14, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            report_path = write_markdown_report(
                root=PACKAGE_ROOT,
                result=self.make_result(),
                started_at=now,
                finished_at=now,
                output_dir=Path(tmp),
            )
            self.assertTrue(report_path.name.startswith("test-results-20260514T123000Z"))
            self.assertTrue(report_path.exists())
            self.assertTrue((report_path.parent / "latest.md").exists())
            metadata = report_path.with_suffix(report_path.suffix + ".metadata.json")
            latest_metadata = report_path.parent / "latest.md.metadata.json"
            self.assertTrue(metadata.exists())
            self.assertTrue(latest_metadata.exists())
            self.assertIn('"ingest_allowed": false', metadata.read_text(encoding="utf-8"))
            self.assertIn('"operational_retrieval_allowed": false', metadata.read_text(encoding="utf-8"))

    def test_default_output_dir_keeps_generated_markdown_under_runtime(self) -> None:
        self.assertEqual(default_output_dir(PACKAGE_ROOT), PACKAGE_ROOT / "runtime" / "test-results")

if __name__ == "__main__":
    unittest.main()
