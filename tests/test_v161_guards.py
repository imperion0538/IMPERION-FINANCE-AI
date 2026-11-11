
"""IMPERION FINANCE AI V17 - Inventory and Safety Tests.

V17 safety principles:
- Invalid portfolio exposure must fail closed.
- Position limits must block additional entries.
- Duplicate-symbol positions must be rejected.
- Inventory must support validated Radar candidates.
- Correlated exposure must never increase trade size.
"""

import pytest

from imperion.analysis.toxicity import (
    book_imbalance,
    toxicity_score,
    toxicity_veto,
)
from imperion.risk.inventory import (
    FIXED_SYMBOLS,
    InventoryManager,
)


def position(
    symbol="BTCUSDT",
    side="LONG",
    qty=0.01,
    entry=60000,
):
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry": entry,
    }


def test_inventory_reduces_same_direction_major_exposure():
    guard = InventoryManager(max_positions=4)

    positions = [
        position()
    ]

    result = guard.assess(
        positions,
        "LONG",
        "ETHUSDT",
    )

    assert result["allowed"]
    assert 0 < result["size_multiplier"] < 1
    assert result["snapshot"]["major_long"] == 600.0


def test_inventory_blocks_when_position_limit_reached():
    guard = InventoryManager(max_positions=1)

    result = guard.assess(
        [
            position(),
            position(
                symbol="SOLUSDT",
                side="SHORT",
                qty=1,
                entry=100,
            ),
        ],
        "SHORT",
        "ETHUSDT",
    )

    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert "INVENTORY_LIMIT" in result["reason"]


def test_inventory_blocks_at_exact_position_limit():
    guard = InventoryManager(max_positions=1)

    result = guard.assess(
        [position()],
        "LONG",
        "XRPUSDT",
    )

    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert "INVENTORY_LIMIT" in result["reason"]


def test_inventory_rejects_duplicate_symbol():
    guard = InventoryManager(max_positions=4)

    result = guard.assess(
        [position()],
        "SHORT",
        "BTCUSDT",
    )

    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert (
        result["reason"]
        == "SYMBOL_ALREADY_HAS_POSITION"
    )


def test_inventory_blocks_invalid_position_direction():
    guard = InventoryManager(max_positions=2)

    result = guard.assess(
        [
            position(
                side="UNKNOWN",
                qty=1,
                entry=10,
            )
        ],
        "LONG",
        "ETHUSDT",
    )

    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert (
        result["reason"]
        == "INVENTORY_INVALID_EXPOSURE_DATA"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
    ],
)
def test_inventory_blocks_invalid_position_quantity(
    invalid,
):
    guard = InventoryManager(max_positions=3)

    result = guard.assess(
        [
            position(qty=invalid)
        ],
        "LONG",
        "ETHUSDT",
    )

    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert (
        result["reason"]
        == "INVENTORY_INVALID_EXPOSURE_DATA"
    )


def test_inventory_handles_empty_portfolio():
    guard = InventoryManager(max_positions=2)

    result = guard.assess(
        [],
        "LONG",
        "BTCUSDT",
    )

    assert result["allowed"]
    assert result["size_multiplier"] == 1.0
    assert result["snapshot"]["positions"] == 0
    assert result["snapshot"]["net_bias"] == 0.0


def test_inventory_supports_fixed_symbols():
    guard = InventoryManager(max_positions=2)

    expected = {
        "BTCUSDT",
        "ETHUSDT",
        "AVAXUSDT",
        "INJUSDT",
        "LINKUSDT",
        "DOTUSDT",
        "FILUSDT",
        "XRPUSDT",
        "SOLUSDT",
        "SUIUSDT",
        "LTCUSDT",
        "DASHUSDT",
        "NEOUSDT",
    }

    assert FIXED_SYMBOLS == expected
    assert len(FIXED_SYMBOLS) == 13

    for symbol in expected:
        result = guard.assess(
            [],
            "LONG",
            symbol,
        )

        assert result["allowed"], symbol


def test_inventory_accepts_radar_candidate_for_risk_assessment():
    """Inventory does not own Radar authorization.

    The execution universe and other risk gates must
    independently authorize the candidate.
    """
    guard = InventoryManager(max_positions=2)

    candidate = "ARBUSDT"

    assert candidate not in FIXED_SYMBOLS

    result = guard.assess(
        [],
        "LONG",
        candidate,
    )

    assert result["allowed"]
    assert result["size_multiplier"] == 1.0


def test_inventory_rejects_malformed_candidate_symbol():
    guard = InventoryManager(max_positions=2)

    for symbol in (
        "",
        "BTC",
        "BTC/USDT",
        "INVALID SYMBOL",
    ):
        result = guard.assess(
            [],
            "LONG",
            symbol,
        )

        assert not result["allowed"]
        assert result["size_multiplier"] == 0


def test_book_imbalance_and_directional_toxicity():
    book = {
        "bids": [
            [100, 20],
            [99, 10],
        ],
        "asks": [
            [101, 1],
            [102, 1],
        ],
    }

    assert book_imbalance(book) > 0

    flow = {
        "available": True,
        "delta_ratio": -0.55,
        "stacked_imbalances": {
            "sell_stacks": 2,
            "buy_stacks": 0,
        },
    }

    result = toxicity_score(
        "LONG",
        flow,
        book,
        0.09,
    )

    assert result["score"] > 0.78
    assert toxicity_veto(result["score"]) is not None


def test_toxicity_does_not_treat_missing_data_as_confirmation():
    result = toxicity_score(
        "LONG",
        None,
        None,
        None,
    )

    assert not result["available"]
    assert result["score"] == 0.15
    assert toxicity_veto(result["score"]) is None


def test_direct_settings_defaults_match_environment_loader_safety_profile():
    from imperion.config import (
        Settings,
        load_settings,
    )

    direct = Settings()
    loaded = load_settings({})

    safety_keys = (
        "risk_per_trade_pct",
        "max_concurrent_positions",
        "max_open_risk_pct",
        "max_daily_new_risk_pct",
        "daily_loss_limit_pct",
        "max_drawdown_pct",
        "leverage",
        "max_leverage",
    )

    for key in safety_keys:
        assert (
            getattr(direct, key)
            == getattr(loaded, key)
        )


# TEST_V161_GUARDS_PY_END
