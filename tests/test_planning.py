"""Safety boundaries introduced by the host-planned exploration path."""

import json
from dataclasses import replace

import pytest
from test_exploration import ORIGIN, MemoryBrowser, RecordingProvider, choice, page, policy, scenario

from jev_frontend_qa.core import agent
from jev_frontend_qa.core.agent import Runner
from jev_frontend_qa.core.planner import HostPlanner, PlannerDecision, PlannerError
from jev_frontend_qa.core.progress import ProgressMemory, semantic_state


class ScriptedPlanner:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def choose(self, **kwargs):
        self.calls.append(kwargs)
        return PlannerDecision(**next(self.decisions))


def planned(monkeypatch, proposals, decisions=(), browser=None, state=None, permissions=None):
    browser = browser or MemoryBrowser()
    state = state or page()
    planner = ScriptedPlanner(proposals)
    provider = RecordingProvider(decisions)
    monkeypatch.setattr(agent, "read_snapshot", lambda transport: state)
    # Recovery decisions depend on semantic observations, not real time in this seam.
    monkeypatch.setattr(Runner, "_settle_action", lambda self, before: state)
    outcome = Runner(browser, provider, permissions or policy(allow_planner=True), scenario(), planner=planner).run()
    return outcome.report, browser, planner, provider


def test_host_disclosure_denial_never_calls_planner(monkeypatch):
    report, browser, planner, _ = planned(monkeypatch, [], permissions=policy())
    assert report.verdict == "blocked"
    assert not planner.calls and browser.clicks == 0


def test_planner_cannot_invent_navigation_even_on_allowed_origin(monkeypatch):
    report, browser, _, _ = planned(monkeypatch, [{"operation": "navigate", "url": ORIGIN + "/guessed"}])
    assert report.verdict == "blocked"
    assert browser.navigations == [ORIGIN + "/"]


def test_observed_navigation_is_allowed_but_never_becomes_pass(monkeypatch):
    destination = ORIGIN + "/observed"
    report, browser, _, _ = planned(
        monkeypatch,
        [
            {"operation": "navigate", "url": destination},
            {"operation": "complete", "reason": "Observed only"},
        ],
        state=page(links=({"url": destination, "label": "Details"},)),
    )
    assert browser.navigations[-1] == destination
    assert report.verdict == "complete"
    assert not any(step.assertions for step in report.steps)


def test_wait_checkpoint_returns_control_to_planner_without_resetting_progress(monkeypatch):
    state = page(actions=({"id": "wait", "kind": "wait", "label": "Wait"},))
    report, browser, planner, _ = planned(
        monkeypatch,
        [
            {"operation": "act", "goal": "Wait for result"},
            {"operation": "complete", "reason": "Result did not change"},
        ],
        [choice("WAIT", "wait"), choice("WAIT", "wait")],
        state=state,
        permissions=policy(allow_planner=True, allow_action_history=True),
    )
    assert report.verdict == "complete" and browser.clicks == 0
    assert report.exploration[0].status == "checkpoint"
    previous = planner.calls[1]["history"][0]
    assert previous["goal"] == "Wait for result"
    assert previous["status"] == "checkpoint"
    assert previous["note"].startswith("Checkpoint:")
    assert previous["actions"] == [{"operation": "WAIT", "label": "Wait"}] * 2
    assert "observation" not in previous
    assert report.exploration[0].observation["page"]["text"] == state.text


def test_stale_observation_refreshes_before_input_but_ambiguous_input_never_retries(monkeypatch):
    browser = MemoryBrowser()
    browser.guard_ok = False
    report, browser, planner, _ = planned(
        monkeypatch,
        [
            {"operation": "act", "goal": "Click observed control"},
            {"operation": "blocked", "reason": "Control keeps changing"},
        ],
        [choice("CLICK", "e1"), choice("CLICK", "e1")],
        browser=browser,
    )
    assert report.verdict == "blocked" and browser.clicks == 0
    assert report.exploration[0].status == "checkpoint"
    assert len(planner.calls) == 2

    browser = MemoryBrowser()

    def interrupted(current):
        raise TimeoutError("input interrupted after press")

    browser.on_click = interrupted
    report, browser, planner, provider = planned(
        monkeypatch,
        [
            {"operation": "act", "goal": "Submit once"},
            {"operation": "act", "goal": "Retry"},
        ],
        [choice("CLICK", "e1"), choice("CLICK", "e1")],
        browser=browser,
    )
    assert report.verdict == "blocked"
    assert browser.clicks == len(planner.calls) == len(provider.calls) == 1


