#!/usr/bin/env python3
"""Create, verify, and promote exact-state continuity receipts."""

from __future__ import annotations

import argparse
import os
import hashlib
import json
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows release is not supported by this pilot
    fcntl = None  # type: ignore[assignment]

from continuity_bridge import ALLOWED_ACTIVE_STATES, build_preflight
from continuity_common import (
    ContinuityError,
    atomic_write_json,
    atomic_write_text,
    external_input_digests,
    git_state,
    load_json,
    minimal_child_env,
    read_markdown_frontmatter,
    receipt_errors,
    render_markdown_frontmatter,
    run_command,
    sha256_file,
    verify_pinned_executable,
)


COMMAND_SPEC_KEYS = {"name", "argv", "scope", "timeout_seconds"}
RUNTIME_SPEC_KEYS = {"name", "argv", "surface", "timeout_seconds"}
ROLLBACK_SPEC_KEYS = {"name", "argv", "mode", "timeout_seconds"}
TEST_PATTERNS = (
    re.compile(r"Summary:\s+\d+ files?,\s+(\d+) tests? passed,\s+(\d+) failed"),
    re.compile(r"(?<![\w])(?P<passed>\d+) passed(?:,\s+(?P<skipped>\d+) skipped)?"),
)
SENSITIVE_ARG = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password|passwd|secret)"
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
    allow_closed_task: bool = False,
    allow_recovery_journal: bool = False,
) -> dict[str, Any]:
    """Re-check all authorities and require a live Beads CLI response."""
    config = load_json(config_path)
    preflight = build_preflight(
        config_path,
        cwd=cwd,
        require_mounted_volume=require_mounted_volume,
        allow_recovery_journal=allow_recovery_journal,
    )
    errors = list(preflight.get("errors") or [])
    beads = config["beads"]
    verify_pinned_executable(config, cwd.resolve(), "beads", Path(beads["binary"]))
    env = minimal_child_env({
        "BEADS_DIR": beads["data_dir"],
        "HOME": config["state_root"],
    })
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
        elif task.get("status") not in ALLOWED_ACTIVE_STATES and not (
            allow_closed_task and task.get("status") == "closed"
        ):
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


