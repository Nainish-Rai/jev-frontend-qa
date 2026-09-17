# Development instructions

Use Matt Pocock's engineering skills for design, tickets, implementation, and review. Implement complete vertical slices from the approved issues; prove behavior through the real browser and API before closing a ticket.

Use the `clean-code` skill during implementation and review. Favor domain-aligned names, focused functions, explicit side effects, consistent error handling, and tests of observable behavior. Keep abstraction proportional to actual complexity; avoid mechanical function splitting and tests that merely mirror implementation.

Retain Browser Harness as the transport and preserve upstream MIT notices when adapting Jev Ultrafast. Use only synthetic demo data. Keep proprietary applications, credentials, browser profiles, and captured evidence out of this repository.

Before implementing or changing Jev calls, read `.agents/skills/typesafe-ai/SKILL.md` and the relevant live TypeSafe API/primitive documentation. Code owns permissions, exact fixture values, and assertions; model confidence does not certify correctness.

## Agent skills

### Issue tracker

Use GitHub Issues in `Nainish-Rai/jev-frontend-qa`, with native blocking dependencies. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the standard five triage roles. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context glossary and ADR layout. See `docs/agents/domain.md`.
