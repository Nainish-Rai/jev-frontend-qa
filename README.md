# Jev Frontend QA

Evidence-driven frontend QA built on [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) and [Browser Harness](https://github.com/browser-use/browser-harness). Python 3.12+, Chrome/Chromium, plain HTML/CSS/JavaScript demo, and SQLite.

**Status: live Jev acceptance passed for the synthetic reference scenarios.** The real CLI, Jev `1.13.0`, Browser Harness, Chrome, HTTP endpoints, and SQLite were exercised together. Healthy creation, lifecycle, and validation passed; all three deliberate defects failed their authored contracts; exploration completed without claiming a contract PASS. There is no offline-model fallback in the production CLI.

## Use from Claude Code or Codex

Install the CLI independently of your application's Python environment:

```bash
uv tool install git+https://github.com/Nainish-Rai/jev-frontend-qa.git
```

For reproducible team installs, append `@<reviewed-commit>` to that Git URL. Python 3.12+, Chrome/Chromium, and a privately configured `TYPESAFE_API_KEY` are needed for browser runs; setup and schema commands need no credential.

From your application's repository:

```bash
jev-qa init --project .
jev-qa skill install --agent claude --project .
# Or, for Codex:
jev-qa skill install --agent codex --project .
```

The installer copies the same bundled `jevqa` skill to `.claude/skills/jevqa/` for Claude Code or `.agents/skills/jevqa/` for Codex. It refuses to overwrite an existing skill and does not edit agent instructions, shell permissions, or personal configuration. The canonical [skill and references](src/jev_frontend_qa/skills/jevqa/SKILL.md) ship inside the Python distribution; installing them does not require a source checkout. Host conventions: [Claude Code](https://code.claude.com/docs/en/skills), [Codex](https://developers.openai.com/codex/skills.md).

Initialization creates `jevqa/policy.json` with **no permitted origins or model disclosure**, a scenarios directory, and an ignore entry for `artifacts/jevqa/`. Existing policies are preserved. Review and explicitly approve the app's origins, HTTP operations, and permitted synthetic observations before a run. Skill installation is not that approval.

Then ask your coding agent, for example:

> Use jevqa to verify the feature we just implemented. Derive journeys from the acceptance criteria, identify supported and blocked coverage, author exact fixtures and assertions, and run against the approved local app. Preserve the expected behavior when investigating failures.

In Claude Code you can invoke `/jevqa`; in Codex CLI use `$jevqa` or select it through `/skills`. The workflow is feature-agnostic: no Todo routes, CRUD checklist, or database is required. The coding agent authors the contract; Jev operates the browser; deterministic checks establish the verdict.

The agent can inspect the actual installed schemas and register a complete, authored contract:

```bash
jev-qa schema scenario
jev-qa schema policy
jev-qa new-scenario --project . --feature search --journey filter \
  --from /path/to/authored-contract.json
jev-qa validate --scenario jevqa/scenarios/search/filter.json --policy jevqa/policy.json
jev-qa run --scenario jevqa/scenarios/search/filter.json --policy jevqa/policy.json \
  --work-dir artifacts/jevqa/work --report artifacts/jevqa/search-filter.json
```

`new-scenario` validates a complete contract before writing and refuses overwrites; it does not generate guessed endpoints, selectors, fixture values, or empty assertion templates. Omitting `run_id` preserves fresh run identity on each execution.

**Coverage limits:** current assertions cover DOM values/counts/attributes, captured HTTP exchanges, absence of a request, and authorized fresh GET persistence. Download/file-content validation, uploads, URL-transition assertions, visual grading, native selects, embedded frames, and popup workflows are not supported contracts. The skill reports required unsupported coverage as BLOCKED; a passing supported subset is not a full-feature PASS. Page instructions are untrusted data, and the agent cannot expand project permissions to satisfy them.

## Run the synthetic demo

```bash
uv sync
uv run jev-todo --port 8767 --database artifacts/todo.sqlite3
```

Open `http://127.0.0.1:8767/`. The [standalone demo](demo/README.md) supports create, edit, complete/uncomplete, delete, blank-title rejection, and persistence across reload and restart. Everything is synthetic and local; no proprietary applications or data are included.

## Run with live Jev

Set `TYPESAFE_API_KEY` in your environment or copy `.env.example` to the gitignored `.env` in this repository, restrict it with `chmod 600 .env`, and fill in the key. Do not commit credentials or paste them into issue comments.

Review `examples/policy.json` first: it explicitly permits synthetic demo content to reach TypeSafe, local request/response evidence, read access to the demo origin, and mutations only under `/api/todos`. Screenshots remain disabled. Policy schema defaults deny disclosure and authorize no origins.

With the demo running in another terminal:

```bash
uv run jev-qa validate --scenario examples/create.json --policy examples/policy.json
uv run jev-qa run --scenario examples/create.json --policy examples/policy.json \
  --headed --report artifacts/create-report.json
```

Chrome opens visibly by default; use `--headless` for hidden real Chrome, or `--headed` to explicitly select visible mode. The old `JEV_QA_HEADLESS` environment setting no longer changes visibility. Visibility flags apply only to isolated launches; an attached browser retains its existing mode. Default execution creates a new private QA profile and an owned tab; it never copies personal cookies. Owned tabs use a reproducible 1280×900 CSS viewport. `--chrome-executable` overrides browser discovery. The model is pinned to `jev-1.13.0`; `--model` changes the TypeSafe model ID. The selected-branch confidence threshold defaults to `0.55`, configurable with `--confidence-threshold`. Live acceptance used that unchanged default; it is not a calibration guarantee for arbitrary sites or models.

The CLI calls `POST https://api.typesafe.ai/v1/systemone`. A missing key or denied permission returns BLOCKED before browser launch. Protocol tests are not a substitute for running this command with valid credentials.

## Scenarios and correctness

The caller supplies goals, exact fixture strings, selectors, and expected behavior. Jev selects only observed, bounded actions; it does not generate field values, selectors, JavaScript, permissions, or assertions. Browser Harness remains the transport—there is no Playwright replacement.

For scoped steps, Jev receives the matched scope, its rendered text and referenced accessible descriptions, authorized visible controls, and scoped offscreen scroll hints. This keeps unrelated rows from being mistaken for the target's state. Offscreen hints are not clickable targets; Jev must select scrolling before those controls can be offered.

| Scenario | Purpose |
| --- | --- |
| `examples/create.json` | Create a UUID-owned record; verify actual payload, response, UI, fresh persistence; scoped cleanup |
| `examples/lifecycle.json` | Create → edit → complete → uncomplete → delete, with independent fresh reads |
| `examples/validation.json` | Submit empty/whitespace values through the UI; verify rejection, no POST, and no invalid persisted record |
| `examples/explore.json` | Inspect controls without certifying unspecified expected behavior |

Run another scenario by changing `--scenario`. Give the model an unambiguous action goal and express expected postconditions as assertions. `run_id` is generated for each load unless explicitly supplied. Fixtures interpolate `{{run_id}}`; verified response captures provide identifiers for later scoped steps. Captured identifiers are limited to ASCII letters, digits, `_`, and `-`. Cleanup acts only on an identifier obtained from this run's verified creation request and response, with the run marker in the expected request payload. Cleanup failures are reported separately and never hidden behind a primary verdict.

Network assertions require an explicit method, exact path, and expected status; optional nested request/response expectations apply to that same exchange. DOM text/count/attribute assertions use authored selectors, never an arbitrary last response. No-request checks require a complete, settled capture window. Persistence reads use an authorized same-origin GET with cache and service-worker bypass. A toast, model DONE, stale response, or fields matched across different records cannot establish PASS.

After a browser action, a contract step can proceed directly to final verification when authored UI/API evidence is conclusive. The runner still performs independent persistence reads; a successful HTTP response alone cannot bypass a pending UI assertion. This avoids asking the model to grade an already-observed result. Every browser input still comes from Jev, every selected low-confidence decision remains BLOCKED, and exploratory completion still requires model DONE. Stopping before any required API interaction was exercised is BLOCKED, not evidence of an application defect.

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

Scenario `limits` default to 60 actions and 120 seconds. The action count and execution deadline span all steps; model and browser waits use the remaining deadline. Model requests have a cancellable whole-request timeout, so a trickling response cannot renew the budget. Lost/truncated/disconnected capture prevents PASS. An interrupted or ambiguous write is not automatically submitted again; cleanup is skipped when ownership or outcome is uncertain.

### Screenshots and Jev session statistics

Screenshot flags are `--screenshots` and `--no-screenshots`. With neither, capture
follows the policy. `--no-screenshots` disables it even when permitted.
`--screenshots` requires `"allow_screenshots": true` in the policy's existing
`model_disclosure` object; denied capture stops preflight rather than silently
ignoring the request or expanding permissions. Policy defaults remain deny-first.

```bash
# Visible Chrome; explicitly request policy-approved screenshots
uv run jev-qa run --scenario examples/create.json --policy examples/policy.json --screenshots

# Hidden Chrome; no screenshots, even if policy permits them
uv run jev-qa run --scenario examples/create.json --policy examples/policy.json --headless --no-screenshots
```

Customize capture frequency using `--screenshot-mode`:

| Mode | Captured states |
| --- | --- |
| `all` (default) | Initial page, post-action states, around reloads, and step outcomes; adjacent redundant terminal captures may be omitted |
| `actions` | After each settled action only |
| `steps` | Final state of each step, including cleanup |
| `failures` | Failed, blocked, or errored step outcomes only |

`--screenshot-every N` captures every Nth settled action across the entire run,
including cleanup, starting at N. It implies `actions` mode and does not reset at
step boundaries. N must be positive; time-based capture is not supported.
Mode/frequency flags request capture and require policy permission. Combining
them with `--no-screenshots`, or combining a cadence with a non-actions mode, is
an error. A preflight failure has no page to capture; deadlines still apply.

Examples: append `--screenshot-mode failures` for failure-only evidence, or
`--screenshot-every 3` for every third action. Claude/Codex can select these flags
from the prompt using the installed skill. Report metadata records the selected
mode and interval.

Each run saves private PNG files beside its report at
`<report-directory>/screenshots/<run_id>/<counter>-<phase>.png`.
The report's `screenshots` array records the path, phase, step, action
index, and capture timestamp. Capture obeys the page-origin/capture policy and
scenario deadline. Failures are listed in `findings.missing_evidence`; they do not
replace the actual assertion verdict.

Screenshots are **unredacted viewport pixels**, kept locally and never sent to Jev.
JSON redaction does not mask images. Use synthetic data and review images before
sharing. Private files use mode 0600; symlinked destinations and overwrites are
refused. Secure image storage currently requires POSIX directory-descriptor support
(macOS/Linux); unsupported platforms report capture unavailable. These images are
human-review evidence, not automated visual correctness assertions.

At completion, the CLI prints a readable summary to **stderr**, preserving JSON-only
stdout. The same counters are stored in `session_stats`: elapsed session time,
attempted browser actions, passed/failed assertions, screenshot count, actual Jev
HTTP attempts and failed calls, cumulative request time, input/output/total tokens,
and estimated USD cost (plus provider-reported cost when available). Cleanup calls count too; a DONE
decision still counts as a model call even though it dispatches no browser action.

Cost estimates use the supplied Jev rates: **$0.042 per million input tokens,
$0 per million output tokens**. `session_stats.jev.estimated_cost_usd` equals
`input_tokens × 0.042 / 1,000,000`; the pricing basis is stored alongside it.
This is an estimate, not an invoice, account balance, or independently verified
price for a different `--model`.

[Jev's documented usage response](https://docs.typesafe.ai/api.md) provides
`input_tokens` and `output_tokens`, not a USD charge. `cost_usd` remains the separate
provider-reported amount and is null unless every attempt reports `usage.cost_usd`.
Missing/invalid usage on any attempt leaves the estimate unavailable rather than
showing a partial bill. Statistics cover this runner's Jev calls, not host-agent usage.

Live verification: the healthy Todo create/cleanup contract produced PASS with
10 PNGs, 3 Jev calls, and 6,815 tokens; the fake-success contract produced FAIL
with 6 PNGs, 2 calls, and 4,995 tokens. Neither returned USD cost.

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
uv build
```

The live CLI acceptance matrix used Jev `1.13.0`, confidence threshold `0.55`, a 1280×900 owned viewport, fresh synthetic healthy state, and separately namespaced fault databases:

| Scenario | Observed verdict |
| --- | --- |
| Healthy create, fresh persistence, owned cleanup | PASS |
| Create → edit → complete → uncomplete → delete | PASS |
| Blank and whitespace UI rejection, no POST, no invalid persisted record | PASS |
| Same create contract against fake-success | FAIL: captured HTTP 503 contradicts expected 201 |
| Same create contract against incorrect-payload | FAIL: submitted payload and response contradict the fixture |
| Same create contract against lost-save | FAIL: independently fetched persistence lacks the record |
| Goal-only exploration | COMPLETE, not contract PASS |
| Lifecycle with an unrelated sentinel record | PASS; sentinel unchanged |
| Headed and headless creation | PASS |
| Explicit attachment to a synthetic profile | PASS; unrelated tab URL/title/body unchanged and no QA tab left behind |
| One-action budget | BLOCKED before submission |
| Committed POST with delayed response | BLOCKED; exactly one backend write, no automatic resubmission |

Additional browser checks cover unsupported frames, no-progress stops, stale targets, and incomplete capture. Synthetic regressions cover policy/disclosure boundaries, preflight secret redaction, isolated daemon imports, and whole-request timeout cancellation. Earlier deterministic-provider browser checks established transport/assertion behavior separately; they are not counted as live Jev proof.

The portable skill was also checked with four authoring prompts, each with and without the skill: catalog search, client-side validation, unsupported CSV downloads, and a denied production/personal-profile request. Both configurations met the checked expectations; one sample per case does not establish a quality or speed advantage. The search evaluation explicitly retained the gap between aggregate container text and proving text in every card. See [evaluation prompts](evals/jevqa/evals.json).

Installed-wheel smoke checks exercised initialization, both host installation paths, schema export, contract import, and validation in a fresh consumer directory. Real Jev runs passed the authored search DOM subset and promotion-rejection DOM/no-request contracts on separate synthetic, non-Todo pages. These are not pixel-visibility or per-card-universality claims. Native Claude Code/Codex auto-discovery and automatic skill triggering were not executed; host paths follow their published documentation.

These are observed acceptance results, not a guarantee that every future model run completes. Uncertainty remains BLOCKED rather than weakening the threshold or substituting scripted decisions. Native select interactions, embedded browsing contexts, and popup workflows are unsupported and fail closed. Raw local reports, profiles, databases, and credentials remain gitignored.

## Attribution

Adapted upstream MIT notices are preserved in [THIRD_PARTY_NOTICES.md](src/jev_frontend_qa/THIRD_PARTY_NOTICES.md). Current protocol references: [HTTP API](https://docs.typesafe.ai/api), [Choice](https://docs.typesafe.ai/primitives/choice), and [models](https://docs.typesafe.ai/models).
