---
title: Hermes Safe Change Specification
status: proposed
date: 2026-08-24
scope: repository-wide
owners: maintainers and engineering agents
---

# Hermes Safe Change Specification

## 1. Purpose

Every Hermes change must remain testable, reversible, and bounded while it is
being built. A final green run is not enough: each material step must preserve
a known-good state and produce evidence that the changed behaviour works.

This specification governs code, configuration, data migrations, packaging,
Desktop, gateway, plugins, updates, backups, and operational repairs.

The north star is simple:

> Make the smallest correct change, prove it continuously, preserve user data,
> and never call a changed state safe using evidence from an older state.

`AGENTS.md` remains authoritative for repository architecture and conventions.
When this file is stricter about testing, state safety, or completion evidence,
this file governs the change workflow.

## 2. Non-negotiable invariants

A change must not:

1. write tests against the live `~/.hermes` state;
2. delete or rewrite user data without a verified recovery path;
3. mutate open or pinned sessions during retention maintenance;
4. write large state, caches, builds, or temporary files to the Mac Mini's
   internal disk when `/Volumes/Storage Unit` is the configured target;
5. continue a storage-dependent write when the external volume is unmounted;
6. change past conversation context, role alternation, toolsets, or the system
   prompt mid-conversation, except through the existing compression contract;
7. weaken or remove a failing test merely to make a change pass;
8. hide a failure as a skip, retry, warning, partial run, or mocked success;
9. mix unrelated repairs into the same change;
10. claim completion after the tested commit, working tree, configuration, or
    runtime has changed.

## 3. Evidence vocabulary

- **Baseline:** the result from the unchanged code and intended environment.
- **Red proof:** a test or deterministic reproduction that fails for the
  reported reason before the repair.
- **Focused test:** the smallest test that proves the changed behaviour.
- **Neighbour tests:** tests for direct callers, consumers, sibling paths, and
  the failure path surrounding the change.
- **Integration proof:** the real import, filesystem, database, process, or
  client-to-backend path, using isolated state rather than a mock-only path.
- **Release gate:** the repository's CI-equivalent suite required by the risk
  tier and change classifier.
- **Exact state:** repository, branch, commit SHA, diff, configuration, runtime
  target, and relevant data version used by the evidence.
- **Receipt:** the concise record binding commands, outcomes, skips, runtime
  checks, and rollback evidence to the exact state.

Passing assertions without exit code `0`, a visible final summary, and the
intended test collection is not a pass. A retry-pass is `FLAKY`, not green.

## 4. Risk tiers

Classify the change before editing. If uncertain, use the higher tier.

| Tier | Change type | Required proof |
| --- | --- | --- |
| T0 | Prose or comments only; no executable contract changes | Diff review, links and examples checked |
| T1 | Pure or isolated logic with no I/O, process, security, state, or public contract impact | Red proof, focused tests, neighbour tests |
| T2 | Subsystem behaviour, configuration, file/network I/O, UI, plugin, packaging, or cross-module contract | T1 plus real-path integration and classified suite |
| T3 | Database, migration, deletion, backup/restore, authentication, updater, gateway lifecycle, external storage, release, or concurrency | T2 plus recovery proof, failure injection, runtime smoke test, and full applicable release gate |

A small diff can still be T3. Risk is determined by impact, not line count.

## 5. Mandatory change loop

Perform this loop in order. Do not batch several unproved changes together.

### Checkpoint 0 — Freeze the starting state

Record:

- repository and intended checkout;
- branch and current SHA;
- `git status --short`;
- relevant runtime and data locations;
- dependency/runtime versions that affect the bug;
- external-volume mount and free space when storage is involved.

If the checkout contains unrelated work, preserve it and use a clean isolated
worktree for implementation and release proof. Do not reset, stash, clean, or
commit another session's work.

### Checkpoint 1 — Establish the baseline

Run the smallest relevant test before editing. Reproduce the real symptom on
current code when practical. Separate these outcomes:

- product failure;
- environment not provisioned;
- test did not run;
- test is flaky;
- premise cannot be reproduced.

Do not repair a premise that has not been verified against current code and
the original design intent.

