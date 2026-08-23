#!/usr/bin/env python3
"""Bounded preflight for Basic Memory + Beads + Spec Kit continuity."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from continuity_common import (
    ContinuityError,
    compact_json,
    external_volume_available,
    git_state,
    load_json,
    read_markdown_frontmatter,
    receipt_errors,
    run_command,
)


ALLOWED_ACTIVE_STATES = {"open", "ready", "in_progress", "blocked"}
REQUIRED_CARD_FIELDS = {
    "project",
    "folder",
    "goal",
    "user_context",
    "state",
    "active_task",
    "spec_change",
    "evidence",
    "next_action",
    "blockers",
    "supersedes",
}
REQUIRED_SPEC_FIELDS = {
    "project",
    "repo_id",
    "goal",
    "state",
    "active_task",
    "change_id",
    "folder",
}


def _task_from_json(value: Any, task_id: str) -> dict[str, Any] | None:
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == task_id:
            return candidate
    return None


def _read_beads(
    config: dict[str, Any], repo_root: Path
) -> tuple[dict[str, Any], bool, str]:
    beads = config["beads"]
    task_id = beads["active_task"]
    env = os.environ.copy()
    env["BEADS_DIR"] = beads["data_dir"]
    degraded_reason = ""
    try:
        result = run_command(
            [beads["binary"], "ready", "--json"],
            cwd=repo_root,
            timeout=float(beads.get("timeout_seconds", 5)),
            env=env,
        )
        if result.returncode != 0:
            raise ContinuityError(
                (result.stderr or result.stdout).strip() or "bd ready failed"
            )
        task = _task_from_json(json.loads(result.stdout), task_id)
        if task is not None:
            return task, False, ""
        degraded_reason = f"bd ready did not return active task {task_id}"
    except (ContinuityError, json.JSONDecodeError) as exc:
        degraded_reason = str(exc)

    issues_path = Path(beads["issues_path"])
    try:
        lines = issues_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            task = _task_from_json(json.loads(line), task_id)
            if task is not None:
                return task, True, degraded_reason or "Beads CLI unavailable"
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityError(
            f"cannot read Beads task or JSONL fallback: {exc}"
        ) from exc
    raise ContinuityError(f"active Beads task {task_id} is absent")


def validate_tool_adjacency(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type not in {"server_tool_use", "tool_use"}:
            continue
        tool_id = event.get("id")
        if index + 1 >= len(events):
            errors.append(f"tool use {tool_id or index} has no adjacent result")
            continue
        following = events[index + 1]
        result_id = following.get("tool_use_id") or following.get("id")
        if (
            following.get("type") not in {"tool_result", "server_tool_result"}
            or result_id != tool_id
        ):
            errors.append(
                f"tool use {tool_id or index} is interrupted before its matching result"
            )
    return errors


def _required(mapping: dict[str, Any], fields: set[str], authority: str) -> list[str]:
    return [
        f"{authority} missing {field}"
        for field in sorted(fields)
        if mapping.get(field) in (None, "")
    ]


def build_preflight(
    config_path: Path,
    *,
    cwd: Path,
    tool_envelope: Path | None = None,
    require_mounted_volume: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    config = load_json(config_path)
    repo_root = Path(config["expected_repo_root"]).resolve()
    external_volume = Path(config["external_volume"])

    if require_mounted_volume and not external_volume_available(external_volume):
        errors.append(f"external volume is not mounted: {external_volume}")

    try:
        state = git_state(cwd.resolve())
    except ContinuityError as exc:
        errors.append(str(exc))
        state = None
    if state and Path(state.root) != repo_root:
        errors.append(
            f"wrong repository folder: expected {repo_root}, got {state.root}"
        )

    try:
        card, card_body = read_markdown_frontmatter(
            Path(config["basic_memory"]["card_path"])
        )
        errors.extend(_required(card, REQUIRED_CARD_FIELDS, "Basic Memory card"))
        card_word_count = len((json.dumps(card, default=str) + " " + card_body).split())
        if card_word_count > 1200:
            errors.append(f"Basic Memory card exceeds 1,200 words: {card_word_count}")
    except ContinuityError as exc:
        errors.append(str(exc))
        card = {}
        card_word_count = None

    try:
        spec_path = repo_root / config["spec"]["path"]
        spec, _ = read_markdown_frontmatter(spec_path)
        errors.extend(_required(spec, REQUIRED_SPEC_FIELDS, "Spec Kit change"))
    except ContinuityError as exc:
        errors.append(str(exc))
        spec = {}

    degraded = False
    try:
        task, degraded, reason = _read_beads(config, repo_root)
        if degraded:
            warnings.append(f"Beads CLI degraded to JSONL: {reason}")
    except ContinuityError as exc:
        errors.append(str(exc))
        task = {}

    expected = {
        "project": config.get("project"),
        "goal": config.get("goal"),
        "folder": str(repo_root),
        "active_task": config.get("beads", {}).get("active_task"),
        "change_id": config.get("spec", {}).get("change_id"),
    }
    comparisons = (
        ("card.project", card.get("project"), expected["project"]),
        ("spec.project", spec.get("project"), expected["project"]),
        ("card.goal", card.get("goal"), expected["goal"]),
        ("spec.goal", spec.get("goal"), expected["goal"]),
        ("task.title", task.get("title"), expected["goal"]),
        ("card.folder", card.get("folder"), expected["folder"]),
        ("spec.folder", spec.get("folder"), expected["folder"]),
        ("card.active_task", card.get("active_task"), expected["active_task"]),
        ("spec.active_task", spec.get("active_task"), expected["active_task"]),
        ("task.id", task.get("id"), expected["active_task"]),
        ("card.spec_change", card.get("spec_change"), expected["change_id"]),
        ("spec.change_id", spec.get("change_id"), expected["change_id"]),
        ("task.spec_id", task.get("spec_id"), config.get("spec", {}).get("path")),
    )
    for label, actual, wanted in comparisons:
        if actual != wanted:
            errors.append(
                f"authority conflict for {label}: expected {wanted!r}, got {actual!r}"
            )
    if task and task.get("status") not in ALLOWED_ACTIVE_STATES:
        errors.append(f"Beads task is stale or terminal: {task.get('status')!r}")
    terminal_states = {"TESTED", "ENFORCED"}
    card_state = card.get("state")
    spec_state = spec.get("state")
    if card_state in terminal_states or spec_state in terminal_states:
        if spec_state in terminal_states and card_state != spec_state:
            errors.append(
                f"terminal lifecycle conflict: card={card_state!r}, spec={spec_state!r}"
            )
        receipt_path = (
            (card.get("evidence") or {}).get("receipt")
            if isinstance(card.get("evidence"), dict)
            else None
        )
        if not receipt_path:
            errors.append("terminal lifecycle has no gate receipt")
        elif state:
            try:
                receipt = load_json(Path(receipt_path))
                errors.extend(receipt_errors(receipt, state))
                if receipt.get("lifecycle_target") != card_state:
                    errors.append(
                        "receipt lifecycle target does not match authority state"
                    )
            except ContinuityError as exc:
                errors.append(str(exc))
    else:
        allowed = {"PROPOSED", "ACTIVE", "IMPLEMENTED", "BLOCKED"}
        if card and card_state not in allowed:
            errors.append(f"invalid card state: {card_state!r}")
        if spec and spec_state not in allowed:
            errors.append(f"invalid spec state: {spec_state!r}")

    for relative in config.get("instructions", []):
        if not (repo_root / relative).is_file():
            errors.append(f"native instruction is missing: {relative}")
    for absolute in config.get("external_instructions", []):
        if not Path(absolute).is_file():
            errors.append(f"external native instruction is missing: {absolute}")

    if tool_envelope is not None:
        try:
            raw = json.loads(tool_envelope.read_text(encoding="utf-8"))
            events = raw.get("events") if isinstance(raw, dict) else raw
            if not isinstance(events, list) or not all(
                isinstance(item, dict) for item in events
            ):
                raise ValueError("expected a list of event objects")
            errors.extend(validate_tool_adjacency(events))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"invalid tool envelope: {exc}")

    status = "BLOCKED" if errors else ("DEGRADED" if degraded else "AVAILABLE")
    completion_allowed = not errors and not degraded
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "completion_allowed": completion_allowed,
        "project": config.get("project"),
        "goal": config.get("goal"),
        "active_task": task.get("id"),
        "task_status": task.get("status"),
        "spec_change": spec.get("change_id"),
        "lifecycle": card.get("state"),
        "card_words": card_word_count,
        "next_action": card.get("next_action"),
        "blockers": card.get("blockers", []),
        "git": state.as_dict() if state else None,
        "errors": errors,
        "warnings": warnings,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".continuity/config.json"))
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--tool-envelope", type=Path)
    parser.add_argument(
        "--allow-unmounted-fixture", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    try:
        config = load_json(args.config)
        result = build_preflight(
            args.config,
            cwd=args.cwd,
            tool_envelope=args.tool_envelope,
            require_mounted_volume=not args.allow_unmounted_fixture,
        )
        print(compact_json(result, int(config.get("max_output_chars", 8000))))
        return 0 if result["status"] != "BLOCKED" else 2
    except (ContinuityError, KeyError, TypeError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "status": "BLOCKED",
            "completion_allowed": False,
            "errors": [str(exc)],
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
