"""Synthetic todo demo: a small HTTP API, a SQLite store, and a static UI.

The demo is the reference application for the Jev Frontend QA runner. It only
exercises synthetic data and ships no proprietary workflows or assets. The
server is intentionally small (stdlib `http.server` + `sqlite3`) so the tester
can drive it end-to-end without introducing framework dependencies.

Public surface:
    * `create_server(host, port, database)` -- returns an `HTTPServer` instance
      bound to a SQLite database file. The caller decides whether to call
      `serve_forever()` or drive the instance manually.
    * `main(argv=None)` -- the `jev-todo` console script entry point.

Design notes:
    * SQL is parameterised; user input is never interpolated into statements.
    * Titles are trimmed and rejected when blank/whitespace-only, in both the
      API (defensive server-side check) and the UI (client-side guard).
    * IDs are RFC 4122 UUID4 strings, generated with the stdlib `uuid` module.
    * The server binds to loopback by default and exposes only the routes
      described in the issue; there is no destructive reset endpoint.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping

_LOGGER = logging.getLogger("jev_frontend_qa.demo")
_MAX_TITLE_LENGTH = 500
_STATIC_ROOT = Path(__file__).parent / "demo_static"
_INDEX_FILE = "index.html"
_STATIC_CACHE_CONTROL = "no-cache"

# SQL statements are module-level constants so they are easy to audit; every
# query uses parameter binding.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todos (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    completed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""
_INSERT_SQL = (
    "INSERT INTO todos (id, title, completed, created_at) VALUES (?, ?, 0, ?)"
)
_SELECT_ALL_SQL = "SELECT id, title, completed FROM todos ORDER BY rowid ASC"
_SELECT_ONE_SQL = (
    "SELECT id, title, completed FROM todos WHERE id = ?"
)
_UPDATE_TITLE_SQL = "UPDATE todos SET title = ? WHERE id = ?"
_UPDATE_COMPLETED_SQL = "UPDATE todos SET completed = ? WHERE id = ?"
_UPDATE_BOTH_SQL = "UPDATE todos SET title = ?, completed = ? WHERE id = ?"
_DELETE_SQL = "DELETE FROM todos WHERE id = ?"


class TodoStore:
    """Serialize store operations and close each operation's SQLite connection."""

    def __init__(self, database: Path) -> None:
        self._database = Path(database)
        self._lock = threading.Lock()
        self._initialise_schema()


    def _connect(self) -> sqlite3.Connection:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise_schema(self) -> None:
        with self._lock, contextlib.closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA_SQL)

    def list_todos(self) -> list[dict[str, Any]]:
        with self._lock, contextlib.closing(self._connect()) as connection:
            rows = connection.execute(_SELECT_ALL_SQL).fetchall()
        return [_row_to_todo(row) for row in rows]

    def create_todo(self, title: str) -> dict[str, Any]:
        todo_id = str(uuid.uuid4())
        created_at = _now_iso()
        with self._lock, contextlib.closing(self._connect()) as connection:
            connection.execute(_INSERT_SQL, (todo_id, title, created_at))
            row = connection.execute(_SELECT_ONE_SQL, (todo_id,)).fetchone()
        assert row is not None  # we just inserted it
        return _row_to_todo(row)

    def get_todo(self, todo_id: str) -> dict[str, Any] | None:
        with self._lock, contextlib.closing(self._connect()) as connection:
            row = connection.execute(_SELECT_ONE_SQL, (todo_id,)).fetchone()
        return _row_to_todo(row) if row is not None else None

    def update_todo(
        self,
        todo_id: str,
        *,
        title: str | None = None,
        completed: bool | None = None,
    ) -> dict[str, None] | dict[str, Any]:
        """Apply a partial update.

        Returns `{"todo": ...}` on success, or `{"missing": True}` when no row
        with the supplied id exists. The caller decides how to translate the
        `missing` sentinel into an HTTP response.
        """
        with self._lock, contextlib.closing(self._connect()) as connection:
            existing = connection.execute(_SELECT_ONE_SQL, (todo_id,)).fetchone()
            if existing is None:
                return {"missing": True}
            if title is not None and completed is not None:
                connection.execute(
                    _UPDATE_BOTH_SQL, (title, 1 if completed else 0, todo_id)
                )
            elif title is not None:
                connection.execute(_UPDATE_TITLE_SQL, (title, todo_id))
            elif completed is not None:
                connection.execute(
                    _UPDATE_COMPLETED_SQL, (1 if completed else 0, todo_id)
                )
            else:
                return {"missing": False, "no_op": True}
            row = connection.execute(_SELECT_ONE_SQL, (todo_id,)).fetchone()
        assert row is not None  # we just confirmed it exists
        return {"todo": _row_to_todo(row)}

    def delete_todo(self, todo_id: str) -> bool:
        with self._lock, contextlib.closing(self._connect()) as connection:
            cursor = connection.execute(_DELETE_SQL, (todo_id,))
        return cursor.rowcount > 0


