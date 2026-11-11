"""Value types of the price-action map. Everything here is plain data: the engines never keep hidden state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ...models import NEUTRAL


@dataclass(frozen=True)
class Swing:
    i: int          # candle index
    t: int          # candle open time (ms)
    price: float
    kind: str       # "H" | "L"


@dataclass
class Break:
    """A candle CLOSE through an unbroken swing: BOS continues the trend, CHOCH is the first break against it."""
    kind: str               # "BOS" | "CHOCH"
    direction: str          # "UP" | "DOWN"
    level: float
    index: int
    displacement: float     # body of the breaking candle in ATR multiples
    major: bool = False     # took out the most extreme swing of the recent window
    age: int = 0            # candles since the break


@dataclass
class Structure:
    trend: str = "UNKNOWN"                  # UP | DOWN | RANGE | UNKNOWN
    last_break: Optional[Break] = None
    breaks: List[Break] = field(default_factory=list)
    ref_high: Optional[Swing] = None        # unbroken swing high: a close above it is the next break up
    ref_low: Optional[Swing] = None
    last_high: Optional[Swing] = None       # most recent confirmed swings (targets / obstacles)
    last_low: Optional[Swing] = None
    protected: Optional[float] = None       # origin of the last break: losing it invalidates the trend
    protected_i: Optional[int] = None
    swing_bias: str = "MIXED"               # HH_HL | LH_LL | MIXED
    # Advanced
    internal_trend: str = "UNKNOWN"         # nested / internal structure bias
    external_trend: str = "UNKNOWN"
    internal_last_break_kind: Optional[str] = None
    internal_last_break_direction: Optional[str] = None
    internal_last_break_age: Optional[int] = None


@dataclass
class DealingRange:
    high: float
    low: float
    position: float     # 0 = range low (deep discount), 1 = range high (deep premium)
    source: str         # "leg" (impulse leg of the current trend) | "window"
    ote_zone: bool = False                  # True if current price is in 0.62-0.79 band
    premium_discount: str = "NEUTRAL"       # PREMIUM | DISCOUNT | NEUTRAL | OTE


@dataclass
class Pool:
    """Resting liquidity: stops above equal/old highs (buy-side) or below equal/old lows (sell-side)."""
    price: float
    kind: str           # EQH | HIGH | PDH | EQL | LOW | PDL
    touches: int
    last_i: int


@dataclass
class Sweep:
    """A wick through a pool that was closed back: the stops were taken and price was rejected."""
    direction: str      # BULLISH after a sell-side sweep, BEARISH after a buy-side sweep
    level: float
    extreme: float      # tip of the sweeping wick (structural invalidation of the reversal)
    index: int
    age: int
    touches: int
    kind: str
    quality: float      # 0.5 .. 0.85
    has_inducement: bool = False            # True if an IDM was taken before this sweep


@dataclass
class Zone:
    kind: str                   # "OB" | "FVG" | "MITIGATION" | "BREAKER"
    direction: str              # polarity at creation
    lo: float
    hi: float
    created: int
    touches: int = 0
    fill: float = 0.0           # share of an FVG already traded into (1 = fully mitigated)
    displacement: float = 0.0   # ATR multiple of the candle that created it
    flipped_at: Optional[int] = None    # index of the close through the zone (OB -> breaker, FVG -> inverted FVG)
    quality: float = 0.5                # 0..1 advanced quality score
    mean_threshold: Optional[float] = None  # 50% of the zone (for OTE-like entries)

    @property
    def bounds(self) -> Tuple[float, float]:
        return self.lo, self.hi


@dataclass
class Inducement:
    """Internal liquidity (IDM) taken before a major external liquidity sweep."""
    direction: str              # BULLISH / BEARISH
    level: float
    index: int
    age: int
    quality: float = 0.6


@dataclass
class CandleSignal:
    direction: str = NEUTRAL
    strength: float = 0.0
    patterns: List[str] = field(default_factory=list)


@dataclass
class Level:
    price: float
    touches: int


@dataclass
class SessionRange:
    """Session liquidity range (Asia, London, NY) with sweep quality."""
    name: str                   # ASIA | LONDON | NY
    high: float
    low: float
    start_i: int
    end_i: int
    swept: bool = False
    sweep_dir: Optional[str] = None  # BULLISH / BEARISH if swept
    sweep_quality: float = 0.0       # 0..1 how clean and significant the sweep was
    range_atr: float = 0.0           # session range width in ATR
    age_candles: int = 0             # candles since session ended
    killzone: Optional[str] = None   # LONDON_OPEN | NY_OPEN | LONDON_NY_OVERLAP
    overlap: bool = False



@dataclass
class CISD:
    """Change in State of Delivery: strong displacement that shifts delivery after liquidity or range break."""
    direction: str              # BULLISH | BEARISH
    index: int
    age: int
    level: float                # close or extreme of the CISD candle
    displacement: float         # ATR multiple
    quality: float = 0.6
    after_sweep: bool = False
    after_session: bool = False
    follow_through: float = 0.0     # how well the next candles continued
    closes_beyond_structure: bool = False



@dataclass
class PAMap:
    """Everything price action knows about one candle series, computed once and shared by engines and playbooks."""
    ok: bool
    reason: str = ""
    n: int = 0
    price: float = 0.0
    atr: float = 0.0
    swings: List[Swing] = field(default_factory=list)
    minor: List[Swing] = field(default_factory=list)
    structure: Structure = field(default_factory=Structure)
    range: Optional[DealingRange] = None
    pools_above: List[Pool] = field(default_factory=list)
    pools_below: List[Pool] = field(default_factory=list)
    sweeps: List[Sweep] = field(default_factory=list)
    external_sweeps: List[Sweep] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    candle: CandleSignal = field(default_factory=CandleSignal)
    displacement: Optional[Tuple[str, float]] = None     # (direction, body in ATR) of the last candle
    levels: List[Level] = field(default_factory=list)
    recent_low: Optional[float] = None
    recent_high: Optional[float] = None
    # Advanced layers
    inducements: List[Inducement] = field(default_factory=list)
    mitigation_zones: List[Zone] = field(default_factory=list)
    confluence_score: float = 0.0
    # Momentum & Trend direction (explicit PA-level)
    momentum: str = "NEUTRAL"           # BULLISH | BEARISH | NEUTRAL
    momentum_strength: float = 0.0      # 0..1
    trend_direction: str = "NEUTRAL"    # aligned with structure.trend but normalized
    trend_strength: float = 0.0         # 0..1 based on recent BOS quality / age
    # Session & CISD
    sessions: List[SessionRange] = field(default_factory=list)
    cisds: List[CISD] = field(default_factory=list)
    displacement_quality: float = 0.0
    displacement_facts: dict = field(default_factory=dict)
    flow_divergence: dict = field(default_factory=dict)



@dataclass
class Playbook:
    """A complete setup model: what happened (steps), where the idea is wrong (invalidation) and where it aims."""
    name: str                       # sweep_reversal | trend_pullback | breakout_retest | range_fade | inducement_sweep | ote_pullback | mitigation_retest
    side: str                       # LONG | SHORT
    score: float                    # 0 .. 1 completeness of the model
    steps: List[str] = field(default_factory=list)
    invalidation: float = 0.0
    target: Optional[float] = None
    cap: Optional[float] = None     # hard ceiling for the final target (range plays)

    def to_dict(self) -> dict:
        return {"name": self.name, "side": self.side, "score": round(self.score, 3), "steps": self.steps,
                "invalidation": self.invalidation, "target": self.target, "cap": self.cap}
