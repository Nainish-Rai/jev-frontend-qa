# Jev Frontend QA

Evidence-driven frontend QA built on [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) and [Browser Harness](https://github.com/browser-use/browser-harness). Python 3.12+, Chrome/Chromium, plain HTML/CSS/JavaScript demo, and SQLite.

**Status: implementation available; live Jev acceptance is pending.** No TypeSafe credential was available during implementation. Browser/transport/assertion checks used a deterministic injected decision provider, not Jev. Those checks do **not** establish live model accuracy, confidence calibration, or end-to-end Jev completion. [Ticket #2](https://github.com/Nainish-Rai/jev-frontend-qa/issues/2) remains open. The production CLI has no offline-model fallback.

## Run the synthetic demo

```bash
uv sync
uv run jev-todo --port 8767 --database artifacts/todo.sqlite3
```

Open `http://127.0.0.1:8767/`. The [standalone demo](demo/README.md) supports create, edit, complete/uncomplete, delete, blank-title rejection, and persistence across reload and restart. Everything is synthetic and local; no proprietary applications or data are included.

## Run with live Jev

Set `TYPESAFE_API_KEY` in your environment or copy `.env.example` to the gitignored `.env` in this repository and fill in the key. Do not commit credentials or paste them into issue comments.

Review `examples/policy.json` first: it explicitly permits synthetic demo content to reach TypeSafe, local request/response evidence, read access to the demo origin, and mutations only under `/api/todos`. Screenshots remain disabled. Policy schema defaults deny disclosure and authorize no origins.

With the demo running in another terminal:

```bash
uv run jev-qa validate --scenario examples/create.json --policy examples/policy.json
uv run jev-qa run --scenario examples/create.json --policy examples/policy.json \
  --headed --report artifacts/create-report.json
```

Use `--headless` for a non-visible browser. Default execution creates a new private QA profile and an owned tab; it never copies personal cookies. `--chrome-executable` overrides browser discovery. The model is pinned to `jev-1.13.0`; `--model` changes the TypeSafe model ID. The default selected-branch confidence threshold is `0.55`, configurable with `--confidence-threshold`; it is **not yet calibrated against live demo runs**.

The CLI calls `POST https://api.typesafe.ai/v1/systemone`. A missing key or denied permission returns BLOCKED before browser launch. Protocol tests are not a substitute for running this command with valid credentials.

## Scenarios and correctness

The caller supplies goals, exact fixture strings, selectors, and expected behavior. Jev selects only observed, bounded actions; it does not generate field values, selectors, JavaScript, permissions, or assertions. Browser Harness remains the transport—there is no Playwright replacement.

| Scenario | Purpose |
| --- | --- |
| `examples/create.json` | Create a UUID-owned record; verify actual payload, response, UI, fresh persistence; scoped cleanup |
| `examples/lifecycle.json` | Create → edit → complete → uncomplete → delete, with independent fresh reads |
| `examples/validation.json` | Submit empty/whitespace values through the UI; verify rejection, no POST, and no invalid persisted record |
| `examples/explore.json` | Inspect controls without certifying unspecified expected behavior |

Run another scenario by changing `--scenario`. `run_id` is generated for each load unless explicitly supplied. Fixtures interpolate `{{run_id}}`; verified response captures can provide identifiers for later scoped steps. Cleanup acts only on an identifier obtained from this run's verified creation response. Cleanup failures are reported separately and never hidden behind a primary verdict.

Assertions include exact network exchange matching, nested request/response values, DOM text/count/attributes, no-request checks, and fresh persistence/absence. Persistence reads use an authorized same-origin GET with cache and service-worker bypass. A toast, model DONE, stale response, or fields matched across different records cannot establish PASS.

### Outcomes

Reports use lowercase values and include per-assertion results, redacted local network evidence, action history, execution mode, and separated observations/model judgments.

| Verdict | Exit | Meaning |
| --- | --- | --- |
| `pass` | 0 | Authored contract assertions passed with complete evidence |
| `complete` | 0 | Exploratory goal completed; **not** a contract PASS |
| `fail` | 1 | Captured behavior contradicted an authored assertion |
| `blocked` | 2 | Missing permission/credential, uncertainty, unsupported interaction, limit, or incomplete evidence |
| `error` | 3 | Model/runner/configuration or resource-release failure |

Consumers must inspect `verdict`, not exit 0 alone. JSON is written to stdout and the private local `--report` file. Evidence, profiles, databases, and `.env` are excluded from Git; nothing is automatically uploaded. Native select controls, embedded browsing contexts, and popup workflows are currently unsupported and stop safely.

Scenario `limits` default to 60 actions and 120 seconds. The action count and execution deadline span all steps; model and browser waits use the remaining deadline. Lost/truncated/disconnected capture prevents PASS. An interrupted or ambiguous write is not automatically submitted again; cleanup is skipped when ownership or outcome is uncertain.

## Deliberate defects

Start the demo with `--variant healthy`, `fake-success`, `incorrect-payload`, or `lost-save`. Fault databases are automatically namespaced. See [demo commands and fault mechanisms](demo/README.md).

Use the **same** creation contract and expected values for all variants. The runner only records variant metadata; it does not branch correctness logic on a variant name.

## Explicit existing-browser attachment

Use a separately prepared **synthetic** Chrome profile with remote debugging enabled. Review `examples/attach-policy.json`, which approves the name `synthetic-demo` and the synthetic demo disclosure policy. Both invocation flags and matching policy permission are required:

```bash
uv run jev-qa run --scenario examples/create.json \
  --policy examples/attach-policy.json \
  --attach-profile synthetic-demo --cdp-url http://127.0.0.1:9222 \
  --report artifacts/attached-report.json
```

Only a newly owned QA tab/session is driven and captured. Cleanup closes that tab and its daemon, not the attached browser or unrelated tabs. Attachment does not imply permission to disclose page content. Origin/method/path rules are guardrails, **not a sandbox for arbitrary backend side effects**.

## Verification performed

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Parent-owned checks exercised real Chrome, Browser Harness, demo HTTP requests, and SQLite with **offline deterministic action selections**:

- Healthy creation, full lifecycle, UI validation, fresh reads, and owned cleanup.
- Same creation contract: healthy PASS, fake-success FAIL, incorrect-payload FAIL, lost-save FAIL.
- Exploratory COMPLETE; unsupported frame, no-progress, and action-limit BLOCKED.
- A real committed write with its response delayed: BLOCKED, exactly one backend write, no automatic resubmission.
- Headed and headless execution; approved synthetic attachment with an unrelated sentinel tab preserved after cleanup.
- Missing-key and denied-attachment CLI preflight returning BLOCKED.

**Still required:** a live Jev-backed acceptance run with valid credentials, including its actual decisions and thresholds. Local offline report verdicts are assertion-engine evidence, not proof that Jev can navigate these scenarios.

## Development and attribution

Use Matt Pocock's workflow: approved design → vertical issues → behavioral verification → independent standards/spec review. Start with `CONTEXT.md`, `docs/design.md`, and `docs/adr/`. [GitHub Issues](https://github.com/Nainish-Rai/jev-frontend-qa/issues) retain the acceptance dependencies.

The project-local [TypeSafe skill](.agents/skills/typesafe-ai/SKILL.md) guides the integration. Adapted upstream MIT notices are preserved in [THIRD_PARTY_NOTICES.md](src/jev_frontend_qa/THIRD_PARTY_NOTICES.md). Current protocol references: [HTTP API](https://docs.typesafe.ai/api), [Choice](https://docs.typesafe.ai/primitives/choice), and [models](https://docs.typesafe.ai/models).
