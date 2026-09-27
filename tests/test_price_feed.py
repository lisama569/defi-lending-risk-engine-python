"""行情有效性测试：重复/乱序/过期/偏离必须可区分地被拒绝。"""

from __future__ import annotations

import pytest

from app.errors import PriceRejectionReason
from app.models import PriceStatus, PriceTick
from app.price_feed import PriceFeed

MAX_AGE = 60.0
MAX_DEV = 0.10
T0 = 1_000_000.0


@pytest.fixture()
def feed():
    # 固定时钟，让测试结果可复现
    state = {"now": T0}
    return PriceFeed(MAX_AGE, MAX_DEV, clock=lambda: state["now"]), state


def _push(feed, symbol, price, ts):
    return feed.push(PriceTick(symbol=symbol, price=price, timestamp=ts))


def test_first_and_followup_accepted(feed):
    feed, state = feed
    assert _push(feed, "ETH", 1000.0, T0).status is PriceStatus.ACCEPTED
    assert _push(feed, "ETH", 1005.0, T0 + 1).status is PriceStatus.ACCEPTED


def test_duplicate_rejected(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0)
    r = _push(feed, "ETH", 1000.0, T0)
    assert r.status is PriceStatus.REJECTED
    assert r.reason is PriceRejectionReason.DUPLICATE


def test_out_of_order_rejected(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0 + 10)
    r = _push(feed, "ETH", 1002.0, T0 + 5)  # 时间戳更旧
    assert r.reason is PriceRejectionReason.OUT_OF_ORDER
    # 旧价没有覆盖新价
    valid = feed.get_valid_price("ETH")
    assert valid[0].price == 1000.0
    assert valid[0].timestamp == T0 + 10


def test_stale_push_rejected(feed):
    feed, state = feed
    r = _push(feed, "ETH", 1000.0, T0 - MAX_AGE - 1)
    assert r.reason is PriceRejectionReason.STALE
    assert feed.get_valid_price("ETH") is None


def test_stale_price_excluded_from_reads(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0)
    state["now"] += MAX_AGE + 1  # 时钟前进，价格变陈旧
    assert feed.get_valid_price("ETH") is None


def test_deviation_protection_distinguishable(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0)
    r = _push(feed, "ETH", 1200.0, T0 + 1)  # +20% > 10%
    assert r.reason is PriceRejectionReason.DEVIATION_TOO_LARGE
    valid = feed.get_valid_price("ETH")
    assert valid[0].price == 1000.0  # 偏离价未写入


def test_deviation_boundary_inclusive(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0)
    # 恰好 10% 允许，超过 10% 拒绝
    assert _push(feed, "ETH", 1100.0, T0 + 1).status is PriceStatus.ACCEPTED
    assert _push(feed, "ETH", 1211.0, T0 + 2).reason is PriceRejectionReason.DEVIATION_TOO_LARGE


def test_invalid_price_rejected(feed):
    feed, state = feed
    assert _push(feed, "ETH", 0.0, T0).reason is PriceRejectionReason.INVALID_PRICE
    assert _push(feed, "ETH", -1.0, T0).reason is PriceRejectionReason.INVALID_PRICE


def test_symbols_independent(feed):
    feed, state = feed
    _push(feed, "ETH", 1000.0, T0)
    _push(feed, "BTC", 20_000.0, T0)
    assert feed.get_valid_price("ETH")[0].price == 1000.0
    assert feed.get_valid_price("BTC")[0].price == 20_000.0
