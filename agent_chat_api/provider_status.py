"""Bounded observations of provider output, never private classifier state."""
from __future__ import annotations

import re
from typing import Any

from .audit import classify_refusal_text
from .models import _sanitize_provider_error_text

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,255}\Z")
_CYBER = re.compile(r"\b(?:cyber(?:security|[ _-]?(?:safety|security|risk|policy)|[ _-]activity)?|high[_-]?risk[_-]?cyber[_-]?activity)\b", re.I)
_BLOCK = re.compile(r"\b(?:blocked|flagged|refused|refusal|declined|rejected)\b|content (?:can'?t|cannot) be shown", re.I)
_POLICY = re.compile(r"\b(?:content_filter|policy_violation|safety_classifier|cyber_safety_block|cyber_policy|bio_policy|misalignment_policy_violation|refusal)\b", re.I)
_ROUTE = re.compile(r"rerout|switch|\brout(?:ed|ing)\b|fall(?:ing)?\s*back|\bfallback\b", re.I)


def _model(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _snippet(value: Any) -> str:
    return _sanitize_provider_error_text(str(value)).replace("\n", " ")[:320]


def collect_provider_status(*, provider: str, requested_model: str | None, raw_json: Any,
                            text: str, stderr: str, exit_code: int) -> dict[str, Any]:
    status: dict[str, Any] = {
        "schema_version": 1, "outcome": "completed" if exit_code == 0 else "failed",
        "reason_code": None, "requested_model": requested_model,
        "initial_model": None, "actual_model": None, "actual_model_source": "unknown",
        "models_used": [], "model_switches": [], "safety_flags": [],
        "block": {"detected": False, "category": None, "source": None, "code": None},
        "signals": [], "visibility": "provider_output_only",
    }
    events = raw_json if isinstance(raw_json, list) else [raw_json]
    previous_model: str | None = None
    usage_models: list[str] = []
    last_block_index = -1
    last_answer_index = -1
    last_route_index = -1
    routed_model: str | None = None
    event_index = -1
    structured_success = False

    def signal(kind: str, source: str, message: Any, **fields: Any) -> None:
        if len(status["signals"]) < 20:
            status["signals"].append({"kind": kind, "source": source, "message": _snippet(message), **fields})

    def flag(category: str, source: str, action: str, message: Any) -> None:
        entry = {"category": category, "source": source, "action": action, "message": _snippet(message)}
        if entry not in status["safety_flags"] and len(status["safety_flags"]) < 20:
            status["safety_flags"].append(entry)

    def block(source: str, code: Any, message: Any, category: Any = None) -> None:
        nonlocal last_block_index
        last_block_index = event_index
        details = f"{code or ''} {category or ''} {message or ''}"
        selected = "cybersecurity" if _CYBER.search(details) else "safety"
        # Prefer explicit provider evidence over an inference from answer text.
        existing = status["block"]
        if not existing["detected"] or (existing["source"] == "response_text" and source != "response_text"):
            status["block"] = {"detected": True, "category": selected, "source": source,
                               "code": _snippet(code) if code else None}
        flag(selected, source, "blocked", message or code)
        signal("block", source, message or code, category=selected)

    def model_seen(value: Any, source: str) -> None:
        nonlocal previous_model
        selected = _model(value)
        if not selected:
            return
        if selected not in status["models_used"]:
            status["models_used"].append(selected)
        if previous_model and previous_model != selected:
            if not any(x["from"] == previous_model and x["to"] == selected for x in status["model_switches"]):
                status["model_switches"].append({"from": previous_model, "to": selected,
                                                 "reason": "not_exposed", "source": source})
        previous_model = selected
        status["actual_model"] = selected
        status["actual_model_source"] = source

    def routing_notice(message: str, source: str) -> None:
        nonlocal routed_model, last_route_index
        match = re.fullmatch(r"model rerouted:\s*(\S+)\s*->\s*(\S+)\s*\(([^)]+)\)", message.strip(), re.I)
        if match and _model(match[1]) and _model(match[2]):
            origin, target, reason = match.groups()
            status["initial_model"] = status["initial_model"] or origin
            status["model_switches"].append({"from": origin, "to": target, "reason": reason, "source": source})
            routed_model, last_route_index = target, event_index
        if _CYBER.search(message) and _ROUTE.search(message):
            flag("cybersecurity", source, "rerouted", message)
        signal("routing_notice" if _ROUTE.search(message) else "warning", source, message)

    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        subtype = str(event.get("subtype") or "")
        if kind == "system" and subtype == "init":
            status["initial_model"] = _model(event.get("model"))
            previous_model = status["initial_model"]
        if kind in {"model_reroute", "model_rerouted", "model_fallback"} or subtype in {"model_reroute", "model_rerouted", "model_fallback"}:
            origin = _model(event.get("from_model")) or previous_model
            target = _model(event.get("to_model"))
            reason = _snippet(event.get("reason") or "not_exposed")
            if target:
                status["model_switches"].append({"from": origin, "to": target, "reason": reason, "source": "provider_event"})
            if _CYBER.search(reason):
                flag("cybersecurity", "provider_event", "rerouted", reason)
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        if kind == "assistant":
            model_seen(message.get("model"), "assistant_event")
            content = message.get("content")
            if message.get("stop_reason") != "refusal" and isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "text" and x.get("text") for x in content):
                last_answer_index = event_index
        if kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and item.get("text"):
                last_answer_index = event_index
            elif item.get("type") == "error" and isinstance(item.get("message"), str):
                # Native Codex ModelRerouted and warnings use nonfatal error items.
                routing_notice(item["message"], "provider_event")
        if kind in {"result", "response.completed"}:
            model_seen(event.get("model"), "result_event")
        usage = event.get("modelUsage")
        if isinstance(usage, dict):
            for name, counts in usage.items():
                if _model(name) and isinstance(counts, dict) and isinstance(counts.get("outputTokens"), (int, float)) and counts["outputTokens"] > 0:
                    if name not in usage_models:
                        usage_models.append(name)
        if message.get("stop_reason") == "refusal":
            details = message.get("stop_details") or {}
            block("provider_event", "refusal", message.get("stop_reason"), details.get("category") if isinstance(details, dict) else None)
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "refusal" for x in content):
            block("provider_event", "refusal", "Provider emitted a refusal content block.")
        failure = event.get("error")
        result_failed = kind == "result" and (event.get("is_error") is True or subtype.startswith("error_"))
        if kind == "turn.completed" or (kind == "result" and not result_failed):
            status["outcome"] = "completed" if exit_code == 0 else "failed"
            structured_success = True
        if kind in {"error", "turn.failed"} or result_failed:
            status["outcome"] = "failed"
            structured_success = False
            if isinstance(failure, dict):
                code, diagnostic = failure.get("code") or failure.get("type"), failure.get("message") or ""
            else:
                errors = event.get("errors")
                error_text = "; ".join(str(x) for x in errors[:5]) if isinstance(errors, list) else ""
                code, diagnostic = event.get("code") or subtype, failure or event.get("message") or error_text or event.get("result") or ""
            signal("error", "provider_event", diagnostic, code=_snippet(code) if code else None)
            if _POLICY.search(f"{code} {diagnostic}") or (_CYBER.search(str(diagnostic)) and _BLOCK.search(str(diagnostic)) and not _ROUTE.search(str(diagnostic))):
                block("provider_event", code, diagnostic)

    # A single usage model with output is explicit evidence; multiple usage models
    # cannot identify which one produced the final answer without assistant events.
    for name in usage_models:
        if name not in status["models_used"]:
            status["models_used"].append(name)
    if not status["actual_model"] and len(usage_models) == 1:
        model_seen(usage_models[0], "single_model_usage")

    for line in stderr.splitlines():
        if (_CYBER.search(line) and _BLOCK.search(line)) or _POLICY.search(line):
            if _ROUTE.search(line):
                routing_notice(line, "provider_diagnostic")
            else:
                # Stderr is unordered relative to stdout. An echoed earlier
                # diagnostic cannot override a later structured success.
                if structured_success and last_answer_index >= 0 and exit_code == 0:
                    flag("cybersecurity" if _CYBER.search(line) else "safety", "provider_diagnostic", "recovered", line)
                    signal("recovered_diagnostic", "provider_diagnostic", line)
                else:
                    block("provider_diagnostic", None, line)
        match = re.search(r"\bfrom\s+([A-Za-z0-9._:/-]+)\s+to\s+([A-Za-z0-9._:/-]+)", line, re.I)
        if match and re.search(r"rerout|switch|fall(?:ing)? back", line, re.I):
            status["model_switches"].append({"from": match[1], "to": match[2],
                "reason": "cybersecurity" if _CYBER.search(line) else "not_exposed", "source": "provider_diagnostic"})

    refusal = classify_refusal_text(text)
    cyber_notice = bool(_CYBER.search(text[:400]) and (
        (re.match(r"\s*we take extra (?:care|caution) with some cybersecurity requests\b", text, re.I)
         and re.search(r"content (?:can't|cannot) be shown|cannot (?:assist|help)|can't (?:assist|help)", text, re.I))
        or re.match(r"\s*(?:this|your) (?:request|content) (?:has been |was |is )?(?:blocked|declined|refused|rejected) (?:by|due to|because|for)\b", text, re.I)))
    if cyber_notice or refusal == "refusal_response":
        block("response_text", "refusal_response", text[:320])
    if status["block"]["detected"] and last_answer_index > last_block_index and status["outcome"] == "completed":
        status["block"] = {"detected": False, "category": None, "source": None, "code": None}
        for entry in status["safety_flags"]:
            if entry["action"] == "blocked":
                entry["action"] = "recovered"
    if status["block"]["detected"]:
        status["outcome"] = "blocked"
        status["reason_code"] = ("provider_cyber_block" if status["block"]["category"] == "cybersecurity" else
                                 "provider_safety_block" if status["block"]["source"] != "response_text" else "provider_refusal")
    elif status["outcome"] == "failed":
        status["reason_code"] = "provider_error"
    elif not text.strip():
        status["outcome"] = "failed"
        status["reason_code"] = "provider_empty_response"
    if not status["actual_model"] and routed_model and last_answer_index > last_route_index and status["outcome"] == "completed":
        model_seen(routed_model, "provider_routing_notice")
    return status
