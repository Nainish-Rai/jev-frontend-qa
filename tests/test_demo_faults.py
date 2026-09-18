"""Demo fault contracts through real HTTP, without inspecting SQLite or JS source.

Browser acceptance separately checks the deceptive UI and actual submitted title.
"""

import contextlib
import json
import threading
from http.client import HTTPConnection

import pytest

from jev_frontend_qa.demo import create_server


@contextlib.contextmanager
def running_variant(database, variant):
    server = create_server("127.0.0.1", 0, database, variant=variant)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(address, method, path="/api/todos", body=None):
    connection = HTTPConnection(*address, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    try:
        connection.request(method, path, payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        content = response.read()
        return response.status, json.loads(content) if content else None
    finally:
        connection.close()


def test_fake_success_rejects_real_write_and_has_no_persisted_record(tmp_path):
    database = tmp_path / "todos.sqlite3"
    with running_variant(database, "fake-success") as address:
        status, error = request(address, "POST", body={"title": "Synthetic task"})
        assert status == 503
        assert "error" in error
        assert request(address, "GET") == (200, {"todos": []})
    with running_variant(database, "fake-success") as address:
        assert request(address, "GET") == (200, {"todos": []})


def test_lost_save_returns_created_record_but_fresh_reads_cannot_find_it(tmp_path):
    database = tmp_path / "todos.sqlite3"
    with running_variant(database, "lost-save") as address:
        status, payload = request(address, "POST", body={"title": "Synthetic task"})
        assert status == 201
        todo = payload["todo"]
        assert todo == {"id": todo["id"], "title": "Synthetic task", "completed": False}
        # Each request opens a new HTTP connection and GET reads the real store.
        assert request(address, "GET") == (200, {"todos": []})
        assert request(address, "PATCH", f"/api/todos/{todo['id']}", {"completed": True})[0] == 404
    with running_variant(database, "lost-save") as address:
        assert request(address, "GET") == (200, {"todos": []})


def test_variants_using_same_base_database_are_isolated_and_identifiable(tmp_path):
    database = tmp_path / "todos.sqlite3"
    variants = ("healthy", "fake-success", "incorrect-payload", "lost-save")
    with contextlib.ExitStack() as stack:
        addresses = {variant: stack.enter_context(running_variant(database, variant)) for variant in variants}
        for variant, address in addresses.items():
            assert request(address, "GET", "/health") == (200, {"status": "ok", "variant": variant})
        status, healthy = request(addresses["healthy"], "POST", body={"title": "Healthy task"})
        assert status == 201
        assert request(addresses["incorrect-payload"], "GET") == (200, {"todos": []})
        # The incorrect-payload fault is in the frontend, not an API rewrite:
        # the backend stores exactly what the real request supplied.
        status, incorrect = request(addresses["incorrect-payload"], "POST", body={"title": "!ynthetic task"})
        assert status == 201
        assert incorrect["todo"]["title"] == "!ynthetic task"
        assert request(addresses["healthy"], "GET") == (200, {"todos": [healthy["todo"]]})
        assert request(addresses["fake-success"], "GET") == (200, {"todos": []})
        assert request(addresses["lost-save"], "GET") == (200, {"todos": []})
    with running_variant(database, "incorrect-payload") as address:
        assert request(address, "GET") == (200, {"todos": [incorrect["todo"]]})
    with running_variant(database, "healthy") as address:
        assert request(address, "GET") == (200, {"todos": [healthy["todo"]]})


@pytest.mark.parametrize("variant", ["healthy", "fake-success", "incorrect-payload", "lost-save"])
def test_fault_injection_does_not_bypass_api_title_validation(tmp_path, variant):
    with running_variant(tmp_path / "todos.sqlite3", variant) as address:
        assert request(address, "POST", body={"title": "  "})[0] == 422
        assert request(address, "GET") == (200, {"todos": []})
