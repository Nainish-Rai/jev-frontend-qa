import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from jev_frontend_qa.core import model_client
from jev_frontend_qa.core.decisions import TypeSafeDecisionProvider
from jev_frontend_qa.core.model_client import ModelClient, ModelError

PAGE = {
    "url": "http://127.0.0.1:8767/",
    "title": "Todo",
    "text": "Synthetic Todo",
    "actions": [
        {"id": "e1", "kind": "fill", "label": "Title", "node": 10, "value": ""},
        {"id": "e2", "kind": "click", "label": "Create todo", "node": 20},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ],
}


def answer(choice, options, confidence=0.95):
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {option: float(option == choice) for option in options},
    }


def provider(callback):
    client = ModelClient(api_key="synthetic-key")
    client._loop.run(client._http.aclose())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(callback))
    return TypeSafeDecisionProvider(name="typesafe", client=client)


def choose_with(callback):
    selected = provider(callback)
    try:
        return selected.choose(goal="Create a todo", page=PAGE, history=[])
    finally:
        selected.client.close()


def test_batched_targets_obey_api_and_only_selected_branch_is_consumed():
    def respond(request):
        body = json.loads(request.content)
        questions = body["questions"]
        assert set(questions) == {"operation", "type_text_target", "click_target"}
        for question in questions.values():
            assert len(question["criteria"]) >= 2
            assert all(value is None or isinstance(value, str) for value in question["criteria"].values())
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "operation": answer("TYPE_TEXT", questions["operation"]["criteria"]),
                    "type_text_target": answer("1", questions["type_text_target"]["criteria"], 0.3),
                    "click_target": "malformed unused branch",
                },
            },
        )

    decision = choose_with(respond)
    assert decision.choice == "e1"
    assert decision.confidence == 0.3  # A confident operation cannot hide an uncertain target.


def test_target_no_match_blocks_instead_of_forcing_the_only_input():
    def respond(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "operation": answer("TYPE_TEXT", questions["operation"]["criteria"]),
                    "type_text_target": answer("NONE", questions["type_text_target"]["criteria"]),
                }
            },
        )

    decision = choose_with(respond)
    assert decision.operation == "BLOCKED"
    assert decision.choice == "BLOCKED"


def test_wait_maps_to_observed_control_without_target_branch():
    def respond(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(200, json={"answers": {"operation": answer("WAIT", questions["operation"]["criteria"])}})

    assert choose_with(respond).choice == "wait"


def test_unknown_operation_never_becomes_an_action():
    def respond(request):
        options = json.loads(request.content)["questions"]["operation"]["criteria"]
        invalid = answer("CLICK", options)
        invalid["choice"] = "execute-arbitrary-javascript"
        return httpx.Response(200, json={"answers": {"operation": invalid}})

    with pytest.raises(ModelError) as failure:
        choose_with(respond)
    assert failure.value.code == "invalid_choice"


def test_boolean_probability_is_not_accepted_as_a_numeric_judgment():
    def respond(request):
        options = json.loads(request.content)["questions"]["operation"]["criteria"]
        invalid = answer("DONE", options)
        invalid["probabilities"]["DONE"] = True
        return httpx.Response(200, json={"answers": {"operation": invalid}})

    with pytest.raises(ModelError):
        choose_with(respond)


def test_invalid_json_returns_structured_model_error():
    with pytest.raises(ModelError) as failure:
        choose_with(lambda _: httpx.Response(200, text="not-json"))
    assert failure.value.code == "invalid_choice"


def test_provider_error_does_not_echo_response_secrets():
    with pytest.raises(ModelError) as failure:
        choose_with(lambda _: httpx.Response(422, text="private-customer-data api_key=not-for-artifacts"))
    assert failure.value.http_status == 422
    assert "private-customer-data" not in str(failure.value)
    assert "not-for-artifacts" not in str(failure.value)


def test_missing_key_never_contacts_provider():
    selected = provider(lambda _: pytest.fail("No HTTP request is permitted without credentials"))
    selected.client.api_key = None
    try:
        with pytest.raises(ModelError) as failure:
            selected.choose(goal="x", page=PAGE, history=[])
        assert failure.value.code == "missing_key"
    finally:
        selected.client.close()


def test_response_trickling_cannot_extend_the_whole_request_deadline(monkeypatch):
    payload = json.dumps({"answers": {"operation": answer("DONE", ["WAIT", "DONE", "BLOCKED"])}}).encode()

    class TrickleHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                for offset in range(0, len(payload), 2):
                    self.wfile.write(payload[offset : offset + 2])
                    self.wfile.flush()
                    time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(model_client, "API_URL", f"http://127.0.0.1:{server.server_port}/")
    try:
        with ModelClient("synthetic-key", timeout_seconds=0.1) as client:
            started = time.monotonic()
            with pytest.raises(ModelError) as failure:
                TypeSafeDecisionProvider("typesafe", client).choose(
                    goal="Inspect", page={"actions": [{"id": "wait", "kind": "wait", "label": "Wait"}]}, history=[]
                )
            assert failure.value.code == "timeout"
            assert time.monotonic() - started < 0.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
