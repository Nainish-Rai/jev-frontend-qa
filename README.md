# Jev Frontend QA

Evidence-driven frontend QA built on [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) and [Browser Harness](https://github.com/browser-use/browser-harness).

**Status:** design approved; implementation tracked in [GitHub Issues](https://github.com/Nainish-Rai/jev-frontend-qa/issues). The runnable CLI and demo are not implemented yet.

## Intended workflow

A coding agent supplies a scenario, exact fixture values, and assertions. Jev selects actions from observed browser controls; Browser Harness executes them. Deterministic checks evaluate captured requests, responses, UI state, and persistence before producing a verdict.

- Contract runs verify explicit expectations; exploratory runs report findings without claiming a contract pass.
- Browser completion and success notifications are not proof of correctness.
- Project policy controls permitted environments, mutations, and disclosure to hosted models.
- Browser execution is isolated by default; existing-profile access requires explicit opt-in.
- Evidence remains local and is excluded from version control.

## Synthetic demo

The separate todo demo will exercise create, edit, complete/uncomplete, delete, validation, and persistence through a real API and SQLite. Deliberately broken variants will prove that the tester detects false success, incorrect payloads, and lost saves. All demo data is synthetic; proprietary applications are out of scope.

## Development

Use Matt Pocock's engineering workflow: approved design → vertical tickets with dependencies → behavior-driven verification → standards/spec review. Start with the glossary in `CONTEXT.md`, the design in `docs/design.md`, and the decisions in `docs/adr/`.

The project-local [TypeSafe skill](.agents/skills/typesafe-ai/SKILL.md) guides the Jev integration. Credentials stay outside source control. Upstream code incorporated during implementation must retain its original MIT notices.