def _validate_closed_spec(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContinuityError(f"{label} contains unknown keys: {', '.join(unknown)}")
    name = value.get("name")
    argv = value.get("argv")
    if not isinstance(name, str) or not name or len(name) > 120:
        raise ContinuityError(f"{label} has an invalid name")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ContinuityError(f"{label} argv must be a non-empty string list")


def _resolved_argv(spec: dict[str, Any], repo_root: Path) -> list[str]:
    argv = list(spec["argv"])
    if any(SENSITIVE_ARG.search(value) for value in argv):
        raise ContinuityError("evidence argv contains a secret-bearing argument name")
    first = Path(argv[0])
    if not first.is_absolute():
        first = (repo_root / first).resolve()
    else:
        first = first.resolve()
    if first == Path(sys.executable).resolve():
        if len(argv) < 2 or argv[1].startswith("-"):
            raise ContinuityError(
                "Python evidence commands require a repository script"
            )
        script = (repo_root / argv[1]).resolve()
        if not script.is_relative_to(repo_root) or script.suffix != ".py":
            raise ContinuityError(
                "Python evidence script must be a repository .py file"
            )
        argv[0] = str(first)
        argv[1] = str(script)
        return argv
    if not first.is_relative_to(repo_root):
        raise ContinuityError(f"evidence executable escapes repository: {first}")
    if not first.is_file():
        raise ContinuityError(f"evidence executable is missing: {first}")
    argv[0] = str(first)
    return argv


def _test_summary(output: str) -> tuple[int, int, int]:
    for pattern in TEST_PATTERNS:
        match = pattern.search(output)
        if not match:
            continue
        groups = match.groupdict()
        if groups:
            return int(groups["passed"]), 0, int(groups.get("skipped") or 0)
        return int(match.group(1)), int(match.group(2)), 0
    return 0, 0, 0


def _execute_evidence(
    spec: dict[str, Any], repo_root: Path, *, kind: str, state_root: Path
) -> dict[str, Any]:
    allowed = {
        "command": COMMAND_SPEC_KEYS,
        "runtime": RUNTIME_SPEC_KEYS,
        "rollback": ROLLBACK_SPEC_KEYS,
    }[kind]
    _validate_closed_spec(spec, allowed, f"{kind} evidence")
    argv = _resolved_argv(spec, repo_root)
    timeout = float(spec.get("timeout_seconds", 900))
    if timeout <= 0 or timeout > 7200:
        raise ContinuityError(f"{kind} evidence timeout is outside 1..7200 seconds")
    started = datetime.now(timezone.utc)
    monotonic_start = time.monotonic()
    result = run_command(
        argv,
        cwd=repo_root,
        timeout=timeout,
        env=minimal_child_env({
            "HOME": str(state_root),
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }),
    )
    completed = datetime.now(timezone.utc)
    output = (result.stdout or "") + (result.stderr or "")
    test_count, failed_count, skipped = _test_summary(output)
    record: dict[str, Any] = {
        "name": spec["name"],
        "kind": kind,
        "argv": argv,
        "cwd": str(repo_root),
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_ms": round((time.monotonic() - monotonic_start) * 1000),
        "exit_code": result.returncode,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    if kind == "command":
        record.update({
            "scope": spec.get("scope", "focused"),
            "test_count": test_count,
            "failed_count": failed_count,
            "skipped": skipped,
            "flaky": False,
        })
    elif kind == "runtime":
        record.update({
            "surface": spec.get("surface"),
            "passed": result.returncode == 0,
        })
    else:
        record.update({
            "mode": spec.get("mode", "dry-run"),
            "passed": result.returncode == 0,
        })
    return record


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
    repo_root = Path(config["expected_repo_root"]).resolve()
    state = git_state(cwd.resolve(), integration_ref=config.get("integration_ref"))
    expected_root = str(Path(config["expected_repo_root"]).resolve())
    if state.root != expected_root:
        raise ContinuityError(
            f"wrong repository folder: expected {expected_root}, got {state.root}"
        )
    receipt = {
        "schema_version": 1,
        "gate": "hermes-continuity-gate/2",
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
        "external_inputs": external_input_digests(config, repo_root),
        "commands": [
            _execute_evidence(
                spec, repo_root, kind="command", state_root=Path(config["state_root"])
            )
            for spec in commands
        ],
        "runtime_checks": [
            _execute_evidence(
                spec, repo_root, kind="runtime", state_root=Path(config["state_root"])
            )
            for spec in runtime_checks
        ],
        "rollback": _execute_evidence(
            rollback,
            repo_root,
            kind="rollback",
            state_root=Path(config["state_root"]),
        )
        if rollback
        else {},
    }
    final_state = git_state(
        cwd.resolve(), integration_ref=config.get("integration_ref")
    )
    receipt["result"] = "PASS" if not receipt_errors(receipt, final_state) else "FAIL"
    return receipt


def verify_receipt(
    config_path: Path,
    receipt_path: Path,
    *,
    cwd: Path,
    allow_recovery_journal: bool = False,
) -> list[str]:
    config = load_json(config_path)
    repo_root = Path(config["expected_repo_root"]).resolve()
    current = git_state(cwd.resolve(), integration_ref=config.get("integration_ref"))
    errors: list[str] = []
    if current.root != str(Path(config["expected_repo_root"]).resolve()):
        errors.append("current repository root does not match continuity config")
    receipt = load_json(receipt_path)
    if receipt.get("project") != config.get("project"):
        errors.append("receipt project does not match continuity config")
    if receipt.get("repo_id") != config.get("repo_id"):
        errors.append("receipt repo_id does not match continuity config")
    try:
        if receipt.get("external_inputs") != external_input_digests(config, repo_root):
            errors.append("external instruction or executable identity is stale")
    except ContinuityError as exc:
        errors.append(str(exc))
    authority = strict_authority_check(
        config_path,
        cwd=cwd,
        require_mounted_volume=True,
        allow_closed_task=receipt.get("lifecycle_target") == "ENFORCED",
        allow_recovery_journal=allow_recovery_journal,
    )
    if not authority.get("passed"):
        errors.append("current strict authority check failed")
        errors.extend(authority.get("errors") or [])
    errors.extend(receipt_errors(receipt, current))
    if receipt.get("result") != "PASS":
        errors.append("receipt result is not PASS")
    return errors


def _run_beads(config: dict[str, Any], arguments: list[str], repo_root: Path) -> None:
    beads = config["beads"]
    verify_pinned_executable(config, repo_root, "beads", Path(beads["binary"]))
    env = minimal_child_env({
        "BEADS_DIR": beads["data_dir"],
        "HOME": config["state_root"],
    })
    result = run_command(
        [beads["binary"], *arguments, "--json"],
        cwd=repo_root,
        timeout=max(30, float(beads.get("timeout_seconds", 5))),
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ContinuityError(f"Beads update failed: {detail}")


@contextmanager
def _promotion_lock(config: dict[str, Any], timeout: float = 15):
    if fcntl is None:
        raise ContinuityError("promotion locking is unavailable on this platform")
    path = Path(config["state_root"]) / "promotion.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ContinuityError("timed out waiting for the promotion lock")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


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
    journal_path = Path(config["state_root"]) / "promotion.json"
    with _promotion_lock(config):
        recovery = False
        if journal_path.is_file():
            existing_journal = load_json(journal_path)
            recovery = (
                existing_journal.get("status") == "RECOVERY_REQUIRED"
                and existing_journal.get("target") == target
                and existing_journal.get("receipt") == str(receipt_path.resolve())
            )
        errors = verify_receipt(
            config_path,
            receipt_path,
            cwd=cwd,
            allow_recovery_journal=recovery,
        )
        receipt = load_json(receipt_path)
        if receipt.get("lifecycle_target") != target:
            errors.append("requested target does not match receipt lifecycle target")
        if errors:
            raise ContinuityError("promotion refused: " + "; ".join(errors))

        card_path = Path(config["basic_memory"]["card_path"])
        card_data, card_body = read_markdown_frontmatter(card_path)
        if journal_path.is_file():
            prior_journal = load_json(journal_path)
            if (
                prior_journal.get("status") == "COMMITTED"
                and prior_journal.get("target") == target
                and prior_journal.get("receipt") == str(receipt_path.resolve())
                and prior_journal.get("commit") == receipt["git"]["commit"]
                and card_data.get("state") == target
            ):
                return
        now = datetime.now(timezone.utc).isoformat()
        evidence = (
            card_data.get("evidence")
            if isinstance(card_data.get("evidence"), dict)
            else {}
        )
        evidence.update({
            "commit": receipt["git"]["commit"],
            "tested_at": now,
            "receipt": str(receipt_path.resolve()),
        })
        journal = {
            "schema_version": 1,
            "status": "PREPARED",
            "target": target,
            "receipt": str(receipt_path.resolve()),
            "commit": receipt["git"]["commit"],
            "updated_at": now,
        }
        atomic_write_json(journal_path, journal)
        card_data["state"] = target
        card_data["evidence"] = evidence
        card_data["next_action"] = (
            "Review the exact-state receipt and decide whether to expand the pilot."
            if target == "TESTED"
            else "Monitor enforced continuity before expanding to another project."
        )
        atomic_write_text(card_path, render_markdown_frontmatter(card_data, card_body))
        journal["status"] = "CARD_WRITTEN"
        journal["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(journal_path, journal)
        if target == "ENFORCED":
            try:
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
            except BaseException as exc:
                journal["status"] = "RECOVERY_REQUIRED"
                journal["error_type"] = type(exc).__name__
                journal["updated_at"] = datetime.now(timezone.utc).isoformat()
                atomic_write_json(journal_path, journal)
                raise
        journal["status"] = "COMMITTED"
        journal["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(journal_path, journal)


def static_validate(config_path: Path) -> list[str]:
    """Validate committed structure without requiring external pilot state (CI-safe)."""
    errors: list[str] = []
    try:
        config = load_json(config_path)
    except ContinuityError as exc:
        return [str(exc)]
    repo_root = config_path.resolve().parent.parent
    for key in (
        "project",
        "repo_id",
        "goal",
        "expected_repo_root",
        "external_volume",
        "integration_ref",
        "toolchain_lock",
    ):
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
    try:
        lock = load_json(repo_root / config["toolchain_lock"])
        tools = lock.get("tools")
        if lock.get("schema_version") != 1 or not isinstance(tools, dict):
            errors.append("toolchain lock has an invalid schema")
        else:
            for name, tool in tools.items():
                if not isinstance(tool, dict) or not tool.get("version"):
                    errors.append(f"toolchain lock entry is invalid: {name}")
                source = tool.get("source") if isinstance(tool, dict) else None
                if not isinstance(source, str) or not source.startswith("https://"):
                    errors.append(f"toolchain lock source is not HTTPS: {name}")
            beads = tools.get("beads")
            hashes = [
                value for key, value in (beads or {}).items() if key.endswith("-sha256")
            ]
            if not hashes or not all(
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in hashes
            ):
                errors.append("toolchain lock has no valid Beads executable digest")
    except (ContinuityError, KeyError) as exc:
        errors.append(str(exc))

    manifests = sorted((repo_root / ".specify/integrations").glob("*.manifest.json"))
    if not manifests:
        errors.append("no integration supply-chain manifests are committed")
    for manifest_path in manifests:
        try:
            manifest = load_json(manifest_path)
            files = manifest.get("files")
            if not manifest.get("integration") or not manifest.get("version"):
                errors.append(
                    f"integration manifest identity missing: {manifest_path.name}"
                )
            if not isinstance(files, dict):
                errors.append(
                    f"integration manifest files invalid: {manifest_path.name}"
                )
                continue
            for relative, expected in files.items():
                candidate = (repo_root / relative).resolve()
                if not candidate.is_relative_to(repo_root):
                    errors.append(f"integration manifest path escapes repo: {relative}")
                elif not candidate.is_file():
                    errors.append(f"integration manifest file missing: {relative}")
                elif sha256_file(candidate) != expected:
                    errors.append(f"integration manifest digest mismatch: {relative}")
        except ContinuityError as exc:
            errors.append(str(exc))
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
