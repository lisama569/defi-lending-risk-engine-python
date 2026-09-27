"""需求场景组合测试：乱序行情、过期价格、健康仓位误清算防护。

每个用例显式断言"可区分的失败原因"，确保不同拒绝原因不会被混淆。
"""

from decimal import Decimal

from app.clock import MutableClock
from app.config import EngineConfig
from app.engine import RiskEngine
from app.models import (
    HealthRating,
    LiquidationOutcome,
    Position,
    PriceTick,
    RejectReason,
)
from app.storage import LocalStore


def build(clock: MutableClock | None = None) -> RiskEngine:
    clock = clock or MutableClock(1000.0)
    engine = RiskEngine(store=LocalStore(), clock=clock)
    engine.deposit("LIQ1", "USDC", Decimal("100000"))
    engine.deposit("alice", "ETH", Decimal("10"))
    engine.deposit("POOL", "USDC", Decimal("1000000"))
    engine.deposit("POOL", "ETH", Decimal("1000"))
    return engine


def open_underwater(engine: RiskEngine) -> None:
    engine.push_price(PriceTick("USDC", Decimal("1"), 999.0))
    engine.push_price(PriceTick("ETH", Decimal("1500"), 999.0))
    engine.open_position(
        Position(
            position_id="p1", owner="alice",
            collateral_symbol="ETH", collateral_amount=Decimal("1"),
            debt_symbol="USDC", debt_amount=Decimal("2000"),
        )
    )


def test_out_of_order_then_stale_then_recovery() -> None:
    """乱序价格被拒；时间流逝后原有效价格变陈旧不可用；新价格到达后恢复。"""
    clock = MutableClock(1000.0)
    engine = build(clock)

    d1 = engine.push_price(PriceTick("ETH", Decimal("2000"), 999.0))
    assert d1.accepted
    # 乱序：更早的时间戳不得覆盖
    d2 = engine.push_price(PriceTick("ETH", Decimal("1000"), 998.0))
    assert d2.reason is RejectReason.OUT_OF_ORDER
    # 重复：相同时间戳
    d3 = engine.push_price(PriceTick("ETH", Decimal("2001"), 999.0))
    assert d3.reason is RejectReason.DUPLICATE

    # 时间前进 61s，旧价格陈旧
    clock.advance(61.0)
    assert engine.price_view("ETH").stale is True
    # 试图推送一个同样过期的价格（ts 仍距现在 60s+），拒绝原因是到达即陈旧
    d4 = engine.push_price(PriceTick("ETH", Decimal("1900"), 999.0))
    assert d4.reason is RejectReason.STALE_ON_ARRIVAL

    # 新鲜、在偏离阈值内的价格到达，恢复服务
    d5 = engine.push_price(PriceTick("ETH", Decimal("2050"), 1059.5))
    assert d5.accepted
    assert engine.price_view("ETH").stale is False


def test_healthy_position_never_liquidated_even_with_stale_dip() -> None:
    """健康仓位不得被误清算：即使有一笔低价推送，若它非法则不能影响判定。"""
    engine = build()
    # 正常高价，仓位健康
    engine.push_price(PriceTick("USDC", Decimal("1"), 999.0))
    engine.push_price(PriceTick("ETH", Decimal("2500"), 999.0))
    engine.open_position(
        Position(
            position_id="p1", owner="alice",
            collateral_symbol="ETH", collateral_amount=Decimal("1"),
            debt_symbol="USDC", debt_amount=Decimal("2000"),
        )
    )
    # 攻击者推送一个暴跌价，但偏离 25% 被保护拒绝
    attack = engine.push_price(PriceTick("ETH", Decimal("500"), 1000.0))
    assert attack.reason is RejectReason.DEVIATION_GUARD

    report = engine.health("p1")
    assert report.rating is HealthRating.HEALTHY

    result = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert result.outcome is LiquidationOutcome.SKIPPED_HEALTHY
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("100000")


def test_replayed_rejected_liquidation_is_idempotent_too() -> None:
    """被拒绝（健康）的清算单重放，返回 DUPLICATE 而不是再次走判定。"""
    engine = build()
    engine.push_price(PriceTick("USDC", Decimal("1"), 999.0))
    engine.push_price(PriceTick("ETH", Decimal("2500"), 999.0))
    engine.open_position(
        Position(
            position_id="p1", owner="alice",
            collateral_symbol="ETH", collateral_amount=Decimal("1"),
            debt_symbol="USDC", debt_amount=Decimal("2000"),
        )
    )
    r1 = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert r1.outcome is LiquidationOutcome.SKIPPED_HEALTHY
    r2 = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert r2.outcome is LiquidationOutcome.DUPLICATE
    assert "SKIPPED_HEALTHY" in r2.detail


def test_insufficient_liquidator_balance_no_state_change() -> None:
    """清算人余额不足导致结算失败时，不留下半完成状态，且清算单不占位可重试。"""
    engine = build()
    open_underwater(engine)
    # LIQ1 有 100000 USDC，足够 1000；改用无资金的清算人
    # POOR 余额为 0，结算前置校验抛 ValueError，无任何账目变化
    import pytest

    with pytest.raises(ValueError):
        engine.liquidate("L1", "p1", "POOR", Decimal("1000"))
    assert engine._store.get_balance("POOR", "USDC") == Decimal("0")
    # 债务未变
    assert engine._store.get_position("p1").debt_amount == Decimal("2000")
