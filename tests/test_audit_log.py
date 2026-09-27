"""审计可解释性测试：显式打印输入、判定依据、结算前后余额。

运行：.venv/bin/python -m pytest tests/test_audit_log.py -s
"""

from decimal import Decimal

from app.clock import MutableClock
from app.engine import RiskEngine
from app.models import Position, PriceTick
from app.storage import LocalStore


def _dump(title: str, result) -> None:
    print(f"\n===== {title} =====")
    print(f"outcome={result.outcome.value} detail={result.detail}")
    if result.before is not None:
        names = ("清算人", "借款方", "资金池")
        for name, b, a in zip(names, result.before, result.after):
            print(
                f"  {name}: 结算前 {dict(b.balances)} "
                f"-> 结算后 {dict(a.balances)}"
            )
        print(
            f"  偿还债务={result.repay_debt_amount} 划转抵押={result.collateral_seized} "
            f"激励={result.liquidator_bonus} 罚金={result.pool_penalty}"
        )


def test_audit_trail_of_healthy_and_liquidatable(capsys) -> None:
    store = LocalStore()
    engine = RiskEngine(store=store, clock=MutableClock(1000.0))
    engine.deposit("LIQ1", "USDC", Decimal("100000"))
    engine.deposit("alice", "ETH", Decimal("10"))
    engine.deposit("POOL", "USDC", Decimal("1000000"))
    engine.deposit("POOL", "ETH", Decimal("1000"))
    engine.open_position(
        Position(
            position_id="p1", owner="alice",
            collateral_symbol="ETH", collateral_amount=Decimal("1"),
            debt_symbol="USDC", debt_amount=Decimal("2000"),
            max_close_factor=Decimal("0.5"),
        )
    )

    # 输入 1：高价 -> 健康 -> 清算被拒
    engine.push_price(PriceTick("USDC", Decimal("1"), 999.0))
    engine.push_price(PriceTick("ETH", Decimal("2500"), 999.0))
    report = engine.health("p1")
    print(f"\n[输入] ETH=2500 -> [判定] {report.detail}")
    rejected = engine.liquidate("L0", "p1", "LIQ1", Decimal("1000"))
    _dump("健康仓位清算请求（应拒绝）", rejected)
    assert rejected.outcome.value == "SKIPPED_HEALTHY"

    # 输入 2：温和下跌至 1900（在 25% 偏离内）-> hf=0.95 -> 可清算
    engine.push_price(PriceTick("ETH", Decimal("1900"), 1000.0))
    report = engine.health("p1")
    print(f"[输入] ETH=1900 -> [判定] {report.detail}")
    executed = engine.liquidate("L1", "p1", "LIQ1", Decimal("1000"))
    _dump("部分清算（应执行）", executed)
    assert executed.outcome.value == "EXECUTED"

    # 守恒校验（按币种三方总和不变）
    for symbol in ("USDC", "ETH"):
        before_total = sum(
            s.balances.get(symbol, Decimal("0")) for s in executed.before
        )
        after_total = sum(
            s.balances.get(symbol, Decimal("0")) for s in executed.after
        )
        assert before_total == after_total, f"{symbol} 不守恒"
        print(f"[守恒] {symbol}: 三方总和结算前={before_total} 结算后={after_total}")
