"""Radar permissions never bypass data, risk, live gates or stop lifecycle."""
from dataclasses import replace
from types import SimpleNamespace
import json
import math
import pytest

from imperion.config import load_settings, STYLES
from imperion.analysis.radar import DEEP_TFS
from imperion.data.admission import RadarAdmission
from imperion.fakes import make_engine, warm_feed
from imperion.util import now_ms
from .helpers import crafted_analyze

ENV = {'RADAR_TRADING_ENABLED':'true','RADAR_TRADE_ALLOWLIST':'HYPEUSDT,INJUSDT,FILUSDT',
       'RADAR_DEEP_COUNT':'3','RADAR_MIN_QUOTE_VOLUME':'1'}


def fixture_data(**changes):
    cfg=load_settings({**ENV,**changes})
    now=now_ms()
    ranked=[{'symbol':'HYPEUSDT','eligible':True,'score':.8,'quote_volume':1e8}]
    deep={'HYPEUSDT':{'ts':now,'approved':True,'grade':'A','direction':'LONG','setup':{'entry':30}}}
    filters={'HYPEUSDT':SimpleNamespace(status='TRADING',contract_type='PERPETUAL')}
    return cfg,now,ranked,deep,filters


def admit(manager,now,ranked,deep,filters,**changes):
    values=dict(radar_updated=now,filters_updated=now,now=now,healthy=lambda _:True)
    values.update(changes)
    manager.replace(ranked,deep,filters=filters,**values)


def test_default_stays_scan_only():
    cfg,now,ranked,deep,filters=fixture_data()
    manager=RadarAdmission(replace(cfg,radar_trading_enabled=False))
    admit(manager,now,ranked,deep,filters)
    assert manager.symbols(now)==[]


@pytest.mark.parametrize('changes',[{'RADAR_COUNT':'0'},
    {'RADAR_DEEP_COUNT':'0'},{'ACTIVE_STYLES':'scalp'}, {'RADAR_MAX_ACTIVE':'1.5'},
    {'RADAR_ADMISSION_TTL_SECONDS':'nan'},{'RADAR_MIN_QUOTE_VOLUME':'nan'},
    {'RADAR_TRADE_ALLOWLIST':'BTC/USDT'},{'RADAR_TRADE_ALLOWLIST':'INJUSDT,INJUSDT'}])
def test_bad_configuration_rejected(changes):
    with pytest.raises(ValueError):load_settings({**ENV,**changes})


def test_lease_expires_and_never_survives_restart():
    cfg,now,r,d,f=fixture_data();manager=RadarAdmission(cfg)
    admit(manager,now,r,d,f)
    assert manager.symbols(now)==['HYPEUSDT']
    assert not manager.symbols(now+cfg.radar_admission_ttl_seconds*1000)
    assert not manager.symbols(now-1)
    assert not RadarAdmission(cfg).symbols(now)


@pytest.mark.parametrize('condition',['operator','liquidity','rank','deep','stale','future','filters','health','filter_age'])
def test_each_required_gate_can_block(condition):
    cfg,now,r,d,f=fixture_data();options={}
    if condition=='operator':cfg=replace(cfg,radar_trade_allowlist=('OTHERUSDT',))
    if condition=='liquidity':r[0]['quote_volume']=0
    if condition=='rank':r[0]['eligible']=False
    if condition=='deep':d['HYPEUSDT']['approved']=False
    if condition=='stale':options['radar_updated']=now-2_000_000
    if condition=='future':d['HYPEUSDT']['ts']=now+1
    if condition=='filters':f['HYPEUSDT'].status='BREAK'
    if condition=='health':options['healthy']=lambda _:False
    if condition=='filter_age':options['filters_updated']=now-2_000_000
    manager=RadarAdmission(cfg);admit(manager,now,r,d,f,**options)
    assert not manager.symbols(now)
    assert manager.snapshot(now)['rejected']['HYPEUSDT']


