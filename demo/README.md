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
| `--database` | `artifacts/todo.sqlite3`  | Base SQLite path. Fault variants add their name before the extension. |
| `--quiet`    | `false`                   | Suppress the startup banner.                             |
| `--variant`  | `healthy`                 | `healthy`, `fake-success`, `incorrect-payload`, or `lost-save`. |

The server runs on the standard library `http.server.ThreadingHTTPServer`
against a `sqlite3` database file. In healthy and incorrect-payload variants,
restarting against the same database preserves records. The lost-save variant
deliberately rolls back each create; fake-success rejects each create.

## Endpoints

All requests and responses use `application/json; charset=utf-8` unless noted.

| Method | Path                | Body                                   | Success                          | Failure              |
| ------ | ------------------- | -------------------------------------- | -------------------------------- | -------------------- |
| GET    | `/health`           | —                                      | `200 {"status": "ok", "variant": "healthy"}` | —             |
| GET    | `/api/todos`        | —                                      | `200 {"todos": [...]}`           | —                    |
| POST   | `/api/todos`        | `{"title": string}`                    | `201 {"todo": {...}}`            | `400`/`422`/`503` (fake-success) |
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

Healthy behavior preserves titles, completion state, and deletions across
restarts. A base path of `artifacts/todo.sqlite3` resolves as follows:

| Variant | SQLite file |
| --- | --- |
| `healthy` | `artifacts/todo.sqlite3` |
| `fake-success` | `artifacts/todo.fake-success.sqlite3` |
| `incorrect-payload` | `artifacts/todo.incorrect-payload.sqlite3` |
| `lost-save` | `artifacts/todo.lost-save.sqlite3` |

This separation also applies to an explicit `--database` path and to
`create_server(..., variant=...)`. Supply the **base** path, not an already
variant-suffixed filename. Use a new base directory for each independent run;
variant isolation does not reset previous runs of the same variant.

## Deliberately broken variants

These switches affect only the synthetic demo, never the tester's verdict
logic. `/health` identifies the selected variant for local run metadata; the
root HTML also carries `data-demo-variant` to configure the demo frontend.
Neither is an oracle for correctness or changes the expected create contract.

| Variant | Actual behavior | Expected contract evidence |
| --- | --- | --- |
| `healthy` | UI submits the intended title; API commits the row and returns 201. | Correct request, successful response, matching UI row and fresh GET: PASS. |
| `fake-success` | The real POST returns 503 without writing. The UI ignores that rejection, shows the intended row, and announces success. | Success UI contradicts API rejection; fresh GET has no record: FAIL. |
| `incorrect-payload` | The UI replaces the first title character with `!` (or `?` when it was already `!`), sends that wrong title, then displays the intended title. The API commits exactly the submitted value. | Observed request title differs from the exact fixture despite 201 and apparent UI success: FAIL. |
| `lost-save` | The API inserts and reads the row inside a real transaction, rolls it back, then returns 201 with that row. The UI shows success. | Request/response/UI initially agree, but fresh GET after reload lacks the row: FAIL. |

Only create behavior is deliberately defective. API title validation still runs
before the fault, and existing edit/complete/delete behavior is unchanged.
The displayed optimistic rows are not restored from browser storage on reload.

Run each variant **one at a time** on the same origin, stopping its server before
starting the next. The following are four alternative launch commands:

```bash
uv run jev-todo --variant healthy --database artifacts/fault-demo/todo.sqlite3
uv run jev-todo --variant fake-success --database artifacts/fault-demo/todo.sqlite3
uv run jev-todo --variant incorrect-payload --database artifacts/fault-demo/todo.sqlite3
uv run jev-todo --variant lost-save --database artifacts/fault-demo/todo.sqlite3
```

With one server running, use the **same unchanged** `examples/create.json` and
`examples/policy.json` against every variant. Set `VARIANT` below solely to name
the output directory; it is not passed to the tester or added to expectations:

```bash
VARIANT=healthy
uv run jev-qa run \
  --scenario examples/create.json \
  --policy examples/policy.json \
  --work-dir "artifacts/fault-demo/$VARIANT/work" \
  --report "artifacts/fault-demo/$VARIANT/report.json" \
  --headed
```

Use the corresponding output label for each server and retain local health
metadata, request/response evidence, pre-reload UI evidence, and post-reload
fresh reads. The runner requires its normal Browser Harness/Chrome setup and
`TYPESAFE_API_KEY`; missing prerequisites must remain BLOCKED/ERROR, not count
as defect detection. The table describes **expected**, not recorded, results.
Do not claim acceptance until real runs yield one PASS and three FAIL outcomes.

Public API regression coverage is in `tests/test_demo_faults.py`; it checks
real HTTP rejection, lost persistence, variant isolation, and unchanged title
validation without SQLite introspection. Browser acceptance must additionally
verify the misleading UI and the wrong title actually sent by that UI.

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

httpd = create_server("127.0.0.1", 0, Path("artifacts/todo.sqlite3"), variant="healthy")
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
