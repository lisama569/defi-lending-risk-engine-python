"""引擎端到端：价格-健康度-清算-幂等全链路。"""

from decimal import Decimal

import pytest

from app.clock import Clock
from app.engine import RiskEngine
from app.models import (
    LiquidationOutcome,
    Position,
    PriceTick,
    RejectReason,
)
from app.storage import LocalStore


def add_prices(engine: RiskEngine, eth: str = "1500") -> None:
    # 时钟固定在 1000，价格时间戳 999 足够新鲜
    assert engine.push_price(PriceTick("USDC", Decimal("1"), 999.0)).accepted
    assert engine.push_price(PriceTick("ETH", Decimal(eth), 999.0)).accepted


def make_position() -> Position:
    # 1 ETH 抵押，2000 USDC 债务；ETH=1500 时 hf=0.75，可清算
    return Position(
        position_id="p1",
        owner="alice",
        collateral_symbol="ETH",
        collateral_amount=Decimal("1"),
        debt_symbol="USDC",
        debt_amount=Decimal("2000"),
        max_close_factor=Decimal("0.5"),
    )


def test_healthy_position_cannot_be_liquidated(engine: RiskEngine) -> None:
    add_prices(engine, eth="2500")   # hf = 1.25 健康
    engine.open_position(make_position())
    result = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert result.outcome is LiquidationOutcome.SKIPPED_HEALTHY
    # 没有任何资金移动
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("100000")


def test_stale_price_blocks_liquidation(engine: RiskEngine) -> None:
    add_prices(engine, eth="1500")
    engine.open_position(make_position())
    # 共享时钟前进 100s，价格 999 已陈旧（101s > 60s）
    engine._clock.advance(100.0)
    result = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert result.outcome is LiquidationOutcome.REJECTED
    assert "UNKNOWN" in result.detail
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("100000")


def test_successful_liquidation_is_conservative(engine: RiskEngine) -> None:
    add_prices(engine, eth="1500")
    engine.open_position(make_position())

    pool_usdc_before = engine._store.get_balance("POOL", "USDC")
    result = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    assert result.outcome is LiquidationOutcome.EXECUTED
    assert result.repay_debt_amount == Decimal("1000")

    # 债务减少 1000，仓位抵押品减少 seized + penalty
    pos = engine._store.get_position("p1")
    assert pos.debt_amount == Decimal("1000")
    assert pos.collateral_amount == Decimal("1") - (
        result.collateral_seized + result.pool_penalty
    )

    # 三方守恒：USDC 总量不变；ETH 总量不变
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("99000")
    assert engine._store.get_balance("POOL", "USDC") == pool_usdc_before + Decimal("1000")
    eth_total = sum(
        engine._store.get_balance(p, "ETH")
        for p in ("LIQ1", "alice", "POOL")
    )
    # alice 初始 10 ETH + POOL 初始 1000 ETH
    assert eth_total == Decimal("1010")

    # 激励 + 罚金不重复：清算人拿到 seized，池拿到 penalty
    assert engine._store.get_balance("LIQ1", "ETH") == result.collateral_seized
    assert engine._store.get_balance("POOL", "ETH") == Decimal("1000") + result.pool_penalty


def test_duplicate_liquidation_idempotent(engine: RiskEngine) -> None:
    add_prices(engine, eth="1500")
    engine.open_position(make_position())

    r1 = engine.liquidate("L-DUP", "p1", "LIQ1", Decimal("1000"))
    assert r1.outcome is LiquidationOutcome.EXECUTED
    liq_usdc_after_first = engine._store.get_balance("LIQ1", "USDC")
    debt_after_first = engine._store.get_position("p1").debt_amount

    # 同一清算单重复提交
    r2 = engine.liquidate("L-DUP", "p1", "LIQ1", Decimal("1000"))
    assert r2.outcome is LiquidationOutcome.DUPLICATE
    # 不重复扣款、不重复减债
    assert engine._store.get_balance("LIQ1", "USDC") == liq_usdc_after_first
    assert engine._store.get_position("p1").debt_amount == debt_after_first


def test_over_max_close_rejected(engine: RiskEngine) -> None:
    add_prices(engine, eth="1500")
    engine.open_position(make_position())
    # max_close_factor=0.5 -> 最多偿还 1000
    result = engine.liquidate("L-BIG", "p1", "LIQ1", Decimal("1500"))
    assert result.outcome is LiquidationOutcome.REJECTED
    assert "部分清算上限" in result.detail
    assert engine._store.get_position("p1").debt_amount == Decimal("2000")
