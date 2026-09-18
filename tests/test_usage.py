"""Request accounting must not turn missing responses into a zero bill."""

import json
import time

import httpx
import pytest
from test_model import PAGE, answer, provider

from jev_frontend_qa.core.model_client import ModelError
from jev_frontend_qa.core.usage import UsageRecorder


def response(request, usage):
    options = json.loads(request.content)["questions"]["operation"]["criteria"]
    return httpx.Response(200, json={"answers": {"operation": answer("DONE", options)}, "usage": usage})


def choose(selected):
    return selected.choose(goal="Observe", page=PAGE, history=[])


def test_all_calls_are_accounted_even_done_without_browser_actions():
    selected = provider(lambda request: response(request, {"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.001}))
    try:
        choose(selected)
        choose(selected)
    finally:
        selected.client.close()
    usage = selected.client.usage_summary()
    assert (usage.requests, usage.failed_requests, usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
        2,
        0,
        24,
        6,
        30,
    )
    assert usage.cost_usd == pytest.approx(0.002)
    assert usage.usage_complete and usage.cost_complete


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
def test_unknown_attempt_invalidates_prior_complete_totals(failure):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls != 2:
            return response(request, {"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.001})
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail")
        return httpx.Response(503 if failure == "http" else 200, text="private error")

    selected = provider(respond)
    try:
        choose(selected)
        with pytest.raises(ModelError):
            choose(selected)
        choose(selected)
    finally:
        selected.client.close()
    usage = selected.client.usage_summary()
    assert (usage.requests, usage.failed_requests) == (3, 1)
    assert usage.total_tokens is None and usage.cost_usd is None
    assert not usage.usage_complete and not usage.cost_complete


@pytest.mark.parametrize("status", [200, 503])
def test_usage_retained_when_response_cannot_produce_decision(status):
    selected = provider(lambda _: httpx.Response(status, json={"usage": {"input_tokens": 8, "output_tokens": 2}}))
    try:
        with pytest.raises(ModelError):
            choose(selected)
    finally:
        selected.client.close()
    usage = selected.client.usage_summary()
    assert (usage.requests, usage.failed_requests, usage.total_tokens) == (1, 1, 10)
    assert usage.cost_usd is None and not usage.cost_complete


def test_expired_deadline_does_not_invent_http_usage():
    selected = provider(lambda _: pytest.fail("Expired deadline must not issue HTTP"))
    selected.client.deadline = time.monotonic() - 1
    try:
        with pytest.raises(ModelError):
            choose(selected)
    finally:
        selected.client.close()
    assert selected.client.usage_summary().requests == 0


@pytest.mark.parametrize("invalid", [True, -1, 1.5, float("nan"), "12"])
def test_invalid_usage_never_becomes_a_trusted_total(invalid):
    recorder = UsageRecorder()
    recorder.record(
        latency_ms=2, failed=False, usage={"input_tokens": invalid, "output_tokens": 2, "cost_usd": invalid}
    )
    result = recorder.summary()
    assert result.total_tokens is None and not result.usage_complete
    if invalid != 1.5:
        assert result.cost_usd is None and not result.cost_complete


def test_missing_cost_not_guessed_and_aliases_not_double_counted():
    recorder = UsageRecorder()
    recorder.record(
        latency_ms=1, failed=False, usage={"input_tokens": 8, "output_tokens": 2, "prompt_tokens": 999, "cost": 2}
    )
    result = recorder.summary()
    assert result.total_tokens == 10
    assert result.cost_usd is None
    recorder.record(latency_ms=1, failed=False, usage={"input_tokens": 8, "output_tokens": 2, "total_tokens": 999})
    assert recorder.summary().total_tokens is None


def test_estimated_input_cost_is_separate_from_actual_provider_charge():
    recorder = UsageRecorder()
    recorder.record(latency_ms=1, failed=False, usage={"input_tokens": 6482, "output_tokens": 333, "cost_usd": 0.5})
    usage = recorder.summary()
    assert usage.estimated_cost_usd == pytest.approx(0.000272244)
    assert usage.cost_usd == 0.5
    recorder.record(latency_ms=1, failed=False, usage={"input_tokens": 0, "output_tokens": 10000, "cost_usd": 0.0})
    assert recorder.summary().estimated_cost_usd == pytest.approx(0.000272244)


def test_missing_usage_invalidates_estimate_instead_of_claiming_partial_bill():
    recorder = UsageRecorder()
    recorder.record(latency_ms=1, failed=False, usage={"input_tokens": 1_000_000, "output_tokens": 100})
    assert recorder.summary().estimated_cost_usd == pytest.approx(0.042)
    recorder.record(latency_ms=1, failed=True, usage=None)
    assert recorder.summary().estimated_cost_usd is None
