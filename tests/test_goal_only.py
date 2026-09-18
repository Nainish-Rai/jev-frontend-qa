"""Explicit goal-only opt-in must not weaken authored-contract execution."""

import pytest
from test_exploration import MemoryBrowser, RecordingProvider, choice, page, policy, scenario
from test_planning import ScriptedPlanner

from jev_frontend_qa.cli import build_parser
from jev_frontend_qa.core import agent
from jev_frontend_qa.core.agent import Runner
from jev_frontend_qa.core.evidence import EvidenceCollector
from jev_frontend_qa.core.models import Policy
from jev_frontend_qa.core.planner import PlannerDecision
from jev_frontend_qa.core.policy import PolicyEnforcer


def goal_policy():
    return Policy.model_validate(
        {
            "goal_only": True,
            "model_disclosure": {"allow_page_text": True, "allow_action_history": True, "allow_planner": True},
        }
    )


def test_goal_only_explicitly_replaces_policy_file_not_contract_requirements():
    parser = build_parser()
    args = parser.parse_args(["explore", "--url", "https://example.com", "--goal", "Search", "--goal-only"])
    assert args.goal_only and args.policy is None
    for argv in [
        ["run", "--scenario", "contract.json"],
        ["explore", "--url", "https://example.com", "--goal", "Search"],
        ["explore", "--url", "https://example.com", "--goal", "Search", "--goal-only", "--policy", "policy.json"],
    ]:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_goal_only_allows_cross_origin_methods_but_not_non_web_destinations():
    ordinary = PolicyEnforcer(Policy())
    explicit = PolicyEnforcer(goal_policy())
    assert not ordinary.check_request("POST", "https://another.example/playlist/create").allowed
    assert explicit.check_request("POST", "https://another.example/playlist/create").allowed
    assert explicit.check_request("DELETE", "https://another.example/a%2Fb").allowed
    for address in ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com/"]:
        assert not explicit.check_request("GET", address).allowed
    collector = EvidenceCollector()
    collector.set_policy(goal_policy())
    assert collector.policy_allows("POST", "https://another.example/playlist/create")
    collector.set_rules([])
    assert not collector.policy_allows("POST", "https://another.example/playlist/create")


def test_goal_only_uses_planner_text_and_does_not_apply_numeric_confidence_gate(monkeypatch):
    state = page(kind="fill", fixture_key="search_query")
    monkeypatch.setattr(agent, "read_snapshot", lambda _: state)
    monkeypatch.setattr(Runner, "_settle_action", lambda self, before: state)
    browser = MemoryBrowser()
    planner = ScriptedPlanner(
        [
            {
                "operation": "act",
                "goal": "Search for songs",
                "inputs": [{"field": "search_query", "value": "Honey Singh songs"}],
            },
            {"operation": "complete", "reason": "Observed only"},
        ]
    )
    outcome = Runner(
        browser,
        RecordingProvider([choice("TYPE_TEXT", "e1", 0.1), choice(confidence=0.1)]),
        goal_policy(),
        scenario(),
        planner=planner,
        confidence_threshold=0,
    ).run()
    assert outcome.report.verdict == "complete"
    assert browser.insertions == ["Honey Singh songs"]
    assert outcome.report.metadata["policy_mode"] == "goal_only"


def test_normal_exploration_rejects_planner_text_even_from_custom_provider(monkeypatch):
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page(kind="fill", fixture_key="search_query"))
    browser = MemoryBrowser()
    planner = ScriptedPlanner(
        [
            {"operation": "act", "goal": "Search", "inputs": [{"field": "search_query", "value": "New text"}]},
        ]
    )
    outcome = Runner(browser, RecordingProvider([]), policy(allow_planner=True), scenario(), planner=planner).run()
    assert outcome.report.verdict == "blocked"
    assert browser.insertions == []


def test_goal_only_never_runs_authored_contract():
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "inspect",
                "goal": "Inspect",
                "assertions": [{"kind": "count", "selector": {"css": "h1"}, "expected": 1}],
            }
        ],
    )
    with pytest.raises(ValueError):
        Runner(MemoryBrowser(), RecordingProvider([]), goal_policy(), spec)


def test_goal_only_generated_fields_require_unique_act_inputs():
    for payload in [
        {"operation": "complete", "inputs": [{"field": "title", "value": "name"}]},
        {
            "operation": "act",
            "goal": "Create",
            "inputs": [{"field": "title", "value": "one"}, {"field": "title", "value": "two"}],
        },
    ]:
        with pytest.raises(ValueError):
            PlannerDecision.model_validate(payload)


@pytest.mark.parametrize("goal_only,verdict", [(True, "complete"), (False, "blocked")])
def test_background_capture_failure_is_diagnostic_only_in_goal_only(monkeypatch, goal_only, verdict):
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    browser = MemoryBrowser()
    browser.pending = [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8767/telemetry",
            "status": None,
            "incomplete": True,
            "error": "net::ERR_ABORTED",
        }
    ]
    browser.lost = 1
    planner = ScriptedPlanner([{"operation": "complete", "reason": "Observed only"}])
    permissions = goal_policy() if goal_only else policy(allow_planner=True)
    outcome = Runner(browser, RecordingProvider([]), permissions, scenario(), planner=planner).run()
    assert outcome.report.verdict == verdict
    if goal_only:
        assert outcome.report.steps[0].evidence[0].error == "net::ERR_ABORTED"
    assert browser.clicks == 0
    assert len(planner.calls) == (1 if goal_only else 0)


def test_goal_only_does_not_replace_explicit_caller_text(monkeypatch):
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page(kind="fill", fixture_key="title"))
    browser = MemoryBrowser()
    planner = ScriptedPlanner(
        [
            {
                "operation": "act",
                "goal": "Create",
                "inputs": [{"field": "title", "value": "Planner replacement"}],
            }
        ]
    )
    spec = scenario(steps=[{"id": "create", "goal": "Create", "fixtures": {"title": "Caller value"}}])
    outcome = Runner(browser, RecordingProvider([]), goal_policy(), spec, planner=planner).run()
    assert outcome.report.verdict == "blocked"
    assert browser.insertions == []
