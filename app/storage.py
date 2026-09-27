"""本地存储：账户余额、仓位、已处理清算单（线程安全）。

所有读写均由同一把可重入锁（RLock）保护。引擎层的"判定 + 结算"
在同一把锁内完成，从而保证并发请求下串行化、无脏读、无半完成状态。
"""

from __future__ import annotations

import threading
from decimal import Decimal

from app.errors import EngineError, PositionNotFound
from app.models import BalanceSnapshot, LiquidationResult, Position


class LocalStore:
    """以内存字典模拟链下本地账本。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # party -> symbol -> 余额
        self._accounts: dict[str, dict[str, Decimal]] = {}
        self._positions: dict[str, Position] = {}
        # 清算单幂等表：liquidation_id -> 已执行结果
        self._liquidations: dict[str, LiquidationResult] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 账户 ----
    def ensure_account(self, party: str) -> None:
        with self._lock:
            self._accounts.setdefault(party, {})

    def get_balance(self, party: str, symbol: str) -> Decimal:
        with self._lock:
            return self._accounts.get(party, {}).get(symbol, Decimal("0"))

    def credit(self, party: str, symbol: str, amount: Decimal) -> None:
        """充值 / 增加余额（仅用于初始化与入账）。"""
        if amount < 0:
            raise EngineError(f"credit 金额不能为负: {amount}")
        with self._lock:
            self.ensure_account(party)
            cur = self._accounts[party].get(symbol, Decimal("0"))
            self._accounts[party][symbol] = cur + amount

    def snapshot(self, party: str) -> BalanceSnapshot:
        with self._lock:
            balances = dict(self._accounts.get(party, {}))
            return BalanceSnapshot(party=party, balances=balances)

    def transfer(self, frm: str, to: str, symbol: str, amount: Decimal) -> None:
        """原子划转。

        先做全部校验（金额为正、付款方余额充足），再落账；
        任何校验失败都不会改动任何余额，因此不存在半完成中间状态。
        """
        if amount <= 0:
            raise EngineError(f"划转金额必须为正: {amount}")
        with self._lock:
            self.ensure_account(frm)
            self.ensure_account(to)
            current = self._accounts[frm].get(symbol, Decimal("0"))
            if current < amount:
                raise EngineError(
                    f"账户 {frm} 的 {symbol} 余额不足: 需要 {amount}, 可用 {current}"
                )
            # 校验全部通过后执行两次入账（字典更新本身不会失败）
            self._accounts[frm][symbol] = current - amount
            self._accounts[to][symbol] = (
                self._accounts[to].get(symbol, Decimal("0")) + amount
            )

    # ---- 仓位 ----
    def upsert_position(self, position: Position) -> None:
        with self._lock:
            self._positions[position.position_id] = position

    def get_position(self, position_id: str) -> Position:
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None:
                raise PositionNotFound(f"仓位不存在: {position_id}")
            return pos

    def list_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    # ---- 清算幂等 ----
    def get_liquidation(self, liquidation_id: str) -> LiquidationResult | None:
        with self._lock:
            return self._liquidations.get(liquidation_id)

    def save_liquidation(self, result: LiquidationResult) -> None:
        with self._lock:
            self._liquidations[result.liquidation_id] = result
