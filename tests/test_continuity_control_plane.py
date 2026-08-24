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
import continuity_gate
from continuity_bridge import build_preflight, validate_tool_adjacency
from continuity_common import ContinuityError, atomic_write_json, git_state, sha256_file
from continuity_event import dispatch
from continuity_gate import create_receipt, promote, verify_receipt
from install_continuity_adapters import (
    install_hermes_adapter,
    render_project_adapters,
    rollback_hermes_adapter,
)


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


def _lock_fake_bd(lock_path: Path, fake_bd: Path) -> None:
    import platform

    machine = (
        platform
        .machine()
        .lower()
        .replace("aarch64", "arm64")
        .replace("x86_64", "amd64")
    )
    atomic_write_json(
        lock_path,
        {
            "schema_version": 1,
            "tools": {
                "beads": {
                    "version": "fixture",
                    f"{platform.system().lower()}-{machine}-sha256": sha256_file(
                        fake_bd
                    ),
                }
            },
        },
    )


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

    for relative in ("AGENTS.md", ".specify/memory/constitution.md"):
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
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "path = pathlib.Path(os.environ['BEADS_DIR']).parent / 'issues.jsonl'\n"
        "items = [json.loads(line) for line in path.read_text().splitlines() if line]\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'close':\n"
        "    items[0]['status'] = 'closed'\n"
        "    path.write_text('\\n'.join(json.dumps(item) for item in items) + '\\n')\n"
        "print(json.dumps(items))\n",
        encoding="utf-8",
    )
    fake_bd.chmod(0o755)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    (scripts_dir / "evidence_pass.py").write_text(
        "print('=== Summary: 1 files, 1 tests passed, 0 failed (100% complete) ===')\n",
        encoding="utf-8",
    )
    (scripts_dir / "evidence_fail.py").write_text(
        "import sys\nprint('=== Summary: 1 files, 0 tests passed, 1 failed ===')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    (scripts_dir / "runtime_check.py").write_text(
        "print('runtime ok')\n", encoding="utf-8"
    )
    (scripts_dir / "rollback_check.py").write_text(
        "print('rollback ok')\n", encoding="utf-8"
    )
    (scripts_dir / "zero_tests.py").write_text(
        "print('command ran without a test summary')\n", encoding="utf-8"
    )
    (scripts_dir / "env_check.py").write_text(
        "import os, sys\n"
        "print('=== Summary: 1 files, 1 tests passed, 0 failed ===')\n"
        "sys.exit(1 if 'TOP_SECRET' in os.environ else 0)\n",
        encoding="utf-8",
    )
    lock_path = repo / ".continuity/toolchain.lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_fake_bd(lock_path, fake_bd)

    event_log = state / "events.jsonl"
    config = {
        "schema_version": 1,
        "project": "Hermes",
        "repo_id": "hermes-agent",
        "integration_ref": "HEAD",
        "toolchain_lock": ".continuity/toolchain.lock.json",
        "goal": GOAL,
        "expected_repo_root": str(repo.resolve()),
        "external_volume": "/",
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
        "instructions": ["AGENTS.md", ".specify/memory/constitution.md"],
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
        "lock": lock_path,
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
                "argv": [sys.executable, "scripts/evidence_pass.py"],
                "scope": "full" if target == "ENFORCED" else "focused",
            }
        ],
        runtime_checks=[
            {
                "name": "sandbox-hosts",
                "argv": [sys.executable, "scripts/runtime_check.py"],
                "surface": "sandbox",
            }
        ],
        rollback={
            "name": "rollback",
            "argv": [sys.executable, "scripts/rollback_check.py"],
            "mode": "dry-run",
        },
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
                "argv": [sys.executable, "scripts/evidence_fail.py"],
                "scope": "focused",
            }
        ],
        runtime_checks=[
            {
                "name": "sandbox",
                "argv": [sys.executable, "scripts/runtime_check.py"],
                "surface": "sandbox",
            }
        ],
        rollback={
            "name": "rollback",
            "argv": [sys.executable, "scripts/rollback_check.py"],
            "mode": "dry-run",
        },
        require_mounted_volume=False,
    )
    assert failed["result"] == "FAIL"

    enforced = _passing_receipt(pilot, target="ENFORCED")
    assert enforced["result"] == "PASS"

    receipt_path = pilot["state"] / "receipt.json"
    atomic_write_json(receipt_path, _passing_receipt(pilot))
    assert verify_receipt(pilot["config"], receipt_path, cwd=pilot["repo"]) == []
    (pilot["repo"] / "seed.txt").write_text("changed\n", encoding="utf-8")
    _git(pilot["repo"], "add", "seed.txt")
    _git(pilot["repo"], "commit", "-m", "change exact SHA")
    assert any(
        "evidence commit is stale" in error for error in _preflight(pilot)["errors"]
    )
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


