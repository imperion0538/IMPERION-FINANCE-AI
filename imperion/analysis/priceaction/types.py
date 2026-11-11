
"""IMPERION FINANCE AI V17 - Price Action Data Models.

Backward-compatible data contracts for structure, liquidity, zones,
sessions, order flow and executable playbooks.

Existing positional constructor arguments and legacy fields are preserved.
New fields are appended with defaults to avoid breaking V17 consumers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ...models import NEUTRAL

LIQUIDITY_STATES = (
    "FRESH",
    "TESTED",
    "SWEPT",
    "INVALIDATED",
)

LIQUIDITY_SIDES = ("BUY_SIDE", "SELL_SIDE")


@dataclass(frozen=True)
class Swing:
    i: int
    t: int
    price: float
    kind: str  # H | L

    @property
    def confirmation_index(self) -> int:
        """Default major swing confirmation width."""
        return self.i + 3


@dataclass
class Break:
    kind: str
    direction: str
    level: float
    index: int
    displacement: float
    major: bool = False
    age: int = 0

    # Extended evidence; not required by legacy constructors.
    confirmed_close: bool = True
    body_ratio: float = 0.0
    swept_liquidity: bool = False


@dataclass
class Structure:
    trend: str = "UNKNOWN"
    last_break: Optional[Break] = None
    breaks: List[Break] = field(default_factory=list)
    ref_high: Optional[Swing] = None
    ref_low: Optional[Swing] = None
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None
    protected: Optional[float] = None
    protected_i: Optional[int] = None
    swing_bias: str = "MIXED"

    internal_trend: str = "UNKNOWN"
    external_trend: str = "UNKNOWN"
    internal_last_break_kind: Optional[str] = None
    internal_last_break_direction: Optional[str] = None
    internal_last_break_age: Optional[int] = None

    # Explicit conflict and structural invalidation metadata.
    htf_conflict: bool = False
    htf_policy: str = "VETO"
    invalidated: bool = False
    invalidation_reason: str = ""


@dataclass
class DealingRange:
    high: float
    low: float
    position: float
    source: str
    ote_zone: bool = False
    premium_discount: str = "NEUTRAL"

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class Pool:
    price: float
    kind: str
    touches: int
    last_i: int

    # Lifecycle fields are optional for legacy liquidity producers.
    state: str = "FRESH"
    side: str = ""
    last_test_i: Optional[int] = None
    swept_i: Optional[int] = None
    invalidated_i: Optional[int] = None

    @property
    def active(self) -> bool:
        return self.state in ("FRESH", "TESTED")


@dataclass
class Sweep:
    direction: str
    level: float
    extreme: float
    index: int
    age: int
    touches: int
    kind: str
    quality: float
    has_inducement: bool = False

    # Additional confirmation evidence.
    reclaimed: bool = True
    reclaim_index: Optional[int] = None
    external_liquidity: bool = False


@dataclass
class Zone:
    kind: str
    direction: str
    lo: float
    hi: float
    created: int
    touches: int = 0
    fill: float = 0.0
    displacement: float = 0.0
    flipped_at: Optional[int] = None
    quality: float = 0.5
    mean_threshold: Optional[float] = None

    # Optional lifecycle and context metadata.
    state: str = "FRESH"
    invalidated_at: Optional[int] = None
    source_tf: str = ""
    htf_aligned: Optional[bool] = None

    @property
    def bounds(self) -> Tuple[float, float]:
        return self.lo, self.hi

    @property
    def midpoint(self) -> float:
        return (self.lo + self.hi) / 2.0

    @property
    def active(self) -> bool:
        return self.state != "INVALIDATED"


@dataclass
class Inducement:
    direction: str
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

    kind: str = ""
    state: str = "FRESH"
    side: str = ""
    source_tf: str = ""


@dataclass
class SessionRange:
    name: str
    high: float
    low: float
    start_i: int
    end_i: int
    swept: bool = False
    sweep_dir: Optional[str] = None
    sweep_quality: float = 0.0
    range_atr: float = 0.0
    age_candles: int = 0
    killzone: Optional[str] = None
    overlap: bool = False

    # Explicit session validity metadata.
    timezone: str = "UTC"
    complete: bool = True


@dataclass
class CISD:
    direction: str
    index: int
    age: int
    level: float
    displacement: float
    quality: float = 0.6
    after_sweep: bool = False
    after_session: bool = False
    follow_through: float = 0.0
    closes_beyond_structure: bool = False


@dataclass
class PAMap:
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
    displacement: Optional[Tuple[str, float]] = None
    levels: List[Level] = field(default_factory=list)
    recent_low: Optional[float] = None
    recent_high: Optional[float] = None

    inducements: List[Inducement] = field(default_factory=list)
    mitigation_zones: List[Zone] = field(default_factory=list)
    confluence_score: float = 0.0

    momentum: str = "NEUTRAL"
    momentum_strength: float = 0.0
    trend_direction: str = "NEUTRAL"
    trend_strength: float = 0.0

    sessions: List[SessionRange] = field(default_factory=list)
    cisds: List[CISD] = field(default_factory=list)
    displacement_quality: float = 0.0
    displacement_facts: dict = field(default_factory=dict)
    flow_divergence: dict = field(default_factory=dict)

    # New advanced ecosystem outputs.
    liquidity_inventory: List[Dict[str, Any]] = field(
        default_factory=list
    )
    previous_week_high: Optional[float] = None
    previous_week_low: Optional[float] = None
    regime_confidence: float = 0.0
    session_valid: Optional[bool] = None
    news_blocked: bool = False
    invalidation_reasons: List[str] = field(default_factory=list)


@dataclass
class Playbook:
    name: str
    side: str
    score: float
    steps: List[str] = field(default_factory=list)
    invalidation: float = 0.0
    target: Optional[float] = None
    cap: Optional[float] = None

    # Advanced entry lifecycle.
    trigger_model: str = ""
    expiry_candles: Optional[int] = None
    confirmation_source: str = ""
    confirmation_passed: Optional[bool] = None
    regime: str = ""
    session: str = ""
    invalidation_reason: str = ""

    def to_dict(self) -> dict:
        """Preserve legacy serialization and add optional evidence."""
        return {
            "name": self.name,
            "side": self.side,
            "score": round(self.score, 3),
            "steps": self.steps,
            "invalidation": self.invalidation,
            "target": self.target,
            "cap": self.cap,
            "trigger_model": self.trigger_model,
            "expiry_candles": self.expiry_candles,
            "confirmation_source": self.confirmation_source,
            "confirmation_passed": self.confirmation_passed,
            "regime": self.regime,
            "session": self.session,
            "invalidation_reason": self.invalidation_reason,
        }
