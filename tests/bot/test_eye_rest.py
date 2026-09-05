from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye, EyeError
from bot.settings import Settings


class FakeKcex:
    base_url = "https://www.kcex.com"

    def __init__(self, *, fail_quotes=False, fail_balances=False, forming_bar=False):
        self.fail_quotes = fail_quotes
        self.fail_balances = fail_balances
        self.forming_bar = forming_bar
        self.quote_calls = 0

    def ticker(self, symbol="BTC_USDT"):
        self.quote_calls += 1
        if self.fail_quotes:
            raise ConnectionError("down")
        return {"data": {"c": "80000.0"}, "code": 0}

    def depth(self, symbol="BTC_USDT", price_precision="0.01"):
        return {
            "data": {"data": {"bids": [{"p": "79999", "q": "1"}], "asks": [{"p": "80001", "q": "1"}]}},
            "code": 200,
        }

    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        if self.forming_bar:
            now = int(time.time())
            first = now - now % 900 - 19 * 900
            t = [first + i * 900 for i in range(20)]  # last one opened < 15 min ago
        else:
            t = [1 + i * 900 for i in range(20)]
        return {"data": {"t": t, "o": [100] * 20, "h": [102] * 20, "l": [99] * 20, "c": [101] * 20, "v": [1] * 20}}

    def balances(self, currencies="BTC,USDT"):
        if self.fail_balances:
            raise ConnectionError("login required")
        return {"data": [{"currency": "USDT", "available": "450.0", "total": "450.0", "frozen": "0"}]}

    def symbol_trade_rules(self, symbol="BTC_USDT"):
        return {"data": {"ps": 2, "qs": 5, "tfr": "0", "mfr": "0", "mi": "1", "ma": "600000"}, "code": 0}

    def ws_token(self):
        return {"code": 0, "data": {"wsToken": "abc"}}


def _settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d.update(kw)
    return Settings(**d)


def test_rest_snapshot_not_stale():
    eye = Eye(FakeKcex(), Settings.from_env(), bot_qty=0.0, bot_avg_entry=None)
    snap = eye.snapshot_rest()
    assert snap.last == 80000.0
    assert snap.bid == 79999.0
    assert snap.ask == 80001.0
    assert snap.free_usdt == 450.0
    assert snap.atr is not None
    assert snap.stale is False
    assert snap.ws_ok is False


def test_rules_are_loaded_once():
    eye = Eye(FakeKcex(), Settings.from_env())
    rules = eye.load_rules()
    assert rules is not None and rules.qty_scale == 5 and rules.min_amount == 1.0


def test_poll_quotes_swallows_errors_and_reports_stale():
    eye = Eye(FakeKcex(fail_quotes=True), Settings.from_env())
    assert eye.poll_quotes(force=True) is False
    assert eye.rest_errors == 1
    assert "ConnectionError" in (eye.last_rest_error or "")
    assert eye.snapshot().stale is True


def test_poll_quotes_respects_interval_and_skips_when_socket_fresh():
    client = FakeKcex()
    eye = Eye(client, _settings(poll_seconds=60))
    assert eye.poll_quotes() is True
    assert eye.poll_quotes() is True  # within the interval: no second REST call
    assert client.quote_calls == 1
    eye.apply_ws_price(80010.0, 80009.0, 80011.0)
    eye.poll_quotes(force=False)
    assert client.quote_calls == 1  # socket is fresh: REST stays quiet


def test_forming_bar_is_dropped_before_atr():
    eye = Eye(FakeKcex(forming_bar=True), Settings.from_env())
    eye.poll_heavy()
    assert len(eye.bars) == 19
    eye2 = Eye(FakeKcex(forming_bar=False), Settings.from_env())
    eye2.poll_heavy()
    assert len(eye2.bars) == 20


def test_live_balance_failure_is_an_error_not_zero():
    eye = Eye(FakeKcex(fail_balances=True), _settings(mode="live"))
    with pytest.raises(EyeError):
        eye.poll_heavy()


def test_paper_balance_failure_uses_virtual_cash():
    eye = Eye(FakeKcex(fail_balances=True), _settings(mode="paper", paper_starting_usdt=450.0))
    eye.poll_heavy()
    assert eye.free_usdt == 450.0