def test_gate_executes_evidence_and_rejects_legacy_attestations(
    pilot: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ContinuityError, match="unknown keys"):
        create_receipt(
            pilot["config"],
            cwd=pilot["repo"],
            risk_tier="T1",
            lifecycle_target="TESTED",
            commands=[
                {
                    "name": "fabricated",
                    "argv": [sys.executable, "scripts/evidence_pass.py"],
                    "passed": True,
                }
            ],
            runtime_checks=[],
            rollback={},
            require_mounted_volume=False,
        )

    zero = create_receipt(
        pilot["config"],
        cwd=pilot["repo"],
        risk_tier="T1",
        lifecycle_target="TESTED",
        commands=[
            {
                "name": "zero-tests",
                "argv": [sys.executable, "scripts/zero_tests.py"],
            }
        ],
        runtime_checks=[],
        rollback={},
        require_mounted_volume=False,
    )
    assert zero["result"] == "FAIL"

    monkeypatch.setenv("TOP_SECRET", "must-not-reach-child")
    clean_env = create_receipt(
        pilot["config"],
        cwd=pilot["repo"],
        risk_tier="T1",
        lifecycle_target="TESTED",
        commands=[
            {
                "name": "minimal-env",
                "argv": [sys.executable, "scripts/env_check.py"],
            }
        ],
        runtime_checks=[],
        rollback={},
        require_mounted_volume=False,
    )
    assert clean_env["result"] == "PASS"
    assert "stdout" not in clean_env["commands"][0]


def test_pinned_inputs_invalidate_preflight_and_receipt(pilot: dict[str, Path]) -> None:
    instruction = pilot["state"] / "external-skill.md"
    instruction.write_text("v1\n", encoding="utf-8")
    config = json.loads(pilot["config"].read_text(encoding="utf-8"))
    config["external_instructions"] = [str(instruction)]
    atomic_write_json(pilot["config"], config)
    receipt_path = pilot["state"] / "receipt.json"
    atomic_write_json(receipt_path, _passing_receipt(pilot))
    instruction.write_text("v2\n", encoding="utf-8")
    assert any(
        "external instruction" in error
        for error in verify_receipt(pilot["config"], receipt_path, cwd=pilot["repo"])
    )

    pilot["bd"].write_text(pilot["bd"].read_text(encoding="utf-8") + "\n")
    assert any("digest mismatch" in error for error in _preflight(pilot)["errors"])


def test_black_box_dispatch_and_host_block_shapes(pilot: dict[str, Path]) -> None:
    entry = REPO_ROOT / ".specify/events.py"
    for surface in ("claude", "codex", "hermes"):
        result = subprocess.run(
            [
                sys.executable,
                str(entry),
                "session_start" if surface != "hermes" else "on_session_start",
                "--surface",
                surface,
                "--config",
                str(pilot["config"]),
            ],
            cwd=pilot["repo"],
            input=json.dumps({"session_id": "black-box"}),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout).get("additional_context")

    interrupted = {
        "events": [
            {"type": "tool_use", "id": "call-1"},
            {"type": "user", "content": "interrupt"},
            {"type": "tool_result", "tool_use_id": "call-1"},
        ]
    }
    hermes = dispatch("pre_tool_call", "hermes", pilot["config"], interrupted)
    assert hermes["action"] == "block"

    pilot["card"].unlink()
    claude = dispatch("stop", "claude", pilot["config"], {})
    assert claude["decision"] == "block"
    audit = pilot["events"].read_text(encoding="utf-8")
    assert '"preflight_status":"BLOCKED"' in audit


def test_hermes_adapter_has_owned_reversible_before_image(
    pilot: dict[str, Path], tmp_path: Path
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    original = {
        "model": "fixture",
        "hooks": {
            "pre_tool_call": [{"command": "existing", "timeout": 3}],
            "unrelated": [{"command": "preserve-me"}],
        },
    }
    config_path = hermes_home / "config.yaml"
    import yaml

    config_path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")
    installed = install_hermes_adapter(REPO_ROOT, hermes_home)
    assert installed["changed"] is True
    active = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert active["hooks"]["pre_tool_call"][0]["fail_closed"] is True
    assert (
        rollback_hermes_adapter(REPO_ROOT, hermes_home, apply=False)["rollback_valid"]
        is True
    )
    rollback_hermes_adapter(REPO_ROOT, hermes_home, apply=True)
    restored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert restored == original


def test_enforced_promotion_closes_task_and_recovers_interruption(
    pilot: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path = pilot["state"] / "enforced.json"
    atomic_write_json(receipt_path, _passing_receipt(pilot, target="ENFORCED"))
    real_run_beads = continuity_gate._run_beads
    monkeypatch.setattr(
        continuity_gate,
        "_run_beads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContinuityError("simulated close interruption")
        ),
    )
    with pytest.raises(ContinuityError, match="simulated close interruption"):
        promote(pilot["config"], receipt_path, cwd=pilot["repo"], target="ENFORCED")
    assert any("RECOVERY_REQUIRED" in error for error in _preflight(pilot)["errors"])

    monkeypatch.setattr(continuity_gate, "_run_beads", real_run_beads)
    promote(pilot["config"], receipt_path, cwd=pilot["repo"], target="ENFORCED")
    result = _preflight(pilot)
    assert result["status"] == "AVAILABLE"
    assert result["lifecycle"] == "ENFORCED"
    assert result["task_status"] == "closed"
    promote(pilot["config"], receipt_path, cwd=pilot["repo"], target="ENFORCED")


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
