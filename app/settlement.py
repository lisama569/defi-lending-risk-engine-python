"""三方资金结算：清算方（liquidator）、借款方（owner）、资金池（pool）。

结算步骤（全部在 store 的同一把锁内完成）：
1. 清算人 -> 资金池：偿还 repay_debt_amount 个债务币种；
2. 借款方 -> 清算人：划转 collateral_seized 个抵押品（含激励）；
3. 借款方 -> 资金池：划转 pool_penalty 个抵押品作为罚金。

守恒关系（按币种分别成立）：
- 债务币种：清算人减少 == 资金池增加 == repay_debt_amount；
- 抵押品：借款方减少 == 清算人增加 + 资金池增加
         == collateral_seized + pool_penalty。

为避免失败留下中间状态，先在锁内取快照并完成所有余额充足性校验，
确认全部可执行后才落账；任一步失败抛错且不改动任何余额。
"""

from __future__ import annotations

from decimal import Decimal

from app.models import BalanceSnapshot, Position
from app.storage import LocalStore


class SettlementService:
    def __init__(self, store: LocalStore) -> None:
        self._store = store

    def settle_partial_liquidation(
        self,
        position: Position,
        liquidator: str,
        pool: str,
        repay_debt_amount: Decimal,
        collateral_seized: Decimal,
        liquidator_bonus: Decimal,
        pool_penalty: Decimal,
    ) -> tuple[
        tuple[BalanceSnapshot, BalanceSnapshot, BalanceSnapshot],
        tuple[BalanceSnapshot, BalanceSnapshot, BalanceSnapshot],
    ]:
        borrower = position.owner
        debt_symbol = position.debt_symbol
        coll_symbol = position.collateral_symbol

        with self._store.lock:
            # ---- 结算前三方快照（顺序：清算人、借款方、资金池）----
            before = (
                self._store.snapshot(liquidator),
                self._store.snapshot(borrower),
                self._store.snapshot(pool),
            )

            # ---- 前置校验：任一不满足都不落账，杜绝半完成状态 ----
            if repay_debt_amount <= 0:
                raise ValueError("偿还债务必须为正数")
            if collateral_seized < 0 or pool_penalty < 0:
                raise ValueError("抵押品划转数量不能为负")

            liq_debt = self._store.get_balance(liquidator, debt_symbol)
            if liq_debt < repay_debt_amount:
                raise ValueError(
                    f"清算人 {liquidator} 的 {debt_symbol} 余额不足: "
                    f"需要 {repay_debt_amount}, 可用 {liq_debt}"
                )

            borrower_coll = self._store.get_balance(borrower, coll_symbol)
            total_coll_out = collateral_seized + pool_penalty
            if borrower_coll < total_coll_out:
                raise ValueError(
                    f"借款方 {borrower} 的 {coll_symbol} 余额不足: "
                    f"需要 {total_coll_out}, 可用 {borrower_coll}"
                )

            # ---- 校验全部通过，执行三次原子划转 ----
            self._store.transfer(
                liquidator, pool, debt_symbol, repay_debt_amount
            )
            self._store.transfer(
                borrower, liquidator, coll_symbol, collateral_seized
            )
            if pool_penalty > 0:
                self._store.transfer(
                    borrower, pool, coll_symbol, pool_penalty
                )

            after = (
                self._store.snapshot(liquidator),
                self._store.snapshot(borrower),
                self._store.snapshot(pool),
            )
            return before, after
