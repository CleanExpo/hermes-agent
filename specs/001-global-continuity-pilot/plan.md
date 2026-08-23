# Implementation plan

## Architecture

One standard-library-oriented Python control plane reads four human-inspectable
authorities: repository state, the Basic Memory card, the Beads task JSONL/CLI, and
the Spec Kit change. `continuity_bridge.py` produces bounded preflight JSON;
`continuity_gate.py` verifies exact-state receipts and controls terminal lifecycle
promotion; `continuity_event.py` is the sole host dispatcher.

Project-level Claude and Codex adapters and a rendered sandbox Hermes adapter call the
dispatcher. No global host file is edited. External state uses atomic JSON/JSONL writes
under the configured Storage Unit root. Tests use temporary repositories and fixtures.

## Risk and rollback

Risk tier is T3 because hook lifecycle and external storage are involved. Failures are
fail-readable: a session may continue in degraded mode, but completion is forbidden.
Rollback disables/removes project adapters, removes the isolated Hermes config, and
archives the external pilot directory; it does not touch the shared checkout.
