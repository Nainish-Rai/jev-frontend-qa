"""Runner safety-state regressions with an explicit offline provider seam."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from jev_frontend_qa.core import agent
from jev_frontend_qa.core.agent import Runner
from jev_frontend_qa.core.decisions import DecisionProvider
from jev_frontend_qa.core.model_client import Decision
from jev_frontend_qa.core.models import Policy, Scenario
from jev_frontend_qa.core.snapshot import PageState

ORIGIN = "http://127.0.0.1:8767"
RUN_ID = "11111111-1111-4111-8111-111111111111"


def choice(operation="DONE", target=None, confidence=1.0, target_confidence=None):
    return Decision(
        choice=target or operation,
        operation=operation,
        target=target,
        confidence=confidence,
        probabilities={operation: confidence},
        target_confidence=target_confidence,
        target_probabilities=None,
        raw_answers={},
        model="offline-test",
        usage={},
        latency_ms=0,
        request={},
    )


class RecordingProvider(DecisionProvider):
    def __init__(self, decisions, on_choose=None):
        super().__init__(name="offline-test")
        self.decisions = list(decisions)
        self.calls = []
        self.on_choose = on_choose

    def choose(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_choose:
            self.on_choose()
        return self.decisions.pop(0)


class MemoryBrowser:
    """A tiny observable browser/network boundary, not a production fallback."""

    execution_mode = "isolated"
    deadline = None

    def __init__(self):
        self.navigations = []
        self.clicks = 0
        self.insertions = []
        self.keys = []
        self.pending = []
        self.lost = 0
        self.on_click = None
        self.guard_ok = True
        self.disconnected = False
        self.fresh_payloads = {}
        self.fresh_reads = []
        self.closed = False

    def navigate(self, url):
        self.navigations.append(url)

    def drain_evidence(self):
        if self.disconnected:
            raise ConnectionError("browser disconnected")
        records, self.pending = self.pending, []
        lost, self.lost = self.lost, 0
        return records, lost

    def dispatch_mouse(self, x, y):
        self.clicks += 1
        if self.on_click:
            self.on_click(self)

    def select_all(self):
        pass

    def insert_text(self, text):
        self.insertions.append(text)

    def dispatch_key(self, key):
        self.keys.append(key)

    def evaluate_js(self, expression, **kwargs):
        if "const c=window.__jevFast" in expression:
            return {"x": 20, "y": 30} if self.guard_ok else None
        raise AssertionError("Unexpected DOM read in this safety scenario")

    def fresh_read(self, path):
        self.fresh_reads.append(path)
        return self.fresh_payloads[path]

    def close(self):
        self.closed = True


def page(*, kind="click", fixture_key=None, **changes):
    action = {
        "id": "e1",
        "node": 1,
        "label": "Todo title" if kind == "fill" else "Create",
        "kind": kind,
        "fixture_key": fixture_key,
        "rect": {"x": 0, "y": 0, "w": 40, "h": 60},
    }
    state = PageState(
        url=ORIGIN + "/",
        title="Synthetic Todo",
        text="Todo controls",
        actions=(action,),
        scroll={"y": 0, "height": 600},
        fingerprint="initial",
        page_key=["owned-document"],
        guards={"1": ["original-control"]},
        width=800,
        height=600,
    )
    return replace(state, **changes)


def scenario(*, mode="exploratory", steps=None, **changes):
    return Scenario.model_validate(
        {
            "name": "safety",
            "mode": mode,
            "run_id": RUN_ID,
            "start_url": ORIGIN,
            "steps": steps or [{"id": "inspect", "goal": "Inspect the synthetic page"}],
            **changes,
        }
    )


def policy(**disclosure):
    return Policy.model_validate(
        {
            "network": {"allowed_origins": [{"origin": ORIGIN, "path_prefix": "/", "methods": ["GET", "POST"]}]},
            "model_disclosure": {"allow_page_text": True, **disclosure},
        }
    )


def run(monkeypatch, decisions, *, browser=None, state=None, spec=None, permissions=None, provider=None):
    browser = browser or MemoryBrowser()
    provider = provider or RecordingProvider(decisions)
    state = state or page()
    monkeypatch.setattr(agent, "read_snapshot", lambda transport: state)
    outcome = Runner(browser, provider, permissions or policy(), spec or scenario()).run()
    return outcome, browser, provider


@pytest.mark.parametrize(
    "decision",
    [
        choice(confidence=0.2),
        choice("CLICK", "e1", target_confidence=0.2),
        choice("CLICK", "NONE"),
        choice("UNSUPPORTED"),
    ],
)
def test_uncertain_terminal_or_selected_target_never_dispatches(monkeypatch, decision):
    outcome, browser, _ = run(monkeypatch, [decision])
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 0


def test_denied_disclosure_blocks_before_navigation_observation_or_model(monkeypatch):
    def forbidden(transport):
        pytest.fail("observation preceded policy enforcement")

    monkeypatch.setattr(agent, "read_snapshot", forbidden)
    browser, provider = MemoryBrowser(), RecordingProvider([choice()])
    outcome = Runner(browser, provider, policy(allow_page_text=False), scenario()).run()
    assert outcome.report.verdict == "blocked"
    assert browser.navigations == []
    assert provider.calls == []


def test_missing_exact_field_mapping_never_uses_generic_text_fixture(monkeypatch):
    spec = scenario(steps=[{"id": "fill", "goal": "Fill the title", "fixtures": {"text": "wrong field value"}}])
    outcome, browser, _ = run(
        monkeypatch, [choice("TYPE_TEXT", "e1")], spec=spec, state=page(kind="fill", fixture_key="title")
    )
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 0
    assert browser.insertions == []


def test_fixture_substitution_preserves_exact_quotes_and_newlines(monkeypatch):
    title = 'qa-{{run_id}} "quoted"\nsecond line'
    spec = scenario(steps=[{"id": "fill", "goal": "Fill {{title}}", "fixtures": {"title": title}}])
    outcome, browser, provider = run(
        monkeypatch, [choice("TYPE_TEXT", "e1"), choice()], spec=spec, state=page(kind="fill", fixture_key="title")
    )
    expected = title.replace("{{run_id}}", RUN_ID)
    assert outcome.report.verdict == "complete"
    assert browser.insertions == [expected]
    assert provider.calls[0]["goal"].startswith("Fill " + expected)
    assert provider.calls[1]["history"] == []


def test_empty_exact_fixture_clears_field_with_backspace(monkeypatch):
    spec = scenario(steps=[{"id": "clear", "goal": "Clear title", "fixtures": {"title": ""}}])
    outcome, browser, _ = run(
        monkeypatch, [choice("TYPE_TEXT", "e1"), choice()], spec=spec, state=page(kind="fill", fixture_key="title")
    )
    assert outcome.report.verdict == "complete"
    assert browser.keys == ["Backspace"]
    assert browser.insertions == []


def test_stale_or_occluded_control_blocks_without_input(monkeypatch):
    browser = MemoryBrowser()
    browser.guard_ok = False
    outcome, _, _ = run(monkeypatch, [choice("CLICK", "e1")], browser=browser)
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 0


@pytest.mark.parametrize("failure", ["pending", "error", "lost", "disconnect", "input"])
def test_ambiguous_write_is_drained_before_any_resubmission(monkeypatch, failure):
    browser = MemoryBrowser()

    def submit(current):
        if failure == "input":
            raise TimeoutError("input interrupted after press")
        if failure == "disconnect":
            current.disconnected = True
        elif failure == "lost":
            current.lost = 1
        else:
            current.pending.append(
                {
                    "method": "POST",
                    "url": ORIGIN + "/api/todos",
                    "status": None,
                    "incomplete": True,
                    "error": "connection reset" if failure == "error" else None,
                }
            )

    browser.on_click = submit
    outcome, _, provider = run(monkeypatch, [choice("CLICK", "e1"), choice("CLICK", "e1")], browser=browser)
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 1
    assert len(provider.calls) == 1


def test_complete_http_application_error_is_fail_not_infrastructure_block(monkeypatch):
    browser = MemoryBrowser()
    browser.on_click = lambda current: current.pending.append(
        {
            "method": "POST",
            "url": ORIGIN + "/api/todos",
            "status": 422,
            "request_body": {"title": "qa"},
            "response_body": {"error": "rejected"},
        }
    )
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "create",
                "goal": "Create todo",
                "assertions": [
                    {"kind": "status", "method": "POST", "path": "/api/todos", "expected_status": 201},
                ],
            }
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice("CLICK", "e1"), choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == "fail"
    assert outcome.report.steps[0].assertions[0].observed == 422
    assert outcome.report.steps[0].evidence[0].status == 422


def test_previous_step_response_cannot_satisfy_next_step(monkeypatch):
    browser = MemoryBrowser()
    browser.on_click = lambda current: current.pending.append(
        {"method": "POST", "url": ORIGIN + "/api/todos", "status": 201}
    )
    assertion = {"kind": "status", "method": "POST", "path": "/api/todos", "expected_status": 201}
    spec = scenario(
        mode="contract",
        steps=[
            {"id": "first", "goal": "Create first", "assertions": [assertion]},
            {"id": "second", "goal": "Create second", "assertions": [assertion]},
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice("CLICK", "e1"), choice(), choice()], browser=browser, spec=spec)
    assert [step.status for step in outcome.report.steps] == ["pass", "blocked"]
    assert outcome.report.steps[1].evidence == ()


def test_persistence_reads_each_declared_path_after_reload(monkeypatch):
    browser = MemoryBrowser()
    browser.fresh_payloads = {"/api/first": [{"title": "first"}], "/api/second": [{"title": "second"}]}
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "persist",
                "goal": "Inspect saved records",
                "assertions": [
                    {"kind": "persistence", "method": "GET", "path": path, "contains": {"title": title}}
                    for path, title in [("/api/first", "first"), ("/api/second", "second")]
                ],
            }
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == "pass"
    assert browser.fresh_reads == ["/api/first", "/api/second"]
    assert browser.navigations == [ORIGIN + "/", ORIGIN + "/"]


def test_no_progress_blocks_repeated_waits(monkeypatch):
    wait = {"id": "wait", "kind": "wait", "label": "Wait"}
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    outcome, _, provider = run(monkeypatch, [choice("WAIT", "wait")] * 10, state=page(actions=(wait,)))
    assert outcome.report.verdict == "blocked"
    assert len(provider.calls) == 3


def test_action_limit_does_not_allow_one_more_input(monkeypatch):
    spec = scenario(limits={"max_actions": 1})
    outcome, browser, _ = run(monkeypatch, [choice("CLICK", "e1"), choice("CLICK", "e1")], spec=spec)
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 1


def test_provider_returning_after_deadline_cannot_dispatch(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])

    def exceed_deadline():
        clock[0] = 102.0

    provider = RecordingProvider([choice("CLICK", "e1")], on_choose=exceed_deadline)
    provider.client = SimpleNamespace(timeout_seconds=25.0, deadline=None)
    outcome, browser, _ = run(monkeypatch, [], provider=provider, spec=scenario(limits={"max_seconds": 1}))
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 0
    assert provider.client.timeout_seconds == 1.0
    assert provider.client.deadline == 101.0


def test_truncated_snapshot_cannot_certify_completion(monkeypatch):
    outcome, _, provider = run(monkeypatch, [choice()], state=page(omitted_actions=1))
    assert outcome.report.verdict == "blocked"
    assert provider.calls == []


def test_sensitive_fixture_is_redacted_from_report_and_model(monkeypatch):
    secret = "synthetic-private-token"
    spec = scenario(steps=[{"id": "fill", "goal": "Fill {{token}}", "fixtures": {"token": secret}}])
    outcome, browser, provider = run(
        monkeypatch, [choice("TYPE_TEXT", "e1"), choice()], spec=spec, state=page(kind="fill", fixture_key="token")
    )
    assert outcome.report.verdict == "complete"
    assert browser.insertions == [secret]
    assert secret not in outcome.report.model_dump_json()
    assert secret not in str(provider.calls)


def test_unknown_assertion_variable_preserves_other_assertion_outcomes(monkeypatch):
    browser = MemoryBrowser()
    browser.pending = [{"method": "GET", "url": ORIGIN + "/api/todos", "status": 200}]
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "inspect",
                "goal": "Inspect",
                "assertions": [
                    {"kind": "status", "method": "GET", "path": "/api/todos", "expected_status": 200},
                    {
                        "kind": "persistence",
                        "method": "GET",
                        "path": "/api/todos",
                        "contains": {"id": "{{missing_id}}"},
                    },
                ],
            }
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == "blocked"
    assertions = outcome.report.steps[0].assertions
    assert {(item.kind, item.passed) for item in assertions} == {("status", True), ("persistence", False)}


def test_unrelated_row_cannot_be_selected_outside_caller_scope(monkeypatch):
    class ScopedBrowser(MemoryBrowser):
        def evaluate_js(self, expression, **kwargs):
            if "const roots=" in expression:
                return {"count": 1, "nodes": [1], "text": "Owned row controls"}
            return super().evaluate_js(expression, **kwargs)

    state = page()
    unrelated = {**state.actions[0], "id": "e2", "node": 2, "label": "Delete unrelated row"}
    spec = scenario(steps=[{"id": "owned", "goal": "Inspect owned row", "scope": {"css": "#owned"}}])
    outcome, browser, provider = run(
        monkeypatch,
        [choice("CLICK", "e2")],
        browser=ScopedBrowser(),
        state=replace(state, actions=(*state.actions, unrelated)),
        spec=spec,
    )
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 0
    assert [action["id"] for action in provider.calls[0]["page"]["actions"]] == ["e1"]


def test_dom_validation_is_evaluated_before_reload_erases_it(monkeypatch):
    class ValidationBrowser(MemoryBrowser):
        def evaluate_js(self, expression, **kwargs):
            if "Array.from(" in expression:
                return ["error" if len(self.navigations) == 1 else ""]
            return super().evaluate_js(expression, **kwargs)

    browser = ValidationBrowser()
    browser.fresh_payloads["/api/todos"] = []
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "reject",
                "goal": "Inspect rejection",
                "reload_after": True,
                "assertions": [
                    {"kind": "equals", "selector": {"css": "#status"}, "expected": "error"},
                    {"kind": "no_request", "method": "POST", "path": "/api/todos"},
                    {
                        "kind": "persistence",
                        "method": "GET",
                        "path": "/api/todos",
                        "contains": {"title": ""},
                        "absent": True,
                    },
                ],
            }
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == "pass"
    assert outcome.report.steps[0].assertions[0].observed == "error"


def test_lost_window_cannot_prove_no_request(monkeypatch):
    browser = MemoryBrowser()
    browser.lost = 1
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "reject",
                "goal": "Inspect",
                "assertions": [
                    {"kind": "no_request", "method": "POST", "path": "/api/todos"},
                ],
            }
        ],
    )
    outcome, _, provider = run(monkeypatch, [choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == "blocked"
    assert outcome.report.steps[0].assertions[0].passed is False
    assert provider.calls == []


def test_cleanup_without_verified_run_owned_capture_does_not_touch_browser(monkeypatch):
    spec = scenario(
        cleanup=[
            {
                "id": "cleanup",
                "goal": "Delete row",
                "scope": {"css": "#arbitrary-row"},
                "assertions": [{"kind": "count", "selector": {"css": "#arbitrary-row"}, "expected": 0}],
            }
        ]
    )
    outcome, browser, provider = run(monkeypatch, [choice(), choice("CLICK", "e1")], spec=spec)
    assert outcome.report.verdict == "complete"
    assert browser.clicks == 0
    assert len(provider.calls) == 1


def test_scoped_goal_can_scroll_to_reach_offscreen_controls(monkeypatch):
    class ScrollBrowser(MemoryBrowser):
        def evaluate_js(self, expression, **kwargs):
            if "const roots=" in expression:
                return {"count": 1, "nodes": [1], "text": "Owned row controls"}
            if "pageKey()" in expression:
                return ["owned-document"]
            return super().evaluate_js(expression, **kwargs)

        def scroll(self, x, y, delta_y):
            return None

    state = page(actions=({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 300},))
    spec = scenario(steps=[{"id": "inspect", "goal": "Inspect the scoped form", "scope": {"css": "#form"}}])
    outcome, _, _ = run(
        monkeypatch, [choice("SCROLL_DOWN", "scroll_down"), choice()], browser=ScrollBrowser(), state=state, spec=spec
    )
    assert outcome.report.verdict == "complete"


@pytest.mark.parametrize(
    "prior_path, fresh_path, expected_verdict",
    [
        ("/api/todos", None, "blocked"),
        (None, "/favicon.ico", "blocked"),
        (None, "/api/todos", "fail"),
    ],
)
def test_fresh_read_failure_only_uses_its_own_http_evidence(monkeypatch, prior_path, fresh_path, expected_verdict):
    class InterruptedReadBrowser(MemoryBrowser):
        def fresh_read(self, path):
            if fresh_path:
                self.pending.append({"method": "GET", "url": ORIGIN + fresh_path, "status": 503})
            raise ConnectionError("independent read interrupted")

    browser = InterruptedReadBrowser()
    if prior_path:
        browser.pending.append({"method": "GET", "url": ORIGIN + prior_path, "status": 503})
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "verify",
                "goal": "Inspect the persisted record",
                "assertions": [
                    {
                        "kind": "persistence",
                        "path": "/api/todos",
                        "records_path": "$.todos",
                        "contains": {"title": "owned"},
                    }
                ],
            }
        ],
    )
    outcome, _, _ = run(monkeypatch, [choice()], browser=browser, spec=spec)
    assert outcome.report.verdict == expected_verdict


@pytest.mark.parametrize("persisted_rows, expected_verdict", [([], "pass"), ([{"title": ""}], "fail")])
def test_observed_rejection_finishes_with_independent_persistence_verification(
    monkeypatch, persisted_rows, expected_verdict
):
    class ValidationBrowser(MemoryBrowser):
        def evaluate_js(self, expression, **kwargs):
            if "document.querySelectorAll" in expression:
                return ["Invalid title"] if self.clicks else []
            return super().evaluate_js(expression, **kwargs)

    browser = ValidationBrowser()
    browser.fresh_payloads["/api/todos"] = {"todos": persisted_rows}
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "reject",
                "goal": "Submit the invalid title once",
                "assertions": [
                    {"kind": "count", "selector": {"css": ".validation-error"}, "expected": 1},
                    {"kind": "no_request", "method": "POST", "path": "/api/todos"},
                    {
                        "kind": "persistence",
                        "path": "/api/todos",
                        "records_path": "$.todos",
                        "contains": {"title": ""},
                        "absent": True,
                    },
                ],
            }
        ],
    )
    outcome, browser, _ = run(monkeypatch, [choice("CLICK", "e1"), choice(confidence=0.1)], browser=browser, spec=spec)
    assert outcome.report.verdict == expected_verdict
    assert browser.clicks == 1
    assert outcome.report.steps[0].assertions[-1].passed is (expected_verdict == "pass")


def test_successful_exchange_does_not_finish_before_required_ui_state(monkeypatch):
    class PendingUIBrowser(MemoryBrowser):
        def evaluate_js(self, expression, **kwargs):
            if "document.querySelectorAll" in expression:
                return []
            return super().evaluate_js(expression, **kwargs)

    browser = PendingUIBrowser()
    browser.on_click = lambda current: current.pending.append(
        {"method": "POST", "url": ORIGIN + "/api/todos", "status": 201}
    )
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "create",
                "goal": "Create the row",
                "assertions": [
                    {"kind": "status", "method": "POST", "path": "/api/todos", "expected_status": 201},
                    {"kind": "count", "selector": {"css": ".created-row"}, "expected": 1},
                ],
            }
        ],
    )
    outcome, browser, _ = run(monkeypatch, [choice("CLICK", "e1"), choice(confidence=0.1)], browser=browser, spec=spec)
    assert outcome.report.verdict == "blocked"
    assert browser.clicks == 1
