import json

from jev_frontend_qa.cli import main
from jev_frontend_qa.core.models import Policy, Scenario


def authored_contract():
    return {
        "name": "Search by exact query",
        "start_url": "http://127.0.0.1:3000/search",
        "steps": [
            {
                "id": "search",
                "goal": "Search using the exact query fixture",
                "fixtures": {"query": "release-{{run_id}}"},
                "assertions": [{"kind": "count", "selector": {"testid": "result"}, "expected": 1}],
            }
        ],
    }


def test_init_denies_permissions_and_preserves_reviewed_policy(tmp_path):
    assert main(["init", "--project", str(tmp_path)]) == 0
    policy_path = tmp_path / "jevqa/policy.json"
    policy = Policy.model_validate_json(policy_path.read_text())
    assert not policy.method_allowed("GET", "http://127.0.0.1:3000", "/")
    assert not policy.model_disclosure.allow_page_text
    reviewed = policy.model_dump()
    reviewed["model_disclosure"]["allow_page_text"] = True
    policy_path.write_text(json.dumps(reviewed))
    before = policy_path.read_bytes()
    assert main(["init", "--project", str(tmp_path)]) == 0
    assert policy_path.read_bytes() == before
    assert (tmp_path / ".gitignore").read_text().splitlines().count("/artifacts/jevqa/") == 1


def test_import_preserves_fresh_identity_and_refuses_overwrite(tmp_path):
    source = tmp_path / "authored.json"
    source.write_text(json.dumps(authored_contract()))
    argv = [
        "new-scenario",
        "--project",
        str(tmp_path),
        "--feature",
        "search",
        "--journey",
        "filter",
        "--from",
        str(source),
    ]
    assert main(argv) == 0
    target = tmp_path / "jevqa/scenarios/search/filter.json"
    payload = json.loads(target.read_text())
    assert "run_id" not in payload
    assert Scenario.model_validate(payload).run_id != Scenario.model_validate(payload).run_id
    before = target.read_bytes()
    changed = authored_contract()
    changed["name"] = "A different contract"
    source.write_text(json.dumps(changed))
    assert main(argv) == 3
    assert target.read_bytes() == before


def test_import_rejects_invalid_contract_and_path_escape(tmp_path):
    source = tmp_path / "authored.json"
    payload = authored_contract()
    payload["steps"][0]["assertions"] = []
    source.write_text(json.dumps(payload))
    argv = [
        "new-scenario",
        "--project",
        str(tmp_path),
        "--feature",
        "search",
        "--journey",
        "filter",
        "--from",
        str(source),
    ]
    assert main(argv) == 3
    assert not (tmp_path / "jevqa/scenarios/search/filter.json").exists()
    source.write_text(json.dumps(authored_contract()))
    argv[4] = "../outside"
    assert main(argv) in (2, 3)
    assert not (tmp_path / "outside").exists()


def test_setup_does_not_follow_existing_child_symlinks(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "jevqa").symlink_to(outside, target_is_directory=True)
    assert main(["init", "--project", str(project)]) == 3
    assert not (outside / "policy.json").exists()


def test_skill_install_refuses_to_replace_existing_agent_instructions(tmp_path):
    assert main(["skill", "install", "--agent", "codex", "--project", str(tmp_path)]) == 0
    skill = tmp_path / ".agents/skills/jevqa/SKILL.md"
    assert skill.exists()
    skill.write_text("User-maintained skill\n")
    assert main(["skill", "install", "--agent", "codex", "--project", str(tmp_path)]) == 3
    assert skill.read_text() == "User-maintained skill\n"
    assert not (tmp_path / ".claude").exists()


def test_init_rejects_symlinked_ignore_without_touching_external_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.write_text("User configuration\n")
    (project / ".gitignore").symlink_to(external)
    assert main(["init", "--project", str(project)]) == 3
    assert external.read_text() == "User configuration\n"
    assert not (project / "jevqa").exists()


def test_skill_install_rejects_symlinked_agent_directory(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / ".agents").symlink_to(external, target_is_directory=True)
    assert main(["skill", "install", "--agent", "codex", "--project", str(project)]) == 3
    assert not (external / "skills").exists()


def test_invalid_contract_does_not_echo_sensitive_input(tmp_path, capsys):
    source = tmp_path / "authored.json"
    payload = authored_contract()
    payload["steps"][0]["id"] = "synthetic-private-value"
    payload["steps"][0]["assertions"] = []
    source.write_text(json.dumps(payload))
    assert (
        main(
            [
                "new-scenario",
                "--project",
                str(tmp_path),
                "--feature",
                "search",
                "--journey",
                "filter",
                "--from",
                str(source),
            ]
        )
        == 3
    )
    output = capsys.readouterr()
    assert "synthetic-private-value" not in output.out + output.err
