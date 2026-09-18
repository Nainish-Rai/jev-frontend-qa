"""Bounded, evidence-driven Browser Harness execution.

Adapted from jev-ultrafast (MIT; see THIRD_PARTY_NOTICES.md). The model
selects observed controls, never fixtures, permissions, or correctness.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .assertions import AssertionOutcome, capture_network_variables, evaluate_step, matching_exchanges
from .browser import BrowserTransport
from .decisions import DecisionProvider, model_name_from_provider, provider_label
from .evidence import EvidenceRecord
from .model_client import ModelError
from .models import (
    ActionRecord,
    Assertion,
    AssertionResult,
    Policy,
    Report,
    Scenario,
    ScreenshotRecord,
    Step,
    StepResult,
)
from .policy import PolicyEnforcer
from .redaction import fixture_secrets, redact_report
from .snapshot import PageState, read_snapshot

INTERPOLATION_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RunBlocked(Exception):
    """The next action cannot be performed safely."""


@dataclass
class RunOutcome:
    report: Report
    exit_code: int


@dataclass
class _StepContext:
    step: Step
    fixtures: dict[str, str]
    goal: str
    variables: dict[str, str]
    actions: list[ActionRecord] = field(default_factory=list)
    evidence_records: list[EvidenceRecord] = field(default_factory=list)
    outcomes: list[AssertionOutcome] = field(default_factory=list)
    evaluated_assertions: set[int] = field(default_factory=set)
    fresh_payloads: dict[str, Any] = field(default_factory=dict)
    evidence_problem: str | None = None


@dataclass
class Runner:
    transport: BrowserTransport
    provider: DecisionProvider
    policy: Policy
    scenario: Scenario
    confidence_threshold: float = 0.55
    started_at_ms: int = 0
    screenshot_dir: Path | None = None
    screenshot_mode: str = "all"
    screenshot_every: int = 1

    def __post_init__(self) -> None:
        if not math.isfinite(self.confidence_threshold) or not 0 <= self.confidence_threshold <= 1:
            raise ValueError("Confidence threshold must be finite and between 0 and 1")
        if self.screenshot_mode not in {"all", "actions", "steps", "failures"}:
            raise ValueError("Unknown screenshot mode")
        if type(self.screenshot_every) is not int or self.screenshot_every < 1:
            raise ValueError("Screenshot interval must be a positive integer")
        if self.screenshot_every != 1 and self.screenshot_mode != "actions":
            raise ValueError("Screenshot intervals require actions mode")

    def run(self) -> RunOutcome:
        self.started_at_ms = int(time.time() * 1000)
        self._actions_taken_total = 0
        self._settled_actions_total = 0
        self._evidence_lost_total = 0
        self._unsafe_evidence = False
        self._variables = {"run_id": self.scenario.run_id}
        self._owned_captures: set[str] = set()
        self._metadata: dict[str, str] = {
            "screenshot_mode": self.screenshot_mode if self.screenshot_dir is not None else "disabled",
            "screenshot_every": str(self.screenshot_every),
        }
        self._screenshots: list[ScreenshotRecord] = []
        self._capture_attempts = 0
        self._secrets = {
            value
            for step in (*self.scenario.steps, *self.scenario.cleanup)
            for value in fixture_secrets(step.fixtures, self.policy.redaction)
        }
        api_key = getattr(getattr(self.provider, "client", None), "api_key", None)
        if api_key:
            self._secrets.add(api_key)
        self._findings: dict[str, list[str]] = {
            "observed_facts": [],
            "suspected_defects": [],
            "missing_evidence": [],
            "model_judgments": [],
        }
        self._enforcer = PolicyEnforcer(self.policy)
        deadline = time.monotonic() + self.scenario.limits.max_seconds
        existing_deadline = getattr(self.transport, "deadline", None)
        self._scenario_deadline = min(deadline, existing_deadline) if existing_deadline is not None else deadline
        self.transport.deadline = self._scenario_deadline
        results: list[StepResult] = []
        cleanup_notes: list[str] = []
        verdict = "complete" if self.scenario.mode == "exploratory" else "pass"
        note = None
        active: _StepContext | None = None
        try:
            self._require_observation_policy()
            self._require_request("GET", str(self.scenario.start_url))
            self._remaining()
            self.transport.navigate(str(self.scenario.start_url))
            self._remaining()
            self._capture_state("initial")
            for step in self.scenario.steps:
                active = self._begin_step(step)
                result = self._drive_step(active)
                results.append(result)
                if result.status not in {"pass", "complete"}:
                    verdict, note = result.status, result.note
                    break
        except (RunBlocked, ModelError, OSError, RuntimeError, ValueError, TypeError, LookupError) as error:
            verdict = self._error_status(error)
            note = self._safe_error_message(error)
            if active is not None:
                results.append(self._finalize_step(active, verdict, note))
            else:
                results.append(StepResult(id="runner.setup", status=verdict, note=note))
            self._findings["missing_evidence"].append(note)
        finally:
            for step in self.scenario.cleanup:
                if self._unsafe_evidence:
                    cleanup_notes.append(f"{step.id}: skipped; ambiguous evidence requires caller review")
                    continue
                try:
                    if step.scope is None:
                        raise RunBlocked("Cleanup requires an explicit caller-owned row scope")
                    scope_variables = {
                        match.group(1)
                        for value in step.scope.model_dump().values()
                        if isinstance(value, str)
                        for match in INTERPOLATION_RE.finditer(value)
                    }
                    if not scope_variables.intersection(self._owned_captures):
                        raise RunBlocked("Cleanup scope must reference an identifier from this run's verified POST")
                    ctx = self._begin_step(step)
                    result = self._drive_step(ctx)
                    results.append(result)
                    cleanup_notes.append(f"{step.id}: {result.status}" + (f" — {result.note}" if result.note else ""))
                except (RunBlocked, ModelError, OSError, RuntimeError, ValueError, TypeError, LookupError) as error:
                    cleanup_notes.append(f"{step.id}: cleanup blocked — {self._safe_error_message(error)}")
            try:
                self.transport.close()
            except (OSError, RuntimeError) as error:
                cleanup_notes.append(f"Browser release failed: {self._safe_error_message(error)}")
                if verdict in {"pass", "complete"}:
                    verdict, note = "error", "Browser session did not close cleanly"
        if self._unsafe_evidence and verdict in {"pass", "complete"}:
            verdict, note = "blocked", "Evidence was lost, incomplete, or disconnected"
        if self._evidence_lost_total:
            self._findings["missing_evidence"].append(f"Lost evidence entries: {self._evidence_lost_total}")
        if self.scenario.mode == "exploratory":
            self._findings["missing_evidence"].append(
                "No contract PASS is claimed; unspecified expected behavior was not verified"
            )
        report = Report(
            scenario_name=self.scenario.name,
            run_id=self.scenario.run_id,
            verdict=verdict,
            execution_mode=getattr(self.transport, "execution_mode", self.policy.identity.mode),
            model_provider=provider_label(self.provider),
            model_used=model_name_from_provider(self.provider),
            started_at_ms=self.started_at_ms,
            completed_at_ms=int(time.time() * 1000),
            steps=tuple(results),
            note=note,
            cleanup_notes=tuple(cleanup_notes),
            metadata=self._metadata,
            findings={key: tuple(values) for key, values in self._findings.items()},
            screenshots=tuple(self._screenshots),
        )
        report = Report.model_validate(self._redact(report.model_dump()))
        return RunOutcome(report, _exit_code(verdict))

    def _begin_step(self, step: Step) -> _StepContext:
        variables = dict(self._variables)
        pending = dict(step.fixtures)
        if set(pending) & set(variables):
            raise RunBlocked("Fixtures cannot overwrite run identity or captured identifiers")
        while pending:
            ready = [
                key
                for key, value in pending.items()
                if all(match.group(1) in variables for match in INTERPOLATION_RE.finditer(value))
            ]
            if not ready:
                raise RunBlocked("Fixture references are unknown or cyclic")
            for key in ready:
                variables[key] = interpolate_fixtures(pending.pop(key), variables)
        fixtures = {key: variables[key] for key in step.fixtures}
        self._secrets.update(fixture_secrets(fixtures, self.policy.redaction))
        goal = interpolate_fixtures(step.goal, variables)
        scope = step.scope
        if scope is not None:
            scope = type(scope).model_validate(_interpolate_structure(scope.model_dump(), variables))
            step = step.model_copy(update={"scope": scope})
        return _StepContext(step=step, fixtures=fixtures, goal=goal, variables=variables)

    def _drive_step(self, ctx: _StepContext) -> StepResult:
        history: list[dict[str, Any]] = []
        stagnant = 0
        previous_fingerprint: str | None = None
        try:
            while True:
                self._remaining()
                self._require_observation_policy()
                self._drain_evidence_into_records(ctx)
                self._require_complete_evidence(ctx)
                page = self._snapshot_with_retry()
                self._require_request("GET", page.url)
                if "demoVariant" in page.metadata:
                    self._metadata["demo_variant"] = page.metadata["demoVariant"]
                if page.omitted_actions or page.truncated_text:
                    raise RunBlocked("Page observation was truncated; full evidence is unavailable")
                if page.unsupported:
                    raise RunBlocked("Embedded browsing contexts are unsupported")
                page, scope_present = self._scoped_page(ctx, page)
                if not scope_present:
                    if ctx.step.skip_if_absent or (ctx.actions and ctx.step.assertions):
                        return self._evaluate_step(ctx)
                    raise RunBlocked("Caller-owned action scope is absent or ambiguous")
                if self.scenario.mode == "contract" and ctx.actions and self._contract_ready(ctx):
                    self._record_observation(ctx, page)
                    return self._evaluate_step(ctx)
                if previous_fingerprint == page.fingerprint:
                    stagnant += 1
                else:
                    stagnant = 0
                if stagnant >= 3:
                    raise RunBlocked("No progress after three observations; no further input was sent")
                previous_fingerprint = page.fingerprint
                decision = self._choose(ctx, page, history)
                confidences = [decision.confidence]
                if decision.target_confidence is not None:
                    confidences.append(decision.target_confidence)
                self._findings["model_judgments"].append(
                    f"{ctx.step.id}: {decision.operation}, confidence {min(confidences):.3f}; not a correctness verdict"
                )
                if any(not math.isfinite(value) or value < self.confidence_threshold for value in confidences):
                    raise RunBlocked("Selected operation or target confidence is below the configured threshold")
                if decision.operation in {"BLOCKED", "NONE", "NO_MATCH", "UNSUPPORTED"} or decision.choice in {
                    "NONE",
                    "NO_MATCH",
                }:
                    raise RunBlocked("Model reported no supported matching operation or target")
                if decision.operation == "DONE":
                    self._record_observation(ctx, page)
                    return self._evaluate_step(ctx)
                action = next((item for item in page.actions if str(item.get("id")) == decision.choice), None)
                if action is None:
                    raise RunBlocked("Decision selected an unobserved or out-of-scope target")
                expected_kind = {
                    "CLICK": "click",
                    "TYPE_TEXT": "fill",
                    "SELECT": "select",
                    "SCROLL_DOWN": "scroll",
                    "SCROLL_UP": "scroll",
                    "WAIT": "wait",
                }
                if expected_kind.get(decision.operation) != action.get("kind"):
                    raise RunBlocked("Selected target does not match the chosen operation")
                if action.get("unsupported") or action.get("kind") not in {"click", "fill", "scroll", "wait"}:
                    raise RunBlocked("Selected interaction is unsupported; no scripted input fallback is permitted")
                if self._actions_taken_total >= self.scenario.limits.max_actions:
                    raise RunBlocked("Scenario exceeded max_actions budget")
                text = self._resolve_text(action, ctx.fixtures)
                self._actions_taken_total += 1
                ctx.actions.append(
                    ActionRecord(
                        step=ctx.step.id,
                        action_id=str(action["id"]),
                        label=action["label"],
                        kind=action["kind"],
                        text=text,
                        confidence=min(confidences),
                        operation=decision.operation,
                        target=decision.target,
                        page_url=page.url,
                        executed_at_ms=int(time.time() * 1000),
                        latency_ms=decision.latency_ms,
                    )
                )
                try:
                    self._dispatch_action(page, action, text, ctx)
                except RunBlocked:
                    raise
                except Exception as error:
                    ctx.evidence_problem = (
                        f"Input interrupted; outcome unknown and never resubmitted: {self._safe_error_message(error)}"
                    )
                    self._unsafe_evidence = True
                    raise RunBlocked(ctx.evidence_problem) from error
                # Consume and settle this input's requests before asking for another decision.
                self._drain_evidence_into_records(ctx)
                self._require_complete_evidence(ctx)
                self._settled_actions_total += 1
                self._capture_state("after_action", ctx)
                history.append(
                    {
                        "action": action["label"],
                        "kind": action["kind"],
                        "text": text,
                        "operation": decision.operation,
                        "step": len(ctx.actions),
                    }
                )
        except (RunBlocked, ModelError, OSError, RuntimeError, ValueError, TypeError, LookupError) as error:
            return self._finalize_step(ctx, self._error_status(error), self._safe_error_message(error))

    def _record_observation(self, ctx: _StepContext, page: PageState) -> None:
        self._findings["observed_facts"].append(f"{ctx.step.id}: observed page {page.url} with title {page.title!r}")
        self._findings["observed_facts"].append(f"{ctx.step.id}: observed scope/page text {page.text!r}")

    def _contract_ready(self, ctx: _StepContext) -> bool:
        positives = tuple(
            type(assertion).model_validate(_interpolate_structure(assertion.model_dump(), ctx.variables))
            for assertion in ctx.step.assertions
            if assertion.kind in {"network", "status"}
        )
        if any(not matching_exchanges(ctx.evidence_records, method=item.method, path=item.path) for item in positives):
            return False
        if any(not outcome.passed for outcome in self._check_assertions(ctx, positives)):
            return True
        captured = capture_network_variables(positives, ctx.evidence_records)
        variables = ctx.variables | captured if captured else ctx.variables
        immediate = tuple(
            type(assertion).model_validate(_interpolate_structure(assertion.model_dump(), variables))
            for assertion in ctx.step.assertions
            if assertion.kind not in {"network", "status", "persistence"}
        )
        outcomes = self._check_assertions(ctx, immediate)
        if any(outcome.kind == "no_request" and not outcome.passed for outcome in outcomes):
            return True
        # Absence alone is not a completion signal. Require an observed UI/API anchor;
        # successful HTTP alone cannot finish while an authored UI state is still pending.
        anchored = bool(positives) or any(outcome.kind != "no_request" for outcome in outcomes)
        return anchored and all(outcome.passed for outcome in outcomes)

    def _choose(self, ctx: _StepContext, page: PageState, history: list[dict[str, Any]]):
        self._require_observation_policy()
        remaining = self._remaining()
        client = getattr(self.provider, "client", None)
        if client is not None:
            client.timeout_seconds = min(self.scenario.limits.model_timeout_seconds, remaining)
            client.deadline = self._scenario_deadline
        goal = ctx.goal
        if ctx.fixtures:
            goal += "\nCaller-supplied exact fixtures: " + json.dumps(ctx.fixtures, ensure_ascii=False)
        decision = self.provider.choose(
            goal=self._redact(goal),
            page=self._redact(
                {
                    "url": page.url,
                    "title": page.title,
                    "text": page.text,
                    "width": page.width,
                    "height": page.height,
                    "scroll": page.scroll,
                    "scope": ctx.step.scope.model_dump(exclude_none=True) if ctx.step.scope else None,
                    "actions": list(page.actions),
                }
            ),
            history=self._redact(history) if self.policy.model_disclosure.allow_action_history else [],
        )
        self._remaining()
        return decision

    def _evaluate_step(self, ctx: _StepContext) -> StepResult:
        self._drain_evidence_into_records(ctx)
        self._require_complete_evidence(ctx)
        if not ctx.actions:
            for assertion in ctx.step.assertions:
                if assertion.kind in {"network", "status"} and not matching_exchanges(
                    ctx.evidence_records,
                    method=assertion.method,
                    path=_interpolate_structure(assertion.path, ctx.variables),
                ):
                    raise RunBlocked("Step stopped before exercising a required API interaction")
        networks = tuple(
            type(assertion).model_validate(_interpolate_structure(assertion.model_dump(), ctx.variables))
            for assertion in ctx.step.assertions
            if assertion.kind == "network"
        )
        ctx.outcomes = self._assertions(ctx, networks)
        network_failed = any(not outcome.passed for outcome in ctx.outcomes)
        if not network_failed:
            captured = capture_network_variables(networks, ctx.evidence_records)
            if any(key in ctx.variables and ctx.variables[key] != value for key, value in captured.items()):
                raise RunBlocked("Captured identity would overwrite an existing run variable")
            ctx.variables.update(captured)
            self._variables.update(captured)
            for assertion in networks:
                successful_post = assertion.method == "POST" and any(
                    record.method == "POST"
                    and urlsplit(record.url).path == assertion.path
                    and record.status is not None
                    and 200 <= record.status < 300
                    for record in ctx.evidence_records
                )
                if successful_post and self.scenario.run_id in json.dumps(assertion.payload_contains):
                    self._owned_captures.update(assertion.capture)
        immediate, persistence = [], []
        for index, assertion in enumerate(ctx.step.assertions):
            if assertion.kind == "network":
                continue
            try:
                rendered = type(assertion).model_validate(_interpolate_structure(assertion.model_dump(), ctx.variables))
            except ValueError:
                if not network_failed:
                    raise
                ctx.evaluated_assertions.add(index)
                ctx.outcomes.append(
                    AssertionOutcome(
                        name=f"{ctx.step.id}.{index}.{assertion.kind}",
                        kind=assertion.kind,
                        passed=False,
                        detail="Required identity was not captured from the failed exchange",
                    )
                )
                continue
            (persistence if assertion.kind == "persistence" else immediate).append(rendered)
        # Validation messages and transient UI state must be checked before reload.
        ctx.outcomes.extend(self._assertions(ctx, immediate))
        fresh_paths = tuple(dict.fromkeys(assertion.path for assertion in persistence))
        if ctx.step.reload_after or fresh_paths:
            previous_evidence_count = len(ctx.evidence_records)
            self._capture_state("before_reload", ctx)
            try:
                self._reload_and_collect_fresh(ctx, fresh_paths)
                self._capture_state("after_reload", ctx)
            except Exception:
                self._drain_evidence_into_records(ctx)
                self._require_complete_evidence(ctx)
                if not any(
                    record.method == "GET"
                    and urlsplit(record.url).path in fresh_paths
                    and record.status is not None
                    and record.status >= 400
                    for record in ctx.evidence_records[previous_evidence_count:]
                ):
                    raise
                ctx.outcomes.extend(self._assertions(ctx, persistence))
                return self._result(ctx, "fail", "Fresh read failed with a captured application HTTP error")
        ctx.outcomes.extend(self._assertions(ctx, persistence))
        self._drain_evidence_into_records(ctx)
        self._require_complete_evidence(ctx)
        failed = [outcome for outcome in ctx.outcomes if not outcome.passed]
        if failed:
            self._findings["suspected_defects"].extend(_format_outcome(outcome) for outcome in failed)
            return self._result(ctx, "fail", "; ".join(_format_outcome(outcome) for outcome in failed))
        status = "complete" if self.scenario.mode == "exploratory" else "pass"
        return self._result(ctx, status, None)

    def _check_assertions(self, ctx: _StepContext, assertions: Sequence[Assertion]) -> list[AssertionOutcome]:
        return evaluate_step(
            step_id=ctx.step.id,
            step_goal=ctx.goal,
            assertions=assertions,
            evidence=ctx.evidence_records,
            fresh_page_payloads=ctx.fresh_payloads,
            transport=self.transport
            if not ctx.evidence_problem and time.monotonic() < self._scenario_deadline
            else None,
            policy=self.policy,
            evidence_complete=not ctx.evidence_problem,
        )

    def _assertions(self, ctx: _StepContext, assertions: Sequence[Assertion]) -> list[AssertionOutcome]:
        outcomes = []
        for assertion in assertions:
            index = next(
                index
                for index, original in enumerate(ctx.step.assertions)
                if original.kind == assertion.kind and index not in ctx.evaluated_assertions
            )
            try:
                outcome = self._check_assertions(ctx, (assertion,))[0]
            except Exception:
                ctx.outcomes.extend(outcomes)
                raise
            outcome.name = f"{ctx.step.id}.{index}.{assertion.kind}"
            ctx.evaluated_assertions.add(index)
            outcomes.append(outcome)
        return outcomes

    def _reload_and_collect_fresh(self, ctx: _StepContext, paths: tuple[str, ...]) -> None:
        self._remaining()
        self._require_request("GET", str(self.scenario.start_url))
        self.transport.navigate(str(self.scenario.start_url))
        self._snapshot_with_retry()
        for path in paths:
            self._remaining()
            ctx.fresh_payloads[path] = self.transport.fresh_read(path)
        self._drain_evidence_into_records(ctx)
        self._require_complete_evidence(ctx)

    def _drain_evidence_into_records(self, ctx: _StepContext) -> None:
        try:
            records, lost = self.transport.drain_evidence()
            self._evidence_lost_total += lost
            if lost:
                ctx.evidence_problem = f"Evidence lost: {lost} entries"
            for entry in records:
                record = EvidenceRecord.from_drain(entry)
                ctx.evidence_records.append(record)
                self._findings["observed_facts"].append(
                    f"{ctx.step.id}: {record.method} {record.url} returned {record.status}"
                )
                if record.incomplete or record.error or record.status is None:
                    prefix = (
                        "Ambiguous write; never resubmitted"
                        if record.method.upper() in MUTATING_METHODS
                        else "Incomplete network evidence"
                    )
                    ctx.evidence_problem = f"{prefix}: {record.error or 'response not fully captured'}"
                elif record.status >= 400:
                    self._findings["suspected_defects"].append(
                        f"{ctx.step.id}: application returned HTTP {record.status} for {record.method} {record.url}"
                    )
        except (OSError, RuntimeError, ValueError, TypeError, LookupError) as error:
            ctx.evidence_problem = f"Evidence disconnected; no input retry: {self._safe_error_message(error)}"
        if ctx.evidence_problem:
            self._unsafe_evidence = True

    def _require_complete_evidence(self, ctx: _StepContext) -> None:
        if ctx.evidence_problem:
            raise RunBlocked(ctx.evidence_problem)
        self._remaining()

    def _snapshot_with_retry(self) -> PageState:
        deadline = min(self._scenario_deadline, time.monotonic() + self.scenario.limits.navigation_timeout_seconds)
        while time.monotonic() < deadline:
            self._require_observation_policy()
            page = read_snapshot(self.transport)
            self._remaining()
            if page is not None:
                return page
            time.sleep(min(0.05, self._remaining()))
        raise RunBlocked("Page observation did not settle within the navigation limit")

    def _scoped_page(self, ctx: _StepContext, page: PageState) -> tuple[PageState, bool]:
        if ctx.step.scope is None:
            return page, True
        query = _scope_query(ctx.step)
        result = self.transport.evaluate_js(
            "(() => { const roots=" + query + "; if(roots.length!==1) return {count:roots.length,nodes:[]};"
            " const root=roots[0], c=window.__jevFast;"
            " const ids=new Set([root,...root.querySelectorAll('[aria-describedby]')].flatMap("
            " e=>(e.getAttribute('aria-describedby')||'').split(/\\s+/)));"
            " const extra=[...ids].map(id=>document.getElementById(id)).filter(e=>e&&!root.contains(e));"
            " const text=[root.innerText,...extra.map(e=>e.innerText)].join('\\n').slice(0,6001);"
            " return {count:1,text,nodes:[...c.nodes].filter(([id,e])=>root.contains(e)).map(([id])=>id)}; })()"
        )
        if not isinstance(result, dict) or result.get("count", 0) > 1:
            raise RunBlocked("Caller-owned action scope is ambiguous or unavailable")
        if result["count"] == 0:
            return page, False
        if not isinstance(result.get("text"), str) or len(result["text"]) > 6000:
            raise RunBlocked("Caller-owned scope text is unavailable or truncated")
        allowed_nodes = set(result["nodes"])
        return replace(
            page,
            text=result["text"],
            actions=tuple(
                {**action, "reveals": [item for item in action.get("reveals", ()) if item["node"] in allowed_nodes]}
                if action.get("kind") == "scroll"
                else action
                for action in page.actions
                if action.get("node") in allowed_nodes or action.get("kind") in {"wait", "scroll"}
            ),
        ), True

    def _guarded_point(self, page: PageState, action: dict, ctx: _StepContext, *, focused: bool = False) -> dict:
        self._remaining()
        node = action.get("node")
        guard = page.guards.get(str(node))
        if not isinstance(node, int) or guard is None:
            raise RunBlocked("Observed control lacks a stable identity guard")
        scope_check = ""
        if ctx.step.scope is not None:
            scope_check = (
                "const roots=" + _scope_query(ctx.step) + "; if(roots.length!==1||!roots[0].contains(e)) return null;"
            )
        script = (
            "(() => { const c=window.__jevFast; if(!c) return null;"
            f" const e=c.nodes.get({node}); if(!e||!e.isConnected) return null;"
            f" if(JSON.stringify(c.pageKey())!==JSON.stringify({json.dumps(page.page_key)})) return null;"
            f" if(JSON.stringify(c.guard(e))!==JSON.stringify({json.dumps(guard)})) return null;"
            + scope_check
            + (" if(document.activeElement!==e) return null;" if focused else "")
            + " if(e.matches(':disabled')||e.closest('[inert],[aria-disabled=\"true\"]')) return null;"
            " const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;"
            " if(r.width<=0||r.height<=0||x<0||y<0||x>=innerWidth||y>=innerHeight) return null;"
            " const hit=document.elementFromPoint(x,y); if(!hit||(hit!==e&&!e.contains(hit))) return null;"
            " return {x,y}; })()"
        )
        point = self.transport.evaluate_js(script)
        if not isinstance(point, dict):
            raise RunBlocked("Control changed, lost focus, or became occluded before input; no input was retried")
        self._remaining()
        return point

    def _dispatch_action(self, page: PageState, action: dict, text: str | None, ctx: _StepContext) -> None:
        kind = action["kind"]
        if kind == "wait":
            time.sleep(min(0.1, self._remaining()))
            return
        if kind == "scroll":
            current = self.transport.evaluate_js("window.__jevFast?.pageKey()")
            if current != page.page_key:
                raise RunBlocked("Page changed before scroll")
            self.transport.scroll(page.width / 2, page.height / 2, delta_y=action["delta"])
            return
        point = self._guarded_point(page, action, ctx)
        self.transport.dispatch_mouse(point["x"], point["y"])
        if kind == "fill":
            self._guarded_point(page, action, ctx, focused=True)
            self.transport.select_all()
            self._guarded_point(page, action, ctx, focused=True)
            if text:
                self.transport.insert_text(text)
            else:
                self.transport.dispatch_key("Backspace")
        self._remaining()

    def _resolve_text(self, action: dict, fixtures: Mapping[str, str]) -> str | None:
        if action.get("kind") != "fill":
            return None
        key = action.get("fixture_key")
        if key in fixtures:
            return fixtures[key]
        label = action.get("label")
        if label in fixtures:
            return fixtures[label]
        raise RunBlocked("No exact caller fixture maps to the selected field")

    def _finalize_step(self, ctx: _StepContext, status: str, note: str | None) -> StepResult:
        self._drain_evidence_into_records(ctx)
        if ctx.evidence_problem:
            status, note = "blocked", ctx.evidence_problem
        # Retain every check that can be evaluated; unknown captures are missing evidence,
        # never fabricated values and never a reason to discard prior assertion outcomes.
        for index, assertion in enumerate(ctx.step.assertions):
            if index in ctx.evaluated_assertions:
                continue
            try:
                rendered = type(assertion).model_validate(_interpolate_structure(assertion.model_dump(), ctx.variables))
                ctx.outcomes.extend(self._assertions(ctx, (rendered,)))
            except (RunBlocked, OSError, RuntimeError, ValueError, TypeError, LookupError) as error:
                ctx.evaluated_assertions.add(index)
                ctx.outcomes.append(
                    AssertionOutcome(
                        name=f"{ctx.step.id}.{index}.{assertion.kind}",
                        kind=assertion.kind,
                        passed=False,
                        detail=f"Assertion evidence unavailable: {self._safe_error_message(error)}",
                    )
                )
        self._findings["missing_evidence"].append(note or "Step did not complete")
        return self._result(ctx, status, note)

    def _result(self, ctx: _StepContext, status: str, note: str | None) -> StepResult:
        self._capture_state(f"step_{status}", ctx)
        return StepResult(
            id=ctx.step.id,
            status=status,
            note=note,
            actions=tuple(ctx.actions),
            evidence=tuple(record.sanitized(self.policy.redaction) for record in ctx.evidence_records),
            assertions=tuple(
                AssertionResult(
                    name=outcome.name,
                    kind=outcome.kind,
                    passed=outcome.passed,
                    expected=outcome.expected,
                    observed=outcome.observed,
                    detail=outcome.detail,
                )
                for outcome in ctx.outcomes
            ),
        )

    def _capture_state(self, phase: str, ctx: _StepContext | None = None) -> None:
        if self.screenshot_dir is None or not self.policy.model_disclosure.allow_screenshots:
            return
        if self.screenshot_mode == "actions":
            if phase != "after_action" or self._settled_actions_total % self.screenshot_every:
                return
        elif (
            self.screenshot_mode == "steps"
            and not phase.startswith("step_")
            or self.screenshot_mode == "failures"
            and phase not in {"step_fail", "step_blocked", "step_error"}
        ):
            return
        step_id = ctx.step.id if ctx else None
        # Avoid a duplicate terminal capture when the last capture for this
        # step already recorded the settled post-action view; assertion
        # evaluation never mutates the page, so step_{status} would be a
        # pixel-for-pixel repeat of after_action.
        if self.screenshot_mode == "all" and phase.startswith("step_") and self._screenshots:
            last = self._screenshots[-1]
            if last.step == step_id and last.phase == "after_action":
                return
        self._capture_attempts += 1
        path = self.screenshot_dir / f"{self._capture_attempts:04d}-{phase}.png"
        try:
            self._remaining()
            self.transport.screenshot(path)
        except (OSError, RuntimeError, ValueError, TypeError, LookupError, RunBlocked, PermissionError):
            # Authored safe note; never leak transport/exception text into the report.
            self._findings["missing_evidence"].append(
                f"Screenshot unavailable: {phase}; capture was denied, failed, or exceeded the deadline."
            )
            return
        self._screenshots.append(
            ScreenshotRecord(
                step=step_id,
                phase=phase,
                action_index=len(ctx.actions) if ctx else None,
                captured_at_ms=int(time.time() * 1000),
                path=str(path.absolute()),
            )
        )

    def _remaining(self) -> float:
        remaining = self._scenario_deadline - time.monotonic()
        if remaining <= 0:
            raise RunBlocked("Scenario exceeded max_seconds budget")
        return remaining

    def _require_request(self, method: str, url: str) -> None:
        decision = self._enforcer.check_request(method, url)
        if not decision.allowed:
            raise RunBlocked(decision.reason or "Network policy denied the request")

    def _require_observation_policy(self) -> None:
        if not self.policy.model_disclosure.allow_page_text:
            raise RunBlocked("Policy does not permit page text, labels, or field values to be disclosed to the model")

    def _error_status(self, error: Exception) -> str:
        if isinstance(error, (RunBlocked, ValueError, PermissionError, TimeoutError, ConnectionError)):
            return "blocked"
        if isinstance(error, ModelError):
            return "blocked" if error.code in {"missing_key", "invalid_choice", "timeout"} else "error"
        return "error"

    def _safe_error_message(self, error: Exception) -> str:
        return self._redact(str(error) or error.__class__.__name__)

    def _redact(self, value: Any) -> Any:
        return redact_report(value, self.policy.redaction, secrets=self._secrets)


def _scope_query(step: Step) -> str:
    scope = step.scope
    if scope.css is not None:
        return f"document.querySelectorAll({json.dumps(scope.css)})"
    return (
        "[...document.querySelectorAll('[data-testid]')].filter(e=>e.getAttribute('data-testid')==="
        + json.dumps(scope.testid)
        + ")"
    )


def _interpolate_structure(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return interpolate_fixtures(value, variables)
    if isinstance(value, dict):
        return {key: _interpolate_structure(item, variables) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_interpolate_structure(item, variables) for item in value]
    return value


def interpolate_fixtures(text: str, fixtures: Mapping[str, str]) -> str:
    """Interpolate string leaves without serializing/reparsing fixture text as JSON."""

    def replace_match(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in fixtures:
            raise ValueError(f"Missing fixture: {key!r}")
        return fixtures[key]

    return INTERPOLATION_RE.sub(replace_match, text)


def _format_outcome(outcome: AssertionOutcome) -> str:
    return (
        f"{outcome.kind}: {outcome.detail}"
        if outcome.detail
        else f"{outcome.kind}: expected={outcome.expected!r}, observed={outcome.observed!r}"
    )


def _exit_code(verdict: str) -> int:
    return {"pass": 0, "complete": 0, "fail": 1, "blocked": 2, "error": 3}.get(verdict, 3)
