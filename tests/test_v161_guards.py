from imperion.analysis.toxicity import book_imbalance, toxicity_score, toxicity_veto
from imperion.risk.inventory import InventoryManager


def test_inventory_reduces_same_direction_major_exposure():
    guard = InventoryManager(max_positions=4)
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01, "entry": 60000}]
    result = guard.assess(positions, "LONG", "ETHUSDT")
    assert result["allowed"]
    assert 0 < result["size_multiplier"] < 1
    assert result["snapshot"]["major_long"] == 600.0


def test_inventory_blocks_when_position_limit_reached():
    guard = InventoryManager(max_positions=1)
    result = guard.assess([
        {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01, "entry": 60000},
        {"symbol": "SOLUSDT", "side": "SHORT", "qty": 1, "entry": 100},
    ], "SHORT", "ETHUSDT")
    assert not result["allowed"]
    assert result["size_multiplier"] == 0
    assert "INVENTORY_LIMIT" in result["reason"]


def test_inventory_ignores_invalid_sides_and_handles_empty_portfolio():
    guard = InventoryManager(max_positions=2)
    result = guard.assess([{"symbol": "BTCUSDT", "side": "UNKNOWN", "qty": 1, "entry": 10}], "LONG", "ETHUSDT")
    assert result["snapshot"]["positions"] == 1
    assert result["size_multiplier"] == 1.0


def test_book_imbalance_and_directional_toxicity():
    book = {"bids": [[100, 20], [99, 10]], "asks": [[101, 1], [102, 1]]}
    assert book_imbalance(book) > 0
    flow = {"available": True, "delta_ratio": -0.55,
            "stacked_imbalances": {"sell_stacks": 2, "buy_stacks": 0}}
    result = toxicity_score("LONG", flow, book, 0.09)
    assert result["score"] > 0.78
    assert toxicity_veto(result["score"]) is not None


def test_toxicity_does_not_treat_missing_data_as_confirmation():
    result = toxicity_score("LONG", None, None, None)
    assert not result["available"]
    assert result["score"] == 0.15
    assert toxicity_veto(result["score"]) is None


def test_direct_settings_defaults_match_environment_loader_safety_profile():
    from imperion.config import Settings, load_settings
    direct, loaded = Settings(), load_settings({})
    for key in ("risk_per_trade_pct", "max_concurrent_positions", "max_open_risk_pct",
                "max_daily_new_risk_pct", "daily_loss_limit_pct", "max_drawdown_pct", "leverage", "max_leverage"):
        assert getattr(direct, key) == getattr(loaded, key)
