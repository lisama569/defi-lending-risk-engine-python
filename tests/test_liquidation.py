"""清算测试：健康仓防误清算、幂等、并发重复清算、三方账目守恒。"""

from __future__ import annotations

import logging
import threading

import pytest

from app.errors import LiquidationRejectionReason
from app.liquidation import CASH_ASSET, LiquidationEngine, _collateral_asset
from app.models import LiquidationStatus, Position, PriceTick
from app.positions import Ledger, PositionStore
from app.price_feed import PriceFeed

MAX_AGE = 60.0
MAX_DEV = 0.25
THRESHOLD = 1.0
BONUS = 0.05
FEE = 0.02
T0 = 1_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture()
def world():
    clock = Clock()
    feed = PriceFeed(MAX_AGE, MAX_DEV, clock=clock)
    store = PositionStore()
    ledger = Ledger()
    engine = LiquidationEngine(
        store, feed, ledger,
        liquidation_threshold=THRESHOLD,
        liquidation_bonus=BONUS,
        protocol_fee_ratio=FEE,
        clock=clock,
    )
    return engine, feed, store, ledger, clock


def seed(world, user="alice", collateral=10.0, debt=8000.0, liquidator_cash=10_000.0):
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=1000.0, timestamp=T0))
    engine.open_position(Position(user=user, collateral_symbol="ETH",
                                   collateral_amount=collateral, debt_amount=debt))
    ledger.credit("liq", CASH_ASSET, liquidator_cash)
    ledger.credit("pool", CASH_ASSET, 0.0)
    return world


def totals(world, user="alice"):
    engine, feed, store, ledger, clock = world
    coll = _collateral_asset("ETH")
    p = store.get(user)
    return {
        "cash": (ledger.balance_of("liq", CASH_ASSET)
                 + ledger.balance_of("pool", CASH_ASSET)),
        "collateral": (ledger.balance_of("liq", coll)
                       + ledger.balance_of("pool", coll)
                       + ledger.balance_of(f"borrower:{user}", coll)),
        "debt": p.debt_amount if p else 0.0,
        "pool_cash": ledger.balance_of("pool", CASH_ASSET),
    }


def test_healthy_position_cannot_be_liquidated(world, caplog):
    """健康仓位（HF>=阈值）必须被明确拒绝，且账目不变。"""
    seed(world)
    engine, feed, store, ledger, clock = world
    with caplog.at_level(logging.INFO):
        r = engine.liquidate("req-1", "alice", 1000.0, liquidator="liq", now=T0)
    assert r.status is LiquidationStatus.REJECTED
    assert r.reason is LiquidationRejectionReason.POSITION_HEALTHY
    # HF = 10*1000/8000 = 1.25，健康；无任何扣款
    assert totals(world) == {"cash": 10_000.0, "collateral": 10.0, "debt": 8000.0, "pool_cash": 0.0}
    assert store.get("alice").collateral_amount == 10.0
    assert "health_factor=1.250000" in caplog.text


def test_unhealthy_position_partial_liquidation_conservation(world, caplog):
    """低于阈值后部分清算：激励+罚金不漏扣，三方守恒。"""
    seed(world)
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=900.0, timestamp=T0 + 1))  # HF=1.125 仍健康
    assert engine.liquidate("req-x", "alice", 1000.0, "liq", now=T0 + 1).reason \
        is LiquidationRejectionReason.POSITION_HEALTHY
    feed.push(PriceTick(symbol="ETH", price=700.0, timestamp=T0 + 2))  # HF=0.875 可清算

    with caplog.at_level(logging.INFO):
        r = engine.liquidate("req-2", "alice", 2000.0, liquidator="liq", now=T0 + 2)

    assert r.status is LiquidationStatus.EXECUTED
    s = r.settlement
    assert s.repaid_debt == 2000.0
    assert s.collateral_seized == pytest.approx(2000 * 1.05 / 700)
    assert s.liquidator_reward + s.protocol_penalty == pytest.approx(s.collateral_seized)
    assert s.protocol_penalty == pytest.approx(2000 * FEE / 700)

    t = totals(world)
    # 现金守恒：清算方 -2000，资金池 +2000
    assert t["cash"] == pytest.approx(10_000.0)
    assert t["pool_cash"] == pytest.approx(2000.0)
    # 抵押物守恒：仓内 + 清算方 + 资金池罚金
    assert t["collateral"] == pytest.approx(10.0)
    # 债务只减少一次
    assert t["debt"] == pytest.approx(6000.0)
    assert "结算前系统" in caplog.text and "结算后系统" in caplog.text


def test_stale_price_blocks_liquidation(world):
    seed(world)
    engine, feed, store, ledger, clock = world
    clock.now = T0 + MAX_AGE + 1
    r = engine.liquidate("req-3", "alice", 1000.0, "liq")
    assert r.reason is LiquidationRejectionReason.STALE_PRICE
    assert store.get("alice").debt_amount == 8000.0


