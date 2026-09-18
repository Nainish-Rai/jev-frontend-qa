import json

from jev_frontend_qa.core.evidence import EvidenceCollector
from jev_frontend_qa.core.models import RedactionPolicy
from jev_frontend_qa.core.redaction import redact_report

ORIGIN = "http://127.0.0.1:8767"


def collector(**limits):
    value = EvidenceCollector(**limits)
    value.set_rules([{"origin": ORIGIN, "methods": ["GET", "POST", "DELETE"], "path_prefix": "/api"}])
    return value


def request(value, request_id="network-1", method="POST", post_data=True):
    params = {"method": method, "url": ORIGIN + "/api/todos", "headers": {}}
    if method == "POST":
        params["hasPostData"] = True
        if post_data:
            params["postData"] = '{"title":"owned"}'
    value.handle_event("Network.requestWillBeSent", {"requestId": request_id, "request": params}, "owned-session")
    return params


def response(value, request_id="network-1", status=201, body='{"todo":{"title":"owned"}}'):
    value.handle_event(
        "Network.responseReceived",
        {"requestId": request_id, "response": {"status": status, "headers": {"Content-Type": "application/json"}}},
        "owned-session",
    )
    value.attach_response_body(request_id, body)


def test_fetch_and_network_ids_correlate_without_duplicate_or_lost_payload():
    value = collector()
    params = request(value, post_data=False)
    params["postData"] = '{"title":"owned"}'
    value.handle_event(
        "Fetch.requestPaused",
        {"requestId": "fetch-other-id", "networkId": "network-1", "request": params},
        "owned-session",
    )
    assert value.drain() == ([], 0)  # A mid-request read must retain correlation.
    response(value)
    records, lost = value.drain()
    assert lost == 0
    assert len(records) == 1
    assert records[0]["request_body"] == {"title": "owned"}
    assert records[0]["response_body"] == {"todo": {"title": "owned"}}
    assert records[0]["status"] == 201
    assert not records[0]["incomplete"]


def test_empty_delete_response_is_complete_but_missing_body_retrieval_is_not():
    value = collector()
    request(value, method="DELETE")
    response(value, status=204, body="")
    records, lost = value.drain()
    assert records[0]["status"] == 204
    assert not records[0]["incomplete"] and lost == 0
    request(value, request_id="network-2")
    value.mark_incomplete("network-2")
    records, _ = value.drain()
    assert records[0]["incomplete"]


def test_overflow_and_oversized_bodies_cannot_silently_preserve_success():
    value = collector(buffer_size=2)
    for index in range(5):
        request(value, request_id=str(index))
        response(value, request_id=str(index))
    records, lost = value.drain()
    assert lost == 3
    assert {record["request_id"] for record in records} == {"3", "4"}
    limited = collector(max_body_bytes=8)
    request(limited)
    response(limited)
    records, _ = limited.drain()
    assert records[0]["incomplete"]
    assert records[0]["request_body"] is None


def test_interrupted_pending_mutation_is_retained_as_incomplete():
    value = collector()
    request(value)
    assert value.pending_count == 1
    value.mark_pending_incomplete()
    records, _ = value.drain()
    assert records[0]["method"] == "POST"
    assert records[0]["status"] is None
    assert records[0]["incomplete"]
    assert value.pending_count == 0


def test_aborted_next_prefetch_is_not_fatal_but_other_aborted_gets_are():
    value = collector()
    params = {"method": "GET", "url": ORIGIN + "/api/todos", "headers": {"next-router-prefetch": "1"}}
    value.handle_event("Network.requestWillBeSent", {"requestId": "prefetch", "request": params}, "owned-session")
    value.handle_event(
        "Network.loadingFailed", {"requestId": "prefetch", "errorText": "net::ERR_ABORTED"}, "owned-session"
    )
    assert value.pending_count == 0
    assert value.drain() == ([], 0)

    request(value, request_id="ordinary-get", method="GET")
    value.handle_event(
        "Network.loadingFailed", {"requestId": "ordinary-get", "errorText": "net::ERR_ABORTED"}, "owned-session"
    )
    records, _ = value.drain()
    assert records[0]["incomplete"]
    assert records[0]["error"] == "net::ERR_ABORTED"


def test_capture_exception_and_denial_are_observable_without_disclosing_body():
    value = collector()
    value.mark_incomplete(None)
    params = {
        "method": "POST",
        "url": "https://denied.example/api",
        "headers": {"Authorization": "PRIVATE"},
        "postData": '{"secret":"PRIVATE"}',
    }
    value.handle_event("Network.requestWillBeSent", {"requestId": "denied", "request": params}, "owned-session")
    value.mark_denied("POST", params["url"])
    records, lost = value.drain()
    assert lost == 1
    assert records[0]["incomplete"] and records[0]["error"] == "denied_by_policy"
    assert "PRIVATE" not in json.dumps(records)


def test_report_redacts_nested_headers_case_variant_keys_urls_and_known_values():
    raw = {
        "evidence": [
            {
                "request_headers": {"Authorization": "Bearer PRIVATE_AUTH", "Cookie": "PRIVATE_COOKIE"},
                "response_headers": {"Set-Cookie": "PRIVATE_SESSION"},
                "request_body": {"apiKey": "PRIVATE_API", "nested": [{"ToKeN": "PRIVATE_TOKEN"}]},
                "url": "https://user:PRIVATE_PASSWORD@example.com/api?custom=PRIVATE_QUERY#PRIVATE_FRAGMENT",
            }
        ],
        "actions": [{"label": "Type PRIVATE_FIXTURE", "text": "PRIVATE_FIXTURE"}],
        "assertions": [{"observed": "PRIVATE_FIXTURE"}],
        "title": "synthetic safe title",
    }
    sanitized = redact_report(raw, RedactionPolicy(), secrets=("PRIVATE_FIXTURE",))
    assert "PRIVATE" not in json.dumps(sanitized)
    assert sanitized["title"] == "synthetic safe title"
    assert raw["actions"][0]["text"] == "PRIVATE_FIXTURE"  # Assertions keep the original value.
