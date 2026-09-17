# Jev Frontend QA design

## Accepted product decisions

- The primary caller is a coding agent, through a CLI that can also be invoked manually. A dedicated dashboard is not part of the initial build.
- Support both contract-driven verification and exploratory investigation, with distinct correctness claims. See ADR 0001.
- Use explicit per-project environment and side-effect permissions. See ADR 0002.
- Validate the tester against a separate, synthetic-data-only todo demo application with working flows and deliberately broken variants. This replaces the previously proposed real-application acceptance target.
- The reference scenarios must expose a fake success notification despite API failure, an incorrect submitted payload, and a save that does not survive a fresh read. Agent blockage or lost evidence must not produce a pass.
- The calling coding agent owns planning, exact fixture values, and assertion authoring. The CLI executes scenarios, collects evidence, and verifies supplied expectations; it does not independently infer requirements from application code.
- Browser execution defaults to a dedicated QA profile. Existing-profile access is an explicit opt-in, never an implicit copy of personal cookies.
- Project policy controls what content may be sent to hosted models, denying unapproved content by default. The demo uses synthetic data only.
- Uncertain or unsupported browser interactions return a structured BLOCKED result to the caller; no automatic stronger-model escalation.
- No proprietary application code, data, screenshots, workflows, or integration is in scope. Do not inspect or use proprietary projects for this demo.

## Approved implementation direction

- A Python CLI accepts a JSON scenario and a separately configured project policy, and emits machine-readable results plus local evidence.
- Reuse the upstream Jev indexed-control and guarded-action approach, with exact fixture values supplied by the caller rather than generated per field.
- Retain Browser Harness as the transport, as explicitly selected by the user. Add reliable session-scoped network evidence capture and browser isolation; do not substitute Playwright.
- Keep the todo demo a separately runnable application within this repository, with its own API and SQLite persistence. Use plain HTML/CSS/JavaScript rather than a frontend framework.
- Exclude authentication, external integrations, and real user data from the demo.
- The todo demo supports create, edit, complete/uncomplete, delete, blank-title rejection, and persistence across reload through its API and SQLite.
- Contract outcomes are PASS, FAIL, BLOCKED, or ERROR. Exploratory completion reports findings without a contract PASS.
- Project policy authorizes origins, API operations, model disclosure, and screenshot capture. These controls are guardrails, not a sandbox for arbitrary backend side effects.
- Evidence stays local: JSON report, relevant redacted requests/responses, action history, and policy-permitted screenshots. Exclude evidence from Git and do not upload automatically.
- Default limits are 60 actions and 120 seconds per scenario, configurable by the caller. Do not automatically resubmit after an ambiguous write.
- Support a visible browser for demonstrations and headless execution.
- Jev is the only model inside the runner; fixture values are exact caller inputs.

## Development workflow

Use Matt Pocock's engineering skills. The design interview and six-ticket vertical breakdown are approved. Publish tickets to GitHub Issues in `Nainish-Rai/jev-frontend-qa`, encode their blocking dependencies, then implement unblocked tickets with behavioral verification and standards/spec review. Tracker and domain-document conventions are under `docs/agents/`.

## TypeSafe integration guidance

The project-local TypeSafe skill is installed at `.agents/skills/typesafe-ai/SKILL.md` through the skills CLI. Use it for the Jev integration and consult the live API and relevant primitive documentation before implementation.

Keep exact fixture values, permissions, assertions, and execution in code. Batch independent operation/target questions against the same observation, consume only the chosen branch, and validate freshness before acting. Include an explicit no-match outcome; confidence is not correctness or permission. Evaluate thresholds on representative demo scenarios rather than treating cookbook examples as universal defaults.
