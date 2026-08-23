from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import continuity_bridge
from continuity_bridge import build_preflight, validate_tool_adjacency
from continuity_common import ContinuityError, atomic_write_json, git_state
from continuity_event import dispatch
from continuity_gate import create_receipt, promote, verify_receipt
from install_continuity_adapters import render_project_adapters


GOAL = "Pilot deterministic cross-agent continuity in Hermes"
TASK_ID = "hermes-continuity-b6l"
CHANGE_ID = "001-global-continuity-pilot"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _frontmatter(data: dict, body: str = "fixture") -> str:
    import yaml

    return f"---\n{yaml.safe_dump(data, sort_keys=False).strip()}\n---\n\n{body}\n"


@pytest.fixture()
def pilot(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    _git(repo, "init", "-b", "pilot")
    _git(repo, "config", "user.email", "pilot@example.invalid")
    _git(repo, "config", "user.name", "Pilot")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "seed")

    for relative in ("AGENTS.md", "spec.md", ".specify/memory/constitution.md"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")

    spec_path = repo / "specs/001-global-continuity-pilot/spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        _frontmatter({
            "project": "Hermes",
            "repo_id": "hermes-agent",
            "goal": GOAL,
            "state": "ACTIVE",
            "active_task": TASK_ID,
            "change_id": CHANGE_ID,
            "folder": str(repo.resolve()),
        }),
        encoding="utf-8",
    )
    card_path = state / "current.md"
    card_path.write_text(
        _frontmatter({
            "project": "Hermes",
            "folder": str(repo.resolve()),
            "goal": GOAL,
            "user_context": "bounded fixture",
            "state": "ACTIVE",
            "active_task": TASK_ID,
            "spec_change": CHANGE_ID,
            "evidence": {"commit": _git(repo, "rev-parse", "HEAD"), "receipt": None},
            "next_action": "run the pilot",
            "blockers": [],
            "supersedes": "none",
        }),
        encoding="utf-8",
    )
    task = {
        "id": TASK_ID,
        "title": GOAL,
        "status": "in_progress",
        "spec_id": "specs/001-global-continuity-pilot/spec.md",
    }
    issues = state / "issues.jsonl"
    issues.write_text(json.dumps(task) + "\n", encoding="utf-8")
    fake_bd = state / "bd"
    fake_bd.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps([task]) + "'\n", encoding="utf-8"
    )
    fake_bd.chmod(0o755)

    event_log = state / "events.jsonl"
    config = {
        "schema_version": 1,
        "project": "Hermes",
        "repo_id": "hermes-agent",
        "goal": GOAL,
        "expected_repo_root": str(repo.resolve()),
        "external_volume": str(tmp_path),
        "state_root": str(state),
        "max_output_chars": 8000,
        "basic_memory": {"project": "pilot", "card_path": str(card_path)},
        "beads": {
            "binary": str(fake_bd),
            "data_dir": str(state / "beads"),
            "issues_path": str(issues),
            "active_task": TASK_ID,
            "timeout_seconds": 1,
        },
        "spec": {
            "change_id": CHANGE_ID,
            "path": "specs/001-global-continuity-pilot/spec.md",
        },
        "instructions": ["AGENTS.md", "spec.md", ".specify/memory/constitution.md"],
        "external_instructions": [],
        "event_log": str(event_log),
        "receipt_dir": str(state / "receipts"),
    }
    config_path = state / "config.json"
    atomic_write_json(config_path, config)
    return {
        "repo": repo,
        "state": state,
        "config": config_path,
        "card": card_path,
        "spec": spec_path,
        "issues": issues,
        "bd": fake_bd,
        "events": event_log,
    }


def _preflight(pilot: dict[str, Path]) -> dict:
    return build_preflight(
        pilot["config"], cwd=pilot["repo"], require_mounted_volume=False
    )


def _passing_receipt(pilot: dict[str, Path], target: str = "TESTED") -> dict:
    return create_receipt(
        pilot["config"],
        cwd=pilot["repo"],
        risk_tier="T3",
        lifecycle_target=target,
        commands=[
            {
                "name": "focused",
                "exit_code": 0,
                "test_count": 12,
                "skipped": 0,
                "flaky": False,
            }
        ],
        runtime_checks=[{"name": "sandbox-hosts", "passed": True}],
        rollback={"mode": "dry-run", "passed": True},
        require_mounted_volume=False,
    )


def test_healthy_preflight_recovers_exact_scope(pilot: dict[str, Path]) -> None:
    first = _preflight(pilot)
    second = _preflight(pilot)
    assert first == second
    assert first["status"] == "AVAILABLE"
    assert first["completion_allowed"] is True
    assert first["active_task"] == TASK_ID
    assert first["spec_change"] == CHANGE_ID
    assert len(json.dumps(first, separators=(",", ":"))) <= 8000