def _now_iso() -> str:
    """Return the current UTC timestamp formatted as ISO 8601 with `Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_todo(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "completed": bool(row["completed"]),
    }


def _make_handler(store: TodoStore) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to the given store."""

    class TodoHandler(BaseHTTPRequestHandler):
        server_version = "JevFrontendQADemo/0.1"

        # Silence the default per-request stderr access log; the demo is meant
        # to be quiet when running interactively. Uncomment to debug routing.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            _LOGGER.debug(format, *args)

        # -- Request dispatch -------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        # -- Dispatch ---------------------------------------------------------
        def _dispatch(self, method: str) -> None:
            try:
                self._route(method)
            except _ClientError as error:
                self._write_error(error.status, str(error))
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Unhandled error in request handler")
                self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error")

        def _route(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health" and method == "GET":
                self._write_json(HTTPStatus.OK, {"status": "ok"})
                return
            if path in {"/", "/styles.css", "/app.js"} and method == "GET":
                self._serve_static(path)
                return
            if path == "/api/todos":
                self._handle_collection(method)
                return
            if path.startswith("/api/todos/"):
                self._handle_item(method, path[len("/api/todos/"):])
                return
            raise _ClientError(HTTPStatus.NOT_FOUND, "Not found")

        # -- Static assets ---------------------------------------------------
        def _serve_static(self, path: str) -> None:
            filename, content_type = {
                "/": (_INDEX_FILE, "text/html; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }[path]
            try:
                body = (_STATIC_ROOT / filename).read_bytes()
            except FileNotFoundError as error:
                raise _ClientError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Demo assets missing: {error.filename}",
                ) from error
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", _STATIC_CACHE_CONTROL)
            self.end_headers()
            self.wfile.write(body)

        # -- API: collection -------------------------------------------------
        def _handle_collection(self, method: str) -> None:
            if method == "GET":
                self._write_json(HTTPStatus.OK, {"todos": store.list_todos()})
                return
            if method == "POST":
                payload = self._read_json_body()
                title = _validate_title(payload.get("title"))
                todo = store.create_todo(title)
                self._write_json(HTTPStatus.CREATED, {"todo": todo})
                return
            self._method_not_allowed({"GET", "POST"})

        # -- API: item --------------------------------------------------------
        def _handle_item(self, method: str, todo_id: str) -> None:
            # Method dispatch comes first so an unknown HTTP verb returns 405
            # regardless of whether the id is well-formed.
            if method == "GET":
                self._method_not_allowed({"PATCH", "DELETE"})
                return
            if method == "PATCH":
                self._update_item(todo_id)
                return
            if method == "DELETE":
                self._delete_item(todo_id)
                return
            self._method_not_allowed({"PATCH", "DELETE"})

        def _update_item(self, todo_id: str) -> None:
            todo_id = _validate_id(todo_id)
            payload = self._read_json_body()
            if not isinstance(payload, Mapping):
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Request body must be a JSON object",
                )
            title_provided = "title" in payload
            completed_provided = "completed" in payload
            if not title_provided and not completed_provided:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "At least one of 'title' or 'completed' must be provided",
                )
            title: str | None = None
            completed: bool | None = None
            if title_provided:
                title = _validate_title(payload["title"])
            if completed_provided:
                completed = _validate_completed(payload["completed"])
            result = store.update_todo(todo_id, title=title, completed=completed)
            if result.get("missing"):
                raise _ClientError(HTTPStatus.NOT_FOUND, "Todo not found")
            self._write_json(HTTPStatus.OK, {"todo": result["todo"]})

        def _delete_item(self, todo_id: str) -> None:
            todo_id = _validate_id(todo_id)
            if not store.delete_todo(todo_id):
                raise _ClientError(HTTPStatus.NOT_FOUND, "Todo not found")
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- Body parsing -----------------------------------------------------
        def _read_json_body(self) -> Any:
            length = self.headers.get("Content-Length")
            if length is None:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Missing Content-Length header",
                )
            try:
                size = int(length)
            except ValueError as error:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid Content-Length header",
                ) from error
            if size < 0 or size > 65536:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Request body too large",
                )
            raw = self.rfile.read(size) if size > 0 else b""
            if not raw:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Request body must be a JSON object",
                )
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid UTF-8 JSON body",
                ) from error
            if not isinstance(payload, Mapping):
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "Request body must be a JSON object",
                )
            return payload

        # -- Responses --------------------------------------------------------
        def _write_json(self, status: HTTPStatus, body: Mapping[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _write_error(self, status: HTTPStatus, message: str) -> None:
            self._write_json(status, {"error": message})

        def _method_not_allowed(self, allowed: Iterable[str]) -> None:
            allow = ", ".join(sorted(allowed))
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", allow)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps({"error": f"Method not allowed; expected one of: {allow}"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TodoHandler


# -- Validation helpers --------------------------------------------------------
class _ClientError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def _validate_title(value: Any) -> str:
    if not isinstance(value, str):
        raise _ClientError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "'title' must be a string",
        )
    title = value.strip()
    if not title:
        raise _ClientError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "'title' must not be blank",
        )
    if len(title) > _MAX_TITLE_LENGTH:
        raise _ClientError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"'title' must be {_MAX_TITLE_LENGTH} characters or fewer",
        )
    return title


