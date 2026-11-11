"""Leakage-resistant expanding-window rolling walk-forward validation utilities."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

@dataclass(frozen=True)
class Fold:
    train_end: int
    test_start: int
    test_end: int


def make_folds(n: int, min_train: int = 60, n_splits: int = 4, test_size: int | None = None) -> List[Fold]:
    """Create chronological expanding-window folds; every test row is strictly later than training."""
    if n < 0 or min_train < 2 or n_splits < 1:
        raise ValueError("invalid walk-forward dimensions")
    if test_size is None:
        test_size = max(1, (n - min_train) // n_splits) if n > min_train else 0
    if test_size < 1 or min_train + test_size > n:
        return []
    folds: List[Fold] = []
    start = min_train
    while start < n and len(folds) < n_splits:
        end = min(n, start + test_size)
        folds.append(Fold(train_end=start, test_start=start, test_end=end))
        start = end
    return folds


def evaluate_rolling(samples: Sequence[Dict[str, Any]], min_train: int, n_splits: int, evaluator) -> Dict[str, Any]:
    """Run caller-supplied fit/evaluate callback on disjoint future windows."""
    rows = sorted(samples, key=lambda item: int(item["ts"]))
    folds = make_folds(len(rows), min_train=min_train, n_splits=n_splits)
    results = []
    for index, fold in enumerate(folds, 1):
        train, test = rows[:fold.train_end], rows[fold.test_start:fold.test_end]
        if not train or not test or max(int(x["ts"]) for x in train) >= min(int(x["ts"]) for x in test):
            raise ValueError("walk-forward leakage detected")
        metrics = evaluator(train, test)
        results.append({"fold": index, "train": len(train), "test": len(test),
                        "train_end_ts": int(train[-1]["ts"]), "test_start_ts": int(test[0]["ts"]),
                        "metrics": metrics})
    return {"method": "expanding_window_rolling_walk_forward", "folds": results,
            "fold_count": len(results), "evaluated_samples": sum(x["test"] for x in results),
            "status": "validated" if results else "insufficient_data"}
