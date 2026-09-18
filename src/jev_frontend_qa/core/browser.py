"""Owned Browser Harness sessions, policy-gated capture, and bounded IPC.

The daemon extends Browser Harness without modifying its installed modules. Each
run has a private endpoint and a new tab; an attached browser is never owned.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from browser_harness import _ipc as bh_ipc
from browser_harness.daemon import Daemon

from .evidence import EvidenceCollector
from .models import Policy
from .policy import PolicyEnforcer

EVIDENCE_BUFFER = 512
TRANSPORT_ERRORS = (OSError, RuntimeError, ValueError, TypeError, LookupError, subprocess.SubprocessError)


class PolicyBlocked(PermissionError):
    """An authored, safe-to-report permission denial, not an OS error."""


def validate_identity(policy: Policy, attach_profile: str | None, cdp_url: str | None) -> str:
    """Authorize identity selection without contacting any endpoint."""
    if attach_profile is None and cdp_url is None:
        if policy.identity.mode != "isolated":
            raise PolicyBlocked("Attachment requires explicit --attach-profile and --cdp-url")
        return "isolated"
    if not attach_profile or not cdp_url:
        raise PolicyBlocked("Attachment requires both --attach-profile and --cdp-url")
    if policy.identity.mode != "attach" or policy.identity.attach_profile_name != attach_profile:
        raise PolicyBlocked("Requested browser identity is not approved by policy")
    parts = urlsplit(cdp_url)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname or parts.fragment:
        raise PolicyBlocked("Invalid explicit CDP endpoint")
    if parts.username or parts.password:
        raise PolicyBlocked("CDP endpoint must not contain credentials")
    return "attach"


def _pick_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class ChromeLaunch:
    executable: str
    user_data_dir: Path
    remote_port: int
    headless: bool
    process: subprocess.Popen | None = None

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        self.user_data_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        args = [
            self.executable,
            f"--user-data-dir={self.user_data_dir}",
            f"--remote-debugging-port={self.remote_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        if self.headless:
            args.append("--headless=new")
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        finally:
            self.process = None


# A child window cannot become an unguarded execution surface. Reject common
# page-level escape routes before their default action; CDP catches new targets.
_POPUP_GUARD = """(() => {
  const deny = () => { window.__jev_unsupported('popup'); return null; };
  Object.defineProperty(window, 'open', {value: deny, writable: false, configurable: false});
  for (const event of ['click', 'submit']) document.addEventListener(event, e => {
    const node = event === 'click' ? e.target.closest?.('a,area') : e.target;
    const target = node?.getAttribute('target');
    if (target && !['_self', '_top', '_parent'].includes(target.toLowerCase())) {
      e.preventDefault(); e.stopImmediatePropagation(); deny();
    }
  }, true);
})()"""


class EvidenceDaemon(Daemon):
    """The upstream server with one owned tab and no personal-tab discovery."""

    def __init__(self) -> None:
        super().__init__()
        self._owned_session_id: str | None = None
        self._qa_collector = EvidenceCollector(buffer_size=EVIDENCE_BUFFER)
        self._capture_tasks: set[asyncio.Task] = set()
        self._requests: dict[str, tuple[str, str, int | None]] = {}
        self._policy = Policy()
        self._deadline: float | None = None

    async def attach_first_page(self, replaces_session=None, enable_domains=True):
        if self.dedicated_target_id:
            raise RuntimeError("Owned session was lost; automatic tab recovery is forbidden")
        target = await self.cdp.send_raw("Target.createTarget", {"url": "about:blank", "background": True})
        self.target_id = self.dedicated_target_id = target["targetId"]
        session = await self.cdp.send_raw("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})
        self.session = self._owned_session_id = session["sessionId"]
        return {"targetId": self.target_id}

    def _schedule(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_finished)

    def _capture_finished(self, task: asyncio.Task) -> None:
        self._capture_tasks.discard(task)
        if task.cancelled() or task.exception() is not None:
            self._qa_collector.mark_incomplete(None)

    def _captures(self, method: str, url: str) -> bool:
        if not PolicyEnforcer(self._policy).check_request(method, url).allowed:
            return False
        rules = self._policy.network.capture_origins
        if rules is None:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return any(
            str(rule.origin).rstrip("/") == origin
            and method.upper() in rule.methods
            and parts.path.startswith(rule.path_prefix)
            for rule in rules
        )

    async def handle(self, req):
        expected = bh_ipc.expected_token()
        if expected is not None and req.get("token") != expected:
            return {"error": "unauthorized"}
        meta = req.get("meta")
        if meta == "owned_session":
            return {"target_id": self.target_id, "session_id": self._owned_session_id}
        if meta == "set_evidence_policy":
            self._policy = Policy.model_validate(req["policy"])
            self._deadline = req.get("deadline")
            self._qa_collector.set_rules(
                [
                    {
                        "origin": str(rule.origin).rstrip("/"),
                        "methods": sorted(rule.methods),
                        "path_prefix": rule.path_prefix,
                    }
                    for rule in self._policy.network.allowed_origins
                ]
            )
            return {"ok": True}
        if meta == "drain_evidence":
            end = time.monotonic() + min(float(req.get("settle_seconds", 2)), 2)
            if self._deadline is not None:
                end = min(end, self._deadline)
            # A quiet turn is required even when no request has reached CDP yet.
            quiet_since = time.monotonic()
            while time.monotonic() < end:
                if self._capture_tasks or self._qa_collector.pending_count:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= 0.05:
                    break
                await asyncio.sleep(min(0.01, max(0, end - time.monotonic())))
            if self._capture_tasks or self._qa_collector.pending_count:
                self._qa_collector.mark_pending_incomplete()
                self._qa_collector.mark_incomplete(None)
                for task in tuple(self._capture_tasks):
                    task.cancel()
            records, lost = self._qa_collector.drain()
            return {"records": records, "lost": lost}
        if meta == "shutdown":
            for task in tuple(self._capture_tasks):
                task.cancel()
            self.stop.set()
            return {"ok": True}
        if meta == "owned_auto_attach":
            try:
                result = await self._send("Target.setAutoAttach", req["params"], self._owned_session_id)
                return {"result": result}
            except TRANSPORT_ERRORS:
                self._qa_collector.mark_incomplete(None)
                return {"error": "Cannot guard owned child targets"}
        if req.get("method") == "Target.closeTarget":
            if req.get("params", {}).get("targetId") != self.target_id:
                return {"error": "Target is not owned"}
            try:
                result = await asyncio.wait_for(
                    self.cdp.send_raw("Target.closeTarget", {"targetId": self.target_id}), timeout=2
                )
                self.dedicated_target_id = None
                return {"result": result}
            except TRANSPORT_ERRORS:
                return {"error": "Owned tab cleanup failed"}
        try:
            timeout = min(10, max(0, self._deadline - time.monotonic())) if self._deadline else 10
            return await asyncio.wait_for(super().handle(req), timeout=timeout)
        except TRANSPORT_ERRORS:
            self._qa_collector.mark_incomplete(None)
            return {"error": "Browser operation failed or exceeded its deadline"}

    def _record_event(self, method, params, session_id=None):
        params = params or {}
        # Browser-level discovery contains other tabs' metadata. Never retain it.
        if method == "Target.targetCreated":
            info = params.get("targetInfo", {})
            if info.get("openerId") == self.target_id:
                self._qa_collector.mark_incomplete(None)
                self._schedule(self._close_popup(info["targetId"]))
            return
        if session_id != self._owned_session_id:
            return
        try:
            if method == "Target.attachedToTarget":
                self._qa_collector.mark_incomplete(None)
                self._schedule(self._close_popup(params["targetInfo"]["targetId"]))
            elif method in {"Page.javascriptDialogOpening", "Runtime.bindingCalled"}:
                self._qa_collector.mark_incomplete(None)
                if method == "Page.javascriptDialogOpening":
                    self._schedule(self._dismiss_dialog(session_id))
            elif method == "Fetch.requestPaused":
                self._schedule(self._handle_fetch(params, session_id))
            elif method == "Network.requestWillBeSent":
                request = params.get("request", {})
                verb, url = request.get("method", "GET"), request.get("url", "")
                request_id = params["requestId"]
                self._requests.pop(request_id, None)
                if self._captures(verb, url):
                    self._requests[request_id] = (verb, url, None)
                    self._qa_collector.handle_event(method, params, session_id)
            elif params.get("requestId") in self._requests:
                request_id = params["requestId"]
                if method == "Network.responseReceived":
                    verb, url, _ = self._requests[request_id]
                    self._requests[request_id] = (verb, url, int(params["response"]["status"]))
                self._qa_collector.handle_event(method, params, session_id)
                if method == "Network.loadingFinished":
                    self._schedule(self._capture_body(request_id, session_id))
                elif method == "Network.loadingFailed":
                    self._requests.pop(request_id, None)
        except TRANSPORT_ERRORS:
            self._qa_collector.mark_incomplete(None)

    async def _send(self, method: str, params: dict, session_id=None):
        timeout = min(5, max(0, self._deadline - time.monotonic())) if self._deadline else 5
        try:
            return await asyncio.wait_for(self.cdp.send_raw(method, params, session_id=session_id), timeout)
        except Exception as error:
            raise RuntimeError("Browser protocol request failed") from error

    async def _close_popup(self, target_id: str) -> None:
        try:
            await self._send("Target.closeTarget", {"targetId": target_id})
        except TRANSPORT_ERRORS:
            self._qa_collector.mark_incomplete(None)

    async def _dismiss_dialog(self, session_id: str) -> None:
        try:
            await self._send("Page.handleJavaScriptDialog", {"accept": False}, session_id)
        except TRANSPORT_ERRORS:
            self._qa_collector.mark_incomplete(None)

    async def _handle_fetch(self, params: dict, session_id: str) -> None:
        request = params.get("request", {})
        method, url = request.get("method", "GET"), request.get("url", "")
        try:
            allowed = PolicyEnforcer(self._policy).check_request(method, url).allowed
            if not allowed:
                self._qa_collector.mark_denied(method, url)
                await self._send(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                    session_id,
                )
                return
            if self._captures(method, url):
                self._qa_collector.handle_event("Fetch.requestPaused", params, session_id)
                network_id = params.get("networkId")
                if network_id:
                    self._requests.setdefault(network_id, (method, url, None))
            await self._send("Fetch.continueRequest", {"requestId": params["requestId"]}, session_id)
        except TRANSPORT_ERRORS:
            # Fetch IDs are NOT Network IDs; only networkId can identify a record.
            self._qa_collector.mark_incomplete(params.get("networkId"))

    async def _capture_body(self, request_id: str, session_id: str) -> None:
        try:
            method, _, status = self._requests[request_id]
            if method == "HEAD" or status in {204, 205, 304}:
                body = ""
            else:
                response = await self._send("Network.getResponseBody", {"requestId": request_id}, session_id)
                body = response["body"]
                if response.get("base64Encoded"):
                    body = base64.b64decode(body, validate=True).decode("utf-8")
            self._qa_collector.attach_response_body(request_id, body)
        except TRANSPORT_ERRORS:
            self._qa_collector.mark_incomplete(request_id)
        finally:
            self._requests.pop(request_id, None)


def _daemon_subprocess_script() -> str:
    return (
        "import asyncio, os, signal\n"
        "from browser_harness.daemon import serve\n"
        "from jev_frontend_qa.core.browser import EvidenceDaemon\n"
        "async def main():\n"
        "    d = EvidenceDaemon()\n"
        "    task = asyncio.current_task()\n"
        "    if os.name == 'posix':\n"
        "        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)\n"
        "    try:\n"
        "        await asyncio.wait_for(d.start(), float(os.environ['JEV_START_TIMEOUT']))\n"
        "        await serve(d)\n"
        "    finally:\n"
        "        if d.dedicated_target_id and d.cdp:\n"
        "            try:\n"
        "                await asyncio.wait_for(d.cdp.send_raw('Target.closeTarget', {'targetId': d.dedicated_target_id}), 2)\n"
        "            except Exception: pass\n"
        "asyncio.run(main())\n"
    )


@dataclass
class BrowserTransport:
    policy: Policy
    evidence: EvidenceCollector
    bu_name: str
    runtime_dir: Path
    tmp_dir: Path
    workspace_dir: Path
    chrome: ChromeLaunch | None
    execution_mode: str = "isolated"
    cdp_url: str | None = None
    deadline: float | None = None
    cleanup_notes: tuple[str, ...] = ()
    process: subprocess.Popen | None = None
    _target_id: str | None = None
    _session_id: str | None = None
    _closed: bool = False

    def _remaining(self, maximum: float = 10) -> float:
        remaining = maximum if self.deadline is None else min(maximum, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Scenario deadline exceeded")
        return remaining

    def start(self) -> None:
        try:
            self._start()
        except BaseException:
            self.close()
            raise

    def _start(self) -> None:
        self._remaining()
        if self.chrome is not None:
            self.chrome.start()
        websocket = self._discover_cdp_ws()
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "PYTHONPATH", "SYSTEMROOT", "HOME", "LANG"}
        }
        env.update(
            {
                "BU_NAME": self.bu_name,
                "BU_CDP_WS": websocket,
                "BH_HOME": str(self.workspace_dir),
                "BH_RUNTIME_DIR": str(self.runtime_dir),
                "BH_TMP_DIR": str(self.tmp_dir),
                "BH_AGENT_WORKSPACE": str(self.workspace_dir),
                "BH_TAB_MARKER": "0",
                "PYTHONUNBUFFERED": "1",
                "JEV_START_TIMEOUT": str(self._remaining(20)),
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", _daemon_subprocess_script()],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        end = time.monotonic() + self._remaining(20)
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise RuntimeError("Browser Harness startup failed")
            try:
                if self._ipc_call({"meta": "ping"}, timeout=min(0.2, end - time.monotonic())).get("pong"):
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(min(0.05, self._remaining()))
        else:
            raise TimeoutError("Browser Harness startup deadline exceeded")
        owned = self._ipc_call({"meta": "owned_session"})
        self._target_id, self._session_id = owned["target_id"], owned["session_id"]
        self._ipc_call(
            {"meta": "set_evidence_policy", "policy": self.policy.model_dump(mode="json"), "deadline": self.deadline}
        )
        for domain in ("Page", "Runtime", "Network"):
            self._cdp(f"{domain}.enable")
        self._cdp("Network.setCacheDisabled", cacheDisabled=True)
        self._cdp("Network.setBypassServiceWorker", bypass=True)
        self._cdp("Runtime.addBinding", name="__jev_unsupported")
        self._cdp("Page.addScriptToEvaluateOnNewDocument", source=_POPUP_GUARD)
        self._cdp("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=True, flatten=True)
        self._ipc_call({"method": "Target.setDiscoverTargets", "params": {"discover": True}})
        self._cdp("Fetch.enable", patterns=[{"requestStage": "Request"}])
        # Hidden targets do not reliably acknowledge compositor input such as wheel events.
        self._cdp("Page.bringToFront")
        self.focus_emulation()

    def _discover_cdp_ws(self) -> str:
        endpoint = self.cdp_url or f"http://127.0.0.1:{self.chrome.remote_port}"
        if urlsplit(endpoint).scheme in {"ws", "wss"}:
            return endpoint
        end = time.monotonic() + self._remaining(10)

        # Do not follow a discovery redirect to another endpoint or inspect profiles.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        while time.monotonic() < end:
            try:
                with opener.open(endpoint.rstrip("/") + "/json/version", timeout=self._remaining(1)) as response:
                    payload = json.loads(response.read())
                websocket = payload.get("webSocketDebuggerUrl", "")
                if urlsplit(websocket).scheme not in {"ws", "wss"}:
                    raise ValueError("Invalid discovery response")
                return websocket
            except (OSError, ValueError):
                if self.execution_mode == "attach":
                    raise RuntimeError("Explicit CDP endpoint discovery failed") from None
                time.sleep(min(0.05, self._remaining()))
        raise TimeoutError("Chrome DevTools startup deadline exceeded")

    def _ipc_call(self, request: dict, timeout: float = 10, *, cleanup: bool = False) -> dict:
        duration = timeout if cleanup else self._remaining(timeout)
        end = time.monotonic() + duration
        # Official request serialization/authentication, with an explicit per-run
        # endpoint: _ipc.connect otherwise consults process-global imported paths.
        if sys.platform == "win32":
            endpoint = json.loads((self.runtime_dir / "bu.port").read_text())
            sock = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=duration)
            token = endpoint["token"]
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(duration)
            try:
                sock.connect(str(self.runtime_dir / "bu.sock"))
            except BaseException:
                sock.close()
                raise
            token = None

        class DeadlineSocket:
            def sendall(self, data):
                sock.settimeout(max(0.001, end - time.monotonic()))
                return sock.sendall(data)

            def recv(self, size):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Browser IPC deadline exceeded")
                sock.settimeout(remaining)
                return sock.recv(size)

        try:
            response = bh_ipc.request(DeadlineSocket(), token, request)
        finally:
            sock.close()
        if not response or "error" in response:
            raise RuntimeError("Browser Harness operation failed")
        return response

    def _cdp(self, method: str, **params: Any) -> dict:
        if not self._session_id:
            raise RuntimeError("Owned browser session is not started")
        # Target.setAutoAttach must target this page, unlike browser-level calls.
        if method == "Target.setAutoAttach":
            return self._ipc_call({"meta": "owned_auto_attach", "params": params}).get("result", {})
        return self._ipc_call({"method": method, "params": params, "session_id": self._session_id}).get("result", {})

    def evaluate_js(self, expression: str, *, await_promise: bool = False) -> Any:
        response = self._cdp("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=await_promise)
        if response.get("exceptionDetails"):
            raise RuntimeError("Owned page evaluation failed")
        return response.get("result", {}).get("value")

    def navigate(self, url: str) -> None:
        if not PolicyEnforcer(self.policy).check_request("GET", url).allowed:
            raise PolicyBlocked("Navigation is not permitted by project policy")
        result = self._cdp("Page.navigate", url=url)
        if result.get("errorText"):
            raise RuntimeError("Owned page navigation failed")
        end = time.monotonic() + self._remaining(15)
        while time.monotonic() < end:
            if self.evaluate_js("document.readyState") == "complete":
                return
            time.sleep(min(0.05, self._remaining()))
        raise TimeoutError("Navigation deadline exceeded")

    def fresh_read(self, path: str) -> Any:
        if not path.startswith("/") or path.startswith("//") or "\\" in path or urlsplit(path).scheme:
            raise PolicyBlocked("Fresh reads require a same-origin absolute path")
        current = self.evaluate_js("location.href")
        url = urljoin(current, path)
        if (
            urlsplit(url).netloc != urlsplit(current).netloc
            or not PolicyEnforcer(self.policy).check_request("GET", url).allowed
        ):
            raise PolicyBlocked("Fresh read is not permitted by project policy")
        capture_rules = self.policy.network.capture_origins
        parts = urlsplit(url)
        if capture_rules is not None and not any(
            str(rule.origin).rstrip("/") == f"{parts.scheme}://{parts.netloc}"
            and "GET" in rule.methods
            and parts.path.startswith(rule.path_prefix)
            for rule in capture_rules
        ):
            raise PolicyBlocked("Fresh read evidence is not permitted by capture policy")
        self._cdp("Network.setCacheDisabled", cacheDisabled=True)
        self._cdp("Network.setBypassServiceWorker", bypass=True)
        payload = self.evaluate_js(
            f"""(async () => {{
          const response = await fetch({json.dumps(url)}, {{method:'GET', cache:'no-store', redirect:'error',
            credentials:'same-origin', signal:AbortSignal.timeout({max(1, int(self._remaining() * 1000))})}});
          if (!response.ok) return {{ok:false, status:response.status}};
          return {{ok:true, payload:await response.json()}};
        }})()""",
            await_promise=True,
        )
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Fresh read did not return a successful JSON response")
        return payload["payload"]

    def dispatch_mouse(self, x: float, y: float, *, button: str = "left", click_count: int = 1) -> None:
        for event in ("mousePressed", "mouseReleased"):
            self._cdp("Input.dispatchMouseEvent", type=event, x=x, y=y, button=button, clickCount=click_count)

    def select_all(self) -> None:
        modifier = 4 if sys.platform == "darwin" else 2
        self._cdp(
            "Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=modifier, commands=["selectAll"]
        )
        self._cdp("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifier)

    def insert_text(self, text: str) -> None:
        self._cdp("Input.insertText", text=text)

    def dispatch_key(self, key: str, *, modifiers: int = 0) -> None:
        codes = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8}
        for event in ("keyDown", "keyUp"):
            self._cdp(
                "Input.dispatchKeyEvent",
                type=event,
                key=key,
                code=key,
                windowsVirtualKeyCode=codes.get(key, 0),
                modifiers=modifiers,
            )

    def scroll(self, x: float, y: float, delta_y: float) -> None:
        self._cdp("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=delta_y)

    def set_viewport(self, width: int, height: int) -> None:
        self._cdp("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def focus_emulation(self) -> None:
        self._cdp("Emulation.setFocusEmulationEnabled", enabled=True)

    def screenshot(self, path: Path) -> None:
        if not self.policy.model_disclosure.allow_screenshots:
            raise PolicyBlocked("Screenshot capture is not permitted")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = base64.b64decode(self._cdp("Page.captureScreenshot", format="png")["data"], validate=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)

    def drain_evidence(self) -> tuple[list[dict], int]:
        response = self._ipc_call({"meta": "drain_evidence", "settle_seconds": self._remaining(2)})
        return list(response["records"]), int(response["lost"])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is not None:
            try:
                if self._target_id:
                    self._ipc_call(
                        {"method": "Target.closeTarget", "params": {"targetId": self._target_id}},
                        timeout=2,
                        cleanup=True,
                    )
            except TRANSPORT_ERRORS:
                self.cleanup_notes += ("Could not confirm owned tab cleanup.",)
            with contextlib.suppress(Exception):
                self._ipc_call({"meta": "shutdown"}, timeout=2, cleanup=True)
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            self.process = None
        if self.chrome is not None:
            try:
                self.chrome.stop()
            except TRANSPORT_ERRORS:
                self.cleanup_notes += ("Could not confirm isolated Chrome cleanup.",)
            else:
                shutil.rmtree(self.chrome.user_data_dir, ignore_errors=True)
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        self._target_id = self._session_id = None


def make_transport(
    *,
    policy: Policy,
    evidence: EvidenceCollector,
    workdir: Path,
    chrome_executable: str | None = None,
    headless: bool = False,
    remote_port: int | None = None,
    bu_name: str | None = None,
    attach_profile: str | None = None,
    cdp_url: str | None = None,
    deadline: float | None = None,
) -> BrowserTransport:
    mode = validate_identity(policy, attach_profile, cdp_url)
    if mode == "isolated" and not chrome_executable:
        raise ValueError("Isolated execution requires a Chrome executable")
    if bu_name is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", bu_name):
        raise ValueError("Invalid browser namespace")
    name = f"{bu_name or 'jevqa'}-{uuid.uuid4().hex[:12]}"
    root = workdir / name
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    tmp, workspace = root / "tmp", root / "workspace"
    tmp.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    # AF_UNIX paths must fit macOS's 104-byte limit, regardless of workdir depth.
    runtime = Path(tempfile.mkdtemp(prefix="jq-", dir="/tmp" if os.name == "posix" else None))
    chrome = (
        None
        if mode == "attach"
        else ChromeLaunch(chrome_executable, root / "browser-profile", remote_port or _pick_free_port(), headless)
    )
    return BrowserTransport(
        policy, evidence, name, runtime, tmp, workspace, chrome, execution_mode=mode, cdp_url=cdp_url, deadline=deadline
    )
