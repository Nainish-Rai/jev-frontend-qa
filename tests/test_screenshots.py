"""Screenshot evidence respects capture permissions and preserves QA verdicts."""

import base64
from pathlib import Path

import pytest
from test_exploration import MemoryBrowser, RecordingProvider, choice, page, policy, scenario
from test_identity import ORIGIN, transport_without_browser
from test_identity import policy as transport_policy

from jev_frontend_qa.core import agent
from jev_frontend_qa.core.agent import Runner


@pytest.mark.parametrize("capture", [False, True])
def test_capture_is_gated_without_changing_contract_result(tmp_path, monkeypatch, capture):
    browser = MemoryBrowser()
    browser.evaluate_js = lambda expression: ["expected"]

    def screenshot(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image evidence")

    browser.screenshot = screenshot
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "read",
                "goal": "Inspect",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )
    outcome = Runner(
        browser,
        RecordingProvider([choice()]),
        policy(allow_screenshots=capture),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    assert outcome.report.verdict == "pass"
    if capture:
        assert [record.phase for record in outcome.report.screenshots] == ["initial", "step_pass"]
        assert all(Path(record.path).read_bytes() == b"image evidence" for record in outcome.report.screenshots)
    else:
        assert not (tmp_path / "shots").exists() and not outcome.report.screenshots


def test_capture_failure_preserves_failed_assertions_without_leaking_exception(tmp_path, monkeypatch):
    browser = MemoryBrowser()
    browser.evaluate_js = lambda expression: ["wrong"]

    def screenshot(path):
        raise OSError("private screenshot path or page detail")

    browser.screenshot = screenshot
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "read",
                "goal": "Inspect",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )
    report = (
        Runner(browser, RecordingProvider([choice()]), policy(allow_screenshots=True), spec, screenshot_dir=tmp_path)
        .run()
        .report
    )
    assert report.verdict == "fail" and not report.steps[0].assertions[0].passed
    assert not report.screenshots
    assert any("Screenshot unavailable" in note for note in report.findings["missing_evidence"])
    assert "private screenshot" not in report.model_dump_json()


def test_capture_denied_for_nonapproved_page_and_capture_origin(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    transport.policy = transport_policy(screenshots=True)
    monkeypatch.setattr(transport, "evaluate_js", lambda _: "https://unapproved.invalid/")
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: pytest.fail("Must not capture pixels"))
    with pytest.raises(PermissionError):
        transport.screenshot(tmp_path / "denied.png")
    transport.policy = transport_policy(screenshots=True, capture=[])
    monkeypatch.setattr(transport, "evaluate_js", lambda _: ORIGIN)
    with pytest.raises(PermissionError):
        transport.screenshot(tmp_path / "denied.png")
    assert not (tmp_path / "denied.png").exists()


def test_screenshot_never_overwrites_or_follows_symlink_parent(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    transport.policy = transport_policy(screenshots=True)
    monkeypatch.setattr(transport, "evaluate_js", lambda _: ORIGIN)
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: {"data": base64.b64encode(b"pixels").decode()})
    original = tmp_path / "original.png"
    original.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        transport.screenshot(original)
    assert original.read_bytes() == b"keep"
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        transport.screenshot(tmp_path / "link" / "new.png")
    assert not (real / "new.png").exists()


# ---------------------------------------------------------------------------
# Capture lifecycle: dedup, phase metadata, and reload bookkeeping
# ---------------------------------------------------------------------------


