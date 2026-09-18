# Jev Frontend QA

Evidence-driven frontend QA built on [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) and [Browser Harness](https://github.com/browser-use/browser-harness). Python 3.12+, Chrome/Chromium, plain HTML/CSS/JavaScript demo, and SQLite.

**Status: live Jev acceptance passed for the synthetic reference scenarios.** The real CLI, Jev `1.13.0`, Browser Harness, Chrome, HTTP endpoints, and SQLite were exercised together. Healthy creation, lifecycle, and validation passed; all three deliberate defects failed their authored contracts; exploration completed without claiming a contract PASS. There is no offline-model fallback in the production CLI.

## Current features

- **Real browser QA:** visible Chrome by default, optional headless execution, isolated profiles, and explicit policy-approved attachment.
- **Bounded Jev decisions:** typed operation/target choices over observed controls; exact fixture values and correctness checks remain in code.
- **Contract and exploratory runs:** check DOM state, correlated HTTP requests/responses, absent requests, and fresh persistence reads; exploration never implies a contract PASS.
- **Host-planned exploration:** `explore` lets an authenticated Claude or Codex CLI choose bounded subgoals while Jev remains the browser-action selector.
- **Richer observations:** accessibility names and form/dialog context, unavailable-control explanations, loaded-text pagination/search, keyboard-driven widgets, and nested scrolling.
- **Portable coding-agent skill:** Claude Code or Codex can author feature-specific scenarios and run the same CLI. No Todo-specific workflow is required.
- **Configurable screenshots:** all lifecycle states, every action, step outcomes, failures only, or every Nth action; private local PNGs.
- **Session accounting:** duration, actions, assertions, screenshots, Jev calls/latency/tokens, and estimated cost in the terminal and JSON report.
- **Safety controls:** deny-default policy, redaction, run-owned cleanup, bounded time/actions, and no automatic retry of ambiguous writes.

## How Jev works in this CLI

**Jev is the action selector, not the test author or the correctness judge.** With `run`, Claude/Codex (or a human) supplies a scenario and policy and the CLI executes independently. With `explore`, an explicitly selected, authenticated host CLI plans subgoals during execution; the local runner still authorizes every operation.

```text
Human / Claude / Codex
        |
        v
Scenario + approved policy
        |
        v
Python runner observes Chrome through Browser Harness
        |
        v
Approved, redacted page state + goal + bounded choices
        |
        v
Jev / TypeSafe chooses operation + observed target
        |
        v
Python validates choice, confidence, scope, and permissions
        |
        v
Browser Harness executes input; runner collects evidence
        |
        +---- observe again if more interaction is needed
        |
        v
Deterministic assertions + fresh reads -> verdict + report
```

1. **Load and authorize.** The CLI validates the scenario/policy, checks credentials and disclosure permissions, and starts a private browser session unless explicitly authorized to attach.
2. **Observe.** Local snapshot code reads page text and visible controls, including field values, validation/toggle state, and scoped offscreen scroll hints. An authored scope restricts which record/form Jev can act on.
3. **Ask Jev once per decision.** [`ModelClient`](src/jev_frontend_qa/core/model_client.py) sends one HTTP request to `POST https://api.typesafe.ai/v1/systemone`, using `jev-1.13.0` by default. The request batches an operation question and target questions for available element operations. Executable choices include clicks, exact-fixture text entry, Enter/Escape/ArrowUp/ArrowDown on observed compatible controls, viewport/container scrolling, and waiting, plus `DONE`/`BLOCKED`.
4. **Accept only a valid offered choice.** The response includes typed choices, probability distributions, and confidence. Code validates the answer against the offered choices and uses the selected operation/target confidence—not confidence for unrelated target questions. The default action threshold is `0.55`; low confidence stops the run as BLOCKED.
5. **Execute locally.** [`Runner`](src/jev_frontend_qa/core/agent.py) resolves exact input text from scenario fixtures and dispatches through [Browser Harness](src/jev_frontend_qa/core/browser.py). Jev does not generate selectors, JavaScript, field values, or unrestricted tool calls. After input, the runner waits for network evidence to settle before making another decision.
6. **Verify independently.** Jev's `DONE` means “stop interacting,” not “the test passed.” Authored assertions check the actual DOM and HTTP evidence; persistence assertions perform fresh authorized reads. Conclusive contract evidence can trigger verification without an extra `DONE` call. Only these checks establish a contract verdict.
7. **Clean up and report.** Where safe, the runner drives authored cleanup scoped to verified run-owned records, closes its owned resources, and writes the report, screenshots, and usage totals.

**Example: creating a Todo.** The scenario supplies `qa-run-{{run_id}}`. Jev chooses the title field and then the submit button. Python fills the exact title, checks the actual POST payload and response, captures that response's record ID, checks that specific row, reloads and reads persistence, then deletes only the owned record. A success toast with no saved record fails the contract even if Jev thinks the interaction is finished.

### What leaves the machine?

Jev receives permitted, redacted structured page observations, the current step goal and supplied fixtures, and up to ten recent actions when action-history disclosure is enabled. It receives **text/structured state, not screenshot pixels**. The request does not include captured HTTP request/response bodies; those are local assertion evidence governed by policy and redaction. Do not put real secrets or personal data in test fixtures.

Browser input, assertions, captured evidence, screenshots, and reports stay under local runner control. Screenshots are unredacted and require explicit policy permission. Host-agent usage from Claude/Codex is separate from the Jev usage reported by this CLI.

Planned exploration additionally sends approved, redacted goals, fixtures, page observations, loaded-text reads, and permitted history to the selected host provider. It requires **both** `model_disclosure.allow_page_text` and `model_disclosure.allow_planner`; the latter defaults to false. Host CLIs run in a temporary directory with their tools disabled; Codex also uses its read-only sandbox and ignores project/user configuration. Host authentication and model availability remain external prerequisites. The runner does not silently switch providers.

## Getting started

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/), Chrome/Chromium, and a TypeSafe API key for live runs. Schema, initialization, scenario registration, and validation commands need neither an API key nor a running browser.

