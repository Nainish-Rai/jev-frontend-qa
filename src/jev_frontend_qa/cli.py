"""``jev-qa`` command-line interface.

The CLI exposes three subcommands:

* ``run`` - load a scenario + policy, run the QA loop, write a report, and
  exit with the documented code (0 PASS, 1 FAIL, 2 BLOCKED, 3 ERROR).
* ``validate`` - check the scenario and policy without launching the
  browser; useful in CI before a model key is available.
* ``help`` - show the per-subcommand help and exit.

The CLI never installs a fake model mode. The verdict reflects the actual
provider (or BLOCKED/ERROR when the key is missing). Project policy is
applied before any browser or model call.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .core.agent import Runner, RunOutcome
from .core.browser import PolicyBlocked, make_transport, validate_identity
from .core.config import load_config
from .core.decisions import TypeSafeDecisionProvider
from .core.evidence import EvidenceCollector
from .core.model_client import ModelClient, ModelError
from .core.models import Policy, RedactionPolicy, Report, Scenario, StepResult
from .core.policy import PolicyEnforcer
from .core.redaction import fixture_secrets, redact_report

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_ERROR = 3


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def _parse_confidence(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("Confidence threshold must be finite and between 0 and 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-qa",
        description=(
            "Evidence-driven frontend QA. The CLI loads a contract scenario "
            "and a project policy, drives a real browser through Jev and "
            "Browser Harness, and reports PASS/FAIL/BLOCKED/ERROR."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)

    run = sub.add_parser("run", help="Run a scenario and write a report.")
    run.add_argument("--scenario", type=Path, required=True, help="Path to scenario JSON")
    run.add_argument("--policy", type=Path, required=True, help="Path to project policy JSON")
    run.add_argument(
        "--report", type=Path, default=Path("artifacts/report.json"), help="Where to write the JSON report"
    )
    run.add_argument(
        "--work-dir",
        type=Path,
        default=Path("artifacts/work"),
        help="Working directory for browser profile, runtime, and tmp dirs",
    )
    run.add_argument(
        "--chrome-executable",
        type=str,
        default=None,
        help="Path to the Chrome executable (defaults to JEV_QA_CHROME_EXECUTABLE then autodetect)",
    )
    visibility = run.add_mutually_exclusive_group()
    visibility.add_argument("--headless", action="store_true", help="Run Chrome headless (default: headed)")
    visibility.add_argument("--headed", action="store_true", help="Force headed Chrome")
    run.add_argument("--attach-profile", default=None, help="Explicit policy-approved synthetic profile name")
    run.add_argument("--cdp-url", default=None, help="Explicit DevTools endpoint for the approved profile")
    run.add_argument("--model", type=str, default=None, help="Override the TypeSafe model id (default jev-1.13.0)")
    run.add_argument(
        "--bu-name", type=str, default=None, help="Prefix for the unique per-run Browser Harness namespace"
    )
    run.add_argument(
        "--confidence-threshold",
        type=_parse_confidence,
        default=0.55,
        help="Minimum confidence required to act on a decision (default 0.55)",
    )

    validate = sub.add_parser("validate", help="Validate the scenario and policy JSON without running the browser.")
    validate.add_argument("--scenario", type=Path, required=True)
    validate.add_argument("--policy", type=Path, required=True)

    sub.add_parser("help", help="Print the help for the available subcommands.")

    return parser


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_validate(args: argparse.Namespace) -> int:
    try:
        load_scenario(args.scenario)
        load_policy(args.policy)
    except (OSError, ValueError):
        print("validate: invalid or unreadable scenario/policy JSON", file=sys.stderr)
        return EXIT_ERROR
    print("validate: scenario and policy accepted")
    return EXIT_PASS


def cmd_run(args: argparse.Namespace) -> int:
    mode = "attach" if args.attach_profile is not None or args.cdp_url is not None else "isolated"
    scenario = None
    policy = None
    transport = None
    client = None
    started = int(time.time() * 1000)
    try:
        config = load_config(
            Path.cwd(),
            cli_overrides={
                "chrome_executable": args.chrome_executable,
                "headless": _resolve_headless(args),
                "work_dir": args.work_dir,
                "bu_name": args.bu_name,
            },
        )
        scenario = load_scenario(args.scenario)
        policy = load_policy(args.policy)
        mode = validate_identity(policy, args.attach_profile, args.cdp_url)
        enforcer = PolicyEnforcer(policy)
        if not enforcer.check_request("GET", str(scenario.start_url)).allowed:
            raise PolicyBlocked("Start URL is not permitted by project policy")
        if not enforcer.may_disclose("page_text"):
            raise PolicyBlocked("Project policy does not permit page-content model disclosure")
        api_key = _read_api_key()
        if not api_key:
            raise PolicyBlocked("TYPESAFE_API_KEY is not set; cannot make model calls")
        chrome_path = resolve_chrome_executable(config.chrome_executable) if mode == "isolated" else None
        if mode == "isolated" and chrome_path is None:
            raise RuntimeError("Chrome executable is unavailable")
        deadline = time.monotonic() + scenario.limits.max_seconds
        client = ModelClient(api_key=api_key, model=args.model or config.typesafe_model)
        client.deadline = deadline
        transport = make_transport(
            policy=policy,
            evidence=EvidenceCollector(),
            workdir=config.work_dir.expanduser().resolve(),
            chrome_executable=chrome_path,
            headless=config.chrome_headless,
            bu_name=config.bu_name,
            attach_profile=args.attach_profile,
            cdp_url=args.cdp_url,
            deadline=deadline,
        )
        transport.start()
        provider = TypeSafeDecisionProvider(name="typesafe", client=client)
        outcome = Runner(
            transport=transport,
            provider=provider,
            policy=policy,
            scenario=scenario,
            confidence_threshold=args.confidence_threshold,
        ).run()
        outcome = RunOutcome(
            report=outcome.report.model_copy(update={"execution_mode": mode}), exit_code=outcome.exit_code
        )
    except PolicyBlocked as error:
        outcome = _early_outcome(scenario, mode, started, "blocked", str(error))
    except TimeoutError:
        outcome = _early_outcome(scenario, mode, started, "blocked", "Scenario deadline exceeded")
    except ModelError:
        outcome = _early_outcome(scenario, mode, started, "blocked", "Hosted model is unavailable")
    except (OSError, RuntimeError, ValueError, TypeError, LookupError):
        # Validation/CDP/provider exceptions can contain fixture values, endpoint
        # credentials or page content. Never serialize their raw messages.
        outcome = _early_outcome(
            scenario, mode, started, "error", "Run failed; check scenario, policy and browser configuration"
        )
    finally:
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
    cleanup_notes = getattr(transport, "cleanup_notes", ())
    if cleanup_notes:
        outcome = RunOutcome(
            report=outcome.report.model_copy(
                update={
                    "verdict": "error",
                    "cleanup_notes": outcome.report.cleanup_notes + cleanup_notes,
                }
            ),
            exit_code=EXIT_ERROR,
        )
    try:
        _emit_outcome(args, outcome, scenario=scenario, policy=policy)
    except OSError:
        print(
            json.dumps(
                {
                    "verdict": "error",
                    "exit": EXIT_ERROR,
                    "execution_mode": mode,
                    "error": "Could not write the local report",
                }
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR
    return outcome.exit_code


def cmd_help(_args: argparse.Namespace) -> int:
    print(build_parser().format_help())
    return EXIT_PASS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_headless(args: argparse.Namespace) -> bool:
    if args.headless:
        return True
    if args.headed:
        return False
    return bool(load_config(Path.cwd()).chrome_headless)


def load_scenario(path: Path) -> Scenario:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Scenario.model_validate(payload)


def load_policy(path: Path) -> Policy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Policy.model_validate(payload)


def resolve_chrome_executable(override: str | None) -> str | None:
    if override:
        return override
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def _read_api_key() -> str | None:
    import os

    return os.environ.get("TYPESAFE_API_KEY")


def _early_outcome(scenario: Scenario | None, mode: str, started: int, verdict: str, note: str) -> RunOutcome:
    report = Report(
        scenario_name=scenario.name if scenario else "unloaded",
        run_id=scenario.run_id if scenario else "unloaded",
        execution_mode=mode,
        verdict=verdict,
        model_provider="typesafe",
        model_used=None,
        started_at_ms=started,
        completed_at_ms=int(time.time() * 1000),
        steps=(StepResult(id="preflight", status=verdict, actions=(), evidence=(), note=note),),
        note=note,
    )
    return RunOutcome(report=report, exit_code=EXIT_BLOCKED if verdict == "blocked" else EXIT_ERROR)


def _emit_outcome(
    args: argparse.Namespace, outcome: RunOutcome, *, scenario: Scenario | None, policy: Policy | None
) -> None:
    redaction = policy.redaction if policy else RedactionPolicy()
    secrets = {_read_api_key() or ""}
    if scenario:
        for step in (*scenario.steps, *scenario.cleanup):
            secrets.update(fixture_secrets(step.fixtures, redaction))
    payload = redact_report(outcome.report.model_dump(mode="json"), redaction, secrets=secrets)
    payload["exit"] = outcome.exit_code
    import os

    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None or args.command == "help":
        return cmd_help(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "run":
        return cmd_run(args)
    parser.print_help()
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