### Checkpoint 2 — Pin the red proof

Add or identify a behavioural test that fails for the intended reason. The
test must assert an invariant, not freeze an expected-to-change catalogue,
count, version, timestamp, or snapshot.

For T2 and T3 changes, include at least one negative or failure-path case.
For regression repairs, preserve the red proof in the suite.

### Checkpoint 3 — Make one bounded change

Change only what is needed to move the red proof. Prefer existing extension
points and modules. Do not add dependencies, environment variables, hooks, or
core tools when existing code, configuration, a CLI command, skill, or plugin
can solve the problem.

### Checkpoint 4 — Run the focused test immediately

Stop if the focused test is not green. Diagnose the first real failure before
making another behavioural change. Do not weaken timeouts, assertions, or test
isolation without evidence that the test contract was wrong.

### Checkpoint 5 — Run neighbour and failure-path tests

Exercise:

- direct callers and consumers;
- sibling platforms or profiles using the same contract;
- error, timeout, cancellation, retry, and partial-success paths;
- backwards-compatible configuration and persisted-state paths;
- the operating systems genuinely affected by the change.

Use Hermes OS markers. Never fake the host by patching `sys.platform`.

### Checkpoint 6 — Prove the real path

Mocks may isolate units but cannot be the only evidence for T2 or T3. Use a
temporary `HERMES_HOME`, real imports, real serialization, real SQLite files,
or a real local client/backend path as applicable. No live credentials or
network calls are required unless the acceptance contract explicitly needs
them and safe test credentials are available.

### Checkpoint 7 — Refactor only while green

After behaviour is correct, remove duplication, reduce wording and branching,
and clarify names. Re-run focused tests after each refactor. A refactor that
changes behaviour returns to Checkpoint 2.

### Checkpoint 8 — Run the release gate

Python tests must use `scripts/run_tests.sh`; do not call `pytest` directly.
JavaScript/TypeScript assertions belong in the JS test suite so the CI change
classifier runs them. Run the applicable formatter, lint, type, build, package,
and classified test lanes.

T3 changes require the full applicable release gate unless the repository's
documented classifier excludes a lane. Every exclusion must be recorded with
its reason. Zero-test, skipped, incomplete, timed-out, or flaky lanes are not
green.

### Checkpoint 9 — Rebind and observe

After the final edit or commit:

1. record the new exact state;
2. rerun every required gate invalidated by that change;
3. exercise the installed or running surface;
4. verify the observed runtime uses the intended code and state;
5. execute or dry-run the rollback procedure;
6. write the receipt.

A commit, rebase, merge, push, dependency update, configuration change, data
migration, restart, or base-branch advance invalidates older evidence that
depends on it.

## 6. Stateful data profile

All database, pruning, migration, deduplication, and retention work is T3.

Before writing:

1. stop or quiesce every writer;
2. prove the exact database path and physical volume;
3. record byte size, journal mode, SQLite runtime version, schema version,
   row counts relevant to the change, and `PRAGMA quick_check`;
4. create a WAL-safe backup on the external volume;
5. run `PRAGMA quick_check` against the backup;
6. dry-run the selection and record what will change and what is protected.

During the change:

- use one transaction for related mutations;
- retain open and pinned sessions unless an independently approved lifecycle
  repair proves they are abandoned;
- prefer lossless index optimization and compaction before deletion;
- never use a SQLite runtime with a known corruption defect for maintenance
  writes when a safe installed runtime is available;
- leave the original recoverable until the replacement passes integrity,
  runtime, and location checks.

After the change:

- run integrity checks against the final file;
- compare expected and actual affected rows;
- verify search, session loading, and a representative write/read cycle;
- prove backup restore into an isolated temporary home;
- restart the real consumer and verify it has the intended file open;
- record reclaimed bytes and retained recovery artifacts.

### Database size guard

For `state.db`, use binary units against the 1 GiB quick-snapshot limit:

| State | File size | Required action |
| --- | ---: | --- |
| Healthy | below 850 MiB | normal maintenance |
| Warning | 850 MiB to below 950 MiB | measure growth sources and confirm maintenance runs |
| Action | 950 MiB to below 1 GiB | stop non-essential growth, prune eligible ended sessions, optimize, and snapshot |
| Hard stop | 1 GiB or larger | do not update or prune old recovery snapshots until a complete current backup exists |

