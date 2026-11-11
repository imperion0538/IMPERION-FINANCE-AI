"""Adverse-selection / toxicity guard using available aggTrade and book data."""
from __future__ import annotations
from typing import Optional


def book_imbalance(book: Optional[dict], depth: int = 10) -> Optional[float]:
    if not book:
        return None
    bids = book.get("bids") or []; asks = book.get("asks") or []
    b = sum(float(x[0]) * float(x[1]) for x in bids[:depth])
    a = sum(float(x[0]) * float(x[1]) for x in asks[:depth])
    total = b + a
    return (b-a)/total if total > 0 else None


def toxicity_score(side: str, flow: Optional[dict] = None, book: Optional[dict] = None,
                   spread_pct: Optional[float] = None) -> dict:
    """Score 0..1; missing inputs are omitted, never interpreted as confirmation."""
    side = side.upper()
    score = 0.15
    facts = {}
    if flow and flow.get("available"):
        delta = float(flow.get("delta_ratio") or 0.0)  # + = aggressive buy, - = aggressive sell
        adverse = -delta if side == "LONG" else delta
        facts["delta_ratio"] = round(delta, 4)
        if adverse > 0.20: score += 0.18
        if adverse > 0.40: score += 0.15
        stacks = flow.get("stacked_imbalances") or {}
        opposite = stacks.get("sell_stacks" if side == "LONG" else "buy_stacks", 0)
        if opposite: score += min(0.18, 0.06 * int(opposite))
    imb = book_imbalance(book)
    if imb is not None:
        facts["book_imbalance"] = round(imb, 4)
        adverse_book = -imb if side == "LONG" else imb
        if adverse_book > 0.25: score += 0.12
        if adverse_book > 0.50: score += 0.12
    if spread_pct is not None:
        facts["spread_pct"] = round(float(spread_pct), 5)
        if spread_pct > 0.08: score += 0.20
        elif spread_pct > 0.05: score += 0.10
    score = round(max(0.0, min(1.0, score)), 4)
    return {"score": score, "facts": facts, "available": bool(facts)}


def toxicity_veto(score: float, threshold: float = 0.78) -> Optional[str]:
    if score >= 0.90:
        return f"TOXICITY_EXTREME ({score:.2f})"
    if score >= threshold:
        return f"TOXICITY_HIGH ({score:.2f} >= {threshold:.2f})"
    return None
