# Synthetic Todo — Jev Frontend QA reference app

A deliberately small reference application used by the
[Jev Frontend QA](https://github.com/Nainish-Rai/jev-frontend-qa) runner to
prove that the tester detects false success notifications, incorrect payloads,
and lost saves. **The demo is synthetic**: no proprietary code, data, or
workflows are involved.

## Start it

After installing the package (`pip install -e .` from the repository root), the
console script is registered:

```bash
jev-todo
```

Or override the defaults:

```bash
jev-todo --host 127.0.0.1 --port 8767 --database artifacts/todo.sqlite3
```

| Flag         | Default                   | Purpose                                                  |
| ------------ | ------------------------- | -------------------------------------------------------- |
| `--host`     | `127.0.0.1`               | Interface to bind to (loopback only by default).         |
| `--port`     | `8767`                    | TCP port to listen on. Use `0` to let the OS pick.       |
| `--database` | `artifacts/todo.sqlite3`  | SQLite file. Parent directories are created if missing.  |
| `--quiet`    | `false`                   | Suppress the startup banner.                             |

The server runs on the standard library `http.server.ThreadingHTTPServer`
against a `sqlite3` database file. Restarting the process against the same
database preserves every record.

## Endpoints

All requests and responses use `application/json; charset=utf-8` unless noted.

| Method | Path                | Body                                   | Success                          | Failure              |
| ------ | ------------------- | -------------------------------------- | -------------------------------- | -------------------- |
| GET    | `/health`           | —                                      | `200 {"status": "ok"}`           | —                    |
| GET    | `/api/todos`        | —                                      | `200 {"todos": [...]}`           | —                    |
| POST   | `/api/todos`        | `{"title": string}`                    | `201 {"todo": {...}}`            | `400`/`422`          |
| PATCH  | `/api/todos/<id>`   | `{"title"?: string, "completed"?: bool}` | `200 {"todo": {...}}`         | `400`/`422`/`404`    |
| DELETE | `/api/todos/<id>`   | —                                      | `204`                            | `404`                |
| GET    | `/`                 | —                                      | `200 text/html`                  | —                    |

A todo is shaped as:

```json
{ "id": "uuid-v4-string", "title": "Buy milk", "completed": false }
```

### Validation rules

- `title` must be a string. After stripping leading and trailing whitespace it
  must be non-empty and at most 500 characters. Blank or whitespace-only
  titles are rejected with `422 Unprocessable Entity`; the database is not
  modified.
- `completed` must be a JSON boolean (`true` or `false`); any other type is
  rejected with `422`.
- PATCH requests must include at least one of `title` or `completed`; an
  empty body returns `400`.
- Unknown todo ids return `404 {"error": "Todo not found"}`.
- All errors respond with `{"error": "<message>"}`.

### Persistence

Restarting the server with the same database preserves titles, completion
state, and deletions. Use a fresh database path to isolate a demo run.

## Using the UI

1. Open `http://127.0.0.1:8767/` in any modern browser.
2. Type a title into the **Todo title** field and press **Create todo** (or
   Enter).
3. Each row exposes three controls:
   - **Complete _&lt;title&gt;_** / **Uncomplete _&lt;title&gt;_** — toggles the
     completed state.
   - **Edit _&lt;title&gt;_** — swaps the row for an **Edit title** field with
     **Save changes** and **Cancel** buttons.
   - **Delete _&lt;title&gt;_** — immediately removes that todo.
4. A live region at the foot of the page reports each request's result.
   The QA runner must verify network and persisted state independently of
   these UI messages.

The page is keyboard-operable end-to-end: Tab moves through controls in
document order, Enter submits forms, and Escape cancels an in-progress edit.

## Programmatic access

The server factory is exposed for tests and embedded use:

```python
from pathlib import Path
from jev_frontend_qa.demo import create_server

httpd = create_server("127.0.0.1", 0, Path("artifacts/todo.sqlite3"))
print("listening on", httpd.server_address)
httpd.serve_forever()
```

`create_server()` returns a `ThreadingHTTPServer` instance, so callers can
bind to an ephemeral port (`0`) for tests, inspect `server_address`, and
`shutdown()`/`server_close()` cleanly.

## Synthetic-data policy

This demo is a controlled reference application:

- No logins, no tokens, no external services, no analytics, no telemetry.
- No fonts, icons, or scripts are loaded from a third-party CDN. The stylesheet
  and script live alongside `index.html` in `src/jev_frontend_qa/demo_static/`.
- All data is user-supplied and synthetic; no proprietary application or
  workflow is referenced.
- Evidence, screenshots, browser profiles, and the SQLite database are kept
  out of version control by `.gitignore` (`artifacts/`, `evidence/`,
  `browser-profiles/`, `*.sqlite*`, `*.log`).