def test_active_symbol_limit_and_revocation():
    cfg,now,r,d,f=fixture_data(RADAR_MAX_ACTIVE='1')
    r.append(dict(r[0],symbol='INJUSDT'));d['INJUSDT']=dict(d['HYPEUSDT']);f['INJUSDT']=f['HYPEUSDT']
    manager=RadarAdmission(cfg);admit(manager,now,r,d,f)
    assert manager.symbols(now)==['HYPEUSDT']
    manager.revoke_missing(['INJUSDT'])
    assert manager.symbols(now)==[]


@pytest.fixture
def prepared(monkeypatch):
    engine,fake=make_engine(env={**ENV,"RADAR_TRADE_ALLOWLIST":"INJUSDT"})
    warm_feed(fake,engine.trades,engine.books,symbols=list(engine.radar.symbols()))
    monkeypatch.setattr(engine.director,'analyze',crafted_analyze(fake))
    scan=engine.radar_scan()
    assert scan['admission']['active'],scan
    symbol=next(iter(scan['admission']['active']))
    assert symbol not in engine.cfg.symbols
    return engine,fake,symbol


def test_admission_alone_does_not_trade_and_bootstraps_data(prepared):
    e,f,s=prepared
    assert not f.pos and not e.db.open_trades()
    assert s in e.execution_symbols('day') and s not in e.execution_symbols('scalp')
    assert all(e.candles.count(s,tf)>=150 for tf in DEEP_TFS)
    assert s in e.poller.symbols


def test_dynamic_entry_uses_existing_order_and_stop_lifecycle(prepared):
    e,f,s=prepared
    result=e.evaluate(s,'day')
    assert result['traded'],result
    trade=e.db.open_trades()[0]
    assert json.loads(trade['meta_json'])['execution_universe']=='radar_admitted'
    assert s in f.pos and any(order['symbol']==s for order in f.algos)
    assert result['portfolio_guard']['inventory']['size_multiplier']<=1


def test_expired_position_remains_monitored_and_managed(prepared):
    e,f,s=prepared;assert e.evaluate(s,'day')['traded']
    e.admission.clear();e.radar.markets=[];e._sync_execution_feeds()
    assert s not in e.execution_symbols()
    assert s in e._stream_symbols() and s in e.poller.symbols
    assert s in e.snapshot()['symbols']
    with pytest.raises(ValueError):e.evaluate(s,'day')
    e.manage_pass()
    assert s in f.pos and any(order['symbol']==s for order in f.algos)


def test_live_gate_is_not_bypassed_by_radar(prepared):
    e,f,s=prepared;e.db.kv_set('gate.live_approval',None)
    r=e.evaluate(s,'day')
    assert not r['traded'] and r['status']=='APPROVED_NOT_TRADED'
    assert any('LIVE_GATE_CLOSED' in x for x in r['reasons'])
    assert not f.pos


def test_kill_switch_still_blocks_dynamic_entry(prepared):
    e,f,s=prepared;e.halt('radar test')
    r=e.evaluate(s,'day')
    assert not r['traded'] and not f.pos
    assert any('HALT' in x.upper() for x in r['reasons'])


def test_unknown_symbol_and_non_day_entry_rejected(prepared):
    e,f,s=prepared
    with pytest.raises(ValueError):e.evaluate('UNAPPROVEDUSDT','day')
    with pytest.raises(ValueError):e.evaluate(s,'scalp')


def test_revoke_between_analysis_and_entry_blocks(prepared,monkeypatch):
    e,f,s=prepared;original=e.can_trade
    def revoke():
        result=original();e.admission.clear();return result
    monkeypatch.setattr(e,'can_trade',revoke)
    r=e.evaluate(s,'day')
    assert not r['traded'] and 'RADAR_ADMISSION_EXPIRED_OR_REVOKED' in r['reasons']
    assert not f.pos