def test_wrong_folder_and_missing_drive_block(
    pilot: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = pilot["state"] / "other"
    other.mkdir()
    _git(other, "init", "-b", "other")
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "config", "user.name", "Other")
    (other / "seed.txt").write_text("other\n", encoding="utf-8")
    _git(other, "add", "seed.txt")
    _git(other, "commit", "-m", "other")
    wrong = build_preflight(pilot["config"], cwd=other, require_mounted_volume=False)
    assert wrong["status"] == "BLOCKED"
    assert any("wrong repository folder" in error for error in wrong["errors"])

    monkeypatch.setattr(
        continuity_bridge, "external_volume_available", lambda _path: False
    )
    missing = build_preflight(
        pilot["config"], cwd=pilot["repo"], require_mounted_volume=True
    )
    assert missing["completion_allowed"] is False
    assert any("not mounted" in error for error in missing["errors"])


def test_stale_task_missing_card_and_incomplete_spec_block(
    pilot: dict[str, Path],
) -> None:
    task = json.loads(pilot["issues"].read_text(encoding="utf-8"))
    task["status"] = "closed"
    pilot["issues"].write_text(json.dumps(task) + "\n", encoding="utf-8")
    pilot["bd"].write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps([task]) + "'\n", encoding="utf-8"
    )
    assert any("stale or terminal" in error for error in _preflight(pilot)["errors"])

    pilot["card"].unlink()
    assert any(
        "cannot read authority" in error for error in _preflight(pilot)["errors"]
    )

    pilot["card"].write_text("---\nproject: [unterminated\n---\n", encoding="utf-8")
    assert any("invalid YAML" in error for error in _preflight(pilot)["errors"])

    pilot["card"].write_text(_frontmatter({"project": "Hermes"}), encoding="utf-8")
    pilot["spec"].write_text(_frontmatter({"project": "Hermes"}), encoding="utf-8")
    errors = _preflight(pilot)["errors"]
    assert any("Basic Memory card missing" in error for error in errors)
    assert any("Spec Kit change missing" in error for error in errors)


def test_beads_timeout_uses_degraded_jsonl_and_forbids_completion(
    pilot: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = json.loads(pilot["config"].read_text(encoding="utf-8"))
    config["beads"]["timeout_seconds"] = 0.01
    atomic_write_json(pilot["config"], config)
    monkeypatch.setattr(
        continuity_bridge,
        "run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContinuityError("bd ready timed out")
        ),
    )
    result = _preflight(pilot)
    assert result["status"] == "DEGRADED"
    assert result["completion_allowed"] is False
    assert "JSONL" in result["warnings"][0]


def test_interrupted_tool_adjacency_is_rejected() -> None:
    events = [
        {"type": "server_tool_use", "id": "call-1"},
        {"type": "user", "content": "interrupt"},
        {"type": "tool_result", "tool_use_id": "call-1"},
    ]
    assert "interrupted" in validate_tool_adjacency(events)[0]
    assert not validate_tool_adjacency([
        {"type": "tool_use", "id": "call-1"},
        {"type": "tool_result", "tool_use_id": "call-1"},
    ])


def test_failed_test_and_changed_sha_invalidate_receipt(pilot: dict[str, Path]) -> None:
    failed = create_receipt(
        pilot["config"],
        cwd=pilot["repo"],
        risk_tier="T3",
        lifecycle_target="TESTED",
        commands=[
            {
                "name": "focused",
                "exit_code": 1,
                "test_count": 0,
                "skipped": 0,
                "flaky": False,
            }
        ],
        runtime_checks=[{"name": "sandbox", "passed": True}],
        rollback={"passed": True},
        require_mounted_volume=False,
    )
    assert failed["result"] == "FAIL"

    not_enforced = _passing_receipt(pilot, target="ENFORCED")
    assert not_enforced["result"] == "FAIL"

    receipt_path = pilot["state"] / "receipt.json"
    atomic_write_json(receipt_path, _passing_receipt(pilot))
    assert verify_receipt(pilot["config"], receipt_path, cwd=pilot["repo"]) == []
    (pilot["repo"] / "seed.txt").write_text("changed\n", encoding="utf-8")
    _git(pilot["repo"], "add", "seed.txt")
    _git(pilot["repo"], "commit", "-m", "change exact SHA")
    assert any(
        "stale" in error
        for error in verify_receipt(pilot["config"], receipt_path, cwd=pilot["repo"])
    )


def test_only_gate_promotes_terminal_state(
    pilot: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path = pilot["state"] / "receipt.json"
    atomic_write_json(receipt_path, _passing_receipt(pilot))
    promote(pilot["config"], receipt_path, cwd=pilot["repo"], target="TESTED")
    result = _preflight(pilot)
    assert result["lifecycle"] == "TESTED"
    assert result["completion_allowed"] is True

    with pytest.raises(Exception, match="only TESTED or ENFORCED"):
        promote(pilot["config"], receipt_path, cwd=pilot["repo"], target="ACTIVE")


def test_dispatcher_logs_metadata_not_payload(pilot: dict[str, Path]) -> None:
    payload = {"session_id": "session-secret", "tool_input": {"token": "TOP-SECRET"}}
    result = dispatch("post_tool", "hermes", pilot["config"], payload)
    assert result["handled"] is True
    log = pilot["events"].read_text(encoding="utf-8")
    assert "TOP-SECRET" not in log
    assert "session-secret" not in log
    assert "post_tool" in log


def test_committed_adapters_match_single_contract() -> None:
    for path, rendered in render_project_adapters(REPO_ROOT).items():
        assert path.read_text(encoding="utf-8") == rendered