def _action_browser(tmp_path):
    """A MemoryBrowser whose ``evaluate_js`` answers both the guard script
    and the assertion DOM read; required for CLICK-bearing steps."""

    browser = MemoryBrowser()
    answers = {"dom": ["expected"]}

    def evaluate_js(expression, **kwargs):
        if "location.href" in expression:
            return ORIGIN
        if "document.querySelectorAll" in expression:
            return answers["dom"]
        if "const c=window.__jevFast" in expression:
            return {"x": 20, "y": 30}
        return None

    browser.evaluate_js = evaluate_js

    def screenshot(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image evidence")

    browser.screenshot = screenshot
    return browser


def test_terminal_capture_is_suppressed_when_after_action_already_recorded(tmp_path, monkeypatch):
    """A step with actions must not duplicate the terminal capture on top of
    the post-action capture; assertion evaluation never mutates the page."""

    browser = _action_browser(tmp_path)
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    provider = RecordingProvider([choice("CLICK", "e1"), choice()])
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "interact",
                "goal": "Interact",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )
    outcome = Runner(
        browser,
        provider,
        policy(allow_screenshots=True),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    assert outcome.report.verdict == "pass"
    phases = [record.phase for record in outcome.report.screenshots]
    # The terminal step_pass capture is suppressed because the last capture
    # for this step was after_action; only one PNG survives per lifecycle moment.
    assert phases == ["initial", "after_action"]


def test_terminal_capture_is_kept_after_reload_intervened(tmp_path, monkeypatch):
    """A reload changes the page, so the post-reload terminal capture is
    meaningful and must not be deduplicated against the prior after_action."""

    browser = _action_browser(tmp_path)
    browser.fresh_payloads = {"/api/todos": [{"title": "qa"}]}
    browser.pending.append(
        {
            "method": "POST",
            "url": ORIGIN + "/api/todos",
            "status": 201,
            "request_body": {"title": "qa"},
            "response_body": {"title": "qa"},
        }
    )
    provider = RecordingProvider([choice("CLICK", "e1"), choice()])
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "create",
                "goal": "Create todo",
                "assertions": [
                    {"kind": "status", "method": "POST", "path": "/api/todos", "expected_status": 201},
                    {"kind": "persistence", "path": "/api/todos", "contains": {"title": "qa"}},
                ],
                "reload_after": True,
            }
        ],
    )
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    outcome = Runner(
        browser,
        provider,
        policy(allow_screenshots=True),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    phases = [record.phase for record in outcome.report.screenshots]
    assert "before_reload" in phases
    assert "after_reload" in phases
    # Reload separated the post-action view from the terminal view.
    assert phases[-1].startswith("step_")


def test_screenshot_records_carry_action_index_and_step_metadata(tmp_path, monkeypatch):
    browser = _action_browser(tmp_path)
    # Each click changes the DOM so the runner does not declare the contract
    # satisfied on the first dispatch and emits a second after_action capture.
    states = iter([["step-1"], ["step-2"], ["step-2"]])

    def page_with_state(_):
        return page()

    monkeypatch.setattr(agent, "read_snapshot", page_with_state)

    def evaluate_js(expression, **kwargs):
        if "location.href" in expression:
            return ORIGIN
        if "document.querySelectorAll" in expression:
            return next(states)
        if "const c=window.__jevFast" in expression:
            return {"x": 20, "y": 30}
        return None

    browser.evaluate_js = evaluate_js
    provider = RecordingProvider([choice("CLICK", "e1"), choice("CLICK", "e1"), choice()])
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "interact",
                "goal": "Interact twice",
                "assertions": [
                    {"kind": "equals", "selector": {"css": "h1"}, "expected": "step-2"},
                ],
            }
        ],
    )
    outcome = Runner(
        browser,
        provider,
        policy(allow_screenshots=True),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    # after_action captures record the action index after dispatch so the
    # report is sortable by click sequence; the initial capture has no
    # context and stays step=None, action_index=None.
    initial = next(record for record in outcome.report.screenshots if record.phase == "initial")
    assert initial.step is None and initial.action_index is None
    after_actions = [record for record in outcome.report.screenshots if record.phase == "after_action"]
    assert [record.action_index for record in after_actions] == [1, 2]
    assert all(record.step == "interact" for record in after_actions)
    assert all(record.captured_at_ms > 0 for record in outcome.report.screenshots)


def test_screenshot_filenames_are_unique_counters_without_user_text(tmp_path, monkeypatch):
    browser = MemoryBrowser()
    browser.evaluate_js = lambda expression: ["expected"]
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    provider = RecordingProvider([choice()])
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "secret step id",
                "goal": "Inspect",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )

    def screenshot(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image evidence")

    browser.screenshot = screenshot
    outcome = Runner(
        browser,
        provider,
        policy(allow_screenshots=True),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    filenames = [Path(record.path).name for record in outcome.report.screenshots]
    assert len(filenames) == len(set(filenames))
    for name in filenames:
        assert name.endswith(".png")
        # Counter prefix + underscore phase; never contains the step id or
        # any caller-controlled text.
        prefix, phase = name[:-4].split("-", 1)
        assert prefix.isdigit() and len(prefix) == 4
        assert "secret" not in name
        assert phase in {
            "initial",
            "after_action",
            "before_reload",
            "after_reload",
            "step_pass",
            "step_fail",
            "step_blocked",
            "step_complete",
            "step_error",
        }


def test_policy_blocked_from_transport_is_recorded_as_safe_authored_note(tmp_path, monkeypatch):
    """The browser raises PermissionError (PolicyBlocked) on non-approved
    origins; the runner must catch it and write only a safe authored note."""

    browser = MemoryBrowser()
    browser.evaluate_js = lambda expression: ["expected"]
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())

    def screenshot(path):
        # Simulate the browser's PolicyBlocked by raising PermissionError;
        # the runner should treat it the same as OSError.
        raise PermissionError("internal path /private/secret should never appear in report")

    browser.screenshot = screenshot
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "read",
                "goal": "Inspect",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )
    outcome = Runner(
        browser,
        RecordingProvider([choice()]),
        policy(allow_screenshots=True),
        spec,
        screenshot_dir=tmp_path / "shots",
    ).run()
    assert outcome.report.verdict == "pass"
    notes = outcome.report.findings.get("missing_evidence", ())
    assert any("Screenshot unavailable" in note for note in notes)
    assert not any("private" in note or "secret" in note for note in notes)
    assert outcome.report.screenshots == ()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("actions", ["after_action"]), ("steps", ["step_pass"]), ("failures", [])],
)
def test_capture_modes_select_only_requested_states(tmp_path, monkeypatch, mode, expected):
    browser = _action_browser(tmp_path)
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "interact",
                "goal": "Interact",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
        ],
    )
    report = (
        Runner(
            browser,
            RecordingProvider([choice("CLICK", "e1"), choice()]),
            policy(allow_screenshots=True),
            spec,
            screenshot_dir=tmp_path / "shots",
            screenshot_mode=mode,
        )
        .run()
        .report
    )
    assert report.verdict == "pass"
    assert [record.phase for record in report.screenshots] == expected
    if not expected:
        assert not (tmp_path / "shots").exists()


