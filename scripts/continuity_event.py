#!/usr/bin/env python3
"""Single redacting dispatcher for Claude, Codex, and Hermes hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from continuity_bridge import build_preflight
from continuity_common import ContinuityError, compact_json, load_json


MAX_STDIN_BYTES = 1_048_576
PREFLIGHT_EVENTS = {"session_start", "on_session_start", "pre_llm_call"}


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
    value = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _append_redacted_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def dispatch(
    event_name: str, surface: str, config_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    config = load_json(config_path)
    result: dict[str, Any] = {"event": event_name, "surface": surface, "handled": True}
    if event_name in PREFLIGHT_EVENTS:
        preflight = build_preflight(config_path, cwd=Path.cwd())
        rendered = compact_json(preflight, int(config.get("max_output_chars", 8000)))
        result["preflight"] = preflight
        if surface == "hermes" and event_name == "pre_llm_call":
            result = {"context": f"[Continuity preflight]\n{rendered}"}
        else:
            result["additional_context"] = f"[Continuity preflight]\n{rendered}"

    audit = {
        "schema_version": 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "surface": surface,
        "session": _session_fingerprint(payload),
        "preflight_status": result.get("preflight", {}).get("status")
        if isinstance(result.get("preflight"), dict)
        else None,
    }
    _append_redacted_event(Path(config["event_log"]), audit)
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
        return 0
    except (ContinuityError, KeyError, OSError, TypeError, ValueError) as exc:
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
