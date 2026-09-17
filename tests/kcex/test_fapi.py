import pytest

from kcex.client import KcexClient, KcexError
from kcex.fapi import ContractSpec, FuturesPublic
from kcex.fws import FutDeal, FutTicker

DETAIL = {
    "symbol": "BTC_USDT", "contractSize": 0.0001, "minVol": 1, "maxVol": 714000,
    "priceUnit": 0.1, "takerFeeRate": 0.0001, "makerFeeRate": 0, "maintenanceMarginRate": 0.005,
    "maxLeverage": 125, "state": 0,
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, headers))
        return FakeResponse(self.routes[url.split("/fapi/v1/")[1]])


def make(routes):
    session = FakeSession(routes)
    client = KcexClient(token="", user_device="", session=session, sleep=lambda s: None)
    return FuturesPublic(client), session


def ok(data):
    return {"success": True, "code": 0, "data": data}


def test_contract_detail_parses_spec_and_sends_no_auth():
    api, session = make({"contract/detail": ok(DETAIL)})
    assert api.contract_detail("BTC_USDT") == ContractSpec(
        symbol="BTC_USDT", contract_size=0.0001, min_vol=1, max_vol=714000, price_unit=0.1,
        taker_fee=0.0001, maker_fee=0.0, mmr=0.005, max_leverage=125, state=0)
    method, _url, params, headers = session.calls[0]
    assert method == "GET"
    assert params == {"symbol": "BTC_USDT"}
    assert "authorization" not in headers


def test_contract_detail_missing_field_raises():
    data = dict(DETAIL)
    del data["takerFeeRate"]
    api, _ = make({"contract/detail": ok(data)})
    with pytest.raises(KcexError):
        api.contract_detail("BTC_USDT")


def test_contract_detail_rejects_nonsense_values():
    api, _ = make({"contract/detail": ok(dict(DETAIL, contractSize=0))})
    with pytest.raises(KcexError):
        api.contract_detail("BTC_USDT")


def test_ticker():
    api, _ = make({"contract/ticker": ok({
        "lastPrice": 76413, "bid1": 76412.9, "ask1": 76413, "fairPrice": 76413.9,
        "indexPrice": 76446.3, "fundingRate": 0.000077, "timestamp": 1789613014008})})
    assert api.ticker("BTC_USDT") == FutTicker(1789613014008, 76413.0, 76412.9, 76413.0, 76413.9, 76446.3, 0.000077)


def test_depth_returns_version_and_levels():
    api, session = make({"contract/depth/BTC_USDT": ok({
        "asks": [[76360.4, 785199, 3], [76360.5, 17700, 1]],
        "bids": [[76360.3, 942300, 3]], "version": 14452999560})})
    assert api.depth("BTC_USDT", limit=5) == (
        14452999560, ((76360.3, 942300),), ((76360.4, 785199), (76360.5, 17700)))
    assert session.calls[0][2] == {"limit": 5}


def test_deals_are_sorted_oldest_first():
    api, _ = make({"contract/deals/BTC_USDT": ok([
        {"p": 76360.4, "v": 100, "T": 2, "O": 3, "M": 1, "t": 1789614184159},
        {"p": 76360.3, "v": 800, "T": 1, "O": 3, "M": 1, "t": 1789614180839}])})
    assert api.deals("BTC_USDT") == [
        FutDeal(1789614180839, 76360.3, 800, "sell"), FutDeal(1789614184159, 76360.4, 100, "buy")]


def test_klines_1m():
    api, session = make({"contract/kline/BTC_USDT": ok({
        "time": [1789614060, 1789614120], "open": [76380.0, 76356.9], "high": [76390.8, 76360.4],
        "low": [76356.9, 76338.2], "close": [76356.9, 76360.3], "vol": [233153.0, 206165.0]})})
    assert api.klines_1m("BTC_USDT", 1789614000, 1789614180) == [
        (1789614060, 76380.0, 76390.8, 76356.9, 76356.9, 233153.0),
        (1789614120, 76356.9, 76360.4, 76338.2, 76360.3, 206165.0)]
    assert session.calls[0][2] == {"interval": "Min1", "start": 1789614000, "end": 1789614180}


def test_funding():
    api, _ = make({"contract/funding_rate/BTC_USDT": ok({
        "fundingRate": 0.000077, "collectCycle": 8, "nextSettleTime": 1789632000000})})
    assert api.funding("BTC_USDT") == (0.000077, 1789632000000)


def test_unsuccessful_envelope_raises():
    api, _ = make({"contract/ticker": {"success": False, "code": 0, "data": None}})
    with pytest.raises(KcexError):
        api.ticker("BTC_USDT")
