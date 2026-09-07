from dataclasses import replace
import json

import pytest

from bot.store import Store
from bot.types import TradeIntent


def test_journal_resolves_linked_buy_and_filters_outcome_as_of(tmp_path):
    store = Store(tmp_path / 'journal.db')
    jid = store.journal_begin(TradeIntent('BUY', .8, 'thesis', 'range'), {'last': 100},
                              decision_ms=1000, track_entry=True)
    store.add_fill('1970-01-01', 0, ts='1970-01-01T00:00:02Z', side='BUY',
                   qty=1, price=100, fee=0, known_ms=2000)
    store.add_fill('1970-01-01', 10, ts='1970-01-01T00:00:03Z', side='SELL',
                   qty=1, price=110, fee=0, source='paper', known_ms=5000)
    assert store.journal_lessons(as_of_ms=4999) == []
    lesson = store.journal_lessons(as_of_ms=5000)[0]
    assert lesson['id'] == jid
    assert lesson['outcome_known_ms'] == 5000  # knowledge, NOT execution timestamp
    assert lesson['outcome']['realized_pnl_usdt'] == 10
    assert lesson['outcome']['duration_seconds'] == 1
    assert lesson['outcome']['exit_type'] == 'paper'


def test_future_outcome_and_future_reflection_never_enter_prompt(tmp_path):
    from bot.brain import _user_payload
    from bot.types import Snapshot
    s = Store(tmp_path / 'pit.db')
    older = s.journal_begin(TradeIntent('BUY', .8, 'past', 'range'), {}, decision_ms=100)
    future = s.journal_begin(TradeIntent('BUY', .8, 'future', 'range'), {}, decision_ms=200)
    s.journal_resolve(older, {'kind': 'closed', 'realized_pnl_usdt': 1}, known_ms=300)
    s.journal_resolve(future, {'kind': 'closed', 'realized_pnl_usdt': 99}, known_ms=500)
    s.journal_reflect(older, 'Only written later. Must not leak.', {'reason': 'reflection_ok'}, known_ms=600)
    snap = Snapshot(400, 100, 99, 101, 2, [], 1, 450, 0, None, False, False)
    # Even if a caller supplies a later read, the final payload independently cuts.
    payload = json.loads(_user_payload(snap, lessons=s.journal_lessons(as_of_ms=1000), as_of_ms=400))
    assert len(payload['lessons']) == 1
    assert payload['lessons'][0]['id'] == older
    assert 'reflection' not in payload['lessons'][0]
    assert 'future' not in json.dumps(payload)
    assert 'Must not leak' not in json.dumps(payload)
    later = json.loads(_user_payload(snap, lessons=s.journal_lessons(as_of_ms=600), as_of_ms=600))
    assert any('reflection' in row for row in later['lessons'])


def test_reflection_reserves_decision_budget_and_charges_real_cost():
    from unittest.mock import Mock
    from bot.brain import Budget, reflect_result, think_result
    from bot.settings import Settings
    from bot.types import Snapshot
    s = replace(Settings.from_env(), llm_model='test', openrouter_api_key='test',
                llm_fallback_cost_usd=.02)
    budget = Budget(.97, 1, 'day')
    post = Mock()
    result = reflect_result({'outcome': {'kind': 'closed'}}, s, budget, http_post=post)
    assert result.reason == 'reflection_budget'
    assert result.text is None
    post.assert_not_called()
    snap = Snapshot(400, 100, 99, 101, 2, [], 1, 450, 0, None, False, False)
    post.return_value.status_code = 200
    post.return_value.json.return_value = {'choices': [{'message': {'content':
        '{"action":"HOLD","confidence":1,"reason":"test","regime":"range"}'}}],
        'usage': {'cost': .001}}
    decision = think_result(snap, s, budget, http_post=post)
    assert decision.intent is not None  # the skipped luxury did not spend its funds
    assert budget.spent_usd == pytest.approx(.971)
    budget = Budget(0, 1, 'day')
    post.return_value.json.return_value = {'choices': [{'message': {'content':
        'The range thesis failed. Wait for confirmation next time.'}}], 'usage': {'cost': .003}}
    reflected = reflect_result({'outcome': {'kind': 'closed'}}, s, budget, http_post=post)
    assert reflected.reason == 'reflection_ok'
    assert budget.spent_usd == .003
    assert post.call_args.kwargs['json']['max_tokens'] == 160


