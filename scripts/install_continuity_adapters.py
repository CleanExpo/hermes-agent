#!/usr/bin/env python3
"""Render project adapters and optionally install the sandbox Hermes adapter."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from continuity_common import ContinuityError, atomic_write_text, load_json


CLAUDE_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt": "UserPromptSubmit",
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "stop": "Stop",
    "session_end": "SessionEnd",
}
CODEX_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt": "UserPromptSubmit",
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "stop": "Stop",
}


def _hook_block(
    command: str, timeout: int, *, matcher: str | None = None
) -> list[dict[str, Any]]:
    block: dict[str, Any] = {
        "hooks": [{"type": "command", "command": command, "timeout": timeout}]
    }
    if matcher:
        block["matcher"] = matcher
    return [block]


def render_project_adapters(repo_root: Path) -> dict[Path, str]:
    contract = load_json(repo_root / ".continuity/adapters.json")
    timeout = int(contract["timeout_seconds"])
    claude: dict[str, Any] = {"hooks": {}}
    for event in contract["surfaces"]["claude"]:
        command = (
            f'python3 "$CLAUDE_PROJECT_DIR/.specify/events.py" {event} --surface claude'
        )
        matcher = ".*" if event in {"pre_tool", "post_tool"} else None
        claude["hooks"][CLAUDE_EVENT_NAMES[event]] = _hook_block(
            command, timeout, matcher=matcher
        )
    codex: dict[str, Any] = {"hooks": {}}
    for event in contract["surfaces"]["codex"]:
        command = f"python3 .specify/events.py {event} --surface codex"
        matcher = ".*" if event in {"pre_tool", "post_tool"} else None
        codex["hooks"][CODEX_EVENT_NAMES[event]] = _hook_block(
            command, timeout, matcher=matcher
        )
    return {
        repo_root / ".claude/settings.json": json.dumps(
            claude, indent=2, sort_keys=True
        )
        + "\n",
        repo_root / ".codex/hooks.json": json.dumps(codex, indent=2, sort_keys=True)
        + "\n",
    }


def render_hermes_config(
    repo_root: Path, existing: dict[str, Any] | None = None
) -> str:
    contract = load_json(repo_root / ".continuity/adapters.json")
    timeout = int(contract["timeout_seconds"])
    config = dict(existing or {})
    hooks = dict(config.get("hooks") or {})
    entry = repo_root / ".specify/events.py"
    for event in contract["surfaces"]["hermes"]:
        hooks[event] = [
            {
                "command": f"python3 {shlex.quote(str(entry))} {event} --surface hermes",
                "timeout": timeout,
            }
        ]
    config["hooks"] = hooks
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--apply-hermes", action="store_true")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        mismatches: list[str] = []
        for path, expected in render_project_adapters(repo_root).items():
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path.relative_to(repo_root)))
        if args.check and mismatches:
            raise ContinuityError(
                "generated adapters are stale: " + ", ".join(mismatches)
            )
        if args.apply_hermes:
            if args.hermes_home is None:
                raise ContinuityError("--hermes-home is required with --apply-hermes")
            target = args.hermes_home.resolve() / "config.yaml"
            existing: dict[str, Any] = {}
            if target.is_file():
                loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise ContinuityError("existing Hermes config is not a mapping")
                existing = loaded
            atomic_write_text(target, render_hermes_config(repo_root, existing))
            print(json.dumps({"hermes_config": str(target), "installed": True}))
        else:
            print(json.dumps({"valid": not mismatches, "mismatches": mismatches}))
        return 0
    except (
        ContinuityError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
