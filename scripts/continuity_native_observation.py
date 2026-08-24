#!/usr/bin/env python3
"""Run exact-host continuity hook observations without persisting payload data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from continuity_common import ContinuityError, load_json, minimal_child_env, run_command
from install_continuity_adapters import render_project_adapters


def _run_json_hook(
    repo_root: Path, config_path: Path, surface: str, event: str
) -> None:
    payload = {
        "session_id": f"native-observation-{surface}",
        "extra": {
            "turn_id": "native-observation-turn",
            "conversation_history": [{"role": "user", "content": "admission smoke"}],
        },
    }
    result = run_command(
        [
            sys.executable,
            str(repo_root / ".specify/events.py"),
            event,
            "--surface",
            surface,
            "--config",
            str(config_path),
        ],
        cwd=repo_root,
        timeout=60,
        env=minimal_child_env(),
    )
    if result.returncode != 0:
        raise ContinuityError(f"{surface} admission hook exited nonzero")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContinuityError(f"{surface} admission hook returned invalid JSON") from exc
    if response.get("completion_allowed") is not True and not (
        isinstance(response.get("preflight"), dict)
        and response["preflight"].get("completion_allowed") is True
    ):
        raise ContinuityError(f"{surface} admission hook did not allow completion")


def observe_project_hook(repo_root: Path, config_path: Path, surface: str) -> None:
    rendered = render_project_adapters(repo_root)
    suffix = ".claude/settings.json" if surface == "claude" else ".codex/hooks.json"
    target = repo_root / suffix
    if target.read_text(encoding="utf-8") != rendered[target]:
        raise ContinuityError(f"{surface} project hook adapter is stale")
    _run_json_hook(repo_root, config_path, surface, "session_start")


def observe_hermes_hook(
    repo_root: Path, config_path: Path, hermes_home: Path
) -> None:
    env = minimal_child_env({"HERMES_HOME": str(hermes_home)})
    commands = (
        ("list", ["hooks", "list"]),
        ("doctor", ["hooks", "doctor"]),
        ("test", ["hooks", "test", "pre_llm_call"]),
    )
    for action, arguments in commands:
        result = run_command(
            [sys.executable, str(repo_root / "main.py"), *arguments],
            cwd=repo_root,
            timeout=120,
            env=env,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            raise ContinuityError(f"hermes hooks {action} exited nonzero")
        if action == "list" and "Configured shell hooks" not in output:
            raise ContinuityError("hermes hooks list found no configured shell hooks")
        if action == "doctor" and "All shell hooks look healthy." not in output:
            raise ContinuityError("hermes hooks doctor did not report healthy hooks")
        if action == "test" and (
            "Firing" not in output or '"completion_allowed": true' not in output
        ):
            raise ContinuityError(
                "hermes hooks test did not produce an allowed fresh-session admission"
            )
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        _run_json_hook(repo_root, config_path, "hermes", "pre_llm_call")
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("claude", "codex", "hermes"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path)
    args = parser.parse_args(argv)
    config_path = args.config.resolve()
    config = load_json(config_path)
    repo_root = Path(config["expected_repo_root"]).resolve()
    try:
        if args.surface == "hermes":
            if args.hermes_home is None:
                raise ContinuityError("--hermes-home is required for Hermes observation")
            observe_hermes_hook(repo_root, config_path, args.hermes_home.resolve())
            checks = ["hooks-list", "hooks-doctor", "fresh-session-admission"]
        else:
            observe_project_hook(repo_root, config_path, args.surface)
            checks = ["generated-adapter", "project-admission"]
        print(json.dumps({"surface": args.surface, "checks": checks}, sort_keys=True))
        return 0
    except (ContinuityError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"native observation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
