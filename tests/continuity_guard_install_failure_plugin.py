"""Inject a fixture-wiring failure for the continuity guard self-test."""

from __future__ import annotations

import asyncio
import pty
from pathlib import Path

import pytest


_MODE_PATH = Path.home() / ".continuity-guard-install-failure-mode"
if _MODE_PATH.is_file():
    _MODE = _MODE_PATH.read_text(encoding="utf-8").strip()
    _ORIGINAL_SETATTR = pytest.MonkeyPatch.setattr

    def _refusing_setattr(self, target, *args, **kwargs):
        name = args[0] if args else None
        if (_MODE == "pty" and target is pty and name == "spawn") or (
            _MODE == "asyncio"
            and target is asyncio
            and name == "create_subprocess_exec"
        ):
            raise RuntimeError(f"injected {_MODE} guard install failure")
        return _ORIGINAL_SETATTR(self, target, *args, **kwargs)

    pytest.MonkeyPatch.setattr = _refusing_setattr