def test_idempotent_duplicate_liquidation(world, caplog):
    """同一 request_id 串行重复提交：第二次回放首次结果，不重复扣款。"""
    seed(world)
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=900.0, timestamp=T0 + 1))
    feed.push(PriceTick(symbol="ETH", price=700.0, timestamp=T0 + 2))
    r1 = engine.liquidate("req-dup", "alice", 2000.0, "liq", now=T0 + 2)
    t1 = totals(world)
    with caplog.at_level(logging.INFO):
        r2 = engine.liquidate("req-dup", "alice", 2000.0, "liq", now=T0 + 2)
    assert r2.status is LiquidationStatus.DUPLICATE
    assert r2.request_id == r1.request_id
    assert r2.settlement == r1.settlement
    assert r2.health_before == r1.health_before
    assert "幂等命中" in caplog.text
    assert totals(world) == t1  # 账目无第二次变化


def test_concurrent_duplicate_liquidation_executes_once(world):
    """同一 request_id 并发提交：只有一次实际扣款/减债。"""
    seed(world)
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=900.0, timestamp=T0 + 1))
    feed.push(PriceTick(symbol="ETH", price=700.0, timestamp=T0 + 2))
    results: list = []

    def worker():
        results.append(engine.liquidate("req-conc", "alice", 2000.0, "liq", now=T0 + 2))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed = [r for r in results if r.status is LiquidationStatus.EXECUTED]
    duplicates = [r for r in results if r.status is LiquidationStatus.DUPLICATE]
    assert len(executed) == 1
    assert len(duplicates) == 15
    assert all(r.settlement == executed[0].settlement for r in results)
    t = totals(world)
    assert t["debt"] == pytest.approx(6000.0)
    assert t["cash"] == pytest.approx(10_000.0)
    assert t["collateral"] == pytest.approx(10.0)


def test_concurrent_distinct_requests_each_settle_once(world):
    """不同 request_id 并发提交：每个请求最多结算一次，部分清算使仓位恢复健康后
    其余请求被明确拒绝；最终现金/抵押物守恒，不留中间状态。结果数与调度顺序无关。"""
    seed(world, collateral=4.0, debt=4000.0, liquidator_cash=100_000.0)
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=900.0, timestamp=T0 + 1))
    feed.push(PriceTick(symbol="ETH", price=700.0, timestamp=T0 + 2))
    results: list = []

    def worker(rid):
        results.append(engine.liquidate(rid, "alice", 1000.0, "liq", now=T0 + 2))

    threads = [threading.Thread(target=worker, args=(f"req-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    # 每个 request_id 只有一条结果，且该结果要么执行一次要么拒绝
    assert len({r.request_id for r in results}) == 8
    executed = [r for r in results if r.status is LiquidationStatus.EXECUTED]
    rejected = [r for r in results if r.status is LiquidationStatus.REJECTED]
    assert len(executed) + len(rejected) == 8
    assert {r.reason for r in rejected} <= {
        LiquidationRejectionReason.POSITION_HEALTHY,
        LiquidationRejectionReason.INSUFFICIENT_COLLATERAL,
    }
    total_repaid = sum(r.settlement.repaid_debt for r in executed)
    t = totals(world)
    # 资金池收到的现金 == 债务减少额 == 清算方支出
    assert t["pool_cash"] == pytest.approx(total_repaid)
    assert 4000.0 - t["debt"] == pytest.approx(total_repaid)
    # 三方现金与抵押物各自守恒
    assert t["cash"] == pytest.approx(100_000.0)
    assert t["collateral"] == pytest.approx(4.0)


def test_failed_settlement_leaves_no_half_state(world):
    """清算方现金不足 -> 预校验失败，账本与仓位都不发生任何变化。"""
    engine, feed, store, ledger, clock = world
    feed.push(PriceTick(symbol="ETH", price=1000.0, timestamp=T0))
    engine.open_position(Position(user="bob", collateral_symbol="ETH",
                                  collateral_amount=10.0, debt_amount=8000.0))
    ledger.credit("poorp", CASH_ASSET, 100.0)  # 余额远小于代还额
    feed.push(PriceTick(symbol="ETH", price=900.0, timestamp=T0 + 1))
    feed.push(PriceTick(symbol="ETH", price=700.0, timestamp=T0 + 2))

    before = totals(world, user="bob")
    r = engine.liquidate("req-fail", "bob", 2000.0, "poorp", now=T0 + 2)
    after = totals(world, user="bob")

    assert r.status is LiquidationStatus.REJECTED
    assert r.reason is LiquidationRejectionReason.NOTHING_TO_LIQUIDATE
    # 现金、抵押物、债务全部保持原状
    assert before == after
    assert store.get("bob").debt_amount == 8000.0
    assert store.get("bob").collateral_amount == 10.0
    assert ledger.balance_of("poorp", CASH_ASSET) == 100.0


def test_position_not_found_is_distinguishable(world):
    engine, *_ = world
    r = engine.liquidate("req-404", "ghost", 1.0)
    assert r.status is LiquidationStatus.REJECTED
    assert r.reason is LiquidationRejectionReason.POSITION_NOT_FOUND
