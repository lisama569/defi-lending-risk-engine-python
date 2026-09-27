"""HTTP API 集成测试。"""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import create_app
from app.clock import MutableClock
from app.engine import RiskEngine
from app.storage import LocalStore


def client_with_engine() -> tuple[TestClient, RiskEngine]:
    engine = RiskEngine(store=LocalStore(), clock=MutableClock(1000.0))
    return TestClient(create_app(engine)), engine


def deposit(client: TestClient, party: str, symbol: str, amount: str) -> None:
    r = client.post("/accounts/deposit", json={"party": party, "symbol": symbol, "amount": amount})
    assert r.status_code == 200


def test_full_flow_over_http() -> None:
    client, engine = client_with_engine()

    deposit(client, "LIQ1", "USDC", "100000")
    deposit(client, "alice", "ETH", "10")
    deposit(client, "POOL", "USDC", "1000000")
    deposit(client, "POOL", "ETH", "1000")

    # 行情：可清算价
    r = client.post("/prices", json={"symbol": "USDC", "price": "1", "timestamp": 999.0})
    assert r.json()["accepted"] is True
    r = client.post("/prices", json={"symbol": "ETH", "price": "1500", "timestamp": 999.0})
    assert r.json()["accepted"] is True

    # 乱序价格被拒，原因可区分
    r = client.post("/prices", json={"symbol": "ETH", "price": "1400", "timestamp": 990.0})
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "OUT_OF_ORDER"

    # 偏离过大被拒
    r = client.post("/prices", json={"symbol": "ETH", "price": "3000", "timestamp": 1000.0})
    assert r.json()["reason"] == "DEVIATION_GUARD"

    # 开仓
    r = client.post("/positions", json={
        "position_id": "p1", "owner": "alice",
        "collateral_symbol": "ETH", "collateral_amount": "1",
        "debt_symbol": "USDC", "debt_amount": "2000",
        "max_close_factor": "0.5",
    })
    assert r.status_code == 200

    # 健康度：hf=0.75，LIQUIDATABLE
    r = client.get("/positions/p1/health")
    h = r.json()
    assert h["rating"] == "LIQUIDATABLE"
    assert Decimal(h["health_factor"]) == Decimal("0.75")

    # 清算
    r = client.post("/liquidations", json={
        "liquidation_id": "L1", "position_id": "p1",
        "liquidator": "LIQ1", "repay_debt_amount": "1000",
    })
    res = r.json()
    assert res["outcome"] == "EXECUTED"
    # 返回结算前后余额
    assert res["before"]["liquidator"]["balances"]["USDC"] == "100000"
    assert res["after"]["liquidator"]["balances"]["USDC"] == "99000"

    # 重复清算：幂等
    r = client.post("/liquidations", json={
        "liquidation_id": "L1", "position_id": "p1",
        "liquidator": "LIQ1", "repay_debt_amount": "1000",
    })
    assert r.json()["outcome"] == "DUPLICATE"
    # 余额不变
    r2 = client.post("/accounts/deposit", json={"party": "x", "symbol": "x", "amount": "0"})
    # 直接确认清算人 USDC 没有二次扣减
    assert engine._store.get_balance("LIQ1", "USDC") == Decimal("99000")


def test_healthy_position_rejected_over_http() -> None:
    client, _ = client_with_engine()
    deposit(client, "LIQ1", "USDC", "100000")
    deposit(client, "alice", "ETH", "10")
    client.post("/prices", json={"symbol": "USDC", "price": "1", "timestamp": 999.0})
    client.post("/prices", json={"symbol": "ETH", "price": "2500", "timestamp": 999.0})
    client.post("/positions", json={
        "position_id": "p1", "owner": "alice",
        "collateral_symbol": "ETH", "collateral_amount": "1",
        "debt_symbol": "USDC", "debt_amount": "2000",
    })
    r = client.post("/liquidations", json={
        "liquidation_id": "L1", "position_id": "p1",
        "liquidator": "LIQ1", "repay_debt_amount": "1000",
    })
    assert r.json()["outcome"] == "SKIPPED_HEALTHY"
