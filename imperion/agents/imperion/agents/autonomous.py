"""AUTONOMOUS PRO 1.5 RL -> V17 adapter: evidence-only specialist agents.

The eight reports are read-only, non-voting supplementary observations. Their
confidence is NOT a calibrated probability and can NEVER approve, size, veto or
place a trade. Original ideas adapted: SMCAgent, MultiTimeframeConfluenceAgent,
CryptoDayTradeAgent, WhaleAgent, LiquidityAgent, FundingAgent, VP/OB and delta /
footprint. Actual orderflow comes from V17 aggTrade, not synthetic OHLCV prints.
All missing/stale evidence remains explicitly unavailable. Zero extra REST calls.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from ..models import BULLISH, BEARISH, NEUTRAL
from ..analysis.mtf import cached_tf_bias
from ..util import clamp
from .base import Agent, AgentContext


def _valid(n) -> bool:
    return isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)


def _obs(name: str, direction: str = NEUTRAL, score: float = 0.0,
         reason: str = "", facts=None, available: bool = True) -> dict:
    direction = direction if direction in (BULLISH, BEARISH, NEUTRAL) else NEUTRAL
    return {"name": name, "direction": direction,
            "confidence": round(clamp(float(score), 0, 1), 3) if _valid(score) else 0.0,
            "available": bool(available), "reason": str(reason)[:220],
            "facts": facts or {}}


def _map_dir(trend) -> str:
    return {"UP": BULLISH, "DOWN": BEARISH, BULLISH: BULLISH, BEARISH: BEARISH}.get(trend, NEUTRAL)


class SMCAgent:
    """Second-order SMC checklist over validated V17 PA maps, not a second execution engine.

    Upstream SMC uses its own Candle/OrderBlock models; use V17's established
    structure, zones, sweeps and volume-profile adapters instead. This output
    intentionally DOES NOT claim independence of underlying candle data.
    """
    name = 'smc'

    def analyze(self, ctx: AgentContext) -> dict:
        m = ctx.setup_map
        if m is None or not m.ok:
            return _obs(self.name, reason='setup PA map unavailable', available=False)
        direction = _map_dir(m.structure.trend)
        if direction == NEUTRAL:
            return _obs(self.name, reason='neutral / range structure', facts={'structure':m.structure.trend})
        obs = [z for z in m.zones if z.kind == 'OB' and z.active and z.direction == direction]
        fvg = [z for z in m.zones if z.kind in ('FVG', 'IFVG') and z.active and z.direction == direction]
        sweeps = [s for s in m.sweeps if s.age <= 12 and s.direction == direction]
        dr = m.range
        good_pd = bool(dr and (dr.premium_discount in ('OTE+ZONE','DISCOUNT') if direction==BULLISH
                               else dr.premium_discount in ('OTE+ZONE','PREMIUM')))
        vp = [r for r in m.vp_ob_confluence if r.get('direction')==direction and r.get('preferred')]
        conf = min(0.95, 0.28 + 0.17*bool(m.structure.last_break) + 0.14*bool(obs)
                   + 0.10*bool(fvg) + 0.12*bool(sweeps) + 0.06*good_pd + 0.08*bool(vp))
        facts={'structure':m.structure.trend, 'ob':len(obs), 'fvg':len(fvg),
               'sweep':len(sweeps), 'premium_discount':dr.premium_discount if dr else None,
               'vp_ob_matches':len(vp), 'source':'V17_PAMap_and_OHLCV_VP_proxy'}
        return _obs(self.name,direction,conf,'SMC structure/OB/FVG/liquidity checklist',facts)


class MultiTimeframeConfluenceAgent:
    name = 'mtf_confluence'
    # Source AUTONOMOUS weighting: HTF 45%, MTF 35%, LTF 20%.
    buckets = (('htf',('4h','1h'),0.45),('mtf',('15m','30m'),0.35),('ltf',('5m','1m'),0.20))

    def analyze(self,ctx: AgentContext)->dict:
        facts={};votes=[];total=0.0
        for role,tfs,w in self.buckets:
            d=NEUTRAL;score=0.0;found=False
            for tf in tfs:
                # Existing V17 mtf view is computed once by Director; avoid any additional indicator computation.
                row=(ctx.mtf.get('tfs') or {}).get(tf) or {}
                if row.get('bias') in (BULLISH,BEARISH,NEUTRAL):
                    d=row['bias'];score=float(row.get('score') or 0);found=True;break
            facts[role]={'direction':d,'score':round(score,3),'available':found}
            if found:
                total+=w
                if d in (BULLISH,BEARISH):votes.append(d)
        if total < 0.99:
            return _obs(self.name,reason='incomplete HTF/MTF/LTF evidence',facts=facts,available=False)
        aligned=len(votes)==3 and len(set(votes))==1
        direction=votes[0] if aligned else NEUTRAL
        c=sum(abs(facts[r]['score'])*w for r,_,w in self.buckets)
        return _obs(self.name,direction,c if aligned else c*0.6,
                    '3-layer alignment' if aligned else 'mixed / neutral timeframes',
                    {**facts,'aligned':aligned,'source':'V17_mtf_view'})


class WhaleAgent:
    name='whale'

    def analyze(self,ctx: AgentContext)->dict:
        if ctx.trades is None or not hasattr(ctx.trades,'whale_stats'):
            return _obs(self.name,reason='no trade event feed',available=False)
        if hasattr(ctx.trades,'last_ts'):
            ts=ctx.trades.last_ts(ctx.symbol)
            if not ts or abs(ctx.now-ts)>max(30_000,ctx.cfg.max_stream_latency_ms):
                return _obs(self.name,reason='stale/missing trade event feed',available=False)
        v=ctx.trades.whale_stats(ctx.symbol,window_ms=15*60_000,now=ctx.now)
        buy,sell=v.get('buy_usdt',0),v.get('sell_usdt',0)
        total=buy+sell
        if not _valid(total) or total<=0:
            return _obs(self.name,reason='no qualified whale events',facts={'count':0})
        ratio=(buy-sell)/total
        d=BULLISH if ratio>=0.18 else BEARISH if ratio<=-0.18 else NEUTRAL
        return _obs(self.name,d,min(0.9,abs(ratio)) if d!=NEUTRAL else 0,
                    f'qualified aggTrade whale net {buy-sell:+.0f} USDT / 15m',
                    {'net_usdt':round(buy-sell,2),'ratio':round(ratio,4),
                     'buy_count':v.get('buy_count',0),'sell_count':v.get('sell_count',0),
                     'source':'V17_aggTrade'})


class LiquidityAgent:
    name='liquidity_sweep'

    def analyze(self,ctx: AgentContext)->dict:
        m,t=ctx.setup_map,ctx.trigger_map
        if m is None or not m.ok:
            return _obs(self.name,reason='setup map unavailable',available=False)
        rows=[('setup',s) for s in m.sweeps if s.age<=12]
        if t is not None and t.ok:
            rows += [('trigger',s) for s in t.sweeps if s.age<=8]
        if not rows:return _obs(self.name,reason='no fresh sweep',facts={'found':0})
        sides={s.direction for _,s in rows if s.direction in (BULLISH,BEARISH)}
        if len(sides)!=1:
            return _obs(self.name,reason='conflicting sweep directions',facts={'found':len(rows)})
        src,s=max(rows,key=lambda row:row[1].quality)
        return _obs(self.name,s.direction,clamp(s.quality,0,0.95),
                    f'{src} {s.kind} sweep, age={s.age}',
                    {'level':s.level,'quality':s.quality,'age':s.age,'source':'V17_sweeps'})


class FundingAgent:
    name='funding_oi'

    def analyze(self,ctx: AgentContext)->dict:
        if ctx.deriv is None:return _obs(self.name,reason='derivatives unavailable',available=False)
        # The V17 premiumIndex store currently has funding and mark only, NOT OI.
        # Never invent OI signals; use the limited funding-only observation.
        d=ctx.deriv.get(ctx.symbol)
        if not isinstance(d,dict) or not _valid(d.get('funding')):
            return _obs(self.name,reason='funding unavailable',available=False)
        age=ctx.deriv.age_ms() if hasattr(ctx.deriv,'age_ms') else None
        if age is None or age>10*60_000:
            return _obs(self.name,reason='stale derivatives data',available=False)
        pct=float(d['funding'])*100
        direction=BEARISH if pct>0.05 else BULLISH if pct< -0.05 else NEUTRAL
        return _obs(self.name,direction,min(0.65,abs(pct)*3) if direction!=NEUTRAL else 0,
                    f'funding {pct:+.4f}% (OI unavailable; no OI vote)',
                    {'funding_pct':round(pct,5),'oi_available':False,'source':'V17_premiumIndex'})


class VolumeProfileOBAgent:
    name='volume_profile_ob'

    def analyze(self,ctx: AgentContext)->dict:
        m=ctx.setup_map
        if m is None or not m.ok:return _obs(self.name,reason='map unavailable',available=False)
        if not m.volume_profile:
            return _obs(self.name,reason='OHLCV volume profile unavailable',available=False)
        active=[r for r in m.vp_ob_confluence if r.get('preferred') and
                r.get('distance_atr',100)<=1.5]
        sides={r.get('direction') for r in active if r.get('direction') in (BULLISH,BEARISH)}
        if len(sides)!=1:
            return _obs(self.name,reason='no unique preferred OB+VP area',
                        facts={'poc':m.volume_profile.get('poc'),'active_matches':len(active)})
        best=max((r for r in active if r.get('direction') in sides),
                 key=lambda x:x.get('vp_score',0))
        return _obs(self.name,best['direction'],min(.95,float(best['vp_score'])),
                    'active Order Block with estimated POC/HVN confluence',
                    {'poc':m.volume_profile.get('poc'),'val':m.volume_profile.get('val'),
                     'vah':m.volume_profile.get('vah'),'ob_mid':best.get('mid'),
                     'score':best['vp_score'],'source':'OHLCV_uniform_bin_estimate'})


class OrderFlowFootprintAgent:
    name='orderflow_footprint'

    def analyze(self,ctx:AgentContext)->dict:
        if ctx.trades is None or not hasattr(ctx.trades,'flow_profile'):
            return _obs(self.name,reason='live aggTrade feed unavailable',available=False)
        if hasattr(ctx.trades,'last_ts'):
            ts=ctx.trades.last_ts(ctx.symbol)
            if not ts or abs(ctx.now-ts)>max(30_000,ctx.cfg.max_stream_latency_ms):
                return _obs(self.name,reason='stale trade event feed',available=False)
        flow=ctx.trades.flow_profile(ctx.symbol,minutes=5,now=ctx.now)
        if not flow.get('available'):
            return _obs(self.name,reason='insufficient trade-level data',available=False)
        delta=flow.get('delta_ratio')
        if not _valid(delta):return _obs(self.name,reason='delta unavailable',available=False)
        direction=BULLISH if delta>0.15 else BEARISH if delta< -0.15 else NEUTRAL
        # V17 already has a voting orderflow agent; this duplicate observation has NO vote.
        return _obs(self.name,direction,min(.85,abs(delta)*1.2) if direction!=NEUTRAL else 0,
                    'real aggTrade footprint/delta observation',
                    {'delta_ratio':round(delta,4),'poc_bin':flow.get('poc_bin'),
                     'levels':len(flow.get('levels') or []),'source':'V17_real_aggTrade'})


class CryptoDayTradeAgent:
    name='crypto_daytrade'

    def analyze(self,ctx:AgentContext,source:Dict[str,dict])->dict:
        """Join price, MTF, flow, whale and derivatives evidence without creating orders."""
        primary=source.get('smc',{})
        mtf=source.get('mtf_confluence',{})
        if not primary.get('available') or not mtf.get('available'):
            return _obs(self.name,reason='required SMC/MTF incomplete',available=False)
        if ctx.regime.get('regime') in ('UNKNOWN',None):
            return _obs(self.name,reason='regime unknown',available=False)
        direction=primary.get('direction',NEUTRAL)
        agree=[];oppose=[]
        for name in ('mtf_confluence','liquidity_sweep','whale','funding_oi','orderflow_footprint','volume_profile_ob'):
            row=source.get(name,{})
            if not row.get('available') or row.get('direction')==NEUTRAL:continue
            (agree if row['direction']==direction else oppose).append(name)
        confidence=clamp(.55*float(primary['confidence']) + .45*float(mtf['confidence']),0,.95)
        if direction==NEUTRAL or not mtf.get('facts',{}).get('aligned'):
            return _obs(self.name,reason='SMC/MTF direction unaligned',
                        facts={'agree':agree,'oppose':oppose,'regime':ctx.regime.get('regime')})
        return _obs(self.name,direction,confidence,'advisory day-trade multi-evidence agreement',
                    {'agree':agree,'oppose':oppose,'regime':ctx.regime.get('regime'),
                     'source':'V17_existing_market_context','risk_authority':'V17_only'})


class AutonomousIntelligence:
    """Separate non-voting team: cannot change V17 AGENT_WEIGHTS or execution gates."""
    def __init__(self):
        self.smc=SMCAgent()
        self.mtf=MultiTimeframeConfluenceAgent()
        self.whale=WhaleAgent()
        self.liquidity=LiquidityAgent()
        self.funding=FundingAgent()
        self.vp=VolumeProfileOBAgent()
        self.flow=OrderFlowFootprintAgent()
        self.day=CryptoDayTradeAgent()
        self._agents=(self.smc,self.mtf,self.whale,self.liquidity,self.funding,self.vp,self.flow)

    def analyze(self,ctx:AgentContext)->dict:
        if ctx.style.name != 'day':return {'mode':'shadow_only','agents':{},'reason':'day_trade_only'}
        result={}
        for agent in self._agents:
            try:
                result[agent.name]=agent.analyze(ctx)
            except Exception as exc:
                result[agent.name]=_obs(agent.name,reason=f'adapter failed: {type(exc).__name__}',available=False)
        try:
            result[self.day.name]=self.day.analyze(ctx,result)
        except Exception as exc:
            result[self.day.name]=_obs(self.day.name,reason=f'adapter failed: {type(exc).__name__}',available=False)
        return {'mode':'shadow_only','vote_weight':0.0,'risk_authority':'V17_Guardian',
                'agents':result,'available':sum(bool(v['available']) for v in result.values())}
