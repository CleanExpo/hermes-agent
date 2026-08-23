#!/usr/bin/env python3
"""Create, verify, and promote exact-state continuity receipts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from continuity_bridge import ALLOWED_ACTIVE_STATES, build_preflight
from continuity_common import (
    ContinuityError,
    atomic_write_json,
    atomic_write_text,
    git_state,
    load_json,
    read_markdown_frontmatter,
    receipt_errors,
    render_markdown_frontmatter,
    run_command,
)


def _task_from_value(value: Any, task_id: str) -> dict[str, Any] | None:
    candidates = value if isinstance(value, list) else [value]
    return next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and item.get("id") == task_id
        ),
        None,
    )


def strict_authority_check(
    config_path: Path,
    *,
    cwd: Path,
    require_mounted_volume: bool,
) -> dict[str, Any]:
    """Re-check all authorities and require a live Beads CLI response."""
    config = load_json(config_path)
    preflight = build_preflight(
        config_path,
        cwd=cwd,
        require_mounted_volume=require_mounted_volume,
    )
    errors = list(preflight.get("errors") or [])
    beads = config["beads"]
    env = os.environ.copy()
    env["BEADS_DIR"] = beads["data_dir"]
    try:
        result = run_command(
            [beads["binary"], "show", beads["active_task"], "--json"],
            cwd=cwd,
            timeout=float(beads.get("completion_timeout_seconds", 60)),
            env=env,
        )
        if result.returncode != 0:
            raise ContinuityError(
                (result.stderr or result.stdout).strip() or "bd show failed"
            )
        task = _task_from_value(json.loads(result.stdout), beads["active_task"])
        if task is None:
            errors.append("strict Beads query did not return the active task")
        elif task.get("status") not in ALLOWED_ACTIVE_STATES:
            errors.append(
                f"strict Beads query returned terminal status {task.get('status')!r}"
            )
    except (ContinuityError, json.JSONDecodeError) as exc:
        errors.append(f"strict Beads query failed: {exc}")
        task = None
    return {
        "passed": not errors,
        "preflight_status": preflight.get("status"),
        "task_id": task.get("id") if task else None,
        "task_status": task.get("status") if task else None,
        "errors": errors,
    }


def _load_list(path: Path | None, label: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ContinuityError(f"{label} must be a JSON list of objects")
    return value


def _load_object(path: Path | None, label: str) -> dict[str, Any]:
    if path is None:
        return {}
    return load_json(path)


def create_receipt(
    config_path: Path,
    *,
    cwd: Path,
    risk_tier: str,
    lifecycle_target: str,
    commands: list[dict[str, Any]],
    runtime_checks: list[dict[str, Any]],
    rollback: dict[str, Any],
    require_mounted_volume: bool = True,
) -> dict[str, Any]:
    config = load_json(config_path)
    state = git_state(cwd.resolve())
    expected_root = str(Path(config["expected_repo_root"]).resolve())
    if state.root != expected_root:
        raise ContinuityError(
            f"wrong repository folder: expected {expected_root}, got {state.root}"
        )
    receipt = {
        "schema_version": 1,
        "gate": "hermes-continuity-gate/1",
        "project": config["project"],
        "repo_id": config["repo_id"],
        "lifecycle_target": lifecycle_target,
        "risk_tier": risk_tier,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git": state.as_dict(),
        "authority_check": strict_authority_check(
            config_path,
            cwd=cwd,
            require_mounted_volume=require_mounted_volume,
        ),
        "commands": commands,
        "runtime_checks": runtime_checks,
        "rollback": rollback,
    }
    receipt["result"] = "PASS" if not receipt_errors(receipt, state) else "FAIL"
    return receipt


def verify_receipt(config_path: Path, receipt_path: Path, *, cwd: Path) -> list[str]:
    config = load_json(config_path)
    current = git_state(cwd.resolve())
    errors: list[str] = []
    if current.root != str(Path(config["expected_repo_root"]).resolve()):
        errors.append("current repository root does not match continuity config")
    receipt = load_json(receipt_path)
    if receipt.get("project") != config.get("project"):
        errors.append("receipt project does not match continuity config")
    if receipt.get("repo_id") != config.get("repo_id"):
        errors.append("receipt repo_id does not match continuity config")
    errors.extend(receipt_errors(receipt, current))
    if receipt.get("result") != "PASS":
        errors.append("receipt result is not PASS")
    return errors


def _run_beads(config: dict[str, Any], arguments: list[str], repo_root: Path) -> None:
    beads = config["beads"]
    env = os.environ.copy()
    env["BEADS_DIR"] = beads["data_dir"]
    result = run_command(
        [beads["binary"], *arguments, "--json"],
        cwd=repo_root,
        timeout=max(30, float(beads.get("timeout_seconds", 5))),
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ContinuityError(f"Beads update failed: {detail}")


def promote(
    config_path: Path,
    receipt_path: Path,
    *,
    cwd: Path,
    target: str,
) -> None:
    if target not in {"TESTED", "ENFORCED"}:
        raise ContinuityError("only TESTED or ENFORCED may be promoted by the gate")
    config = load_json(config_path)
    errors = verify_receipt(config_path, receipt_path, cwd=cwd)
    receipt = load_json(receipt_path)
    if receipt.get("lifecycle_target") != target:
        errors.append("requested target does not match receipt lifecycle target")
    if errors:
        raise ContinuityError("promotion refused: " + "; ".join(errors))

    card_path = Path(config["basic_memory"]["card_path"])
    card_data, card_body = read_markdown_frontmatter(card_path)
    original_card = render_markdown_frontmatter(card_data, card_body)
    now = datetime.now(timezone.utc).isoformat()
    evidence = (
        card_data.get("evidence") if isinstance(card_data.get("evidence"), dict) else {}
    )
    evidence.update({
        "commit": receipt["git"]["commit"],
        "tested_at": now,
        "receipt": str(receipt_path.resolve()),
    })
    card_data["state"] = target
    card_data["evidence"] = evidence
    card_data["next_action"] = (
        "Review the exact-state receipt and decide whether to expand the pilot."
        if target == "TESTED"
        else "Monitor enforced continuity before expanding to another project."
    )
    try:
        atomic_write_text(card_path, render_markdown_frontmatter(card_data, card_body))
        if target == "ENFORCED":
            _run_beads(
                config,
                [
                    "close",
                    config["beads"]["active_task"],
                    "--reason",
                    f"ENFORCED by exact-state receipt {receipt_path.name}",
                ],
                Path(config["expected_repo_root"]),
            )
    except BaseException:
        atomic_write_text(card_path, original_card)
        raise


def static_validate(config_path: Path) -> list[str]:
    """Validate committed structure without requiring external pilot state (CI-safe)."""
    errors: list[str] = []
    try:
        config = load_json(config_path)
    except ContinuityError as exc:
        return [str(exc)]
    repo_root = config_path.resolve().parent.parent
    for key in ("project", "repo_id", "goal", "expected_repo_root", "external_volume"):
        if not config.get(key):
            errors.append(f"config missing {key}")
    for relative in config.get("instructions", []):
        if not (repo_root / relative).is_file():
            errors.append(f"instruction missing: {relative}")
    try:
        spec, _ = read_markdown_frontmatter(repo_root / config["spec"]["path"])
        if spec.get("change_id") != config["spec"].get("change_id"):
            errors.append("spec change_id does not match config")
        if spec.get("active_task") != config["beads"].get("active_task"):
            errors.append("spec active_task does not match config")
    except (ContinuityError, KeyError) as exc:
        errors.append(str(exc))
    for relative in (
        "scripts/continuity_bridge.py",
        "scripts/continuity_gate.py",
        "scripts/continuity_event.py",
        ".specify/events.py",
        ".claude/settings.json",
        ".codex/hooks.json",
    ):
        if not (repo_root / relative).is_file():
            errors.append(f"continuity surface missing: {relative}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-receipt")
    create.add_argument("--config", type=Path, default=Path(".continuity/config.json"))
    create.add_argument("--cwd", type=Path, default=Path.cwd())
    create.add_argument("--risk-tier", choices=("T0", "T1", "T2", "T3"), required=True)
    create.add_argument("--target", choices=("TESTED", "ENFORCED"), required=True)
    create.add_argument("--commands-json", type=Path)
    create.add_argument("--runtime-json", type=Path)
    create.add_argument("--rollback-json", type=Path)
    create.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify-receipt")
    verify.add_argument("--config", type=Path, default=Path(".continuity/config.json"))
    verify.add_argument("--cwd", type=Path, default=Path.cwd())
    verify.add_argument("--receipt", type=Path, required=True)

    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument(
        "--config", type=Path, default=Path(".continuity/config.json")
    )
    promote_parser.add_argument("--cwd", type=Path, default=Path.cwd())
    promote_parser.add_argument("--receipt", type=Path, required=True)
    promote_parser.add_argument(
        "--target", choices=("TESTED", "ENFORCED"), required=True
    )

    static = subparsers.add_parser("static")
    static.add_argument("--config", type=Path, default=Path(".continuity/config.json"))

    args = parser.parse_args(argv)
    try:
        if args.command == "create-receipt":
            receipt = create_receipt(
                args.config,
                cwd=args.cwd,
                risk_tier=args.risk_tier,
                lifecycle_target=args.target,
                commands=_load_list(args.commands_json, "command evidence"),
                runtime_checks=_load_list(args.runtime_json, "runtime evidence"),
                rollback=_load_object(args.rollback_json, "rollback evidence"),
                require_mounted_volume=True,
            )
            atomic_write_json(args.output, receipt)
            print(
                json.dumps({"result": receipt["result"], "receipt": str(args.output)})
            )
            return 0 if receipt["result"] == "PASS" else 2
        if args.command == "verify-receipt":
            errors = verify_receipt(args.config, args.receipt, cwd=args.cwd)
            print(json.dumps({"valid": not errors, "errors": errors}, sort_keys=True))
            return 0 if not errors else 2
        if args.command == "promote":
            promote(args.config, args.receipt, cwd=args.cwd, target=args.target)
            print(json.dumps({"promoted": args.target, "receipt": str(args.receipt)}))
            return 0
        errors = static_validate(args.config)
        print(json.dumps({"valid": not errors, "errors": errors}, sort_keys=True))
        return 0 if not errors else 2
    except (ContinuityError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