def test_semantic_memory_survives_node_churn_without_hiding_value_changes():
    before = page()
    after = replace(
        before,
        fingerprint="changed-id",
        guards={"99": ["different"]},
        actions=tuple({**action, "id": "e99", "node": 99, "rect": {"x": 999}} for action in before.actions),
    )
    assert semantic_state(before) == semantic_state(after)
    memory = ProgressMemory()
    memory.record(before, before.actions[0], after)
    memory.record(after, after.actions[0], before)
    assert memory.exhausted(after, after.actions[0])
    changed = replace(after, text="New result")
    assert semantic_state(changed) != semantic_state(after)


@pytest.mark.parametrize(
    "output", ["[]", '{"type":"item.completed","item":[]}', '{"type":"turn.completed","usage":[]}']
)
def test_malformed_host_envelopes_are_rejected(output):
    with pytest.raises(TypeError):
        HostPlanner("codex")._parse_output(output)


def test_host_tool_attempt_is_not_accepted_as_a_plan():
    event = {"type": "item.completed", "item": {"type": "command_execution", "command": "unsafe"}}
    with pytest.raises(PlannerError):
        HostPlanner("codex")._parse_output(json.dumps(event))


def test_alternating_meaningful_pages_cannot_cycle_indefinitely():
    memory = ProgressMemory()
    states = [page(text="First tab"), page(text="Second tab")]
    checkpoints = []
    for index in range(6):
        before, after = states[index % 2], states[(index + 1) % 2]
        action = {**before.actions[0], "label": "Next" if index % 2 == 0 else "Previous"}
        checkpoints.append(memory.record(before, action, after))
    assert any(checkpoints)


def test_scroll_geometry_without_new_content_reaches_checkpoint():
    memory = ProgressMemory()
    action = {"kind": "scroll", "label": "Scroll down", "delta": 100}
    checkpoints = []
    for index in range(3):
        before = page(scroll={"y": index * 100, "height": 2000})
        after = replace(before, scroll={"y": (index + 1) * 100, "height": 2000})
        checkpoints.append(memory.record(before, action, after))
    assert checkpoints[-1] is not None


