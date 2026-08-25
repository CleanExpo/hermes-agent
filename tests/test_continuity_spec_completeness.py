from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import continuity_bridge
from continuity_bridge import build_preflight, validate_spec_body
from continuity_common import GitState


GOAL = "Pilot deterministic cross-agent continuity in Hermes"
TASK_ID = "hermes-continuity-b6l"
CHANGE_ID = "001-global-continuity-pilot"
HARDLINE_SKILL_HEADINGS = (
    "When to Use",
    "Prerequisites",
    "How to Run",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
)


def _outside_fence_h2s_and_argument_fences(
    text: str,
) -> tuple[list[str], list[tuple[int, bool]]]:
    headings: list[str] = []
    arguments: list[tuple[int, bool]] = []
    fence_marker: str | None = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if stripped == "$ARGUMENTS" and fence_marker is not None:
            closes_immediately = (
                index + 1 < len(lines) and lines[index + 1].strip() == fence_marker
            )
            arguments.append((index + 1, closes_immediately))
        if fence_marker is None and line.startswith("## "):
            headings.append(line[3:].strip())
    assert fence_marker is None, "unclosed Markdown fence"
    return headings, arguments


def _frontmatter(data: dict, body: str) -> str:
    import yaml

    return f"---\n{yaml.safe_dump(data, sort_keys=False).strip()}\n---\n\n{body}"


@pytest.fixture()
def preflight_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    state_root = tmp_path / "state"
    repo.mkdir()
    state_root.mkdir()

    spec_path = repo / "specs/001-global-continuity-pilot/spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        _frontmatter(
            {
                "project": "Hermes",
                "repo_id": "hermes-agent",
                "goal": GOAL,
                "state": "ACTIVE",
                "active_task": TASK_ID,
                "change_id": CHANGE_ID,
                "folder": str(repo),
            },
            "# Complete fixture specification\n",
        ),
        encoding="utf-8",
    )
    card_path = state_root / "current.md"
    card_path.write_text(
        _frontmatter(
            {
                "project": "Hermes",
                "folder": str(repo),
                "goal": GOAL,
                "user_context": "bounded fixture",
                "state": "ACTIVE",
                "active_task": TASK_ID,
                "spec_change": CHANGE_ID,
                "evidence": {"commit": "a" * 40, "receipt": None},
                "next_action": "run the pilot",
                "blockers": [],
                "supersedes": "none",
            },
            "fixture card\n",
        ),
        encoding="utf-8",
    )
    config_path = repo / ".continuity/config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({
            "project": "Hermes",
            "goal": GOAL,
            "expected_repo_root": str(repo),
            "external_volume": str(tmp_path),
            "state_root": str(state_root),
            "basic_memory": {"card_path": str(card_path)},
            "beads": {"active_task": TASK_ID},
            "spec": {
                "path": str(spec_path.relative_to(repo)),
                "change_id": CHANGE_ID,
            },
            "instructions": [],
        }),
        encoding="utf-8",
    )
    git = GitState(
        root=str(repo),
        branch="pilot",
        commit="a" * 40,
        dirty=False,
        changed_files=(),
        fingerprint="b" * 64,
    )
    monkeypatch.setattr(continuity_bridge, "git_state", lambda *_a, **_kw: git)
    monkeypatch.setattr(
        continuity_bridge,
        "_read_beads",
        lambda *_a, **_kw: (
            {
                "id": TASK_ID,
                "title": GOAL,
                "status": "in_progress",
                "spec_id": str(spec_path.relative_to(repo)),
            },
            False,
            "",
        ),
    )
    monkeypatch.setattr(
        continuity_bridge, "external_input_digests", lambda *_a, **_kw: {}
    )
    return config_path, spec_path


@pytest.mark.parametrize("body", ["", "  \n", "<!-- template guidance only -->\n"])
def test_spec_body_requires_substantive_content(body: str) -> None:
    assert validate_spec_body(body) == [
        "Spec Kit change body is incomplete: no substantive content"
    ]


def test_spec_body_rejects_unresolved_template_markers() -> None:
    errors = validate_spec_body(
        "# [FEATURE NAME]\n\n<!-- guidance -->\nACTION REQUIRED: replace this\n"
    )

    assert errors == [
        "Spec Kit change body is incomplete: unresolved template marker "
        + repr("ACTION REQUIRED:"),
        "Spec Kit change body is incomplete: unresolved template marker "
        + repr("[FEATURE NAME]"),
    ]


def test_every_canonical_template_placeholder_is_rejected() -> None:
    template = continuity_bridge.SPEC_TEMPLATE_PATH.read_text(encoding="utf-8")
    markers = continuity_bridge._balanced_bracket_markers(template)

    assert "[DATE]" in markers
    assert "[Brief Title]" in markers
    clarification = (
        "[NEEDS CLARIFICATION: auth method not specified - email/password, SSO, OAuth?]"
    )
    assert clarification in markers
    for marker in markers:
        assert validate_spec_body(f"# Completed title\n\nUnresolved: {marker}\n")


def test_complete_spec_body_has_no_completeness_error() -> None:
    assert not validate_spec_body(
        "# Continuity pilot\n\nThe gate rejects stale authority with a named cause.\n"
    )


def test_preflight_blocks_empty_spec_body_with_named_cause(
    preflight_fixture: tuple[Path, Path],
) -> None:
    config_path, spec_path = preflight_fixture
    header = spec_path.read_text(encoding="utf-8").split(
        "# Complete fixture specification", 1
    )[0]
    spec_path.write_text(header, encoding="utf-8")

    result = build_preflight(
        config_path, cwd=config_path.parents[1], require_mounted_volume=False
    )

    assert result["status"] == "BLOCKED"
    assert result["completion_allowed"] is False
    assert (
        "Spec Kit change body is incomplete: no substantive content" in result["errors"]
    )


def test_spec_kit_skill_hardline_is_fence_aware_and_mirrored() -> None:
    source_root = REPO_ROOT / ".agents/skills"
    mirror_root = REPO_ROOT / ".claude/skills"
    sources = sorted(source_root.glob("speckit-*/SKILL.md"))

    source_names = {path.parent.name for path in sources}
    mirror_names = {
        path.name
        for path in mirror_root.glob("speckit-*")
        if (path / "SKILL.md").is_file()
    }
    assert sources
    assert source_names == mirror_names
    for source in sources:
        text = source.read_text(encoding="utf-8")
        headings, argument_fences = _outside_fence_h2s_and_argument_fences(text)
        required = [
            heading for heading in headings if heading in HARDLINE_SKILL_HEADINGS
        ]

        assert "Overview" not in headings, source
        assert required[0] == "When to Use", source
        assert [required.index(heading) for heading in HARDLINE_SKILL_HEADINGS] == list(
            range(len(HARDLINE_SKILL_HEADINGS))
        ), source
        assert argument_fences, source
        assert all(closed for _line, closed in argument_fences), source

        mirror = mirror_root / source.parent.name / "SKILL.md"
        assert mirror.read_bytes() == source.read_bytes(), source