def test_unfilled_entry_clears_without_fabricated_return_or_next_trade(tmp_path):
    s = Store(tmp_path / 'unfilled.db')
    first = s.journal_begin(TradeIntent('BUY', 1, 'cancelled', 'range'), {}, decision_ms=0, track_entry=True)
    s.clear_position()  # existing hands path calls only on proven unfilled/closed
    row = s.journal_get(first)
    assert row['outcome']['kind'] == 'not_executed'
    assert row['outcome']['realized_pnl_usdt'] is None
    assert s.fills() == []
    second = s.journal_begin(TradeIntent('BUY', 1, 'next', 'range'), {}, decision_ms=0, track_entry=True)
    s.add_fill('day', 0, side='BUY', qty=1, price=100)
    s.add_fill('day', 2, side='SELL', qty=1, price=102)
    assert s.journal_get(first) == row
    assert s.journal_get(second)['outcome']['realized_pnl_usdt'] == 2


def test_partial_exit_and_restart_preserve_linkage(tmp_path):
    path = tmp_path / 'partial.db'
    s = Store(path)
    jid = s.journal_begin(TradeIntent('BUY', 1, 'partial', 'range'), {}, decision_ms=0, track_entry=True)
    s.add_fill('day', 0, side='BUY', qty=1, price=100)
    s.add_fill('day', .5, side='SELL', qty=.5, price=101)
    assert s.journal_lessons(as_of_ms=10**15) == []
    s._conn.close()
    s = Store(path)
    s.add_fill('day', 1, side='SELL', qty=.5, price=102)
    assert s.journal_get(jid)['outcome']['realized_pnl_usdt'] == 1.5


def test_reflection_candidate_is_closed_buy_not_hold_observation(tmp_path):
    s = Store(tmp_path / 'candidates.db')
    buy = s.journal_begin(TradeIntent('BUY', 1, 'thesis', 'range'), {}, decision_ms=1)
    s.journal_resolve(buy, {'kind': 'closed', 'realized_pnl_usdt': 1}, known_ms=3)
    hold = s.journal_begin(TradeIntent('HOLD', 1, 'wait', 'range'), {}, decision_ms=4)
    s.journal_resolve(hold, {'kind': 'not_executed', 'realized_pnl_usdt': None}, known_ms=4)
    assert s.journal_candidate(as_of_ms=2) is None
    assert s.journal_candidate(as_of_ms=4)['id'] == buy
    s.journal_reflect(buy, None, {'reason': 'reflection_budget'}, known_ms=5)
    assert s.journal_candidate(as_of_ms=5) is None


def test_journal_resolution_failure_cannot_prevent_fill_commit(tmp_path, monkeypatch):
    s = Store(tmp_path / 'failure.db')
    s.journal_begin(TradeIntent('BUY', 1, 'test', 'range'), {}, decision_ms=0, track_entry=True)
    s.add_fill('day', 0, side='BUY', qty=1, price=100)
    def fail(*args):
        raise ValueError('malformed journal evidence')
    monkeypatch.setattr(s, '_journal_close', fail)
    s.add_fill('day', 1, side='SELL', qty=1, price=101)
    assert Store(s.path).fills(1)[0]['side'] == 'SELL'


def test_migration_from_pre_journal_database(tmp_path):
    import sqlite3
    path = tmp_path / 'pre-p4.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE fills (id INTEGER PRIMARY KEY, day TEXT, pnl REAL, ts TEXT, side TEXT, qty REAL, price REAL, fee REAL, order_id TEXT, source TEXT)')
    db.execute("INSERT INTO fills VALUES (1,'2026-01-01',2,'2026-01-01T00:00:00Z','SELL',1,102,0,'old-stop','reconcile')")
    db.execute('CREATE TABLE position (id INTEGER PRIMARY KEY, qty REAL, entry REAL, stop_price REAL, entry_order_id TEXT, stop_order_id TEXT, state TEXT, entry_source TEXT, btc_before REAL, opened_ts TEXT, take_profit_price REAL, exit_reason TEXT)')
    db.execute("INSERT INTO position VALUES (1,1,100,90,'entry','stop','OPEN','estimated',0,'2026-01-01T00:00:00Z',110,NULL)")
    db.commit()
    db.close()
    s = Store(path)
    assert s.fills(1)[0]['pnl'] == 2
    assert s.load_position()['stop_order_id'] == 'stop'
    assert s.load_position()['take_profit_price'] == 110
    assert s.journal_lessons(as_of_ms=10**15) == []  # do not retrofit old fills into fictional theses
    jid = s.journal_begin(TradeIntent('HOLD', .5, 'wait', 'range'), {'last': 102}, decision_ms=1)
    s._conn.close()
    assert Store(path).journal_get(jid)['snapshot']['last'] == 102


