"""AUTONOMOUS PRO ExperienceMemory, AttributionEngine, AutonomousMind for V17.

Adapted into deterministic READ-ONLY shadow-outcome research; no model mutation,
entry creation, risk increase, filesystem weights or unsupervised promotion.
Historical source: AUTONOMOUS PRO 1.5 RL (user-supplied archive).
Evidence is persisted by V17's existing shadow rows and read from SQLite.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..models import BULLISH, BEARISH


class ExperienceMemory:
    """Recall comparable resolved shadow scenarios (R multiples, not win odds)."""
    def __init__(self, samples: List[dict], max_size: int = 500):
        self.samples = [s for s in samples if _finite(s.get('r_result'))][-max_size:]

    @staticmethod
    def _context(s:dict)->dict:
        meta=s.get('meta') or {}
        return {k:meta.get(k) for k in ('regime','session','playbook','grade')}

    def recall_similar(self, context: Dict[str, Any], limit: int = 20, symbol: str = '') -> List[dict]:
        rows=[]
        for s in self.samples:
            sc=0.0; historical=self._context(s)
            for k,weight in (('regime',2.0),('session',1.0),('playbook',1.5),('grade',0.5)):
                # Explicitly skip unknown/None values (upstream had None==None false matches).
                if context.get(k) is not None and historical.get(k)==context[k]:sc+=weight
            if symbol and s.get('symbol')==symbol:sc+=0.4
            if sc>0:rows.append((sc,s))
        rows.sort(key=lambda p:(-p[0],-int(p[1].get('ts') or 0)))
        return [s for _,s in rows[:max(1,min(limit,100))]]

    def verdict(self,context:dict,symbol:str='')->dict:
        found=self.recall_similar(context,limit=50,symbol=symbol)
        n=len(found)
        if not n:return {'samples':0,'expectancy_r':None,'win_rate':None,'reliable':False}
        r=[float(s['r_result']) for s in found]
        return {'samples':n,'expectancy_r':round(sum(r)/n,4),
                'win_rate':round(sum(x>0 for x in r)/n,3),'reliable':n>=30,
                'source':'resolved_shadows_not_live_pnl'}


class AttributionEngine:
    """Outcome-based evidence audit; NEVER writes to production weights."""
    def __init__(self,samples:List[dict]):self.samples=samples

    def summary(self)->dict:
        sums={}
        for s in self.samples:
            r=s.get('r_result')
            if not _finite(r):continue
            side=BULLISH if s.get('side')=='LONG' else BEARISH if s.get('side')=='SHORT' else None
            if side is None:continue
            autonomous=(s.get('meta') or {}).get('autonomous') or {}
            reports=autonomous.get('agents') or {}
            for name,v in reports.items():
                if not v.get('available') or v.get('direction') not in (BULLISH,BEARISH):continue
                signed=float(r) * (1 if v['direction']==side else -1)
                arr=sums.setdefault(name,[]);arr.append(signed)
        out={}
        for name,values in sorted(sums.items()):
            n=len(values)
            out[name]={'n':n,'aligned_expectancy_r':round(sum(values)/n,4),
                       'reliable':n>=30}
        return {'mode':'observational_only','per_agent':out,
                'no_automatic_weight_changes':True}


class AutonomousMind:
    """Explain agent agreement and reflect on resolved outcomes; no self-executing policy."""
    def think(self,analysis:dict)->dict:
        reports=analysis.get('agents') or {}
        positive=[n for n,v in reports.items() if v.get('available') and v.get('direction') in (BULLISH,BEARISH)]
        sides={reports[n]['direction'] for n in positive}
        return {'observations':len(reports),'directional':positive,
                'contradictions':len(sides)>1,'action':'OBSERVE_ONLY',
                'note':'Agents cannot override V17 approval, Guardian or execution'}

    def reflect(self,samples:List[dict],attribution:dict)->dict:
        valid=[float(s['r_result']) for s in samples if _finite(s.get('r_result'))]
        if not valid:return {'resolved':0,'expectancy_r':None,'win_rate':None,
                             'summary':'No resolved shadows; no learning claims'}
        return {'resolved':len(valid),'expectancy_r':round(sum(valid)/len(valid),4),
                'win_rate':round(sum(x>0 for x in valid)/len(valid),3),
                'summary':'Outcome attribution is descriptive, not a validated causal edge',
                'auto_risk_adjustment':False,'manual_promotion_required':True}


def _finite(n):return isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n)


def research_summary(samples:List[dict])->dict:
    """Source of truth is same-core resolved V17 shadow trades."""
    memory=ExperienceMemory(samples)
    attribution=AttributionEngine(samples).summary()
    mind=AutonomousMind().reflect(samples,attribution)
    return {'mode':'shadow_research_only','experience_count':len(memory.samples),
            'mind':mind,'attribution':attribution,'risk_authority':'V17',
            'model_training':'none','proposed_weights_applied':False}
