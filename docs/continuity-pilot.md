# Local cross-agent continuity pilot

This pilot gives Claude, Codex, and Hermes the same bounded orientation without
making a memory system the authority for completion.

## Authority map

| Authority | Purpose | Canonical pilot location |
| --- | --- | --- |
| Basic Memory | Under-1,200-word human orientation card | External `continuity/hermes-agent/current.md` |
| Beads | Exact active work item | External `issues.jsonl`, task `hermes-continuity-b6l` |
| Spec Kit | Intended behaviour and lifecycle | `specs/001-global-continuity-pilot/` |
| Gate receipt | Exact-state test/runtime/rollback proof | External `receipts/` |

Preflight reads all four relevant surfaces, verifies agreement, and emits no more
than 8,000 characters. A Beads CLI timeout falls back to readable JSONL but marks the
result `DEGRADED` and forbids completion. Missing or conflicting state is `BLOCKED`.
Receipt creation separately runs a live, longer-bounded `bd show` check; JSONL alone
can never authorize a terminal lifecycle state.

## Tool pins and isolation

- Basic Memory `0.22.1`
- Beads `1.0.4`
- Spec Kit `1.0.1`
- State root: `/Volumes/Storage Unit/Application-Data/Continuity-Pilot/hermes-20260824`
- Worktree: `/Volumes/Storage Unit/Application-Data/Codex/worktrees/hermes-continuity-pilot-20260824`

The Basic Memory Hermes transcript-capture plugin is deliberately not enabled. The
pilot does not alter `~/.hermes`, `~/.claude`, or `~/.codex`.

## Operator commands

```bash
python3 scripts/continuity_bridge.py --config .continuity/config.json --cwd "$PWD"
python3 scripts/install_continuity_adapters.py --repo-root "$PWD" --check
python3 scripts/continuity_gate.py static --config .continuity/config.json
scripts/run_tests.sh tests/test_continuity_control_plane.py
```

To prove native Hermes registration without touching the live profile:

```bash
python3 scripts/install_continuity_adapters.py \
  --repo-root "$PWD" \
  --hermes-home "/Volumes/Storage Unit/Application-Data/Continuity-Pilot/hermes-20260824/hermes-home/.hermes" \
  --apply-hermes
HERMES_HOME="/Volumes/Storage Unit/Application-Data/Continuity-Pilot/hermes-20260824/hermes-home/.hermes" \
  hermes hooks list --json
```

## Receipt shape

Command evidence is a JSON list containing `name`, `exit_code`, `test_count`,
`skipped`, and `flaky`. T2/T3 receipts also require runtime checks; T3 requires a
passed rollback proof. Create a receipt only after the final commit because any
commit or dirty-state change invalidates it.

```bash
python3 scripts/continuity_gate.py create-receipt \
  --config .continuity/config.json --cwd "$PWD" --risk-tier T3 --target TESTED \
  --commands-json /external/evidence/commands.json \
  --runtime-json /external/evidence/runtime.json \
  --rollback-json /external/evidence/rollback.json \
  --output /external/receipts/tested.json
python3 scripts/continuity_gate.py verify-receipt \
  --config .continuity/config.json --cwd "$PWD" --receipt /external/receipts/tested.json
```

`promote` is the only supported route to `TESTED` or `ENFORCED`. It updates the
external card, leaving the committed active specification unchanged so promotion does
not invalidate its own exact-state fingerprint. `ENFORCED` also closes the Beads item;
if that update fails, the card write is rolled back.

## Rollback

Rollback is recoverable and scoped:

1. Verify the shared checkout and live global hook files are unchanged.
2. Remove this isolated worktree through `git worktree remove` only after preserving
   any desired branch/receipt evidence.
3. Move the isolated Hermes `config.yaml` and external pilot state directory into an
   archive on the Storage Unit; do not delete them during the pilot.
4. Delete the pilot branch only after confirming no evidence is needed.

Because the live host profiles are not modified, rollback does not require restoring
global configuration or a Hermes database.