def test_observation_only_planner_saves_one_step_screenshot(tmp_path, monkeypatch):
    browser = MemoryBrowser()

    def screenshot(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test screenshot evidence")

    browser.screenshot = screenshot
    monkeypatch.setattr(agent, "read_snapshot", lambda transport: page())
    outcome = Runner(
        browser,
        RecordingProvider([]),
        policy(allow_planner=True, allow_screenshots=True),
        scenario(),
        planner=ScriptedPlanner([{"operation": "complete", "reason": "Observed only"}]),
        screenshot_dir=tmp_path / "shots",
        screenshot_mode="steps",
    ).run()
    assert outcome.report.verdict == "complete"
    assert len(outcome.report.screenshots) == 1


def test_completed_progress_survives_recent_history_without_replaying_page_snapshots(monkeypatch):
    state = page(text="A large observed page " * 200, links=({"url": ORIGIN + "/details", "label": "Details"},))
    report, browser, planner, _ = planned(
        monkeypatch,
        [
            {"operation": "act", "goal": "Create the item", "reason": "Exercise creation"},
            *[{"operation": "observe", "reason": "Inspect result"} for _ in range(8)],
            {"operation": "complete", "reason": "Exploration finished"},
        ],
        [choice("CLICK", "e1"), choice()],
        state=state,
        permissions=policy(allow_planner=True, allow_action_history=True),
    )
    assert report.verdict == "complete" and browser.clicks == 1
    progress = planner.calls[1]["history"][0]
    assert progress["goal"] == "Create the item"
    assert progress["status"] == "complete"
    assert progress["reason"] == "Exercise creation"
    assert progress["url"] == state.url
    assert progress["actions"] == [{"operation": "CLICK", "label": "Create"}]
    assert planner.calls[-1]["observation"]["milestones"] == [
        {"goal": "Create the item", "status": "complete", "url": state.url}
    ]
    for call in planner.calls:
        for previous in call["history"]:
            assert not {"observation", "page", "controls", "links", "text", "milestones"} & previous.keys()
    assert all(previous["operation"] == "observe" for previous in planner.calls[-1]["history"])
    assert report.exploration[0].observation["page"]["text"] == state.text
    assert report.exploration[1].observation["links"] == list(state.links)
    assert report.exploration[1].observation["text"] == state.text


def test_planner_retains_field_state_and_blockers_while_summarizing_offscreen_controls(monkeypatch):
    invalid = {
        "valid": False,
        "message": "Enter a title",
        "value_missing": True,
        "too_long": False,
        "pattern_mismatch": False,
    }
    field = {
        "node": 10,
        "id": "e10",
        "role": "textbox",
        "label": "Title",
        "control_label": "Title",
        "fixture_key": "title",
        "value": "",
        "context": ["Create item"],
        "availability": "available",
        "focused": False,
        "unsupported": False,
        "validation": invalid,
        "rect": {"x": 20},
        "accessibility": {"backend_node_id": 10},
    }
    controls = (
        field,
        {"role": "button", "label": "Submit", "availability": "disabled", "fixture_key": None},
        {"role": "button", "label": "Delete", "availability": "occluded"},
        {"role": "tab", "label": "Details", "availability": "available", "selected": False, "expanded": "false"},
        {
            "role": "textbox",
            "label": "Search",
            "availability": "available",
            "value": "query",
            "validation": {"valid": True, "message": "", "value_missing": False},
        },
        {"role": "link", "label": "Below the fold", "availability": "offscreen"},
        {"role": "link", "label": "More below", "availability": "offscreen"},
        {**field, "label": "Hidden required title", "availability": "offscreen"},
    )
    diagnostics = {"backend": "accessibility", "unsupported_widgets": ["canvas"]}
    report, _, planner, _ = planned(
        monkeypatch,
        [{"operation": "observe"}, {"operation": "complete"}],
        state=page(controls=controls, diagnostics=diagnostics),
    )
    observed = planner.calls[0]["observation"]
    projected = {control["label"]: control for control in observed["controls"]}
    assert projected["Title"] == {
        "role": "textbox",
        "label": "Title",
        "fixture_key": "title",
        "value": "",
        "context": ["Create item"],
        "availability": "available",
        "validation": {"valid": False, "message": "Enter a title", "value_missing": True},
    }
    assert projected["Submit"]["availability"] == "disabled"
    assert projected["Delete"]["availability"] == "occluded"
    assert projected["Details"]["selected"] is False
    assert projected["Details"]["expanded"] == "false"
    assert projected["Search"]["value"] == "query"
    assert "validation" not in projected["Search"]
    assert projected["Hidden required title"]["validation"]["valid"] is False
    assert "Below the fold" not in projected and "More below" not in projected
    assert observed["offscreen_controls"] == {"link": 2}
    assert observed["limits"]["offscreen_controls_summarized"] == 2
    assert observed["diagnostics"]["unsupported_widgets"] == ["canvas"]
    assert report.exploration[0].observation["controls"][0]["validation"] == invalid
    assert report.exploration[0].observation["controls"][-2]["label"] == "More below"


def test_truncated_planner_text_can_be_discovered_by_read_without_losing_report_evidence(monkeypatch):
    from jev_frontend_qa.core import page_reader

    text = "Visible content " * 250 + "Details beyond the planner preview"
    destination = ORIGIN + "/discovered-by-reading"
    reading = {
        "url": ORIGIN + "/",
        "text": "Details beyond the planner preview",
        "links": [{"url": destination, "label": "Read details"}],
        "scan_limited": False,
    }
    reads = []

    def read_page(transport, *, query, offset):
        reads.append((query, offset))
        return reading

    monkeypatch.setattr(page_reader, "read_page", read_page)
    report, browser, planner, _ = planned(
        monkeypatch,
        [
            {"operation": "observe"},
            {"operation": "read", "query": "Details"},
            {"operation": "navigate", "url": destination},
            {"operation": "complete"},
        ],
        state=page(text=text),
        permissions=policy(allow_planner=True, allow_action_history=True),
    )
    observed = planner.calls[0]["observation"]
    assert observed["text"] == text[:3000]
    assert observed["limits"]["text_truncated"] is True
    assert observed["limits"]["snapshot_text_truncated"] is False
    assert observed["limits"]["text_characters"] == len(text)
    assert observed["limits"]["text_characters_shown"] == 3000
    assert reads == [("Details", 0)]
    assert planner.calls[2]["observation"]["reading"] == reading
    assert all("observation" not in previous for previous in planner.calls[2]["history"])
    assert destination in browser.navigations
    assert report.verdict == "complete"
    assert report.exploration[0].observation["text"] == text
    assert report.exploration[1].observation == reading
