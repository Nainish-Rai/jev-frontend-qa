# Separate browser execution from correctness

The tester supports contract runs and exploratory runs, but only contract runs can pass explicit expectations grounded in feature requirements. Browser completion, model confidence, success notifications, and the implementation under test are not independent proof of correctness; exploratory runs report findings rather than certify unspecified behavior. This boundary prevents a coding agent and its tester from agreeing on the same implementation mistake and reporting an unsupported pass.
