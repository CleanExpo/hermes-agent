"""Shared primitives for the local Hermes continuity pilot."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ContinuityError(RuntimeError):
    """A safe, user-readable continuity failure."""


@dataclass(frozen=True)
class GitState:
    root: str
    branch: str
    commit: str
    dirty: bool
    changed_files: tuple[str, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "branch": self.branch,
            "commit": self.commit,
            "dirty": self.dirty,
            "changed_files": list(self.changed_files),
            "fingerprint": self.fingerprint,
        }


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContinuityError(f"expected a JSON object in {path}")
    return value


def read_markdown_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContinuityError(f"cannot read authority {path}: {exc}") from exc
    if len(text) > 256_000:
        raise ContinuityError(f"authority is unexpectedly large: {path}")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ContinuityError(f"missing YAML front matter in {path}")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ContinuityError(f"unterminated YAML front matter in {path}") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise ContinuityError(f"invalid YAML front matter in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContinuityError(f"expected YAML mapping in {path}")
    return data, "\n".join(lines[end + 1 :]).lstrip("\n")


def render_markdown_frontmatter(data: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    suffix = body.rstrip() + "\n" if body else ""
    return f"---\n{header}\n---\n\n{suffix}"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContinuityError(f"command failed or timed out: {args[0]}: {exc}") from exc


def git_state(cwd: Path, timeout: float = 10) -> GitState:
    def git(*args: str) -> str:
        result = run_command(["git", *args], cwd=cwd, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ContinuityError(f"git {' '.join(args)} failed: {detail}")
        return result.stdout.strip()

    root = str(Path(git("rev-parse", "--show-toplevel")).resolve())
    branch = git("branch", "--show-current") or "DETACHED"
    commit = git("rev-parse", "HEAD")
    status_result = run_command(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=cwd,
        timeout=timeout,
    )
    if status_result.returncode != 0:
        detail = (status_result.stderr or status_result.stdout).strip()
        raise ContinuityError(f"git status failed: {detail}")
    status = status_result.stdout.rstrip("\r\n")
    changed = tuple(
        line[3:] if len(line) >= 4 else line for line in status.splitlines()
    )
    digest = hashlib.sha256()
    diff_result = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if diff_result.returncode != 0:
        raise ContinuityError("git diff failed while fingerprinting exact state")
    digest.update(diff_result.stdout)
    untracked_result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if untracked_result.returncode != 0:
        raise ContinuityError("git ls-files failed while fingerprinting exact state")
    for encoded in sorted(
        item for item in untracked_result.stdout.split(b"\0") if item
    ):
        relative = os.fsdecode(encoded)
        candidate = Path(root) / relative
        digest.update(encoded)
        if candidate.is_symlink():
            digest.update(
                os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            )
        elif candidate.is_file():
            digest.update(candidate.read_bytes())
    return GitState(root, branch, commit, bool(status), changed, digest.hexdigest())


def external_volume_available(path: Path) -> bool:
    """Require a mounted volume, not merely a same-named internal directory."""
    try:
        return path.exists() and path.is_mount()
    except OSError:
        return False


def compact_json(value: dict[str, Any], max_chars: int) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return rendered
    minimal = {
        "schema_version": value.get("schema_version", 1),
        "status": "BLOCKED",
        "completion_allowed": False,
        "errors": ["preflight output exceeded configured bound"],
    }
    return json.dumps(minimal, sort_keys=True, separators=(",", ":"))


def receipt_errors(receipt: dict[str, Any], current: GitState) -> list[str]:
    """Return every reason an exact-state receipt cannot authorize promotion."""
    errors: list[str] = []
    if receipt.get("gate") != "hermes-continuity-gate/1":
        errors.append("receipt was not emitted by hermes-continuity-gate/1")
    target = receipt.get("lifecycle_target")
    if target not in {"TESTED", "ENFORCED"}:
        errors.append(f"invalid lifecycle target: {target!r}")
    expected_git = current.as_dict()
    if receipt.get("git") != expected_git:
        errors.append("receipt repository, branch, commit, or dirty state is stale")
    authority = receipt.get("authority_check")
    if not isinstance(authority, dict) or authority.get("passed") is not True:
        errors.append("strict authority check is absent or failed")

    tier = receipt.get("risk_tier")
    if tier not in {"T0", "T1", "T2", "T3"}:
        errors.append(f"invalid risk tier: {tier!r}")
        tier = "T3"
    commands = receipt.get("commands")
    if not isinstance(commands, list) or (tier != "T0" and not commands):
        errors.append("required command evidence is absent")
        commands = []
    has_positive_test = False
    for command in commands:
        if not isinstance(command, dict):
            errors.append("command evidence is malformed")
            continue
        name = command.get("name", "unnamed")
        if command.get("exit_code") != 0:
            errors.append(f"command failed: {name}")
        test_count = command.get("test_count", 0)
        skipped = command.get("skipped", 0)
        if not isinstance(test_count, int) or test_count < 0:
            errors.append(f"command has invalid test count: {name}")
        elif test_count > 0:
            has_positive_test = True
        if not isinstance(skipped, int) or skipped < 0:
            errors.append(f"command has invalid skip count: {name}")
        elif skipped != 0:
            errors.append(f"command contains skips: {name}")
        if command.get("flaky") is True:
            errors.append(f"command is flaky: {name}")
    if tier != "T0" and not has_positive_test:
        errors.append("receipt has no command with a positive test count")
    if target == "ENFORCED" and not any(
        isinstance(command, dict)
        and command.get("scope") == "full"
        and command.get("exit_code") == 0
        for command in commands
    ):
        errors.append("ENFORCED requires a passing full-suite or CI command")

    runtime_checks = receipt.get("runtime_checks")
    if tier in {"T2", "T3"} and (
        not isinstance(runtime_checks, list) or not runtime_checks
    ):
        errors.append("runtime checks are absent for T2/T3")
        runtime_checks = []
    for check in runtime_checks or []:
        if not isinstance(check, dict) or check.get("passed") is not True:
            errors.append(
                f"runtime check failed: {check.get('name', 'unnamed') if isinstance(check, dict) else 'malformed'}"
            )

    rollback = receipt.get("rollback")
    if tier == "T3" and (
        not isinstance(rollback, dict) or rollback.get("passed") is not True
    ):
        errors.append("rollback proof is absent or failed for T3")
    return errors
