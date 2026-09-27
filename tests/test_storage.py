"""存储层：原子划转、余额不足不产生中间状态。"""

from decimal import Decimal

import pytest

from app.errors import EngineError
from app.storage import LocalStore


def test_transfer_conservation() -> None:
    store = LocalStore()
    store.credit("alice", "ETH", Decimal("10"))
    store.transfer("alice", "bob", "ETH", Decimal("3"))
    assert store.get_balance("alice", "ETH") == Decimal("7")
    assert store.get_balance("bob", "ETH") == Decimal("3")
    # 系统总量守恒
    total = store.get_balance("alice", "ETH") + store.get_balance("bob", "ETH")
    assert total == Decimal("10")


def test_transfer_insufficient_leaves_no_partial_state() -> None:
    store = LocalStore()
    store.credit("alice", "ETH", Decimal("2"))
    store.credit("bob", "ETH", Decimal("5"))
    with pytest.raises(EngineError):
        store.transfer("alice", "bob", "ETH", Decimal("3"))
    # 双方余额均未被修改
    assert store.get_balance("alice", "ETH") == Decimal("2")
    assert store.get_balance("bob", "ETH") == Decimal("5")


def test_transfer_non_positive_rejected() -> None:
    store = LocalStore()
    store.credit("alice", "ETH", Decimal("2"))
    with pytest.raises(EngineError):
        store.transfer("alice", "bob", "ETH", Decimal("0"))
    assert store.get_balance("bob", "ETH") == Decimal("0")