Time-based retention reduces risk but is not a hard size guarantee. Open-session
lifecycle defects must be measured separately because safe retention does not
delete open sessions.

## 7. Persisted wording and index profile

Reducing user-visible clarity is not an acceptable storage optimization.
Reduce duplication at persistence boundaries instead:

1. store a canonical tool result once and reference it from derived views;
2. do not copy the same payload into visible content, API content, reasoning,
   audit metadata, and search text unless each copy has a tested purpose;
3. apply byte and line limits before persistence, with explicit truncation
   metadata and a content hash;
4. index only fields users need to search; raw tool payloads, reasoning blobs,
   binary data, and recovery metadata are excluded by default;
5. keep display summaries concise while preserving the canonical recovery
   payload where the product contract requires it;
6. migrate historical rows in bounded, restartable batches with progress and
   rollback receipts.

Required tests must prove conversation meaning, prompt-cache stability, search
results, export/import, restore, and old-schema compatibility remain intact.
Byte reduction alone is not acceptance.

## 8. Backup and snapshot profile

A snapshot is complete only when every required database is captured safely.

- Prune excluded trees before traversal; do not walk and then ignore them.
- Copy live SQLite databases through the backup API, not raw file copy.
- Exclude transient WAL, SHM, journal, lock, logs, scratch workspaces, and stale
  recovery artifacts where the snapshot contract declares them regenerable.
- Record oversized and failed database copies in machine-readable metadata.
- Never prune the last complete snapshot after an incomplete snapshot.
- Test restore, not only snapshot creation.
- Bind the restore proof to the exact snapshot identifier and manifest.

## 9. Update, Gateway, and Desktop profile

Preserve the updater contract:

`plan -> snapshot -> apply -> restart per deployment kind -> verify -> report`

Tests must cover refusal and partial-failure paths, not only success. Runtime
acceptance must prove:

- the expected checkout/version is running;
- all intended profiles were handled independently;
- the API health check passes;
- affected messaging or Desktop surfaces complete a representative action;
- a stale or mixed-version fleet is reported as failure;
- every begun update writes a receipt, including refused and failed updates.

Do not infer Gateway health from a live PID or Desktop health from an open
window. Observe the relevant endpoint and behaviour.

## 10. Stop conditions

Stop changing the system and report the boundary when any of these occurs:

- external storage is required but unmounted or resolves to the internal disk;
- no verified recovery copy exists for a destructive T3 operation;
- the baseline is red for an unrelated reason that masks the target;
- the intended test collected zero cases, skipped unexpectedly, timed out, or
  passed only on retry;
- the working tree or SHA changes outside the bounded task;
- a real-data test would require live customer data or credentials;
- integrity, restore, migration count, runtime version, or health proof fails;
- the proposed fix breaks a documented invariant to repair one symptom;
- evidence cannot distinguish a product failure from environment failure.

Do not loop on the same unsafe command. Preserve evidence, restore the last
known-good state when authorized, and identify the precise blocker.

## 11. Completion receipt

Every completed change reports:

```text
Goal:
Risk tier:
Repository / branch / exact SHA:
Starting state:
Files reviewed:
Files changed:
Red proof:
Focused tests:
Neighbour tests:
Integration proof:
Release gate:
Runtime observation:
Data / migration counts:
Backup and restore evidence:
Skipped or flaky tests:
Rollback path:
Known risks:
Final worktree status:
```

Completion requires all applicable fields. Use `not applicable` with a reason;
never omit a field to hide missing evidence.

## 12. Acceptance criteria for adopting this specification

This specification is ready to become mandatory when:

1. maintainers approve its risk tiers and stop conditions;
2. `AGENTS.md` links to it as a required pre-change workflow;
3. PR and handoff templates carry the completion receipt fields;
4. CI checks the appropriate test lane for changed Python and JS files;
5. the current database/storage repair is replayed against this workflow and
   produces a complete T3 receipt;
6. no placeholder, silent skip, or ambiguous completion term remains.

