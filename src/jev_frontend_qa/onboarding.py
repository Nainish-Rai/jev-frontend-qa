"""Local, deny-default project setup and portable skill installation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from pydantic import ValidationError

from .core.models import Policy, Scenario


def add_commands(subparsers: argparse._SubParsersAction) -> None:
    init = subparsers.add_parser("init", help="Initialize jevqa/ with a deny-default policy; preserve existing files.")
    init.add_argument("--project", type=Path, required=True)
    init.set_defaults(handler=_dispatch, operation=_init)

    schema = subparsers.add_parser("schema", help="Print the installed scenario or policy JSON Schema.")
    schema.add_argument("kind", choices=("scenario", "policy"))
    schema.set_defaults(handler=_dispatch, operation=_schema)

    scenario = subparsers.add_parser("new-scenario", help="Register a complete authored contract without overwriting.")
    scenario.add_argument("--project", type=Path, required=True)
    scenario.add_argument("--feature", required=True)
    scenario.add_argument("--journey", required=True)
    scenario.add_argument("--from", dest="source", type=Path, required=True)
    scenario.set_defaults(handler=_dispatch, operation=_new_scenario)

    skill = subparsers.add_parser("skill", help="Install the bundled portable jevqa skill.")
    install = skill.add_subparsers(dest="skill_command", required=True).add_parser("install")
    install.add_argument("--agent", choices=("claude", "codex"), required=True)
    install.add_argument("--project", type=Path, required=True)
    install.set_defaults(handler=_dispatch, operation=_install_skill)


def _dispatch(args: argparse.Namespace) -> int:
    try:
        args.operation(args)
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        # ValidationError includes input values; never echo authored fixture secrets.
        print(f"{args.command}: invalid contract or JSON; inspect jev-qa schema and required fields", file=sys.stderr)
        return 3
    except (OSError, ValueError) as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        return 3
    return 0


def _project(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--project must identify an existing project directory")
    return root


def _child(root: Path, relative: str | Path) -> Path:
    """Refuse pre-existing symlink components, including dangling final links."""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("destination must remain within the selected project")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"refusing symlink destination: {current}")
    return current


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _init(args: argparse.Namespace) -> None:
    root = _project(args.project)
    policy = _child(root, "jevqa/policy.json")
    scenarios = _child(root, "jevqa/scenarios")
    ignore = _child(root, ".gitignore")
    if policy.exists() and not policy.is_file():
        raise ValueError("existing policy is not a regular file")
    if scenarios.exists() and not scenarios.is_dir():
        raise ValueError("existing scenarios path is not a directory")
    if ignore.exists() and (not ignore.is_file() or ignore.stat().st_nlink > 1):
        raise ValueError(".gitignore must be an unshared regular file")
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    scenarios.mkdir(parents=True, exist_ok=True)
    if not policy.exists():
        _write_new(policy, (Policy().model_dump_json(indent=2) + "\n").encode())
    entry = "/artifacts/jevqa/"
    if entry not in existing.splitlines():
        suffix = ("\n" if existing and not existing.endswith("\n") else "") + entry + "\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(ignore, flags, 0o600), "w", encoding="utf-8") as output:
            output.write(suffix)
    print(f"init: ready at {root / 'jevqa'}; existing policy preserved; review permissions before execution")


def _schema(args: argparse.Namespace) -> None:
    model = Scenario if args.kind == "scenario" else Policy
    print(json.dumps(model.model_json_schema(), indent=2))


def _new_scenario(args: argparse.Namespace) -> None:
    root = _project(args.project)
    for slug in (args.feature, args.journey):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", slug):
            raise ValueError("feature and journey must be alphanumeric slugs with optional hyphens/underscores")
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(payload)
    if scenario.mode != "contract":
        raise ValueError("new-scenario requires a contract, not exploratory mode")
    destination = _child(root, Path("jevqa/scenarios") / args.feature / f"{args.journey}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep caller omission of run_id; dumping validated defaults would freeze a UUID.
    _write_new(destination, (json.dumps(payload, indent=2) + "\n").encode())
    print(f"new-scenario: wrote {destination}; schema validation is not browser verification")


def _resources(root: Traversable, prefix: Path = Path()):
    for resource in root.iterdir():
        relative = prefix / resource.name
        if resource.is_dir():
            yield from _resources(resource, relative)
        elif resource.is_file():
            yield relative, resource.read_bytes()


def _install_skill(args: argparse.Namespace) -> None:
    root = _project(args.project)
    host = ".claude" if args.agent == "claude" else ".agents"
    destination = _child(root, Path(host) / "skills/jevqa")
    if destination.exists():
        raise FileExistsError(f"refusing to replace existing skill: {destination}")
    bundle = files("jev_frontend_qa").joinpath("skills", "jevqa")
    contents = list(_resources(bundle))
    if not any(path == Path("SKILL.md") for path, _ in contents):
        raise ValueError("installed distribution lacks the jevqa skill")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # Exclusive ownership; existing installations are untouched.
    try:
        for relative, data in contents:
            target = _child(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_new(target, data)
    except (OSError, ValueError):
        shutil.rmtree(destination)
        raise
    print(f"skill install: installed jevqa for {args.agent} at {destination}")
