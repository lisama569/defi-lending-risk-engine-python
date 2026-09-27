"""风控层：健康度边界、陈旧价格 UNKNOWN、清算数量与激励罚金计算。"""

from decimal import Decimal

import pytest

from app.errors import EngineError
from app.models import HealthRating, Position, PriceView
from app.risk import close_amounts, evaluate_health


def pos(threshold: str = "1.0", max_close: str = "0.5") -> Position:
    return Position(
        position_id="p1",
        owner="alice",
        collateral_symbol="ETH",
        collateral_amount=Decimal("1"),     # 1 ETH
        debt_symbol="USDC",
        debt_amount=Decimal("2000"),       # 2000 USDC
        liquidation_threshold=Decimal(threshold),
        max_close_factor=Decimal(max_close),
    )


def view(symbol: str, price: str, stale: bool = False) -> PriceView:
    return PriceView(symbol=symbol, price=Decimal(price), timestamp=999.0, stale=stale)


def test_healthy_position_not_liquidatable() -> None:
    # 1 ETH * 2500 / 2000 = 1.25 >= 1.0
    report = evaluate_health(pos(), view("ETH", "2500"), view("USDC", "1"), 1000.0)
    assert report.rating is HealthRating.HEALTHY
    assert report.healthy is True
    assert report.health_factor == Decimal("1.25")


def test_boundary_equal_threshold_is_healthy() -> None:
    # 1 * 2000 / 2000 = 1.0，恰好等于阈值 -> 仍然健康，不允许清算
    report = evaluate_health(pos(), view("ETH", "2000"), view("USDC", "1"), 1000.0)
    assert report.rating is HealthRating.HEALTHY


def test_underwater_position_liquidatable() -> None:
    # 1 * 1500 / 2000 = 0.75 < 1.0
    report = evaluate_health(pos(), view("ETH", "1500"), view("USDC", "1"), 1000.0)
    assert report.rating is HealthRating.LIQUIDATABLE
    assert report.healthy is False


def test_missing_price_yields_unknown_not_unhealthy() -> None:
    report = evaluate_health(pos(), None, view("USDC", "1"), 1000.0)
    assert report.rating is HealthRating.UNKNOWN
    assert report.healthy is None


def test_stale_price_yields_unknown() -> None:
    report = evaluate_health(
        pos(), view("ETH", "1500", stale=True), view("USDC", "1"), 1000.0
    )
    assert report.rating is HealthRating.UNKNOWN


def test_close_amounts_math() -> None:
    p = pos()
    seized, bonus, penalty = close_amounts(
        p,
        repay_debt=Decimal("1000"),        # 偿还一半债务
        collateral_price=Decimal("1500"),
        debt_price=Decimal("1"),
        liquidator_bonus_rate=Decimal("0.05"),
        pool_penalty_rate=Decimal("0.03"),
    )
    # base = 1000 / 1500
    base = Decimal("1000") / Decimal("1500")
    assert seized == base * Decimal("1.05")
    assert bonus == base * Decimal("0.05")
    assert penalty == base * Decimal("0.03")


def test_close_amounts_rejects_non_positive() -> None:
    with pytest.raises(EngineError):
        close_amounts(
            pos(), Decimal("0"), Decimal("1500"), Decimal("1"),
            Decimal("0.05"), Decimal("0.03"),
        )
