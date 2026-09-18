import json

import pytest
from test_identity import cli_fixture

from jev_frontend_qa import cli


@pytest.mark.parametrize(
    "flags",
    [
        ["--screenshots"],
        ["--screenshot-mode", "failures"],
        ["--screenshot-every", "3"],
    ],
)
def test_screenshot_flag_cannot_expand_project_permissions(tmp_path, monkeypatch, flags):
    argv = cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "make_transport", lambda **kwargs: pytest.fail("Denied screenshots must block preflight"))
    assert cli.main(argv + flags) == cli.EXIT_BLOCKED
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["session_stats"]["jev"]["requests"] == 0
    assert report["screenshots"] == []
    assert "allow_screenshots" in report["note"]


def test_conflicting_screenshot_flags_are_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "run",
                "--scenario",
                "scenario.json",
                "--policy",
                "policy.json",
                "--screenshots",
                "--no-screenshots",
            ]
        )


@pytest.mark.parametrize(
    "flags",
    [
        ["--screenshot-every", "0"],
        ["--screenshot-mode", "steps", "--screenshot-every", "2"],
        ["--no-screenshots", "--screenshot-mode", "failures"],
        ["--no-screenshots", "--screenshot-every", "3"],
    ],
)
def test_invalid_frequency_stops_before_execution(monkeypatch, flags):
    monkeypatch.setattr(cli, "cmd_run", lambda _: pytest.fail("Invalid capture settings must not execute"))
    with pytest.raises(SystemExit) as result:
        cli.main(["run", "--scenario", "scenario.json", "--policy", "policy.json", *flags])
    assert result.value.code == 2
