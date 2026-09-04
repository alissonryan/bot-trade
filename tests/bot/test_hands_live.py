from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import LiveHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, Bar


def _live_settings() -> Settings:
    d = Settings.from_env().__dict__.copy()
    d["mode"] = "live"
    return Settings(**d)


class FakeClient:
    def __init__(self):
        self.calls = []

    def place_market(self, **kwargs):
        self.calls.append(("market", kwargs))
        return {"code": 0, "data": "oid-m"}

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        return {"code": 0, "data": "oid-t"}

    def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        return {"code": 200}


def test_live_buy_places_market_then_trigger(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient()
    hands = LiveHands(_live_settings(), store, client)
    snap = Snapshot(
        ts_ms=1, last=80000, bid=79999, ask=80001, spread=2,
        bars_15m=[Bar(1, 80000, 80000, 80000, 80000)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    hands.execute(gate, snap)
    assert client.calls[0][0] == "market"
    assert client.calls[1][0] == "trigger"
    assert client.calls[1][1]["market_order"] is True
    assert store.is_bot_order("oid-m")
    assert store.is_bot_order("oid-t")


def test_live_buy_retries_trigger_then_flattens(tmp_path):
    store = Store(tmp_path / "l.db")

    class FailTrigger:
        def __init__(self):
            self.calls = []

        def place_market(self, **kwargs):
            self.calls.append(("market", kwargs))
            return {"code": 0, "data": "oid-m"}

        def place_trigger(self, **kwargs):
            self.calls.append(("trigger", kwargs))
            raise RuntimeError("trigger down")

        def cancel_order(self, order_id: str):
            self.calls.append(("cancel", order_id))
            return {"code": 200}

    client = FailTrigger()
    hands = LiveHands(_live_settings(), store, client)
    snap = Snapshot(
        ts_ms=1, last=80000, bid=79999, ask=80001, spread=2,
        bars_15m=[Bar(1, 80000, 80000, 80000, 80000)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    pos = hands.execute(gate, snap)
    assert pos.qty == 0.0
    assert [c[0] for c in client.calls] == ["market", "trigger", "trigger", "market"]
    assert client.calls[-1][1]["side"] == "SELL"
    assert store.day_pnl(hands.today()) <= 0


def test_live_persists_position(tmp_path):
    db = tmp_path / "l.db"
    store = Store(db)
    client = FakeClient()
    hands = LiveHands(_live_settings(), store, client)
    snap = Snapshot(
        ts_ms=1, last=80000, bid=79999, ask=80001, spread=2,
        bars_15m=[Bar(1, 80000, 80000, 80000, 80000)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    hands.execute(gate, snap)
    again = LiveHands(_live_settings(), Store(db), FakeClient())
    assert again.position.qty == 0.00025
    assert again.stop_order_id == "oid-t"


def test_live_never_cancels_foreign_id(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient()
    hands = LiveHands(_live_settings(), store, client)
    foreign = "C02__723550870020620296064"
    assert store.is_bot_order(foreign) is False
    ok = hands.cancel_if_ours(foreign)
    assert ok is False
    assert client.calls == []
