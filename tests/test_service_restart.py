"""Restart-by-exit.

The process restarts itself by returning from main(); NSSM revives it. That is
what makes the service maintainable without Administrator rights, so the sentinel
handling is worth pinning down — particularly that a failure to clear it can
never produce a reboot loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from run_service import restart_requested
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.service.restart_file = str(tmp_path / "RESTART")
    return c


def test_no_sentinel_means_keep_running(cfg):
    assert restart_requested(cfg) is False


def test_sentinel_requests_a_restart(cfg):
    Path(cfg.service.restart_file).write_text("go", encoding="utf-8")
    assert restart_requested(cfg) is True


def test_sentinel_is_consumed(cfg):
    """Cleared before exiting, so the restarted process does not exit again."""
    path = Path(cfg.service.restart_file)
    path.write_text("go", encoding="utf-8")
    assert restart_requested(cfg) is True
    assert not path.exists()
    assert restart_requested(cfg) is False


def test_unremovable_sentinel_does_not_cause_a_reboot_loop(cfg, monkeypatch):
    """If the file cannot be deleted, exiting would restart into it forever."""
    path = Path(cfg.service.restart_file)
    path.write_text("go", encoding="utf-8")

    def refuse(self):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert restart_requested(cfg) is False, "must keep running rather than loop"
