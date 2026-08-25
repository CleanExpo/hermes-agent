"""Inject a fixture-wiring failure for the continuity guard self-test."""

from __future__ import annotations

import asyncio
import os

import pytest


_MODE = os.environ.get("CONTINUITY_GUARD_INSTALL_FAILURE_MODE", "").strip()
if _MODE:
    _ORIGINAL_SETATTR = pytest.MonkeyPatch.setattr
    if _MODE == "pty" and os.name == "posix":
        import pty as _pty
    else:
        _pty = None

    def _refusing_setattr(self, target, *args, **kwargs):
        name = args[0] if args else None
        if (_MODE == "pty" and target is _pty and name == "spawn") or (
            _MODE == "asyncio"
            and target is asyncio
            and name == "create_subprocess_exec"
        ):
            raise RuntimeError(f"injected {_MODE} guard install failure")
        return _ORIGINAL_SETATTR(self, target, *args, **kwargs)

    pytest.MonkeyPatch.setattr = _refusing_setattr
