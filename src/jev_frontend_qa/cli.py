"""``jev-qa`` command-line interface.

The CLI exposes QA execution, exploration, validation, and setup commands:

* ``run`` - load a scenario + policy, run the QA loop, write a report, and
  exit with the documented code (0 PASS, 1 FAIL, 2 BLOCKED, 3 ERROR).
* ``explore`` - pursue a browser goal with a policy, or explicitly select
  ``--goal-only`` for unrestricted HTTP(S) exploration without a policy file.
* ``validate`` - check the scenario and policy without launching the
  browser; useful in CI before a model key is available.
* ``help`` - show the per-subcommand help and exit.

The CLI never installs a fake model mode. The verdict reflects the actual
provider (or BLOCKED/ERROR when the key is missing). Project policy or
explicit goal-only authorization is applied before browser or model calls.
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
from .core.models import (
    IdentityPolicy,
    JevUsage,
    ModelDisclosurePolicy,
    Policy,
    RedactionPolicy,
    Report,
    RunLimits,
    Scenario,
    SessionStats,
    Step,
    StepResult,
)
from .core.planner import HostPlanner, PlannerError
from .core.policy import PolicyEnforcer
from .core.redaction import fixture_secrets, redact_report
from .onboarding import add_commands

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
            "Evidence-driven frontend QA and browser exploration through Jev "
            "and Browser Harness. Run authored contracts with a project policy, "
            "or explore with a policy or explicit --goal-only authorization."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)

    run = argparse.ArgumentParser(add_help=False)
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
    capture = run.add_mutually_exclusive_group()
    capture.add_argument(
        "--screenshots",
        dest="screenshots",
        action="store_true",
        default=None,
        help="Save app-state PNGs; requires policy permission unless --goal-only (default: follow policy)",
    )
    capture.add_argument(
        "--no-screenshots",
        dest="screenshots",
        action="store_false",
        help="Disable screenshot capture even when policy permits it",
    )
    run.add_argument(
        "--screenshot-mode",
        choices=("all", "actions", "steps", "failures"),
        help="Capture lifecycle states (all), settled actions, step outcomes, or failed/blocked/error outcomes",
    )
    run.add_argument(
        "--screenshot-every",
        type=int,
        metavar="N",
        help="Capture every Nth settled action across the run, including cleanup; implies actions mode",
    )
    run.add_argument(
        "--attach-profile", default=None, help="Explicit profile name; requires policy approval unless --goal-only"
    )
    run.add_argument("--cdp-url", default=None, help="Explicit DevTools endpoint; requires --attach-profile")
    run.add_argument("--model", type=str, default=None, help="Override the TypeSafe model id (default jev-1.13.0)")
    run.add_argument(
        "--bu-name", type=str, default=None, help="Prefix for the unique per-run Browser Harness namespace"
    )
    run.add_argument(
        "--confidence-threshold",
        type=_parse_confidence,
        default=0.55,
        help="Minimum decision confidence (default 0.55); disabled in --goal-only mode",
    )
    contract = sub.add_parser("run", parents=[run], help="Run an authored scenario and write a report.")
    contract.add_argument("--scenario", type=Path, required=True, help="Path to scenario JSON")
    contract.add_argument("--policy", type=Path, required=True, help="Path to project policy JSON")
    explore = sub.add_parser(
        "explore", parents=[run], help="Explore a URL using a policy or explicit --goal-only authorization."
    )
    authorization = explore.add_mutually_exclusive_group(required=True)
    authorization.add_argument("--policy", type=Path, help="Path to project policy JSON")
    authorization.add_argument(
        "--goal-only",
        action="store_true",
        help=(
            "Explore without a policy file: allow HTTP(S) origins and methods, page/history/planner disclosure, "
            "and planner-authored inputs; disable confidence gating. Screenshots remain opt-in."
        ),
    )
    explore.add_argument("--url", required=True, help="Entry URL; must be authorized by policy unless --goal-only")
    explore.add_argument("--goal", required=True, help="Exploration objective, not a correctness assertion")
    explore.add_argument("--planner", choices=("claude", "codex"), default="claude")
    explore.add_argument("--planner-model", help="Optional host planner model override")
    explore.add_argument("--fixtures", type=Path, help="JSON object of exact caller-owned synthetic field values")
    explore.add_argument("--max-turns", type=int, default=24)
    explore.add_argument("--max-actions", type=int, default=60)
    explore.add_argument("--max-seconds", type=float, default=180.0)

    validate = sub.add_parser("validate", help="Validate the scenario and policy JSON without running the browser.")
    validate.add_argument("--scenario", type=Path, required=True)
    validate.add_argument("--policy", type=Path, required=True)

    add_commands(sub)

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
    goal_only = getattr(args, "goal_only", False)
    scenario = None
    policy = None
    transport = None
    client = None
    planner = None
    started = int(time.time() * 1000)
    session_started = time.monotonic()
    try:
        if goal_only and args.command != "explore":
            raise PolicyBlocked("Goal-only mode is only available through explore --goal-only")
        config = load_config(
            Path.cwd(),
            cli_overrides={
                "chrome_executable": args.chrome_executable,
                "headless": args.headless,
                "work_dir": args.work_dir,
                "bu_name": args.bu_name,
            },
        )
        if args.command == "explore":
            fixtures = json.loads(args.fixtures.read_text()) if args.fixtures else {}
            scenario = Scenario(
                name="website-exploration",
                mode="exploratory",
                start_url=args.url,
                steps=(Step(id="explore", goal=args.goal, fixtures=fixtures),),
                limits=RunLimits(max_actions=args.max_actions, max_seconds=args.max_seconds),
            )
        else:
            scenario = load_scenario(args.scenario)
        capture_requested = (
            args.screenshots is True or args.screenshot_mode is not None or args.screenshot_every is not None
        )
        if goal_only:
            if mode == "attach" and (not args.attach_profile or not args.cdp_url):
                raise PolicyBlocked("Attachment requires both --attach-profile and --cdp-url")
            policy = Policy(
                goal_only=True,
                identity=IdentityPolicy(mode=mode, attach_profile_name=args.attach_profile),
                model_disclosure=ModelDisclosurePolicy(
                    allow_page_text=True,
                    allow_action_history=True,
                    allow_planner=True,
                    allow_request_bodies=False,
                    allow_response_bodies=False,
                    allow_screenshots=capture_requested,
                ),
            )
        else:
            policy = load_policy(args.policy)
            if policy.goal_only:
                raise PolicyBlocked(
                    "Goal-only mode requires explore --goal-only; it cannot be enabled by a policy file"
                )
        mode = validate_identity(policy, args.attach_profile, args.cdp_url)
        if mode == "attach" and (args.headless or args.headed):
            raise PolicyBlocked("Visibility flags cannot change an attached browser; omit them or use isolated mode")
        if capture_requested and not policy.model_disclosure.allow_screenshots:
            raise PolicyBlocked("Screenshot options require model_disclosure.allow_screenshots in project policy")
        enforcer = PolicyEnforcer(policy)
        if not enforcer.check_request("GET", str(scenario.start_url)).allowed:
            raise PolicyBlocked("Start URL is not permitted by project policy")
        if not enforcer.may_disclose("page_text"):
            raise PolicyBlocked("Project policy does not permit page-content model disclosure")
        if args.command == "explore":
            if not policy.model_disclosure.allow_planner:
                raise PolicyBlocked("Exploration requires model_disclosure.allow_planner for host-model disclosure")
            planner = HostPlanner(provider=args.planner, model=args.planner_model, goal_only=args.goal_only)
            planner.ensure_ready()
        api_key = _read_api_key()
        if not api_key:
            raise PolicyBlocked("TYPESAFE_API_KEY is not set; cannot make model calls")
        chrome_path = resolve_chrome_executable(config.chrome_executable) if mode == "isolated" else None
        if mode == "isolated" and chrome_path is None:
            raise RuntimeError("Chrome executable is unavailable")
        deadline = time.monotonic() + scenario.limits.max_seconds
        client = ModelClient(api_key=api_key, model=args.model or config.typesafe_model)
        client.deadline = deadline
        evidence = EvidenceCollector()
        evidence.set_policy(policy)
        transport = make_transport(
            policy=policy,
            evidence=evidence,
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
            confidence_threshold=0.0 if goal_only else args.confidence_threshold,
            screenshot_dir=(
                args.report.parent / "screenshots" / scenario.run_id
                if policy.model_disclosure.allow_screenshots and args.screenshots is not False
                else None
            ),
            screenshot_mode=args.screenshot_mode or ("actions" if args.screenshot_every is not None else "all"),
            screenshot_every=args.screenshot_every if args.screenshot_every is not None else 1,
            planner=planner,
            max_planner_turns=args.max_turns if args.command == "explore" else 24,
        ).run()
        outcome = RunOutcome(
            report=outcome.report.model_copy(update={"execution_mode": mode}), exit_code=outcome.exit_code
        )
    except PolicyBlocked as error:
        outcome = _early_outcome(scenario, mode, started, "blocked", str(error))
    except TimeoutError:
        outcome = _early_outcome(scenario, mode, started, "blocked", "Scenario deadline exceeded")
    except (ModelError, PlannerError):
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
        if planner is not None:
            with contextlib.suppress(Exception):
                planner.close()
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
    stats = SessionStats(
        duration_ms=max(0, int((time.monotonic() - session_started) * 1000)),
        actions=sum(len(step.actions) for step in outcome.report.steps),
        assertions_passed=sum(check.passed for step in outcome.report.steps for check in step.assertions),
        assertions_failed=sum(not check.passed for step in outcome.report.steps for check in step.assertions),
        screenshots_saved=len(outcome.report.screenshots),
        jev=client.usage_summary() if client is not None else JevUsage(),
        planner=planner.usage_summary() if planner is not None else {},
    )
    outcome = RunOutcome(
        report=outcome.report.model_copy(
            update={
                "session_stats": stats,
                "metadata": {**outcome.report.metadata, "policy_mode": "goal_only" if goal_only else "policy"},
            }
        ),
        exit_code=outcome.exit_code,
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
    _print_session_summary(payload)


def _print_session_summary(payload: dict) -> None:
    """Keep stdout machine-readable; show only sanitized report data on stderr."""
    stats = payload["session_stats"]
    usage = stats["jev"]

    def tokens(value: int | None) -> str:
        return f"{value:,}" if value is not None else "unavailable"

    cost = (
        f"${usage['cost_usd']:.8f} USD (provider-reported)"
        if usage["cost_usd"] is not None
        else "unavailable (complete USD cost not reported by provider)"
    )
    estimate = (
        f"${usage['estimated_cost_usd']:.10f} USD "
        f"(input ${usage['input_usd_per_million']:g}/million, output ${usage['output_usd_per_million']:g}/million)"
        if usage["estimated_cost_usd"] is not None
        else "unavailable (token accounting incomplete)"
    )
    print(
        f"\nJev QA session: {payload['verdict'].upper()}\n"
        f"  Duration: {stats['duration_ms'] / 1000:.2f}s | Actions: {stats['actions']}\n"
        f"  Assertions: {stats['assertions_passed']} passed, {stats['assertions_failed']} failed\n"
        f"  Screenshots saved: {stats['screenshots_saved']}\n"
        f"  Jev calls: {usage['requests']} | Failed calls: {usage['failed_requests']}\n"
        f"  Jev request time: {usage['latency_ms'] / 1000:.2f}s\n"
        f"  Tokens: input {tokens(usage['input_tokens'])}, output {tokens(usage['output_tokens'])}, "
        f"total {tokens(usage['total_tokens'])}\n"
        f"  Token accounting: {'complete' if usage['usage_complete'] else 'unavailable or incomplete'}\n"
        f"  Estimated Jev cost: {estimate}\n"
        f"  Provider-reported cost: {cost}",
        file=sys.stderr,
    )
    if payload["screenshots"]:
        print(f"  Screenshot files: {Path(payload['screenshots'][0]['path']).parent}", file=sys.stderr)
    if planner := stats.get("planner"):
        print(
            f"  Host planner: {planner['invocations']} invocations, {planner['failed_invocations']} failed"
            f" | input {tokens(planner['input_tokens'])}, output {tokens(planner['output_tokens'])}"
            " (separate from Jev accounting)",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if handler := getattr(args, "handler", None):
        return handler(args)
    if args.command is None or args.command == "help":
        return cmd_help(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command in {"run", "explore"}:
        if args.command == "explore" and (
            args.max_turns < 1 or args.max_actions < 1 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0
        ):
            parser.error("Exploration turn, action, and time limits must be positive and finite")
        if args.screenshot_every is not None:
            if args.screenshot_every < 1:
                parser.error("--screenshot-every must be a positive integer")
            if args.screenshot_mode not in (None, "actions"):
                parser.error("--screenshot-every can only be used with actions mode")
        if args.screenshots is False and (args.screenshot_mode is not None or args.screenshot_every is not None):
            parser.error("--no-screenshots conflicts with screenshot mode/frequency options")
        return cmd_run(args)
    parser.print_help()
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
