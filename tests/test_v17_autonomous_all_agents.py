"""Safety and integration regression for AUTONOMOUS PRO 1.5 -> V17 adapters."""
from types import SimpleNamespace
from dataclasses import replace
import math

from imperion.agents.autonomous import (
    AutonomousIntelligence, SMCAgent, MultiTimeframeConfluenceAgent, WhaleAgent,
    LiquidityAgent, FundingAgent, VolumeProfileOBAgent, OrderFlowFootprintAgent,
    CryptoDayTradeAgent,
)
from imperion.config import load_settings, STYLES
from imperion.models import BULLISH, BEARISH, NEUTRAL, Candle
from imperion.analysis.priceaction import build_map
from imperion.analysis.priceaction.types import PAMap, Structure, Sweep
from imperion.learning.autonomous import (
    ExperienceMemory, AttributionEngine, AutonomousMind, research_summary,
)
from imperion.fakes import make_engine


def ctx(**kw):
    cfg = load_settings({})
    st = Structure(trend='UP',last_break=SimpleNamespace(kind='BOS'))
    m = PAMap(ok=True, n=100, structure=st, price=100, atr=1,
              volume_profile={'poc':100,'val':98,'vah':102},
              vp_ob_confluence=[{'direction':'BULLISH','preferred':True,'distance_atr':0.4,
                                 'vp_score':0.8,'mid':99}],
              sweeps=[Sweep(direction=BULLISH,level=100,extreme=98,index=1,age=2,
                            touches=2,kind='SSL',quality=.8)])
    data = dict(symbol='BTCUSDT',style=STYLES['day'],now=600000,cfg=cfg,
                candles=None,trades=None,books=None,deriv=None,universe=cfg.symbols,
                setup_map=m,trigger_map=m,regime={'regime':'TREND'},mtf={'tfs':{
                    '4h':{'bias':BULLISH,'score':.9},'15m':{'bias':BULLISH,'score':.75},
                    '5m':{'bias':BULLISH,'score':.7}}})
    data.update(kw)
    return SimpleNamespace(**data)


def test_all_eight_analysts_available_and_vote_weight_zero():
    x=AutonomousIntelligence().analyze(ctx())
    assert len(x['agents'])==8 and x['mode']=='shadow_only' and x['vote_weight']==0
    assert x['agents']['smc']['direction']==BULLISH
    assert x['agents']['mtf_confluence']['facts']['aligned'] is True
    assert x['agents']['liquidity_sweep']['direction']==BULLISH
    assert x['agents']['volume_profile_ob']['direction']==BULLISH
    assert x['agents']['crypto_daytrade']['direction']==BULLISH
    assert not x['agents']['funding_oi']['available']
    assert not x['agents']['whale']['available']
    assert not x['agents']['orderflow_footprint']['available']


def test_funding_does_not_fabricate_open_interest():
    deriv=SimpleNamespace(get=lambda _:{'funding':.001},age_ms=lambda:5000)
    r=FundingAgent().analyze(ctx(deriv=deriv))
    assert r['available'] and not r['facts']['oi_available']
    assert r['direction']==BEARISH
    stale=FundingAgent().analyze(ctx(deriv=SimpleNamespace(get=deriv.get, age_ms=lambda:99999999)))
    assert not stale['available']


def test_mtf_conflicts_and_missing_tf_never_claim_alignment():
    x=ctx();x.mtf['tfs']['5m']={'bias':BEARISH,'score':-.9}
    r=MultiTimeframeConfluenceAgent().analyze(x)
    assert not r['facts']['aligned'] and r['direction']==NEUTRAL
    del x.mtf['tfs']['4h']
    assert not MultiTimeframeConfluenceAgent().analyze(x)['available']


def test_invalid_absent_spread_data_cannot_create_agent_veto():
    x=AutonomousIntelligence().analyze(ctx(setup_map=PAMap(ok=False,reason='no candles')))
    assert x['mode']=='shadow_only'
    for v in x['agents'].values():
        assert 'veto' not in v
    assert x['agents']['smc']['available'] is False


def test_memory_ignores_unknown_fields_and_negative_outcomes():
    samples=[{'id':1,'ts':1,'symbol':'BTCUSDT','side':'LONG','r_result':-1.,'meta':{'regime':'RANGE','session':'LONDON','grade':'A'}}]
    memory=ExperienceMemory(samples)
    assert memory.recall_similar({'regime':None,'session':None,'grade':None})==[]
    assert memory.verdict({'regime':'RANGE'},'BTCUSDT')['expectancy_r']==-1.
    assert memory.verdict({'regime':'RANGE'},'BTCUSDT')['reliable'] is False
    res=research_summary(samples)
    assert not res['proposed_weights_applied'] and res['model_training']=='none'


def test_attribution_reads_only_recorded_shadow_agent_outputs():
    source={'mode':'shadow_only','agents':{'smc':{'available':True,'direction':BULLISH},
                                          'whale':{'available':False,'direction':BULLISH}}}
    samples=[{'ts':1,'side':'LONG','r_result':1.3,'meta':{'autonomous':source}}]
    s=AttributionEngine(samples).summary()
    assert s['per_agent']['smc']['aligned_expectancy_r']==1.3
    assert 'whale' not in s['per_agent']
    assert s['no_automatic_weight_changes']


def test_director_keeps_original_agent_consensus_and_serializable_research():
    engine,fake=make_engine(env={'RADAR_TRADING_ENABLED':'false'})
    try:
        a=engine.director.analyze('BTCUSDT','day')
        assert 'autonomous' in a.regime
        assert a.regime['autonomous']['vote_weight']==0
        assert 'thought' in a.regime['autonomous']
        assert a.regime['autonomous']['thought']['action']=='OBSERVE_ONLY'
        assert len(a.reports)==11
        assert a.to_dict()['regime']['autonomous']['risk_authority']=='V17_Guardian'
    finally:
        engine.stop()


def test_shadow_metadata_contains_agent_evidence_but_no_execution_authority():
    from imperion.models import TradeSetup, Analysis
    engine,fake=make_engine(env={'RADAR_TRADING_ENABLED':'false'})
    try:
        setup=TradeSetup('BTCUSDT','day','LONG',100.,99.,101.,102.,1.,2.,1.,playbook='trend_pullback',grade='A')
        an=Analysis('BTCUSDT','day',10000,direction='LONG',setup=setup,grade='A',quality=82.,
                    regime={'regime':'TREND','session':'LONDON','autonomous':{'mode':'shadow_only','agents':{'smc':{'available':True,'direction':BULLISH}}}})
        engine._record_shadow(an,'test')
        row=engine.db.query('SELECT meta_json FROM shadows')
        assert len(row)==1 and '"autonomous"' in row[0]['meta_json']
        assert engine.risk.cfg.risk_per_trade_pct==1.
    finally:
        engine.stop()


def test_learning_observer_reports_no_automatic_training():
    engine,fake=make_engine(env={'RADAR_TRADING_ENABLED':'false'})
    try:
        research=engine.learning.summary()['autonomous']
        assert research['mode']=='shadow_research_only'
        assert research['mind']['resolved']==0
        assert not research['proposed_weights_applied']
    finally:
        engine.stop()
