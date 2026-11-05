"""Provider observations must survive the wrapper and HTTP response boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_chat_api.config import Settings
from agent_chat_api.models import ChatRequest, ProviderError
from agent_chat_api.providers import ChatRunner
from agent_chat_api.tests.support import write_executable, run_handler
from agent_chat_api.server import create_app


class ProviderStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def runner(self, provider, events, stderr="", exit_code=0):
        output = "\n".join(json.dumps(event) for event in events)
        binary = write_executable(self.root / provider, "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
            + f"sys.stdout.write({output!r})\nsys.stderr.write({stderr!r})\nsys.exit({exit_code})\n")
        settings = Settings(workspace_root=self.root, api_key="fixture", **{provider + "_bin": str(binary)})
        return ChatRunner(settings)

    def test_claude_reports_observed_switch_and_actual_model(self):
        runner = self.runner("claude", [
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
            {"type": "system", "subtype": "model_fallback", "from_model": "claude-opus-5-5", "to_model": "claude-opus-4-8", "reason": "cybersecurity"},
            {"type": "assistant", "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "Advice"}]}},
            {"type": "result", "result": "Advice", "is_error": False},
        ])
        result = runner.run(ChatRequest(provider="claude", prompt="fixture", model="opus"))
        self.assertEqual(result.text, "Advice")
        status = result.public_dict()["provider_status"]
        self.assertEqual(status["outcome"], "completed")
        self.assertEqual(status["requested_model"], "opus")
        self.assertEqual(status["actual_model"], "claude-opus-4-8")
        self.assertEqual(status["model_switches"][0]["reason"], "cybersecurity")
        self.assertFalse(status["block"]["detected"])

    def test_codex_structured_cyber_error_survives_http(self):
        runner = self.runner("codex", [
            {"type": "thread.started", "thread_id": "fixture"},
            {"type": "turn.failed", "error": {"message": "This request was blocked by cybersecurity safeguards.", "code": "cyber_safety_block"}},
        ], exit_code=1)
        handler = create_app(runner.settings)
        handler.runner = runner
        code, body = run_handler(handler, "POST", "/api/chat", {"provider": "codex", "prompt": "fixture"}, {"Authorization": "Bearer fixture"})
        self.assertEqual(code, 502)
        error = body["error"]
        self.assertEqual(error["code"], "provider_cyber_block")
        self.assertFalse(error["retryable"])
        status = error["details"]["provider_status"]
        self.assertEqual(status["block"]["category"], "cybersecurity")
        self.assertEqual(status["block"]["source"], "provider_event")
        self.assertIsNone(status["actual_model"])

    def test_codex_json_text_and_unknown_model(self):
        runner = self.runner("codex", [
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "not an answer"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Final advice"}},
            {"type": "turn.completed", "usage": {"output_tokens": 5}},
        ])
        result = runner.run(ChatRequest(provider="codex", prompt="fixture", model="gpt-configured"))
        self.assertEqual(result.text, "Final advice")
        status = result.public_dict()["provider_status"]
        self.assertIsNone(status["actual_model"])
        self.assertEqual(status["actual_model_source"], "unknown")
        self.assertEqual(status["requested_model"], "gpt-configured")

    def test_structured_refusal_with_exit_zero_is_not_success(self):
        runner = self.runner("claude", [
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "stop_reason": "refusal", "stop_details": {"category": "cybersecurity"}, "content": []}},
            {"type": "result", "result": "Unavailable", "is_error": False},
        ])
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="claude", prompt="fixture"))
        self.assertEqual(caught.exception.code, "provider_cyber_block")
        self.assertTrue(caught.exception.details["provider_status"]["block"]["detected"])

    def test_native_policy_codes_and_capacity_are_distinct(self):
        for code in ("cyber_policy", "bio_policy", "misalignment_policy_violation"):
            runner = self.runner("codex", [{"type": "error", "code": code, "message": "Request declined."}], exit_code=1)
            with self.assertRaises(ProviderError) as caught:
                runner.run(ChatRequest(provider="codex", prompt="fixture"))
            self.assertTrue(caught.exception.details["provider_status"]["block"]["detected"])
        runner = self.runner("codex", [{"type": "error", "code": "policy_capacity_unavailable", "message": "Capacity unavailable."}], exit_code=1)
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="codex", prompt="fixture"))
        self.assertFalse(caught.exception.details["provider_status"]["block"]["detected"])

    def test_observed_provider_model_can_include_version_at_sign(self):
        runner = self.runner("claude", [{"type": "assistant", "message": {"model": "claude-provider@20260101", "content": [{"type": "text", "text": "Advice"}]}}])
        self.assertEqual(runner.run(ChatRequest(provider="claude", prompt="fixture")).provider_status["actual_model"], "claude-provider@20260101")

    def test_topic_mentions_in_answer_are_not_block_signals(self):
        runner = self.runner("claude", [{"type": "result", "result": "The docs describe cybersecurity safeguards and model fallback.", "modelUsage": {"claude-opus-5-5": {"outputTokens": 10}}}])
        result = runner.run(ChatRequest(provider="claude", prompt="fixture"))
        status = result.public_dict()["provider_status"]
        self.assertFalse(status["block"]["detected"])
        self.assertEqual(status["actual_model"], "claude-opus-5-5")

    def test_native_codex_reroute_notice_reports_models_without_blocking(self):
        runner = self.runner("codex", [
            {"type": "item.completed", "item": {"type": "error", "message": "Your account was flagged for potentially high-risk cyber activity and this request was routed to gpt-5.2 as a fallback."}},
            {"type": "item.completed", "item": {"type": "error", "message": "model rerouted: gpt-6-sol -> gpt-5.2 (HighRiskCyberActivity)"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Advice"}},
            {"type": "turn.completed", "usage": {}},
        ])
        status = runner.run(ChatRequest(provider="codex", prompt="fixture")).provider_status
        self.assertFalse(status["block"]["detected"])
        self.assertEqual(status["actual_model"], "gpt-5.2")
        self.assertEqual(status["actual_model_source"], "provider_routing_notice")
        self.assertEqual(status["model_switches"][0]["reason"], "HighRiskCyberActivity")
        self.assertEqual(status["safety_flags"][0]["action"], "rerouted")

    def test_native_content_filter_error_stops_without_retry(self):
        runner = self.runner("codex", [{"type": "turn.failed", "error": {"message": "stream disconnected before completion: Incomplete response returned, reason: content_filter"}}], exit_code=1)
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="codex", prompt="fixture", fallback=True))
        self.assertEqual(caught.exception.code, "provider_safety_block")
        self.assertTrue(caught.exception.details["provider_status"]["block"]["detected"])

    def test_claude_result_error_array_is_observed(self):
        runner = self.runner("claude", [{"type": "result", "subtype": "error_during_execution", "is_error": False,
            "result": "partial", "errors": ["Request blocked by cybersecurity safeguards."]}])
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="claude", prompt="fixture"))
        self.assertEqual(caught.exception.code, "provider_cyber_block")

    def test_security_triage_answers_are_not_refusals(self):
        for answer in ["This request concerns cybersecurity events flagged by your SIEM. Here is the triage plan.",
                       "Your request asks why cybersecurity logins were rejected. Check the IdP audit trail."]:
            runner = self.runner("claude", [{"type": "result", "result": answer}])
            self.assertFalse(runner.run(ChatRequest(provider="claude", prompt="fixture")).provider_status["block"]["detected"])

    def test_recovered_errors_and_flags_do_not_mask_final_success(self):
        runner = self.runner("codex", [
            {"type": "error", "message": "Request blocked by cybersecurity safeguards."},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Completed after provider handling."}},
            {"type": "turn.completed", "usage": {}},
        ], stderr="Earlier diagnostic: reason: content_filter")
        status = runner.run(ChatRequest(provider="codex", prompt="fixture")).provider_status
        self.assertEqual(status["outcome"], "completed")
        self.assertFalse(status["block"]["detected"])
        self.assertEqual(status["safety_flags"][0]["action"], "recovered")

    def test_cyber_notice_is_labeled_as_text_inference(self):
        runner = self.runner("codex", [{"type": "item.completed", "item": {"type": "agent_message", "text": "We take extra care with some cybersecurity requests. This content can't be shown."}}])
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="codex", prompt="fixture"))
        status = caught.exception.details["provider_status"]
        self.assertEqual(status["block"]["category"], "cybersecurity")
        self.assertEqual(status["block"]["source"], "response_text")

    def test_failed_turn_is_failure_even_with_partial_answer_and_zero_exit(self):
        runner = self.runner("codex", [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Partial response"}},
            {"type": "turn.failed", "error": {"message": "server unavailable"}},
        ])
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="codex", prompt="fixture"))
        self.assertEqual(caught.exception.details["provider_status"]["outcome"], "failed")

    def test_codex_persistence_uses_json_thread_identifier(self):
        runner = self.runner("codex", [
            {"type": "thread.started", "thread_id": "fixture-thread-id"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Advice"}},
        ])
        result = runner.run(ChatRequest(provider="codex", prompt="fixture", persist=True, conversation_id="fixture-conversation"))
        self.assertEqual(runner.session_store.get(result.conversation_id).provider_session_id, "fixture-thread-id")

    def test_multiple_usage_models_do_not_invent_final_model_or_switch(self):
        runner = self.runner("claude", [{"type": "result", "result": "Advice", "modelUsage": {
            "claude-opus-5-5": {"outputTokens": 5}, "claude-opus-4-8": {"outputTokens": 10},
        }}])
        status = runner.run(ChatRequest(provider="claude", prompt="fixture")).public_dict()["provider_status"]
        self.assertIsNone(status["actual_model"])
        self.assertEqual(status["actual_model_source"], "unknown")
        self.assertEqual(status["model_switches"], [])

    def test_explicit_cyber_block_does_not_trigger_provider_retry(self):
        runner = self.runner("codex", [{"type": "error", "message": "Cybersecurity request blocked.", "code": "cyber_safety_block"}], exit_code=1)
        with self.assertRaises(ProviderError) as caught:
            runner.run(ChatRequest(provider="codex", prompt="fixture", fallback=True))
        self.assertEqual(caught.exception.code, "provider_cyber_block")
        self.assertEqual(caught.exception.details["provider"], "codex")

    def test_stream_reports_model_status_and_preserves_codex_session(self):
        runner = self.runner("codex", [
            {"type": "thread.started", "thread_id": "stream-thread-id"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Advice"}},
        ])
        events = list(runner.stream(ChatRequest(provider="codex", prompt="fixture", conversation_id="stream-conversation")))
        done = events[-1]
        self.assertEqual(done.event, "done")
        self.assertEqual(done.data["text"], "Advice")
        self.assertEqual(done.data["provider_status"]["outcome"], "completed")
        self.assertEqual(runner.session_store.get("stream-conversation").provider_session_id, "stream-thread-id")

    def test_openai_compatible_success_and_stream_errors_preserve_status(self):
        runner = self.runner("claude", [{"type": "result", "result": "Advice", "modelUsage": {"claude-opus-4-8": {"outputTokens": 5}}}])
        handler = create_app(runner.settings)
        handler.runner = runner
        code, body = run_handler(handler, "POST", "/v1/chat/completions", {"provider": "claude", "prompt": "fixture"}, {"Authorization": "Bearer fixture"})
        self.assertEqual(code, 200)
        self.assertEqual(body["provider_meta"]["provider_status"]["actual_model"], "claude-opus-4-8")
        from agent_chat_api.tests.support import run_handler_raw
        runner = self.runner("codex", [{"type": "turn.failed", "error": {"message": "Cybersecurity request blocked.", "code": "cyber_safety_block"}}])
        handler = create_app(runner.settings)
        handler.runner = runner
        code, _, body = run_handler_raw(handler, "POST", "/v1/chat/completions", {"provider": "codex", "prompt": "fixture", "stream": True}, {"Authorization": "Bearer fixture"})
        self.assertEqual(code, 200)
        rows = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        error = next(row["error"] for row in rows if "error" in row)
        self.assertEqual(error["type"], "provider_cyber_block")
        self.assertTrue(error["details"]["provider_status"]["block"]["detected"])


if __name__ == "__main__":
    unittest.main()
