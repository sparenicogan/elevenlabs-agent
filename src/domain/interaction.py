"""The immutable record of one completed call.

Separate from the conversation record because the two have opposite lifetimes. Call state --
whether someone verified, how many values they tried, the fingerprints of what they got wrong
-- is useful for minutes and a liability for months. What a call cost, how long the caller sat
in silence and whether it resolved is worth years. They shared a row until now, which meant
the only reset available destroyed both.

Everything ElevenLabs sends is kept, because the payload is 17KB at its worst against a 400KB
item limit and there is no reason to choose. The flat fields on top of it are the ones we
query, aggregate or join on; the nested copy is there so a question nobody has asked yet is
still answerable a year from now.
"""

from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

# Kept out of the row. The transcript is already written to S3, where it is cheaper, and it
# is the one part of the payload that can push an item past DynamoDB's limit.
_EXCLUDED = ("transcript", "otlp_traces")


def _decimalise(value: Any) -> Any:
    """
    Converts floats to Decimal, recursively, because DynamoDB stores no float type.

    value: any part of the payload.

    Returns: the same structure with every float replaced. Converted through str rather than
             float, so 0.02055312 stores as itself instead of the nearest binary approximation.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalise(v) for v in value]
    return value


def tool_performance(payload: dict) -> dict:
    """
    What each of our tools did on this call, and how long it kept the caller waiting.

    payload: the post-call payload, whose transcript turns carry tool_results.

    Returns: tool name to calls, errors, and latency in milliseconds -- median and worst.

    ElevenLabs measures this for us: every tool_result carries tool_latency_secs and is_error,
    so the time our own webhooks cost is already in the payload and needs no instrumentation
    of ours. It is the missing half of their latency metrics, which time their model and not
    our backend -- a slow tool shows up in their numbers only as silence on the line.
    """
    seen: dict[str, dict] = {}
    for turn in payload.get("transcript") or []:
        for result in turn.get("tool_results") or []:
            name = str(result.get("tool_name") or "unknown")
            entry = seen.setdefault(name, {"calls": 0, "errors": 0, "_latencies": []})
            entry["calls"] += 1
            if result.get("is_error"):
                entry["errors"] += 1
            seconds = result.get("tool_latency_secs")
            if seconds is not None:
                entry["_latencies"].append(float(seconds))

    performance = {}
    for name, entry in seen.items():
        latencies = sorted(entry.pop("_latencies"))
        performance[name] = {
            **entry,
            "latency_p50_ms": int(median(latencies) * 1000) if latencies else 0,
            "latency_max_ms": int(max(latencies) * 1000) if latencies else 0,
        }
    return performance


def _llm_usage(payload: dict) -> dict:
    """
    Token and model usage, summed across every model the call used.

    payload: the post-call payload.

    Returns: models used, and input, output and cached token totals.

    Summed rather than taken from the first entry because a call is not served by one model:
    the conversation runs on one, and analysis and summarisation on others. A schema with a
    single model column loses that silently.
    """
    charging = (payload.get("metadata") or {}).get("charging") or {}
    usage = (charging.get("llm_usage") or {}).get("irreversible_generation") or {}
    by_model = usage.get("model_usage") or {}

    totals = {"tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0}
    for model in by_model.values():
        totals["tokens_input"] += int((model.get("input") or {}).get("tokens") or 0)
        totals["tokens_output"] += int((model.get("output_total") or {}).get("tokens") or 0)
        totals["tokens_cache_read"] += int((model.get("input_cache_read") or {}).get("tokens") or 0)
    return {"models": sorted(by_model), **totals}


def build(payload: dict, customer_id: str, outcome: str) -> dict:
    """
    Assembles the row for one completed call.

    payload:     the post-call payload from ElevenLabs.
    customer_id: the account, where the caller verified. Empty for a call that did not, which
                 still belongs in the record -- an unverified call is a data point, not a gap.
    outcome:     which of the five outcomes in data-model.md this call had. Decided by the
                 caller of this function, because it depends on state this module cannot see.

    Returns: the item to write. Flat fields for querying and aggregation, plus the whole
             ElevenLabs payload under `elevenlabs` so nothing is lost to a schema decision
             made before the question was asked.
    """
    metadata = payload.get("metadata") or {}
    analysis = payload.get("analysis") or {}
    sentiment = analysis.get("sentiment_analysis") or {}
    started = metadata.get("start_time_unix_secs")

    row = {
        "conversation_id": str(payload.get("conversation_id") or ""),
        # Empty string rather than absent: the GSI needs the attribute, and a call nobody
        # verified is still a row worth keeping.
        "customer_id": customer_id or "",
        "started_at": (
            datetime.fromtimestamp(int(started), UTC).isoformat()
            if started
            else datetime.now(UTC).isoformat()
        ),
        "recorded_at": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "duration_seconds": int(metadata.get("call_duration_secs") or 0),
        "language": str(metadata.get("main_language") or metadata.get("language") or "en"),
        "agent_version": str(payload.get("version_id") or ""),
        "termination_reason": str(metadata.get("termination_reason") or ""),
        "cost_credits": int(metadata.get("cost") or 0),
        "cost_fiat": metadata.get("cost_fiat"),
        "call_successful": str(analysis.get("call_successful") or ""),
        "sentiment_label": str(sentiment.get("overall_label") or ""),
        "sentiment_score": sentiment.get("overall_sentiment_score"),
        "frustration_score": sentiment.get("overall_frustration_score"),
        **_llm_usage(payload),
        "tools": tool_performance(payload),
        "elevenlabs": {k: v for k, v in payload.items() if k not in _EXCLUDED},
    }
    return _decimalise(row)
