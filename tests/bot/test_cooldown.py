from dataclasses import replace

import pytest

from bot.collar import decide
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, Snapshot, TradeIntent


def _settings(**kwargs) -> Settings:
    data = Settings.from_env().__dict__.copy()
    data.update(kwargs)
    return Settings(**data)


def _flat_snap() -> Snapshot:
    return Snapshot(
        ts_ms=1, last=100_000.0, bid=99_999.0, ask=100_001.0, spread=2.0,
        bars_15m=[Bar(t=i, o=100, h=101, l=99, c=100) for i in range(20)],
        atr=500.0, free_usdt=450.0, bot_qty=0.0, bot_avg_entry=None,
        ws_ok=True, stale=False,
    )


def _buy(store: Store, *, now_ms: int, minutes: float = 30):
    """Compose store + collar exactly as cycle.run_once does."""
    return decide(
        TradeIntent("BUY", 1.0, "", "trend"),
        _flat_snap(),
        _settings(cooldown_minutes=minutes),
        session_ok=True,
        day_pnl_usdt=0.0,
        last_loss_exit_ms=store.last_loss_exit_ms(),
        now_ms=now_ms,
    )


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


# --- NO REVENGE: only a losing exit starts the clock -------------------------
# Rafael Vargas retired the post-any-exit form after backtesting it (Apex Brief
# v17, Rule 3). His anchor: an ETH bear cascade worth +$2,300 over four trades
# that a post-win cooldown would have cut after the first. Re-entering the same
# direction after a WIN while the move continues is riding a cascade, not
# revenge; the fresh-signal requirement already blocks signal-less chasing.

def test_only_a_losing_exit_starts_the_cooldown_clock(tmp_path):
    store = Store(tmp_path / 'loss.db')
    assert store.last_loss_exit_ms() is None
    store.add_fill('2026-01-01', 5.0, side='SELL', ts='2026-01-01T10:00:00Z')
    assert store.last_loss_exit_ms() is None, "a win must not start the clock"
    store.add_fill('2026-01-01', 0.0, side='SELL', ts='2026-01-01T10:01:00Z')
    assert store.last_loss_exit_ms() is None, "breakeven is not a loss"
    store.add_fill('2026-01-01', -2.0, side='SELL', ts='2026-01-01T10:02:00Z')
    lost = store.last_loss_exit_ms()
    assert lost is not None
    store.add_fill('2026-01-01', 9.0, side='SELL', ts='2026-01-01T10:03:00Z')
    assert store.last_loss_exit_ms() == lost, "a later win must not reset it"


def test_buy_after_a_win_is_not_blocked_but_after_a_loss_is(tmp_path):
    win = Store(tmp_path / 'w.db')
    win.add_fill('2026-01-01', 7.5, side='SELL', ts='2026-01-01T10:00:00Z')
    at = 1767261600000  # 2026-01-01T10:00:00Z, the instant of the exit
    assert _buy(win, now_ms=at).rule == 'ok_buy'

    loss = Store(tmp_path / 'l.db')
    loss.add_fill('2026-01-01', -7.5, side='SELL', ts='2026-01-01T10:00:00Z')
    assert _buy(loss, now_ms=at).rule == 'cooldown'
    assert _buy(loss, now_ms=at + 30 * 60_000).rule == 'ok_buy'


def test_a_losing_buy_fill_never_starts_the_clock(tmp_path):
    """Only exits count. A BUY row can carry a pnl column; it is not an exit."""
    store = Store(tmp_path / 'b.db')
    store.add_fill('2026-01-01', -3.0, side='BUY', ts='2026-01-01T10:00:00Z')
    assert store.last_loss_exit_ms() is None
