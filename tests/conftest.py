"""pytest 公共夹具：固定时钟下的引擎与已充值账户。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.clock import MutableClock
from app.engine import RiskEngine
from app.storage import LocalStore


@pytest.fixture
def engine() -> RiskEngine:
    store = LocalStore()
    clock = MutableClock(1000.0)
    engine = RiskEngine(store=store, clock=clock)
    # 预置账户：清算人资金充足、资金池与借款方各持有相应币种
    engine.deposit("LIQ1", "USDC", Decimal("100000"))
    engine.deposit("LIQ1", "ETH", Decimal("0"))
    engine.deposit("alice", "ETH", Decimal("10"))
    engine.deposit(engine.pool_account, "USDC", Decimal("1000000"))
    engine.deposit(engine.pool_account, "ETH", Decimal("1000"))
    return engine
