"""Win-probability model: regularised logistic regression over the agent reports (pure Python, deterministic).

Features: for every agent, how strongly it AGREED with the trade side (+confidence), disagreed (-confidence) or was
silent (0); plus one indicator per playbook (the setup model itself carries information).
Trained on resolved shadow trades with expanding-window rolling walk-forward validation. The model is only "validated"
(allowed to veto) when it beats the base-rate forecast out of sample on Brier score AND ranks outcomes better than
chance (AUC). An unvalidated model is advisory: its probability is reported, never acted on.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.policy import AGENT_WEIGHTS
from .walk_forward import evaluate_rolling

PLAYBOOKS = ("sweep_reversal", "trend_pullback", "breakout_retest", "range_fade")
AGENT_FEATURES: Tuple[str, ...] = tuple(sorted(AGENT_WEIGHTS))
FEATURES: Tuple[str, ...] = AGENT_FEATURES + tuple(f"pb:{p}" for p in PLAYBOOKS)
FEATURE_SET = "agents-1"
VALID_FRACTION = 0.30
MIN_VALID = 20
EPOCHS, LEARNING_RATE, L2 = 300, 0.5, 0.02
MIN_AUC, BRIER_EDGE = 0.55, 0.98


def features(snapshot: Mapping[str, Mapping[str, Any]], side: str, playbook: Optional[str] = None) -> List[float]:
    sign = 1.0 if side == "LONG" else -1.0
    out = []
    for name in AGENT_FEATURES:
        v = snapshot.get(name)
        if not v or v.get("d") not in ("BULLISH", "BEARISH"):
            out.append(0.0)
        else:
            out.append((1.0 if v["d"] == "BULLISH" else -1.0) * sign * float(v.get("s") or 0.0))
    out += [1.0 if playbook == p else 0.0 for p in PLAYBOOKS]
    return out


def _row_features(row: Mapping[str, Any]) -> List[float]:
    return features(row.get("evidence") or {}, row["side"], (row.get("meta") or {}).get("playbook"))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class WinModel:
    def __init__(self, weights: Optional[Mapping[str, float]] = None, bias: float = 0.0, validated: bool = False,
                 metrics: Optional[Dict[str, Any]] = None):
        self.w = [float((weights or {}).get(name, 0.0)) for name in FEATURES]
        self.bias = float(bias)
        self.validated = bool(validated)
        self.metrics = metrics or {}

    # ---- inference -------------------------------------------------------
    def _p(self, x: Sequence[float]) -> float:
        return _sigmoid(self.bias + sum(w * v for w, v in zip(self.w, x)))

    def predict(self, snapshot: Mapping[str, Mapping[str, Any]], side: str, playbook: Optional[str] = None) -> float:
        return self._p(features(snapshot, side, playbook))

    # ---- training --------------------------------------------------------
    def fit(self, xs: Sequence[Sequence[float]], ys: Sequence[int]) -> None:
        n, k = len(xs), len(FEATURES)
        base = min(max(sum(ys) / n, 0.02), 0.98)
        self.w, self.bias = [0.0] * k, math.log(base / (1 - base))
        for _ in range(EPOCHS):
            gw, gb = [0.0] * k, 0.0
            for x, y in zip(xs, ys):
                err = self._p(x) - y
                gb += err
                for i, v in enumerate(x):
                    if v:
                        gw[i] += err * v
            self.bias -= LEARNING_RATE * gb / n
            self.w = [w - LEARNING_RATE * (g / n + L2 * w) for w, g in zip(self.w, gw)]

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"feature_set": FEATURE_SET, "weights": {name: round(w, 5) for name, w in zip(FEATURES, self.w) if abs(w) > 1e-9},
                "bias": round(self.bias, 5), "validated": self.validated, "metrics": self.metrics}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["WinModel"]:
        """None for missing/malformed data and for models of another feature set (e.g. saved by an older engine)."""
        if not isinstance(data, dict) or not isinstance(data.get("weights"), dict) or data.get("feature_set") != FEATURE_SET:
            return None
        try:
            return cls(data["weights"], float(data.get("bias") or 0.0), bool(data.get("validated")), data.get("metrics") or {})
        except (TypeError, ValueError):
            return None


def brier(ps: Sequence[float], ys: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys) if ys else 0.0


def auc(ps: Sequence[float], ys: Sequence[int]) -> float:
    """Probability that a random winner is ranked above a random loser (0.5 = chance)."""
    pos = [p for p, y in zip(ps, ys) if y]
    neg = [p for p, y in zip(ps, ys) if not y]
    if not pos or not neg:
        return 0.5
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def train(samples: List[Dict[str, Any]], min_samples: int) -> Tuple[Optional[WinModel], Dict[str, Any]]:
    """Validate on multiple strictly-future windows; train final model only after validation."""
    report: Dict[str, Any] = {"samples": len(samples), "min_samples": min_samples,
                              "method": "expanding_window_rolling_walk_forward", "status": "insufficient_data"}
    if len(samples) < min_samples:
        return None, report
    rows = sorted(samples, key=lambda sample: int(sample["ts"]))
    min_train = max(40, int(len(rows) * 0.40))

    def evaluate(train_rows, test_rows):
        train_x = [_row_features(row) for row in train_rows]
        train_y = [1 if float(row["r_result"]) > 0 else 0 for row in train_rows]
        test_x = [_row_features(row) for row in test_rows]
        test_y = [1 if float(row["r_result"]) > 0 else 0 for row in test_rows]
        if len(set(train_y)) < 2 or len(set(test_y)) < 2:
            return {"status": "insufficient_class_variation", "brier": None, "brier_base": None, "auc": None}
        fold_model = WinModel()
        fold_model.fit(train_x, train_y)
        base_rate = sum(train_y) / len(train_y)
        probabilities = [fold_model._p(x) for x in test_x]
        return {"status": "ok", "brier": brier(probabilities, test_y),
                "brier_base": brier([base_rate] * len(test_y), test_y),
                "auc": auc(probabilities, test_y), "test_samples": len(test_y)}

    wf = evaluate_rolling(rows, min_train=min_train, n_splits=4, evaluator=evaluate)
    usable = [fold["metrics"] for fold in wf["folds"] if fold["metrics"].get("status") == "ok"]
    if len(usable) < 2:
        report.update({"status": "insufficient_walk_forward_folds", "walk_forward": wf})
        return None, report
    avg_brier = sum(m["brier"] for m in usable) / len(usable)
    avg_base = sum(m["brier_base"] for m in usable) / len(usable)
    avg_auc = sum(m["auc"] for m in usable) / len(usable)
    metrics = {"folds": len(usable), "brier": round(avg_brier, 4),
               "brier_base": round(avg_base, 4), "auc": round(avg_auc, 3),
               "evaluated_samples": sum(m["test_samples"] for m in usable)}
    validated = avg_brier <= avg_base * BRIER_EDGE and avg_auc >= MIN_AUC
    # The deployable candidate is fit on all historical rows only AFTER all OOS folds finish.
    xs = [_row_features(row) for row in rows]
    ys = [1 if float(row["r_result"]) > 0 else 0 for row in rows]
    if len(set(ys)) < 2:
        report.update({"status": "insufficient_class_variation", "walk_forward": wf})
        return None, report
    model = WinModel()
    model.fit(xs, ys)
    model.validated = validated
    model.metrics = {**metrics, "walk_forward": wf}
    top = sorted(((name, round(weight, 3)) for name, weight in zip(FEATURES, model.w)),
                 key=lambda item: -abs(item[1]))[:6]
    report.update({"status": "validated" if validated else "advisory_only",
                   "metrics": metrics, "walk_forward": wf, "top_features": top})
    return model, report
