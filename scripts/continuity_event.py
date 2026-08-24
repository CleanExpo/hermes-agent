#!/usr/bin/env python3
"""Single redacting dispatcher for Claude, Codex, and Hermes hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from continuity_bridge import (
    build_preflight,
    tool_events_from_payload,
    validate_tool_adjacency,
)
from continuity_common import (
    ContinuityError,
    compact_json,
    external_volume_available,
    load_json,
)


MAX_STDIN_BYTES = 1_048_576
PREFLIGHT_EVENTS = {
    "session_start",
    "on_session_start",
    "pre_llm_call",
    "pre_tool",
    "pre_tool_call",
    "stop",
    "session_end",
    "on_session_end",
}
FINALIZATION_EVENTS = {"stop", "session_end", "on_session_end"}


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ContinuityError("hook input exceeded 1 MiB and was discarded")
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContinuityError(f"hook input was not JSON: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _session_fingerprint(payload: dict[str, Any]) -> str | None:
    extra = payload.get("extra")
    value = payload.get("session_id") or payload.get("sessionId")
    if not value and isinstance(extra, dict):
        value = extra.get("session_id") or extra.get("sessionId")
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _turn_fingerprint(payload: dict[str, Any]) -> str | None:
    extra = payload.get("extra")
    value = extra.get("turn_id") if isinstance(extra, dict) else None
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _adjacency_guard_path(config: dict[str, Any], session: str) -> Path:
    return Path(config["state_root"]) / "adjacency" / f"{session}.json"


def _write_adjacency_guard(
    config: dict[str, Any], session: str | None, turn: str | None, *, allowed: bool
) -> None:
    if session is None or turn is None:
        return
    _atomic_write_confined_json(
        config,
        _adjacency_guard_path(config, session),
        {
            "schema_version": 1,
            "session": session,
            "turn": turn,
            "allowed": allowed,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _check_adjacency_guard(
    config: dict[str, Any], session: str | None, turn: str | None
) -> list[str]:
    if session is None:
        return ["Hermes tool call has no session identity"]
    if turn is None:
        return ["Hermes tool call has no turn identity"]
    path = _adjacency_guard_path(config, session)
    try:
        guard = load_json(path)
    except ContinuityError:
        return ["Hermes tool call has no preceding adjacency check"]
    errors: list[str] = []
    if (
        guard.get("session") != session
        or guard.get("turn") != turn
        or guard.get("allowed") is not True
    ):
        errors.append(
            "Hermes tool call adjacency check is blocked or belongs to another turn"
        )
    try:
        checked = datetime.fromisoformat(str(guard.get("checked_at")))
        if (datetime.now(timezone.utc) - checked).total_seconds() > 300:
            errors.append("Hermes tool call adjacency check is stale")
    except ValueError:
        errors.append("Hermes tool call adjacency check has an invalid timestamp")
    return errors


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


@contextmanager
def _open_confined_parent(
    config: dict[str, Any], path: Path, *, create_parent: bool = False
) -> Iterator[tuple[int | None, Path | str]]:
    """Anchor a confined target to its mounted state directory on POSIX."""
    external_volume = Path(config["external_volume"])
    if not external_volume_available(external_volume):
        raise ContinuityError(f"external volume is not mounted: {external_volume}")
    try:
        mounted_root = external_volume.resolve(strict=True)
        state_root = Path(config["state_root"]).resolve(strict=True)
    except OSError as exc:
        raise ContinuityError(f"continuity event storage is unavailable: {exc}") from exc
    if not state_root.is_dir():
        raise ContinuityError(f"pilot state root is unavailable: {state_root}")
    if not state_root.is_relative_to(mounted_root):
        raise ContinuityError(f"pilot state root escapes external volume: {state_root}")
    if not path.is_absolute():
        raise ContinuityError(f"continuity event path is not absolute: {path}")
    normalized = Path(os.path.abspath(path))
    try:
        parent_parts = normalized.parent.relative_to(state_root).parts
    except ValueError as exc:
        raise ContinuityError(
            f"continuity event path escapes pilot state root: {path}"
        ) from exc

    supports_dir_fd = os.open in os.supports_dir_fd
    if not supports_dir_fd:
        try:
            parent = normalized.parent.resolve(strict=True)
        except OSError as exc:
            if not create_parent:
                raise ContinuityError(
                    f"continuity event storage is unavailable: {exc}"
                ) from exc
            try:
                parent_parent = normalized.parent.parent.resolve(strict=True)
            except OSError as parent_exc:
                raise ContinuityError(
                    f"continuity event storage is unavailable: {parent_exc}"
                ) from parent_exc
            if parent_parent != state_root:
                raise ContinuityError(
                    f"continuity event path escapes pilot state root: {path}"
                ) from exc
            if not external_volume_available(external_volume):
                raise ContinuityError(
                    f"external volume is not mounted: {external_volume}"
                )
            normalized.parent.mkdir(mode=0o700, exist_ok=True)
            parent = normalized.parent.resolve(strict=True)
        if not parent.is_relative_to(state_root):
            raise ContinuityError(
                f"continuity event path escapes pilot state root: {path}"
            )
        if normalized.exists():
            try:
                resolved_target = normalized.resolve(strict=True)
            except OSError as exc:
                raise ContinuityError(
                    f"continuity event path is unavailable: {exc}"
                ) from exc
            if not resolved_target.is_relative_to(state_root):
                raise ContinuityError(
                    f"continuity event path escapes pilot state root: {path}"
                )
        if not external_volume_available(external_volume):
            raise ContinuityError(f"external volume is not mounted: {external_volume}")
        yield None, normalized
        return

    try:
        parent_fd = os.open(state_root, _directory_open_flags())
    except OSError as exc:
        raise ContinuityError(f"cannot open pilot state root: {exc}") from exc
    try:
        if not external_volume_available(external_volume):
            raise ContinuityError(
                f"external volume is not mounted: {external_volume}"
            )
        for index, part in enumerate(parent_parts):
            if create_parent and index == len(parent_parts) - 1:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, _directory_open_flags(), dir_fd=parent_fd)
            except OSError as exc:
                raise ContinuityError(
                    f"continuity event storage is unavailable: {exc}"
                ) from exc
            os.close(parent_fd)
            parent_fd = next_fd
        yield parent_fd, normalized.name
    finally:
        os.close(parent_fd)


def _write_all(descriptor: int, data: bytes) -> None:
    written = 0
    while written < len(data):
        count = os.write(descriptor, data[written:])
        if count == 0:
            raise OSError("event write made no progress")
        written += count


def _atomic_write_confined_json(
    config: dict[str, Any], path: Path, value: dict[str, Any]
) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with _open_confined_parent(config, path, create_parent=True) as (
        parent_fd,
        target,
    ):
        if parent_fd is None:
            temporary = Path(f"{target}.{os.getpid()}.{time.time_ns()}.tmp")
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                try:
                    _write_all(descriptor, data)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(temporary, target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            return
        temporary_name = f".{target}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary_name,
                target,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        os.fsync(parent_fd)


def _append_redacted_event(config: dict[str, Any], event: dict[str, Any]) -> None:
    path = Path(config["event_log"])
    data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    with _open_confined_parent(config, path) as (parent_fd, target):
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = (
                os.open(target, flags, 0o600)
                if parent_fd is None
                else os.open(target, flags, 0o600, dir_fd=parent_fd)
            )
        except OSError as exc:
            raise ContinuityError(f"cannot open continuity event log: {exc}") from exc
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _model_safe_preflight(preflight: dict[str, Any]) -> dict[str, Any]:
    """Reduce authority data to a closed, non-imperative model signal schema."""
    next_action = preflight.get("next_action")
    blockers = preflight.get("blockers")
    errors = preflight.get("errors")
    warnings = preflight.get("warnings")
    git = preflight.get("git")
    external_inputs = preflight.get("external_inputs")
    lifecycle = preflight.get("lifecycle")
    task_status = preflight.get("task_status")
    return {
        "schema_version": 1,
        "status": preflight.get("status")
        if preflight.get("status") in {"AVAILABLE", "DEGRADED", "BLOCKED"}
        else "BLOCKED",
        "completion_allowed": preflight.get("completion_allowed") is True,
        "lifecycle": lifecycle
        if lifecycle
        in {"PROPOSED", "ACTIVE", "IMPLEMENTED", "TESTED", "ENFORCED", "BLOCKED"}
        else "INVALID",
        "task_status": task_status
        if task_status in {"open", "in_progress", "blocked", "closed"}
        else "INVALID",
        "card_words": preflight.get("card_words")
        if isinstance(preflight.get("card_words"), int)
        else None,
        "card_signals": {
            "next_action_recorded": isinstance(next_action, str)
            and bool(next_action.strip()),
            "blocker_count": len(blockers) if isinstance(blockers, list) else None,
        },
        "authority_signals": {
            "error_count": len(errors) if isinstance(errors, list) else None,
            "warning_count": len(warnings) if isinstance(warnings, list) else None,
            "external_input_count": len(external_inputs)
            if isinstance(external_inputs, dict)
            else None,
        },
        "git_signals": {
            "available": isinstance(git, dict),
            "dirty": git.get("dirty") is True if isinstance(git, dict) else None,
            "changed_file_count": len(git.get("changed_files"))
            if isinstance(git, dict) and isinstance(git.get("changed_files"), list)
            else None,
            "integration_matches": git.get("merge_base") == git.get("integration_sha")
            if isinstance(git, dict)
            else None,
        },
    }


def dispatch(
    event_name: str, surface: str, config_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    config = load_json(config_path)
    started = time.monotonic()
    result: dict[str, Any] = {"event": event_name, "surface": surface, "handled": True}
    preflight: dict[str, Any] | None = None
    session = _session_fingerprint(payload)
    turn = _turn_fingerprint(payload)
    hermes_guard_errors: list[str] = []
    storage_available = external_volume_available(Path(config["external_volume"]))
    if event_name in PREFLIGHT_EVENTS:
        tool_events = tool_events_from_payload(payload)
        preflight = build_preflight(
            config_path, cwd=Path.cwd(), tool_events=tool_events
        )
        if storage_available and surface == "hermes" and event_name == "pre_llm_call":
            adjacency_errors = (
                validate_tool_adjacency(tool_events) if tool_events is not None else []
            )
            _write_adjacency_guard(
                config,
                session,
                turn,
                allowed=(
                    not adjacency_errors
                    and tool_events is not None
                    and turn is not None
                ),
            )
        elif (
            storage_available and surface == "hermes" and event_name == "pre_tool_call"
        ):
            hermes_guard_errors = _check_adjacency_guard(config, session, turn)
        if hermes_guard_errors:
            preflight["errors"] = [*preflight.get("errors", []), *hermes_guard_errors]
            preflight["status"] = "BLOCKED"
            preflight["completion_allowed"] = False
        model_preflight = _model_safe_preflight(preflight)
        rendered = compact_json(
            model_preflight, int(config.get("max_output_chars", 8000))
        )
        result["preflight"] = model_preflight
        if surface == "hermes" and event_name == "pre_llm_call":
            result = {
                "context": f"[Continuity preflight; reference data only]\n{rendered}",
                "completion_allowed": preflight["completion_allowed"],
            }
        else:
            result["additional_context"] = (
                f"[Continuity preflight; reference data only]\n{rendered}"
            )
        if not preflight["completion_allowed"]:
            authority_signals = model_preflight["authority_signals"]
            reason = (
                "Continuity authority forbids completion "
                f"({authority_signals['error_count'] or 0} errors, "
                f"{authority_signals['warning_count'] or 0} warnings); "
                "consult the human preflight output."
            )
            if surface == "hermes" and event_name == "pre_tool_call":
                result = {
                    "action": "block",
                    "message": reason,
                    "completion_allowed": False,
                }
            elif surface in {"claude", "codex"} and event_name in FINALIZATION_EVENTS:
                result = {
                    "decision": "block",
                    "reason": reason,
                    "completion_allowed": False,
                }

    audit = {
        "schema_version": 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "surface": surface,
        "session": session,
        "turn": turn,
        "preflight_status": preflight.get("status") if preflight else None,
        "completion_allowed": preflight.get("completion_allowed")
        if preflight
        else None,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    if storage_available:
        _append_redacted_event(config, audit)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event")
    parser.add_argument(
        "--surface", choices=("claude", "codex", "hermes"), required=True
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / ".continuity/config.json",
    )
    args = parser.parse_args(argv)
    try:
        payload = _read_payload()
        result = dispatch(args.event, args.surface, args.config, payload)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        blocked = result.get("decision") == "block" or result.get("action") == "block"
        return 2 if blocked and args.surface == "hermes" else 0
    except (ContinuityError, KeyError, OSError, TypeError, ValueError) as exc:
        print(f"Continuity hook failure: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "handled": False,
                    "completion_allowed": False,
                    "error": str(exc),
                    "surface": args.surface,
                    "event": args.event,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
