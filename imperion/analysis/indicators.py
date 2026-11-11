"""Pure-python indicators (no numpy: small, portable, deterministic)."""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..models import Candle


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema_series(values: Sequence[float], n: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def ema(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return ema_series(values, n)[-1]


def true_ranges(cs: Sequence[Candle]) -> List[float]:
    out = []
    for i, c in enumerate(cs):
        if i == 0:
            out.append(c.h - c.l)
        else:
            pc = cs[i - 1].c
            out.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    return out


def atr_series(cs: Sequence[Candle], n: int = 14) -> List[float]:
    trs = true_ranges(cs)
    if len(trs) < n:
        return []
    out = [sum(trs[:n]) / n] * n  # warm-up plateau keeps index alignment
    for tr in trs[n:]:
        out.append((out[-1] * (n - 1) + tr) / n)
    return out


def atr(cs: Sequence[Candle], n: int = 14) -> Optional[float]:
    s = atr_series(cs, n)
    return s[-1] if s else None


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def zscore(x: float, history: Sequence[float]) -> float:
    sd = stdev(history)
    if sd <= 1e-12:
        return 0.0
    return (x - sum(history) / len(history)) / sd


def efficiency_ratio(closes: Sequence[float], n: int = 30) -> Optional[float]:
    """Kaufman efficiency ratio: 1 = straight line, 0 = pure noise."""
    if len(closes) <= n:
        return None
    net = abs(closes[-1] - closes[-1 - n])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    return net / path if path > 0 else 0.0


def median(values: Sequence[float]) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def rsi(values: Sequence[float], n: int = 14) -> Optional[float]:
    """Wilder RSI (0..100)."""
    if len(values) <= n or n <= 0:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0.0); loss += max(-d, 0.0)
    gain, loss = gain / n, loss / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
    if loss <= 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def macd_hist(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> List[float]:
    """MACD histogram series (MACD line minus its signal line); empty when there is not enough data."""
    if len(values) < slow + signal:
        return []
    line = [a - b for a, b in zip(ema_series(values, fast), ema_series(values, slow))]
    return [a - b for a, b in zip(line, ema_series(line, signal))]


def adx(cs: Sequence[Candle], n: int = 14) -> Optional[Tuple[float, float, float]]:
    """Wilder ADX with the directional indicators: (adx, +DI, -DI)."""
    if len(cs) < 2 * n + 1:
        return None
    trs, pdm, mdm = [], [], []
    for i in range(1, len(cs)):
        up, down = cs[i].h - cs[i - 1].h, cs[i - 1].l - cs[i].l
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        pc = cs[i - 1].c
        trs.append(max(cs[i].h - cs[i].l, abs(cs[i].h - pc), abs(cs[i].l - pc)))
    tr_s, p_s, m_s = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs: List[float] = []
    pdi = mdi = 0.0
    for i in range(n, len(trs) + 1):
        if i > n:
            tr_s = tr_s - tr_s / n + trs[i - 1]
            p_s = p_s - p_s / n + pdm[i - 1]
            m_s = m_s - m_s / n + mdm[i - 1]
        pdi = 100.0 * p_s / tr_s if tr_s > 0 else 0.0
        mdi = 100.0 * m_s / tr_s if tr_s > 0 else 0.0
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0)
    if len(dxs) < n:
        return None
    value = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        value = (value * (n - 1) + dx) / n
    return value, pdi, mdi


def bb_width(values: Sequence[float], n: int = 20, k: float = 2.0) -> Optional[float]:
    """Bollinger band width relative to the mean (dimensionless)."""
    if len(values) < n:
        return None
    seg = values[-n:]
    m = sum(seg) / n
    return (2 * k * stdev(seg) / m) if m > 0 else None


def percentile_rank(x: float, history: Sequence[float]) -> float:
    """Share of `history` that is <= x (0..1); 0.5 when there is no history."""
    if not history:
        return 0.5
    return sum(1 for v in history if v <= x) / len(history)
