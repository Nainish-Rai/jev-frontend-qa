"""Behavior contracts exercised through the demo's real HTTP interface."""

import contextlib
import json
import threading
from http.client import HTTPConnection

import pytest

from jev_frontend_qa.demo import create_server


@contextlib.contextmanager
def running_server(database):
    server = create_server("127.0.0.1", 0, database)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(address, method, path="/api/todos", body=None, raw=None):
    connection = HTTPConnection(*address, timeout=5)
    payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    try:
        connection.request(method, path, payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        content = response.read()
        kind = response.getheader("Content-Type", "")
        return response.status, json.loads(content) if content and "application/json" in kind else content
    finally:
        connection.close()


@pytest.fixture
def address(tmp_path):
    with running_server(tmp_path / "todos.sqlite3") as address:
        yield address


def create(address, title):
    status, body = request(address, "POST", body={"title": title})
    assert status == 201, body
    return body["todo"]


def test_todo_lifecycle_survives_server_restart(tmp_path):
    database = tmp_path / "todos.sqlite3"
    with running_server(database) as address:
        todo = create(address, "First title")
        path = f"/api/todos/{todo['id']}"
        status, body = request(address, "PATCH", path, {"title": "Edited title", "completed": True})
        assert status == 200
        assert body["todo"] == {"id": todo["id"], "title": "Edited title", "completed": True}
    with running_server(database) as address:
        assert request(address, "GET")[1]["todos"] == [body["todo"]]
        status, body = request(address, "PATCH", path, {"completed": False})
        assert status == 200 and body["todo"]["completed"] is False
        assert request(address, "DELETE", path)[0] == 204
    with running_server(database) as address:
        assert request(address, "GET")[1]["todos"] == []


def test_invalid_title_does_not_create_or_overwrite_a_todo(address):
    todo = create(address, "Keep me")
    assert request(address, "POST", body={"title": " \t\n "})[0] == 422
    assert request(address, "PATCH", f"/api/todos/{todo['id']}", {"title": ""})[0] == 422
    assert request(address, "GET")[1]["todos"] == [todo]


def test_missing_or_wrong_typed_fields_are_rejected(address):
    assert request(address, "POST", body={})[0] == 422
    assert request(address, "POST", body={"title": 123})[0] == 422
    todo = create(address, "Keep active")
    assert request(address, "PATCH", f"/api/todos/{todo['id']}", {"completed": "false"})[0] == 422
    assert request(address, "GET")[1]["todos"] == [todo]


def test_malformed_and_non_utf8_payloads_do_not_mutate_state(address):
    assert request(address, "POST", raw=b"not json")[0] == 400
    assert request(address, "POST", raw=b"\xff")[0] == 400
    assert request(address, "POST", body=["wrong shape"])[0] == 400
    assert request(address, "GET")[1]["todos"] == []


def test_deleting_one_todo_preserves_unrelated_data(address):
    kept = create(address, "Unrelated")
    removed = create(address, "Owned fixture")
    assert request(address, "DELETE", f"/api/todos/{removed['id']}")[0] == 204
    assert request(address, "GET")[1]["todos"] == [kept]
    assert request(address, "DELETE", f"/api/todos/{removed['id']}")[0] == 404


def test_browser_assets_are_served_and_traversal_is_rejected(address):
    for path, expected_type in [("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css")]:
        connection = HTTPConnection(*address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            assert response.status == 200
            assert response.getheader("Content-Type").startswith(expected_type)
        finally:
            connection.close()
    assert request(address, "GET", "/../demo.py")[0] == 404
