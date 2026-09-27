"""行情层测试：重复、乱序、过期、偏离保护与陈旧判定。"""

from decimal import Decimal

from app.clock import Clock
from app.config import EngineConfig
from app.models import PriceTick, RejectReason
from app.oracle import PriceOracle
from app.storage import LocalStore


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def make_oracle(clock: FakeClock, config: EngineConfig | None = None) -> PriceOracle:
    return PriceOracle(LocalStore(), Clock(lambda: clock.t), config or EngineConfig())


def tick(price: str, ts: float, symbol: str = "ETH") -> PriceTick:
    return PriceTick(symbol=symbol, price=Decimal(price), timestamp=ts)


def test_first_price_accepted() -> None:
    clock = FakeClock(1000.0)
    oracle = make_oracle(clock)
    d = oracle.push(tick("2000", 999.0))
    assert d.accepted
    assert oracle.latest("ETH").price == Decimal("2000")


def test_duplicate_timestamp_rejected() -> None:
    clock = FakeClock(1000.0)
    oracle = make_oracle(clock)
    assert oracle.push(tick("2000", 999.0)).accepted
    d = oracle.push(tick("2100", 999.0))  # 同一时间戳，即使价格不同
    assert not d.accepted
    assert d.reason is RejectReason.DUPLICATE
    assert oracle.latest("ETH").price == Decimal("2000")  # 未被覆盖


def test_out_of_order_rejected() -> None:
    clock = FakeClock(2000.0)
    oracle = make_oracle(clock)
    assert oracle.push(tick("2000", 1990.0)).accepted
    d = oracle.push(tick("1900", 1980.0))  # 更早的时间戳
    assert not d.accepted
    assert d.reason is RejectReason.OUT_OF_ORDER
    assert oracle.latest("ETH").price == Decimal("2000")


def test_stale_on_arrival_rejected() -> None:
    clock = FakeClock(2000.0)
    oracle = make_oracle(clock)  # 默认 60s 陈旧阈值
    d = oracle.push(tick("2000", 1900.0))  # 100s 前
    assert not d.accepted
    assert d.reason is RejectReason.STALE_ON_ARRIVAL
    assert oracle.latest("ETH") is None


def test_future_timestamp_rejected() -> None:
    clock = FakeClock(2000.0)
    oracle = make_oracle(clock)
    d = oracle.push(tick("2000", 2010.0))
    assert not d.accepted
    assert d.reason is RejectReason.INVALID_PRICE


def test_invalid_price_rejected() -> None:
    clock = FakeClock(2000.0)
    oracle = make_oracle(clock)
    d = oracle.push(tick("0", 1999.0))
    assert not d.accepted
    assert d.reason is RejectReason.INVALID_PRICE


def test_deviation_guard_rejected_with_distinct_reason() -> None:
    clock = FakeClock(2000.0)
    # 25% 阈值
    oracle = make_oracle(clock, EngineConfig(max_price_deviation=Decimal("0.25")))
    assert oracle.push(tick("2000", 1990.0)).accepted
    # 上涨 30% 被拒绝
    d = oracle.push(tick("2600", 1995.0))
    assert not d.accepted
    assert d.reason is RejectReason.DEVIATION_GUARD
    assert oracle.latest("ETH").price == Decimal("2000")
    # 上涨 10% 在阈值内，接受
    d2 = oracle.push(tick("2200", 1996.0))
    assert d2.accepted
    assert oracle.latest("ETH").price == Decimal("2200")


def test_stale_price_marked_at_evaluation() -> None:
    clock = FakeClock(2000.0)
    oracle = make_oracle(clock)
    assert oracle.push(tick("2000", 1990.0)).accepted
    # 当时新鲜
    assert oracle.view_for("ETH").stale is False
    # 时间前进 70s，价格陈旧，不能再参与判定
    clock.t = 2070.0
    view = oracle.view_for("ETH")
    assert view.stale is True
