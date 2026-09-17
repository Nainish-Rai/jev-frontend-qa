# Issue tracker: GitHub

Use GitHub Issues in `Nainish-Rai/jev-frontend-qa` through `gh`. Specifications and tickets belong in GitHub, not local ticket files.

- Read the issue body and comments before implementation.
- Create complete vertical tickets with acceptance criteria and explicit blockers.
- Apply `ready-for-agent` to fully specified tickets. This label does not imply their blockers are resolved.
- Encode blocking edges using GitHub native issue dependencies, and retain a `Blocked by` section in each issue body. Use the blocker's numeric database ID for the dependency API.
- Work only issues whose blockers are closed. Claim work by assigning the issue to yourself.
- Close an issue only after its acceptance criteria are verified; include commands, results, and relevant artifact references without exposing secrets.
- PRs as a request surface: no.
