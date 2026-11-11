"""Value types shared by data, analysis, agents, the director and execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BULLISH, BEARISH, NEUTRAL = "BULLISH", "BEARISH", "NEUTRAL"
SIDE_OF = {BULLISH: "LONG", BEARISH: "SHORT"}
DIR_OF = {"LONG": BULLISH, "SHORT": BEARISH}


@dataclass(slots=True)
class Candle:
    t: int          # open time (ms)
    o: float
    h: float
    l: float
    c: float
    v: float        # base volume
    ct: int         # close time (ms)
    qv: float = 0.0  # quote volume
    n: int = 0       # trade count
    tbv: float = 0.0  # taker-buy base volume (aggressive buyers)

    @property
    def delta(self) -> float:
        """Aggressive buy volume minus aggressive sell volume (base units)."""
        return 2.0 * self.tbv - self.v

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @classmethod
    def from_rest(cls, row: list) -> "Candle":
        return cls(
            t=int(row[0]), o=float(row[1]), h=float(row[2]), l=float(row[3]), c=float(row[4]),
            v=float(row[5]), ct=int(row[6]), qv=float(row[7]), n=int(row[8]), tbv=float(row[9]),
        )


@dataclass
class AgentReport:
    """One agent's reading. `available=False` means 'could not evaluate' and is never treated as neutral."""
    agent: str
    direction: str = NEUTRAL
    confidence: float = 0.0
    summary: str = ""
    available: bool = True
    facts: Dict[str, Any] = field(default_factory=dict)
    veto: Dict[str, str] = field(default_factory=dict)      # {"LONG": "reason"} blocks that side outright

    @staticmethod
    def unavailable(agent: str, why: str) -> "AgentReport":
        return AgentReport(agent, NEUTRAL, 0.0, why, available=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"d": self.direction, "c": round(float(self.confidence), 3), "ok": self.available, "summary": self.summary,
                "veto": dict(self.veto)}


@dataclass
class TradeSetup:
    symbol: str
    style: str
    side: str            # LONG | SHORT
    entry: float
    stop: float
    tp1: float
    tp2: float
    atr: float
    rr: float
    stop_pct: float
    notes: List[str] = field(default_factory=list)
    playbook: str = ""
    grade: str = ""

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "style": self.style, "side": self.side, "entry": self.entry,
            "stop": self.stop, "tp1": self.tp1, "tp2": self.tp2, "atr": self.atr, "rr": round(self.rr, 3),
            "stop_pct": round(self.stop_pct, 4), "notes": self.notes, "playbook": self.playbook, "grade": self.grade,
        }


@dataclass
class Analysis:
    """Everything the director concluded about one (symbol, style) at one moment. Never places orders."""
    symbol: str
    style: str
    ts: int
    price: Optional[float] = None
    direction: str = "NO TRADE"          # LONG | SHORT | NO TRADE
    approved: bool = False               # all ANALYSIS gates passed (risk / account / AI are checked by the runtime)
    reasons: List[str] = field(default_factory=list)
    reports: Dict[str, AgentReport] = field(default_factory=dict)
    regime: Dict[str, Any] = field(default_factory=dict)
    playbooks: List[Any] = field(default_factory=list)
    playbook: Optional[Any] = None       # chosen Playbook
    consensus: Dict[str, Any] = field(default_factory=dict)
    setup: Optional[TradeSetup] = None   # plan of the chosen (or best rejected) candidate
    quality: Optional[float] = None
    grade: str = ""
    mtf: Dict[str, Any] = field(default_factory=dict)
    maps: Dict[str, Any] = field(default_factory=dict)
    data_ok: bool = True
    weights_version: int = 0

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Compact agent readings for shadows / the win model: {agent: {"d": direction, "s": confidence}}."""
        return {k: {"d": r.direction, "s": round(float(r.confidence), 3)} for k, r in self.reports.items() if r.available}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "style": self.style, "ts": self.ts, "price": self.price, "direction": self.direction,
            "approved": self.approved, "reasons": self.reasons, "quality": self.quality, "grade": self.grade,
            "playbook": self.playbook.to_dict() if self.playbook is not None else None,
            "playbooks": [p.to_dict() for p in self.playbooks[:4]], "regime": self.regime, "consensus": self.consensus,
            "agents": {k: r.to_dict() for k, r in self.reports.items()}, "setup": self.setup.to_dict() if self.setup else None,
            "mtf": self.mtf, "maps": self.maps, "data_ok": self.data_ok,
        }
