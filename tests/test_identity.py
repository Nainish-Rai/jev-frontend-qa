"""Synthetic browser ownership and fail-closed boundary regressions.

Real Chrome/sentinel acceptance is separate; these tests exercise the transport
against an in-memory CDP peer, never an existing user profile.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest

from jev_frontend_qa import cli
from jev_frontend_qa.core.browser import BrowserTransport, EvidenceDaemon, make_transport, validate_identity
from jev_frontend_qa.core.evidence import EvidenceCollector
from jev_frontend_qa.core.models import Policy, Scenario

ORIGIN = "http://127.0.0.1:8767"


def policy(*, attach=False, capture=None, screenshots=False):
    network = {
        "allowed_origins": [
            {"origin": ORIGIN, "methods": ["GET"], "path_prefix": "/"},
            {"origin": ORIGIN, "methods": ["POST", "DELETE"], "path_prefix": "/api/"},
        ]
    }
    if capture is not None:
        network["capture_origins"] = capture
    return Policy.model_validate(
        {
            "network": network,
            "identity": {"mode": "attach", "attach_profile_name": "synthetic-demo"} if attach else {"mode": "isolated"},
            "model_disclosure": {"allow_page_text": True, "allow_screenshots": screenshots},
        }
    )


class SyntheticCDP:
    def __init__(self):
        self.tabs = {
            "sentinel": {"url": "https://sentinel.invalid/", "title": "untouched", "text": "synthetic sentinel"}
        }
        self.calls = []
        self.body = {"body": base64.b64encode(b'{"todos": [{"title": "synthetic"}]}').decode(), "base64Encoded": True}
        self.fail_body = False

    async def send_raw(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append((method, params, session_id))
        if method == "Target.createTarget":
            self.tabs["owned"] = {"url": params["url"]}
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "owned-session"}
        if method == "Target.closeTarget":
            self.tabs.pop(params["targetId"], None)
            return {"success": True}
        if method == "Network.getResponseBody":
            if self.fail_body:
                raise RuntimeError("synthetic capture failure")
            await asyncio.sleep(0.01)
            return self.body
        return {}


async def daemon_with_policy(selected_policy=None):
    daemon = EvidenceDaemon()
    daemon.cdp = SyntheticCDP()
    daemon.stop = asyncio.Event()
    await daemon.attach_first_page()
    await daemon.handle(
        {"meta": "set_evidence_policy", "policy": (selected_policy or policy()).model_dump(mode="json")}
    )
    return daemon


def network_exchange(
    daemon, *, request_id="network-1", session="owned-session", method="GET", status=200, url=ORIGIN + "/api/todos"
):
    daemon._record_event(
        "Network.requestWillBeSent",
        {"requestId": request_id, "request": {"method": method, "url": url, "headers": {"x-synthetic": "fixture"}}},
        session,
    )
    daemon._record_event(
        "Network.responseReceived", {"requestId": request_id, "response": {"status": status, "headers": {}}}, session
    )
    daemon._record_event("Network.loadingFinished", {"requestId": request_id}, session)


@pytest.mark.parametrize(
    "selected,profile,endpoint",
    [
        (policy(), "synthetic-demo", "http://127.0.0.1:9222"),
        (policy(attach=True), None, "http://127.0.0.1:9222"),
        (policy(attach=True), "synthetic-demo", None),
        (policy(attach=True), "different", "http://127.0.0.1:9222"),
        (policy(attach=True), None, None),
    ],
)
def test_denied_identity_does_not_create_resources(tmp_path, selected, profile, endpoint):
    with pytest.raises(PermissionError):
        make_transport(
            policy=selected,
            evidence=EvidenceCollector(),
            workdir=tmp_path / "work",
            attach_profile=profile,
            cdp_url=endpoint,
        )
    assert not (tmp_path / "work").exists()


def test_attachment_needs_no_chrome_executable_or_profile_copy(tmp_path):
    transport = make_transport(
        policy=policy(attach=True),
        evidence=EvidenceCollector(),
        workdir=tmp_path,
        attach_profile="synthetic-demo",
        cdp_url="ws://127.0.0.1:9222/devtools/browser/synthetic",
    )
    try:
        assert transport.execution_mode == "attach"
        assert transport.chrome is None
        assert validate_identity(policy(attach=True), "synthetic-demo", transport.cdp_url) == "attach"
    finally:
        transport.close()


def test_isolated_runs_never_reuse_profile_or_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("BU_NAME", "personal")
    monkeypatch.setenv("BH_RUNTIME_DIR", "/synthetic-parent-runtime")
    first = make_transport(
        policy=policy(),
        evidence=EvidenceCollector(),
        workdir=tmp_path,
        chrome_executable="synthetic-chrome",
        bu_name="qa",
    )
    second = make_transport(
        policy=policy(),
        evidence=EvidenceCollector(),
        workdir=tmp_path,
        chrome_executable="synthetic-chrome",
        bu_name="qa",
    )
    try:
        assert first.chrome.user_data_dir != second.chrome.user_data_dir
        assert first.runtime_dir != second.runtime_dir
        assert first.bu_name != second.bu_name
        import os

        assert os.environ["BU_NAME"] == "personal"
        assert os.environ["BH_RUNTIME_DIR"] == "/synthetic-parent-runtime"
    finally:
        first.close()
        second.close()


def test_daemon_owns_only_new_tab_and_cleanup_preserves_sentinel():
    async def exercise():
        daemon = await daemon_with_policy()
        sentinel = dict(daemon.cdp.tabs["sentinel"])
        denied = await daemon.handle({"method": "Target.closeTarget", "params": {"targetId": "sentinel"}})
        assert "error" in denied
        await daemon.handle({"method": "Target.closeTarget", "params": {"targetId": "owned"}})
        assert daemon.cdp.tabs == {"sentinel": sentinel}
        assert not any(method == "Target.getTargets" for method, _, _ in daemon.cdp.calls)

    asyncio.run(exercise())


def test_capture_waits_for_body_and_ignores_sentinel_session():
    async def exercise():
        daemon = await daemon_with_policy()
        network_exchange(daemon, session="sentinel-session", request_id="private")
        network_exchange(daemon)
        result = await daemon.handle({"meta": "drain_evidence"})
        assert result["lost"] == 0
        assert [record["request_id"] for record in result["records"]] == ["network-1"]
        assert result["records"][0]["response_body"] == {"todos": [{"title": "synthetic"}]}
        assert result["records"][0]["incomplete"] is False

    asyncio.run(exercise())


def test_capture_error_cannot_produce_complete_evidence():
    async def exercise():
        daemon = await daemon_with_policy()
        daemon.cdp.fail_body = True
        network_exchange(daemon)
        result = await daemon.handle({"meta": "drain_evidence"})
        assert result["records"][0]["incomplete"] is True

    asyncio.run(exercise())


def test_pending_capture_deadline_is_visible():
    async def exercise():
        daemon = await daemon_with_policy()
        daemon._record_event(
            "Network.requestWillBeSent",
            {"requestId": "stalled", "request": {"method": "GET", "url": ORIGIN + "/api/todos"}},
            "owned-session",
        )
        result = await daemon.handle({"meta": "drain_evidence", "settle_seconds": 0.001})
        assert result["lost"] > 0
        assert result["records"][0]["incomplete"] is True

    asyncio.run(exercise())


@pytest.mark.parametrize("method,status", [("DELETE", 204), ("GET", 205)])
def test_empty_success_does_not_require_response_body(method, status):
    async def exercise():
        daemon = await daemon_with_policy()
        daemon.cdp.fail_body = True
        network_exchange(daemon, method=method, status=status)
        result = await daemon.handle({"meta": "drain_evidence"})
        assert result["records"][0]["incomplete"] is False
        assert not any(call[0] == "Network.getResponseBody" for call in daemon.cdp.calls)

    asyncio.run(exercise())


def test_policy_rules_do_not_form_method_path_cross_product():
    async def exercise():
        daemon = await daemon_with_policy()
        await daemon._handle_fetch(
            {
                "requestId": "fetch-denied",
                "networkId": "network-denied",
                "request": {"method": "POST", "url": ORIGIN + "/admin", "postData": "never retained"},
            },
            "owned-session",
        )
        result = await daemon.handle({"meta": "drain_evidence"})
        assert any(call[0] == "Fetch.failRequest" for call in daemon.cdp.calls)
        assert not any(call[0] == "Fetch.continueRequest" for call in daemon.cdp.calls)
        assert result["records"][0]["request_body"] is None
        assert result["records"][0]["incomplete"] is True

    asyncio.run(exercise())


def test_capture_filter_does_not_retain_other_allowed_content():
    async def exercise():
        selected = policy(capture=[{"origin": ORIGIN, "methods": ["GET"], "path_prefix": "/api/"}])
        daemon = await daemon_with_policy(selected)
        network_exchange(daemon, url=ORIGIN + "/private-page")
        result = await daemon.handle({"meta": "drain_evidence"})
        assert result["records"] == []
        assert not any(call[0] == "Network.getResponseBody" for call in daemon.cdp.calls)

    asyncio.run(exercise())


def test_popup_escape_is_closed_without_touching_sentinel():
    async def exercise():
        daemon = await daemon_with_policy()
        daemon.cdp.tabs["popup"] = {"url": "about:blank"}
        daemon._record_event("Target.targetCreated", {"targetInfo": {"targetId": "popup", "openerId": "owned"}})
        daemon._record_event(
            "Target.targetCreated", {"targetInfo": {"targetId": "sentinel", "openerId": "someone-else"}}
        )
        result = await daemon.handle({"meta": "drain_evidence"})
        assert "popup" not in daemon.cdp.tabs
        assert daemon.cdp.tabs["sentinel"]["title"] == "untouched"
        assert result["lost"] > 0

    asyncio.run(exercise())


def transport_without_browser(tmp_path, selected=None):
    return BrowserTransport(
        selected or policy(),
        EvidenceCollector(),
        "synthetic",
        tmp_path / "runtime",
        tmp_path / "tmp",
        tmp_path / "workspace",
        None,
        execution_mode="attach",
    )


def test_expired_deadline_prevents_ipc_contact(tmp_path):
    transport = transport_without_browser(tmp_path)
    transport.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        transport._ipc_call({"meta": "ping"})


def test_runtime_evaluation_reads_actual_cdp_value(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: {"result": {"type": "string", "value": "complete"}})
    assert transport.evaluate_js("document.readyState") == "complete"
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: {"exceptionDetails": {"text": "synthetic"}})
    with pytest.raises(RuntimeError):
        transport.evaluate_js("broken()")


def test_fresh_read_rejects_cross_origin_before_page_fetch(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    monkeypatch.setattr(transport, "evaluate_js", lambda *args, **kwargs: pytest.fail("Page must not be contacted"))
    with pytest.raises(PermissionError):
        transport.fresh_read("//forbidden.invalid/api/todos")


def test_fresh_read_requires_successful_json_response(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    responses = iter([ORIGIN + "/", {"ok": False, "status": 503}])
    monkeypatch.setattr(transport, "evaluate_js", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: {})
    with pytest.raises(RuntimeError):
        transport.fresh_read("/api/todos")


def test_screenshots_require_policy_and_are_private(tmp_path, monkeypatch):
    path = tmp_path / "shot.png"
    transport = transport_without_browser(tmp_path)
    with pytest.raises(PermissionError):
        transport.screenshot(path)
    assert not path.exists()
    transport.policy = policy(screenshots=True)
    monkeypatch.setattr(
        transport, "_cdp", lambda *args, **kwargs: {"data": base64.b64encode(b"synthetic PNG").decode()}
    )
    transport.screenshot(path)
    assert path.read_bytes() == b"synthetic PNG"
    assert path.stat().st_mode & 0o777 == 0o600


def cli_fixture(tmp_path, monkeypatch, *, attach=False):
    scenario = Scenario.model_validate(
        {
            "name": "synthetic",
            "start_url": ORIGIN,
            "steps": [
                {
                    "id": "read",
                    "goal": "Read synthetic todos",
                    "assertions": [{"kind": "contains", "selector": {"css": "body"}, "expected": "Todos"}],
                }
            ],
        }
    )
    selected = policy(attach=attach)
    monkeypatch.setattr(cli, "load_scenario", lambda _: scenario)
    monkeypatch.setattr(cli, "load_policy", lambda _: selected)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *args, **kwargs: SimpleNamespace(
            chrome_executable="synthetic-chrome",
            chrome_headless=False,
            typesafe_model="jev-1.13.0",
            work_dir=tmp_path / "work",
            bu_name=None,
        ),
    )
    argv = [
        "run",
        "--scenario",
        "synthetic.json",
        "--policy",
        "synthetic-policy.json",
        "--report",
        str(tmp_path / "report.json"),
    ]
    return argv


def test_cli_missing_key_blocks_before_browser_launch(tmp_path, monkeypatch):
    argv = cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_read_api_key", lambda: None)
    monkeypatch.setattr(cli, "make_transport", lambda **kwargs: pytest.fail("Must not launch before key preflight"))
    assert cli.main(argv) == cli.EXIT_BLOCKED
    result = json.loads((tmp_path / "report.json").read_text())
    assert result["verdict"] == "blocked"
    assert result["execution_mode"] == "isolated"


def test_cli_denied_attachment_reports_mode_before_endpoint_contact(tmp_path, monkeypatch):
    argv = cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "make_transport", lambda **kwargs: pytest.fail("Must not contact denied endpoint"))
    assert (
        cli.main(argv + ["--attach-profile", "synthetic-demo", "--cdp-url", "http://127.0.0.1:9222"])
        == cli.EXIT_BLOCKED
    )
    assert json.loads((tmp_path / "report.json").read_text())["execution_mode"] == "attach"


def test_cli_closes_client_and_transport_on_startup_failure(tmp_path, monkeypatch):
    argv = cli_fixture(tmp_path, monkeypatch, attach=True)
    closed = []

    class FailingTransport:
        def start(self):
            raise RuntimeError("synthetic secret MUST NOT APPEAR")

        def close(self):
            closed.append("transport")

    monkeypatch.setattr(cli, "_read_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(cli, "ModelClient", lambda **kwargs: SimpleNamespace(close=lambda: closed.append("client")))
    monkeypatch.setattr(cli, "make_transport", lambda **kwargs: FailingTransport())
    assert (
        cli.main(argv + ["--attach-profile", "synthetic-demo", "--cdp-url", "http://127.0.0.1:9222"]) == cli.EXIT_ERROR
    )
    assert closed == ["transport", "client"]
    assert "MUST NOT APPEAR" not in (tmp_path / "report.json").read_text()


def test_headed_and_headless_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--scenario", "s.json", "--policy", "p.json", "--headed", "--headless"])


def test_attached_transport_cleanup_is_owned_and_idempotent(tmp_path, monkeypatch):
    transport = transport_without_browser(tmp_path)
    transport._target_id = "owned"
    transport._session_id = "owned-session"
    transport.process = SimpleNamespace(poll=lambda: 0)
    requests = []
    monkeypatch.setattr(transport, "_ipc_call", lambda request, **kwargs: requests.append(request) or {"ok": True})
    transport.close()
    transport.close()
    assert requests == [
        {"method": "Target.closeTarget", "params": {"targetId": "owned"}},
        {"meta": "shutdown"},
    ]


def test_fetch_body_uses_network_id_not_fetch_id():
    async def exercise():
        daemon = await daemon_with_policy()
        daemon._record_event(
            "Network.requestWillBeSent",
            {"requestId": "network", "request": {"method": "POST", "url": ORIGIN + "/api/todos", "hasPostData": True}},
            "owned-session",
        )
        await daemon._handle_fetch(
            {
                "requestId": "fetch",
                "networkId": "network",
                "request": {"method": "POST", "url": ORIGIN + "/api/todos", "postData": '{"title":"synthetic"}'},
            },
            "owned-session",
        )
        daemon._record_event(
            "Network.responseReceived",
            {"requestId": "network", "response": {"status": 201, "headers": {}}},
            "owned-session",
        )
        daemon._record_event("Network.loadingFinished", {"requestId": "network"}, "owned-session")
        result = await daemon.handle({"meta": "drain_evidence"})
        assert [record["request_id"] for record in result["records"]] == ["network"]
        assert result["records"][0]["request_body"] == {"title": "synthetic"}
        assert result["records"][0]["incomplete"] is False

    asyncio.run(exercise())


def test_fresh_read_obeys_capture_policy(tmp_path, monkeypatch):
    selected = policy(capture=[{"origin": ORIGIN, "methods": ["GET"], "path_prefix": "/public/"}])
    transport = transport_without_browser(tmp_path, selected)
    monkeypatch.setattr(transport, "evaluate_js", lambda *args, **kwargs: ORIGIN + "/")
    monkeypatch.setattr(transport, "_cdp", lambda *args, **kwargs: pytest.fail("Must not fetch disallowed evidence"))
    with pytest.raises(PermissionError):
        transport.fresh_read("/api/todos")
