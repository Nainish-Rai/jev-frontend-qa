# Jev Frontend QA

Evidence-driven frontend QA built on [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) and [Browser Harness](https://github.com/browser-use/browser-harness).

**Status:** the standalone todo demo is implemented and browser-verified. The QA runner is under active implementation; acceptance is tracked in [GitHub Issues](https://github.com/Nainish-Rai/jev-frontend-qa/issues).

## Intended workflow

A coding agent supplies a scenario, exact fixture values, and assertions. Jev selects actions from observed browser controls; Browser Harness executes them. Deterministic checks evaluate captured requests, responses, UI state, and persistence before producing a verdict.

- Contract runs verify explicit expectations; exploratory runs report findings without claiming a contract pass.
- Browser completion and success notifications are not proof of correctness.
- Project policy controls permitted environments, mutations, and disclosure to hosted models.
- Browser execution is isolated by default; existing-profile access requires explicit opt-in.
- Evidence remains local and is excluded from version control.

## Synthetic demo

The [standalone todo demo](demo/README.md) supports create, edit, complete/uncomplete, delete, validation, and persistence through a real API and SQLite. All demo data is synthetic; proprietary applications are out of scope.

```bash
uv sync
uv run jev-todo --port 8767 --database artifacts/todo.sqlite3
```

Open `http://127.0.0.1:8767/`. Deliberately broken variants are tracked separately to prove that the tester detects false success, incorrect payloads, and lost saves.

## Development

Use Matt Pocock's engineering workflow: approved design → vertical tickets with dependencies → behavior-driven verification → standards/spec review. Start with the glossary in `CONTEXT.md`, the design in `docs/design.md`, and the decisions in `docs/adr/`.

The project-local [TypeSafe skill](.agents/skills/typesafe-ai/SKILL.md) guides the Jev integration. Credentials stay outside source control. Upstream code incorporated during implementation must retain its original MIT notices.
