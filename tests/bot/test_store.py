from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.store import Store
from bot.types import GateResult, TradeIntent


def test_audit_and_bot_order_ids(tmp_path):
    db = tmp_path / "bot.db"
    store = Store(db)
    store.append_audit(
        intent=TradeIntent("BUY", 0.5, "x", "trend"),
        gate=GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79000.00"),
        mode="paper",
        order_id="bot-1",
    )
    store.remember_order("bot-1")
    store.remember_order("bot-stop-1")
    assert store.is_bot_order("bot-1") is True
    assert store.is_bot_order("C02__723550870020620296064") is False
    assert store.day_pnl("2026-09-04") == 0.0
    store.add_fill("2026-09-04", 1.5)
    store.add_fill("2026-09-04", -0.5)
    assert store.day_pnl("2026-09-04") == 1.0
