"""结算层：三方账目守恒、激励罚金正确、失败不产生中间状态。"""

from decimal import Decimal

import pytest

from app.models import Position
from app.settlement import SettlementService
from app.storage import LocalStore


def make_position() -> Position:
    return Position(
        position_id="p1",
        owner="alice",
        collateral_symbol="ETH",
        collateral_amount=Decimal("1"),
        debt_symbol="USDC",
        debt_amount=Decimal("2000"),
    )


def setup_store() -> LocalStore:
    store = LocalStore()
    store.credit("LIQ1", "USDC", Decimal("100000"))
    store.credit("alice", "ETH", Decimal("10"))
    store.credit("POOL", "USDC", Decimal("0"))
    store.credit("POOL", "ETH", Decimal("0"))
    return store


def test_settlement_conservation_and_split() -> None:
    store = setup_store()
    svc = SettlementService(store)

    seized = Decimal("0.7")      # 划给清算人（含激励）
    bonus = Decimal("0.0333")
    penalty = Decimal("0.02")
    before, after = svc.settle_partial_liquidation(
        make_position(), "LIQ1", "POOL",
        repay_debt_amount=Decimal("1000"),
        collateral_seized=seized,
        liquidator_bonus=bonus,
        pool_penalty=penalty,
    )

    liq_b, bor_b, pool_b = before
    liq_a, bor_a, pool_a = after

    # 债务币种守恒：清算人 -1000，资金池 +1000
    assert liq_b.balances["USDC"] - liq_a.balances["USDC"] == Decimal("1000")
    assert pool_a.balances["USDC"] - pool_b.balances["USDC"] == Decimal("1000")
    # 抵押品：借款方减少 == 清算人增加 + 资金池增加
    coll_out = bor_b.balances["ETH"] - bor_a.balances["ETH"]
    assert coll_out == seized + penalty
    assert liq_a.balances.get("ETH", Decimal("0")) - liq_b.balances.get("ETH", Decimal("0")) == seized
    assert pool_a.balances["ETH"] - pool_b.balances["ETH"] == penalty
    # 系统总量按币种守恒
    for sym in ("USDC", "ETH"):
        total_b = liq_b.balances.get(sym, 0) + bor_b.balances.get(sym, 0) + pool_b.balances.get(sym, 0)
        total_a = liq_a.balances.get(sym, 0) + bor_a.balances.get(sym, 0) + pool_a.balances.get(sym, 0)
        assert total_b == total_a


def test_settlement_failure_leaves_no_partial_state() -> None:
    store = setup_store()
    svc = SettlementService(store)
    # 清算人 USDC 只有 100000，足够；但让借款方 ETH 不够 -> 应整体回滚
    # alice 只有 10 ETH，要求划出 20 ETH
    with pytest.raises(ValueError):
        svc.settle_partial_liquidation(
            make_position(), "LIQ1", "POOL",
            repay_debt_amount=Decimal("1000"),
            collateral_seized=Decimal("15"),
            liquidator_bonus=Decimal("1"),
            pool_penalty=Decimal("5"),
        )
    # 没有任何一笔划转发生
    assert store.get_balance("LIQ1", "USDC") == Decimal("100000")
    assert store.get_balance("POOL", "USDC") == Decimal("0")
    assert store.get_balance("alice", "ETH") == Decimal("10")
    assert store.get_balance("LIQ1", "ETH") == Decimal("0")