Choose one path:

- **Your own app:** install the CLI and optional skill below, start your app, then authorize its origins and author a contract.
- **A ready-to-run example:** use the [synthetic demo walkthrough](#try-the-synthetic-demo-from-source), which includes complete scenarios and a narrowly scoped policy.

### Use from Claude Code or Codex

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

Configure `TYPESAFE_API_KEY` privately in the environment where `jev-qa run` will execute, or in a gitignored `.env` in that command's working directory. Restrict `.env` with `chmod 600 .env`; an existing environment variable takes precedence. Never put the key in a scenario, policy, prompt, or committed file.

Start your application separately. The CLI does not start your app server. In `jevqa/policy.json`, approve only the required origins, HTTP methods/path prefixes, and synthetic observations. Keep the scenario's `start_url` consistent with that policy. An initialized, unchanged policy intentionally blocks a live run.

Then ask your coding agent, for example:

> Use jevqa to verify the feature we just implemented. Derive journeys from the acceptance criteria, identify supported and blocked coverage, author exact fixtures and assertions, and run against the approved local app. Preserve the expected behavior when investigating failures.

In Claude Code you can invoke `/jevqa`; in Codex CLI use `$jevqa` or select it through `/skills`. The workflow is feature-agnostic: no Todo routes, CRUD checklist, or database is required. The coding agent authors the contract; Jev operates the browser; deterministic checks establish the verdict.

To choose capture behavior through the skill, add “screenshots after every action,” “step outcomes only,” “failures only,” or “every third action” to the prompt. The skill maps these to CLI flags; it must not expand policy permission. For example:

> Use jevqa to verify the search acceptance criteria against our approved local app. Run Chrome visibly, capture failures only if policy permits, and report the verdict, coverage gaps, screenshot paths, Jev tokens, and estimated cost.

After updating the CLI with `uv tool upgrade jev-frontend-qa`, refresh any previously installed skill deliberately: the installer refuses overwrites. Preserve local skill edits before replacing the installed `jevqa` directory and rerunning `skill install`.

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

### Explore an approved website

Use exploration when you want to discover a workflow, not certify acceptance criteria. Install and authenticate the selected `claude` or `codex` CLI. Keep `TYPESAFE_API_KEY` configured for Jev. Explicitly approve planner disclosure in your existing policy; retain the narrow origin/method restrictions.

```bash
jev-qa explore --url http://127.0.0.1:3000/ \
  --goal "Inspect the search workflow and identify what remains unverified" \
  --planner codex --policy jevqa/policy.json \
  --max-turns 24 --max-actions 60 --max-seconds 180 \
  --report artifacts/jevqa/exploration.json
```

For required text entry, supply `--fixtures fixtures.json`, a JSON object mapping actual field names to exact synthetic strings. The planner cannot invent fixture values, selectors, assertions, or navigation URLs. Navigation is limited to the caller's entry URL and observed policy-authorized links. `--planner claude` is the default; `--planner-model` selects a host model independently of Jev's `--model`.

The planner can observe, read/search loaded rendered text without scrolling, navigate, request an eight-action subgoal, complete, or block. Reads paginate at 6,000 characters and disclose scan limits; hidden text and editable values are excluded. A search miss is not proof of absence, especially with lazy-loaded or embedded content.

Progress memory survives subgoals and ignores node-ID/geometry churn. Repeated no-change waits, empty scrolling, action cycles, or a subgoal limit return safe checkpoints to the planner; a third checkpoint blocks. Only stale observations detected **before input** can refresh, at most twice before a checkpoint. Denied requests, low-confidence choices, and ambiguous writes stop rather than being replanned or resubmitted.

Chrome accessibility metadata supplies names, scope context, popup options, and active options. Disabled, offscreen, or occluded controls are context, never executable targets. Accessibility failures use an explicitly reported DOM-only observation; native selects, shadow-root controls, frames, and popups remain unsupported. Keyboard input revalidates and focuses the observed element; container scrolling uses its freshly hit-tested position.

The report includes `exploration` events and separate `session_stats.planner` accounting. Host cost is null unless reported by the provider; Jev's pricing estimate is never applied to host tokens. `complete` means the exploration ended, **not PASS or exhaustive coverage**. Use an authored contract for correctness and run-owned cleanup.

### Explore from a goal alone

Explicit `--goal-only` replaces `--policy` for unrestricted website exploration:

```bash
jev-qa explore --goal-only --url https://www.youtube.com/ \
  --goal "Make a playlist of the top 5 Honey Singh songs" \
  --planner codex --report artifacts/jevqa/goal-only.json
```

No policy file, origin/method allowlist, or fixture file is required. The host planner derives text-entry values from the goal and may navigate to new HTTP(S) destinations. Explicit caller fixtures, if supplied, cannot be overwritten. The numeric confidence gate is disabled; incomplete network capture is reported rather than treated as contract failure. This mode can change real account data.

Opting in permits page text and action-history disclosure to Jev and the selected host planner. Request/response bodies are not enabled for model disclosure; screenshots remain opt-in. Existing-profile attachment still requires explicit `--attach-profile NAME --cdp-url ENDPOINT`; otherwise Chrome uses an isolated profile. Sign in yourself when a task requires an authenticated account.

Execution deadlines, observed-target validation, and stopping after uncertain browser-input delivery remain enforced. Embedded content can remain unobserved while the planner operates main-document controls; this does not add frame, popup, shadow-root, or native-select support. Reports identify `metadata.policy_mode` as `goal_only`; COMPLETE is not a contract PASS, proof of persistence, or a guarantee that network effects were fully captured. Authored contracts and policy-based exploration retain their existing checks.

### Try the synthetic demo from source

Clone the repository and install its dependencies:

```bash
git clone https://github.com/Nainish-Rai/jev-frontend-qa.git
cd jev-frontend-qa
uv sync
```

Configure the key in your environment, or prepare a local file **only if `.env` does not already exist**:

```bash
cp -n .env.example .env
chmod 600 .env
```

Edit the file privately to set `TYPESAFE_API_KEY`. Keep it out of version control. Start the synthetic app in this terminal:

```bash
uv run jev-todo --port 8767 --database artifacts/todo.sqlite3
```

Open `http://127.0.0.1:8767/`. The [standalone demo](demo/README.md) supports create, edit, complete/uncomplete, delete, blank-title rejection, and persistence across reload and restart. Everything is synthetic and local; no proprietary applications or data are included.

### Run your first live check

In a second terminal, enter the same repository directory. `uv run` uses this checkout's CLI; the separately installed version uses `jev-qa` directly.

Review `examples/policy.json` first: it explicitly permits synthetic demo content to reach TypeSafe, local request/response evidence, read access to the demo origin, and mutations only under `/api/todos`. Screenshots remain disabled. Policy schema defaults deny disclosure and authorize no origins.

With the demo running in another terminal:

```bash
uv run jev-qa validate --scenario examples/create.json --policy examples/policy.json
uv run jev-qa run --scenario examples/create.json --policy examples/policy.json \
  --headed --report artifacts/create-report.json
```

The browser opens, creates a unique synthetic record, verifies it, and cleans it up. Inspect `artifacts/create-report.json`: expect `verdict: "pass"` on the healthy demo. The terminal summary includes actions, assertions, Jev calls, tokens, and estimated cost. Run identity and model choices can change between executions, so exact counts are not fixed.

Use `examples/lifecycle.json` for the full create/edit/toggle/delete journey, or `examples/validation.json` for rejected-input checks. Stop the demo server with Ctrl-C when finished.

### Command reference

| Command | Purpose |
| --- | --- |
| `jev-qa init --project .` | Create project layout and a deny-default policy without overwriting existing configuration |
| `jev-qa skill install --agent claude --project .` | Install the bundled skill; use `codex` for the other host |
| `jev-qa schema scenario` / `jev-qa schema policy` | Print the installed JSON schemas |
| `jev-qa new-scenario ... --from contract.json` | Validate and register a complete caller-authored contract |
| `jev-qa validate --scenario ... --policy ...` | Check configuration without running Chrome or Jev |
| `jev-qa run --scenario ... --policy ...` | Execute the scenario and produce a report |
| `jev-qa explore --url ... --goal ... --planner codex --policy ...` | Plan bounded exploration using an authenticated host CLI |
| `jev-qa explore --goal-only --url ... --goal ... --planner codex` | Explore without a policy file, with planner-generated text values |
| `jev-qa run --help` | Show browser, model, screenshot, and attachment options |

Validation is a configuration check, not a passing test of the application.

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

After a browser action, a contract step can proceed directly to final verification when authored UI/API evidence is conclusive. The runner still performs independent persistence reads; a successful HTTP response alone cannot bypass a pending UI assertion. This avoids asking the model to grade an already-observed result. Jev selects browser input within subgoals; the exploration planner may additionally select authorized observed-link navigation. Every low-confidence Jev choice remains BLOCKED. Exploratory completion is a model judgment, never a contract verdict. Stopping before any required API interaction was exercised is BLOCKED, not evidence of an application defect.

### Outcomes

Reports use lowercase values and include per-assertion results, redacted local network evidence, action history, execution mode, and separated observations/model judgments.

| Verdict | Exit | Meaning |
| --- | --- | --- |
| `pass` | 0 | Authored contract assertions passed with complete evidence |
| `complete` | 0 | Exploratory goal completed; **not** a contract PASS |
| `fail` | 1 | Captured behavior contradicted an authored assertion |
| `blocked` | 2 | Missing permission/credential, uncertainty, unsupported interaction, limit, or incomplete evidence |
| `error` | 3 | Model/runner/configuration or resource-release failure |

Consumers must inspect `verdict`, not exit 0 alone. JSON is written to stdout and the private local `--report` file. Reports, screenshots, profiles, databases, and `.env` stay local and are excluded from this repository's Git tracking; approved page observations are sent to TypeSafe as described above. Native select controls, embedded browsing contexts, and popup workflows are currently unsupported and stop safely.

Scenario `limits` default to 60 actions and 120 seconds. The action count and execution deadline span all steps; model and browser waits use the remaining deadline. Model requests have a cancellable whole-request timeout, so a trickling response cannot renew the budget. Lost/truncated/disconnected capture prevents PASS. An interrupted or ambiguous write is not automatically submitted again; cleanup is skipped when ownership or outcome is uncertain.

### Screenshots and Jev session statistics

Screenshot flags are `--screenshots` and `--no-screenshots`. With neither, capture
follows the policy. `--no-screenshots` disables it even when permitted.
`--screenshots` requires `"allow_screenshots": true` in the policy's existing
`model_disclosure` object; denied capture stops preflight rather than silently
ignoring the request or expanding permissions. Policy defaults remain deny-first.

The supplied demo policy disables screenshots, so do not use it unchanged with capture flags. To try capture, copy it to `artifacts/screenshots-policy.json` (create `artifacts/` if needed), review the copy, and change **only** `model_disclosure.allow_screenshots` to `true`. Leave the origin/method restrictions intact.

```bash
# Visible Chrome; explicitly request policy-approved screenshots
uv run jev-qa run --scenario examples/create.json --policy artifacts/screenshots-policy.json \
  --screenshot-mode steps --report artifacts/create-with-screenshots.json

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
showing a partial bill. `session_stats.jev` covers Jev calls; `session_stats.planner` separately covers host invocations in `explore`.

Real-browser capture-frequency checks on the healthy create/cleanup contract saved 2 images in `steps` mode, 3 in `actions` mode, 1 with `--screenshot-every 3`, and 0 in `failures` mode. The deliberately broken fake-success run saved 1 failure image. These are observed runs, not fixed counts for arbitrary scenarios.

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