def test_revoke_during_sizing_blocks_before_order(prepared,monkeypatch):
    e,f,s=prepared;original=e.risk.size
    def revoke(*args,**kwargs):
        result=original(*args,**kwargs);e.admission.clear();return result
    monkeypatch.setattr(e.risk,'size',revoke)
    r=e.evaluate(s,'day')
    assert not r['traded'] and 'RADAR_FINAL_ENTRY_CHECK_FAILED' in r['reasons']
    assert not f.pos


def test_current_delisting_or_stale_filters_revokes_symbol(prepared):
    e,f,s=prepared
    e._current_filter_symbols.remove(s)
    assert s not in e.execution_symbols()
    with pytest.raises(ValueError):e.evaluate(s,'day')


def test_refresh_failure_clears_dynamic_permissions(prepared,monkeypatch):
    e,f,s=prepared
    monkeypatch.setattr(e,'_load_filters',lambda:False)
    e._refresh_radar()
    assert s not in e.execution_symbols()


def test_status_exposes_permissions_without_changing_fixed_list(prepared):
    e,f,s=prepared;status=e.status()['execution_universe']
    assert s in status['radar']['active'] and s not in status['fixed']
    assert tuple(status['fixed'])==e.cfg.symbols


def test_exchange_minimum_cannot_force_extra_risk(monkeypatch):
    e,f=make_engine(env={**ENV,'RADAR_TRADE_ALLOWLIST':'HYPEUSDT','RISK_PER_TRADE_PCT':'0.1'})
    warm_feed(f,e.trades,e.books,symbols=e.radar.symbols())
    monkeypatch.setattr(e.director,'analyze',crafted_analyze(f))
    assert 'HYPEUSDT' in e.radar_scan()['admission']['active']
    r=e.evaluate('HYPEUSDT','day')
    assert not r['traded'] and not f.pos
    assert any('SIZE_BELOW_EXCHANGE_MIN' in reason for reason in r['reasons'])


def test_existing_risk_position_limit_applies_to_admitted_symbol(prepared):
    e,f,s=prepared
    assert e.evaluate('SOLUSDT','day')['traded']
    r=e.evaluate(s,'day')
    assert r['traded'] and s in f.pos  # 5 positions; fixed and dynamic have equal entry rights


def test_restart_retains_owned_position_but_does_not_restore_entry_permission(monkeypatch,tmp_path):
    from imperion.db import Database
    path=str(tmp_path/'state.db')
    e,f=make_engine(env={**ENV,'RADAR_TRADE_ALLOWLIST':'INJUSDT'},db=Database(path))
    warm_feed(f,e.trades,e.books,symbols=e.radar.symbols())
    monkeypatch.setattr(e.director,'analyze',crafted_analyze(f))
    assert 'INJUSDT' in e.radar_scan()['admission']['active']
    assert e.evaluate('INJUSDT','day')['traded']
    e.stop()
    e2,_=make_engine(fake=f,env={'RADAR_COUNT':'0','RADAR_TRADING_ENABLED':'false'},db=Database(path))
    assert 'INJUSDT' not in e2.execution_symbols()
    assert 'INJUSDT' in e2._stream_symbols() and 'INJUSDT' in e2.poller.symbols
    assert e2.db.open_trades() and 'INJUSDT' in f.pos


def test_trade_loop_evaluates_admitted_symbols_only_for_day(prepared,monkeypatch):
    e,f,s=prepared;seen=[]
    monkeypatch.setattr(e,'evaluate',lambda symbol,style,**kwargs:seen.append((symbol,style)))
    e.decision_pass()
    assert (s,'day') in seen and (s,'scalp') not in seen and (s,'swing') not in seen
    e.admission.clear();seen.clear();e._last_eval.clear();e.decision_pass()
    assert all(symbol!=s for symbol,_ in seen)


def test_webhook_accepts_only_currently_admitted_dynamic_symbols(prepared):
    from imperion.server.webhook import normalize_alert
    e,f,s=prepared
    assert normalize_alert({'symbol':s,'action':'long'},e.execution_symbols())['symbol']==s
    e.admission.clear()
    with pytest.raises(ValueError):normalize_alert({'symbol':s,'action':'long'},e.execution_symbols())
