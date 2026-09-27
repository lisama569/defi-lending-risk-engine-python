"""并发测试：同一清算单并发提交只执行一次，三方账目守恒。"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.clock import MutableClock
from app.engine import RiskEngine
from app.models import (
    LiquidationOutcome,
    Position,
    PriceTick,
)
from app.storage import LocalStore


def build_engine() -> RiskEngine:
    store = LocalStore()
    engine = RiskEngine(store=store, clock=MutableClock(1000.0))
    engine.deposit("LIQ1", "USDC", Decimal("100000"))
    engine.deposit("alice", "ETH", Decimal("10"))
    engine.deposit(engine.pool_account, "USDC", Decimal("1000000"))
    engine.deposit(engine.pool_account, "ETH", Decimal("1000"))
    engine.push_price(PriceTick("USDC", Decimal("1"), 999.0))
    engine.push_price(PriceTick("ETH", Decimal("1500"), 999.0))
    engine.open_position(
        Position(
            position_id="p1", owner="alice",
            collateral_symbol="ETH", collateral_amount=Decimal("1"),
            debt_symbol="USDC", debt_amount=Decimal("2000"),
            max_close_factor=Decimal("0.5"),
        )
    )
    return engine


def test_concurrent_same_liquidation_executes_once() -> None:
    engine = build_engine()
    n = 32

    def call(_):
        return engine.liquidate("L-CONC", "p1", "LIQ1", Decimal("1000"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(n)))

    executed = [r for r in results if r.outcome is LiquidationOutcome.EXECUTED]
    duplicates = [r for r in results if r.outcome is LiquidationOutcome.DUPLICATE]
    assert len(executed) == 1
    assert len(duplicates) == n - 1

    # 只扣一次款：清算人 USDC 仅减少 1000
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("99000")
    assert engine._store.get_balance("POOL", "USDC") == Decimal("1001000")
    # 债务只减少一次
    assert engine._store.get_position("p1").debt_amount == Decimal("1000")

    # 三方 ETH 总量守恒
    eth_total = sum(
        engine._store.get_balance(p, "ETH") for p in ("LIQ1", "alice", "POOL")
    )
    assert eth_total == Decimal("1010")


def test_concurrent_distinct_liquidations_on_same_position() -> None:
    """不同清算单并发，但受部分清算上限约束：只有第一笔能成功。"""
    engine = build_engine()

    def call(i: int):
        return engine.liquidate(f"L-{i}", "p1", "LIQ1", Decimal("1000"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))

    executed = [r for r in results if r.outcome is LiquidationOutcome.EXECUTED]
    assert len(executed) == 1  # 第一笔后仓位恢复健康，其余被 SKIPPED_HEALTHY
    assert engine._store.get_position("p1").debt_amount == Decimal("1000")