def test_action_interval_spans_step_boundaries(tmp_path, monkeypatch):
    browser = _action_browser(tmp_path)
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": f"step{index}",
                "goal": "Interact",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "expected"}],
            }
            for index in range(1, 4)
        ],
    )
    report = (
        Runner(
            browser,
            RecordingProvider([choice("CLICK", "e1")] * 3),
            policy(allow_screenshots=True),
            spec,
            screenshot_dir=tmp_path / "shots",
            screenshot_mode="actions",
            screenshot_every=2,
        )
        .run()
        .report
    )
    assert report.verdict == "pass"
    assert [(record.step, record.action_index, record.phase) for record in report.screenshots] == [
        ("step2", 1, "after_action")
    ]


@pytest.mark.parametrize("blocked", [False, True])
def test_failure_mode_captures_failed_and_blocked_outcomes(tmp_path, monkeypatch, blocked):
    browser = _action_browser(tmp_path)
    monkeypatch.setattr(agent, "read_snapshot", lambda _: page())
    spec = scenario(
        mode="contract",
        steps=[
            {
                "id": "read",
                "goal": "Inspect",
                "assertions": [{"kind": "equals", "selector": {"css": "h1"}, "expected": "different"}],
            }
        ],
    )
    report = (
        Runner(
            browser,
            RecordingProvider([choice("BLOCKED" if blocked else "DONE")]),
            policy(allow_screenshots=True),
            spec,
            screenshot_dir=tmp_path / "shots",
            screenshot_mode="failures",
        )
        .run()
        .report
    )
    assert report.verdict == ("blocked" if blocked else "fail")
    assert [record.phase for record in report.screenshots] == [f"step_{report.verdict}"]
