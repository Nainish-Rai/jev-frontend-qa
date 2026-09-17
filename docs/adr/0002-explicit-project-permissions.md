# Authorize QA side effects through project policy

QA runs use per-project rules defining permitted environments and side effects, defaulting to explicitly approved local or staging test environments rather than unrestricted use of the user's logged-in browser. Real payments, external messages, and unrelated deletion remain blocked unless explicitly permitted; a staging hostname alone does not establish that downstream integrations are sandboxed. The enforcement mechanism and approval interface remain open design decisions.