def _validate_completed(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _ClientError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "'completed' must be a boolean",
        )
    return value


def _validate_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise _ClientError(HTTPStatus.NOT_FOUND, "Todo not found")
    try:
        uuid.UUID(candidate)
    except ValueError as error:
        raise _ClientError(HTTPStatus.NOT_FOUND, "Todo not found") from error
    return candidate


# -- Public factory ------------------------------------------------------------
def create_server(host: str, port: int, database: Path) -> ThreadingHTTPServer:
    """Build a `ThreadingHTTPServer` bound to the supplied loopback address.

    Args:
        host: Interface to bind to. Callers should pass a loopback address
            unless they have explicitly opted in to wider exposure.
        port: TCP port. `0` lets the OS pick a free port, which is useful for
            tests that exercise the API without coordinating ports.
        database: Path to the SQLite file. The parent directory is created if
            it does not exist.

    Returns:
        A `ThreadingHTTPServer` instance ready to be passed to
        `serve_forever()` or driven manually.
    """
    store = TodoStore(Path(database))
    handler = _make_handler(store)
    # ThreadingHTTPServer supports per-request handler instantiation; the
    # handler closes over `store` via the enclosing closure.
    server = ThreadingHTTPServer((host, port), handler)
    server.request_queue_size = 32
    return server


# -- CLI -----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-todo",
        description=(
            "Run the synthetic todo demo on loopback. The demo is a "
            "reference app for the Jev Frontend QA runner; it stores todos "
            "in a local SQLite file and serves a small static UI."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8767,
        help="TCP port to listen on (default: 8767)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts") / "todo.sqlite3",
        help="SQLite database file (default: artifacts/todo.sqlite3)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the startup banner",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database = args.database.expanduser()
    database.parent.mkdir(parents=True, exist_ok=True)

    httpd = create_server(args.host, args.port, database)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]

    if not args.quiet:
        print(
            f"jev-todo listening on http://{bound_host}:{bound_port} "
            f"(database: {database})",
            file=sys.stderr,
            flush=True,
        )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not args.quiet:
            print("\nShutting down...", file=sys.stderr, flush=True)
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
