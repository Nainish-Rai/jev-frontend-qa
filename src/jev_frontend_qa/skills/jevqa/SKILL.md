---
name: jevqa
description: >
  Turns a feature's acceptance criteria into authored browser QA contracts and
  executes them with the jev-qa CLI. Use after implementing a user-visible feature,
  when asked to create user journeys or browser-test a change, or when investigating
  a Jev QA report. Covers UI-only and networked features; identifies unsupported
  required coverage instead of claiming a partial test verifies the whole feature.
compatibility: Requires jev-qa, Python 3.12+, and Chrome/Chromium. Browser runs require a privately configured TYPESAFE_API_KEY and an explicitly approved project policy.
---

# Jev QA

The coding agent authors expected behavior; Jev chooses observed browser actions;
the runner evaluates deterministic assertions. Use the existing CLI, not a second
browser driver or a free-form request for Jev to decide whether the feature works.

## Prepare

1. Find the application root, requirement/acceptance criteria, launch instructions,
   and existing `jevqa/` contracts. Read implementation only to locate integration
   details (selectors, field names, routes); expected behavior comes from the
   requirement. Resolve materially missing expectations with the user.
2. Check `jev-qa --help`. If unavailable, follow the package installation instructions
   rather than inventing commands. `jev-qa init --project .` creates a deny-default
   policy and scenario directory while preserving existing policy.
3. Read [safe-operation.md](references/safe-operation.md) before execution. Confirm
   explicit approval for origins, operations, model disclosure, and identity.
   Installation and a feature request alone do not grant these permissions.

## Author the coverage

4. List each requested acceptance criterion, its minimal user journey, and a
   supported observable check or a named blocker. Choose relevant happy/error/
   persistence paths; do not impose CRUD, API calls, or a database on every feature.
5. Read [scenario-authoring.md](references/scenario-authoring.md), then inspect
   `jev-qa schema scenario` and `jev-qa schema policy` for the installed contracts.
   Write complete JSON with a `name`, actual entry URL, exact synthetic fixtures,
   known selectors, and assertions on every contract step. Omit `run_id` for fresh
   identities. A goal is an action instruction, not a substitute for assertions.
6. Register the authored JSON, without generating routes or empty templates:

   ```bash
   jev-qa new-scenario --project . --feature FEATURE --journey JOURNEY --from authored.json
   jev-qa validate --scenario jevqa/scenarios/FEATURE/JOURNEY.json --policy jevqa/policy.json
   ```

   Substitute real feature/journey slugs and file paths. Existing scenarios may be
   edited deliberately; the importer refuses replacement. Preserve the requirement
   when correcting integration details. Never weaken assertions to obtain PASS.

## Execute and report

7. Start the approved local app using its documented command. Use the host agent's
   supervised process mechanism; retain ownership of processes you start. Then run:

   ```bash
   jev-qa run --scenario jevqa/scenarios/FEATURE/JOURNEY.json --policy jevqa/policy.json --headless --work-dir artifacts/jevqa/work --report artifacts/jevqa/FEATURE-JOURNEY.json
   ```

   Use `--headed` for a requested demonstration. Keep the key out of arguments and
   prompts. Stop only processes you started when finished.
8. Read the JSON verdict, assertions, evidence, findings, and cleanup notes. Report
   executed coverage separately from unexecuted or unsupported requirements, with
   report paths and concrete failures/blockers. Exit 0 alone does not mean PASS:
   exploratory `complete` certifies no contract.
9. Required downloads/file contents, uploads, URL transitions, visual grading,
   native selects, frames, or popups are currently unsupported. Report these as
   BLOCKED in your coverage summary. Do not fabricate runner JSON, lower the
   requirement, or change to exploratory mode to disguise missing contract coverage.
   A supported-subset PASS is never a full-feature PASS.
10. Diagnose failures before a bounded rerun. Keep expected behavior unchanged and
    use fresh run identity after an understood fix. An ambiguous write stops the
    workflow: retain evidence and get caller direction before any rerun or cleanup.
