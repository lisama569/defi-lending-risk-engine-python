"""本地仓位与账本存储：线程安全，所有写操作“先校验、后提交”，不留中间状态。"""

from __future__ import annotations

import threading
from collections import defaultdict

from app.models import Position


class InsufficientFundsError(Exception):
    """余额/抵押物/债务不足，无法完成结算。"""


class PositionStore:
    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._lock = threading.RLock()

    def upsert(self, position: Position) -> None:
        with self._lock:
            self._positions[position.user] = position

    def get(self, user: str) -> Position | None:
        with self._lock:
            return self._positions.get(user)

    def _project(self, position: Position, repaid_debt: float, collateral_seized: float):
        if repaid_debt > position.debt_amount + 1e-12:
            raise InsufficientFundsError(
                f"代还债务 {repaid_debt} 超过存量债务 {position.debt_amount}"
            )
        if collateral_seized > position.collateral_amount + 1e-12:
            raise InsufficientFundsError(
                f"待罚没抵押物 {collateral_seized} 超过持仓 {position.collateral_amount}"
            )
        return Position(
            user=position.user,
            collateral_symbol=position.collateral_symbol,
            collateral_amount=position.collateral_amount - collateral_seized,
            debt_amount=position.debt_amount - repaid_debt,
        )

    def preview_settlement(
        self, user: str, repaid_debt: float, collateral_seized: float
    ) -> tuple[Position, Position]:
        """只读预校验：返回 (结算前快照, 结算后快照)，不做任何写入。"""
        with self._lock:
            position = self._positions.get(user)
            if position is None:
                raise InsufficientFundsError(f"仓位不存在: {user}")
            before = Position(
                user=position.user,
                collateral_symbol=position.collateral_symbol,
                collateral_amount=position.collateral_amount,
                debt_amount=position.debt_amount,
            )
            after = self._project(position, repaid_debt, collateral_seized)
            return before, after

    def commit_settlement(
        self, user: str, repaid_debt: float, collateral_seized: float
    ) -> tuple[Position, Position]:
        """提交结算（引擎锁保证此前 preview 已通过）。"""
        with self._lock:
            position = self._positions[user]
            before, after = self.preview_settlement(user, repaid_debt, collateral_seized)
            position.debt_amount = after.debt_amount
            position.collateral_amount = after.collateral_amount
            return before, after


LedgerEntry = tuple[str, str, float]  # (账户, 资产, 带符号变动)


class Ledger:
    """简单账本：apply 的全部分录先在影子账本上校验，通过后一次性提交。"""

    def __init__(self) -> None:
        self._balances: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = threading.Lock()

    def credit(self, account: str, asset: str, amount: float) -> None:
        with self._lock:
            self._balances[account][asset] = self._balances[account].get(asset, 0.0) + amount

    def balance_of(self, account: str, asset: str) -> float:
        with self._lock:
            return self._balances[account].get(asset, 0.0)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {a: dict(m) for a, m in self._balances.items()}

    def _project(self, entries: list[LedgerEntry]) -> dict[str, dict[str, float]]:
        projected = {a: dict(m) for a, m in self._balances.items()}
        for account, asset, delta in entries:
            projected.setdefault(account, {})
            projected[account][asset] = projected[account].get(asset, 0.0) + delta
            if projected[account][asset] < -1e-12:
                raise InsufficientFundsError(
                    f"账户 {account} 资产 {asset} 余额不足: {projected[account][asset]}"
                )
        return projected

    def preview(self, entries: list[LedgerEntry]) -> None:
        with self._lock:
            self._project(entries)

    def apply(self, entries: list[LedgerEntry]) -> None:
        with self._lock:
            projected = self._project(entries)
            self._balances = defaultdict(dict, projected)
