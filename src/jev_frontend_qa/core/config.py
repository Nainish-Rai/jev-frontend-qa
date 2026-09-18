"""Config loading.

The runner reads ``.env`` (which is gitignored) without printing its
contents. Only environment-derived values are exposed to the rest of the
system; secrets stay in the process environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class LoadedConfig(BaseModel):
    """Summary of environment-derived settings. Secrets are NEVER included."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    typesafe_model: str
    chrome_executable: str | None
    chrome_headless: bool
    work_dir: Path
    bu_name: str | None


def load_env(repo_root: Path) -> None:
    """Load ``.env`` if present. ``setdefault`` semantics: existing env wins."""

    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(repo_root: Path, *, cli_overrides: Mapping[str, object] | None = None) -> LoadedConfig:
    overrides = dict(cli_overrides or {})
    load_env(repo_root)
    return LoadedConfig(
        typesafe_model=os.environ.get("TYPESAFE_MODEL", "jev-1.13.0"),
        chrome_executable=_override(overrides, "chrome_executable", os.environ.get("JEV_QA_CHROME_EXECUTABLE")),
        chrome_headless=bool(_override(overrides, "headless", False)),
        work_dir=Path(str(_override(overrides, "work_dir", os.environ.get("JEV_QA_WORK_DIR", "artifacts/work")))),
        bu_name=_override(overrides, "bu_name", None),
    )


def _override(overrides: Mapping[str, object], key: str, default: object) -> object:
    value = overrides.get(key)
    return default if value is None else value
