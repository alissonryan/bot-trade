from dataclasses import replace

import pytest

from bot.settings import Settings
from bot.store import Store


def test_cooldown_settings_optout_and_env(monkeypatch):
    monkeypatch.delenv("COOLDOWN_MINUTES", raising=False)
    assert Settings.from_env().cooldown_minutes == 0
    monkeypatch.setenv("COOLDOWN_MINUTES", "30.5")
    assert Settings.from_env().cooldown_minutes == 30.5


@pytest.mark.parametrize("value", [-1, float('nan'), float('inf'), -float('inf')])
def test_invalid_cooldown_rejected(value):
    with pytest.raises(ValueError, match="cooldown"):
        replace(Settings.from_env(), cooldown_minutes=value)


def test_last_exit_ignores_buys_and_survives_day_boundary(tmp_path):
    store = Store(tmp_path / 'fills.db')
    assert store.last_exit_ms() is None
    store.add_fill('2026-01-01', 0, side='BUY', ts='2026-01-01T23:58:00Z')
    assert store.last_exit_ms() is None
    store.add_fill('2026-01-01', -1, side='SELL', ts='2026-01-01T23:59:00Z')
    exited = store.last_exit_ms()
    store.add_fill('2026-01-02', 0, side='BUY', ts='2026-01-02T00:00:00Z')
    assert store.last_exit_ms() == exited
    store.add_fill('2026-01-02', 1, side='SELL', ts='2026-01-02T00:00:00+00:00')
    latest = store.last_exit_ms()
    assert latest is not None and exited is not None
    assert latest - exited == 60_000


def test_legacy_fills_do_not_fabricate_exit_age(tmp_path):
    store = Store(tmp_path / 'legacy.db')
    store.add_fill('2026-01-01', -1)  # old PnL-only row has no proven side
    assert store.last_exit_ms() is None