@pytest.mark.parametrize('content,finish,expected', [
    ('', 'stop', 'reflection_empty'),
    ('', 'length', 'reflection_truncated'),
    ('- Do this.\n- Then that.', 'stop', 'reflection_parse'),
    ('One sentence.', 'stop', 'reflection_parse'),
    ('First sentence. Second sentence.', 'stop', 'reflection_ok'),
])
def test_reflection_format_failures_are_named_and_charged(content, finish, expected):
    from unittest.mock import Mock
    from bot.brain import Budget, reflect_result
    from bot.settings import Settings
    settings = replace(Settings.from_env(), openrouter_api_key='test', llm_model='test')
    post = Mock(return_value=Mock(status_code=200, json=lambda: {
        'choices': [{'message': {'content': content}, 'finish_reason': finish}], 'usage': {'cost': .002}}))
    budget = Budget(0,1,'day')
    result = reflect_result({},settings,budget,http_post=post)
    assert result.reason == expected
    assert result.cost_usd == budget.spent_usd == .002


def test_prompt_smoke_with_five_lessons_uses_real_parser_and_bounded_text():
    from unittest.mock import Mock
    from bot.brain import Budget, think_result
    from bot.settings import Settings
    from bot.types import Snapshot
    settings = replace(Settings.from_env(), openrouter_api_key='test', llm_model='test')
    snap = Snapshot(1000,100,99,101,2,[],1,450,0,None,False,False)
    lessons = [{'id':i,'decision_ms':i,'outcome_known_ms':i+100,'action':'BUY',
                'confidence':.8,'regime':'range','reason':'r'*500,
                'outcome':{'kind':'closed','realized_pnl_usdt':1},
                'reflection':'x'*1000,'reflection_known_ms':i+200} for i in range(8)]
    response = {'choices':[{'message':{'content':'{"action":"HOLD","confidence":0.5,"reason":"synthetic smoke","regime":"range"}'}}], 'usage':{'cost':.001}}
    post = Mock(return_value=Mock(status_code=200,json=lambda:response))
    result = think_result(snap,settings,Budget(0,2,'day'),lessons=lessons,as_of_ms=1000,http_post=post)
    assert result.intent.action == 'HOLD'
    payload = json.loads(post.call_args.kwargs['json']['messages'][1]['content'])
    assert len(payload['lessons']) == 5
    assert all(len(row['reflection']) == 400 and len(row['reason']) == 240 for row in payload['lessons'])


def test_journal_setting_is_optout(monkeypatch):
    from bot.settings import Settings
    monkeypatch.delenv('JOURNAL_ENABLED', raising=False)
    assert Settings.from_env().journal_enabled is False
    monkeypatch.setenv('JOURNAL_ENABLED', 'true')
    assert Settings.from_env().journal_enabled is True


def test_reflection_storage_failure_keeps_paid_cost_in_audit(tmp_path, monkeypatch):
    from bot.journal import deferred_reflection
    from bot.brain import Budget, ReflectionResult
    from bot.settings import Settings
    store = Store(tmp_path / 'reflect-write.db')
    jid = store.journal_begin(TradeIntent('BUY',1,'past','range'),{},decision_ms=0)
    store.journal_resolve(jid,{'kind':'closed'},known_ms=1)
    def fail(*args, **kwargs):
        raise RuntimeError('db unavailable')
    monkeypatch.setattr(store,'journal_reflect',fail)
    audit = deferred_reflection(store,Settings.from_env(),Budget(0,2,'day'),as_of_ms=2,
                                completed_ms=lambda:3,
                                reflect=lambda *args:ReflectionResult('First. Second.','reflection_ok',.003,'usage'))
    assert audit['reason'] == 'reflection_store_error'
    assert audit['cost_usd'] == .003
