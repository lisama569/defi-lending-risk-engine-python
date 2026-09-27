"""本地 DeFi 借贷风控与清算服务（FastAPI）。

端点：
  POST /prices                       推送/更新行情
  POST /positions                    开仓
  GET  /positions/{user}/health      查询健康度
  POST /liquidations                 提交（部分）清算，request_id 保证幂等
  GET  /accounts/{account}           查询账本余额
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import service
from app.models import PriceTick, Position

app = FastAPI(title="DeFi Lending Risk Engine", version="0.1.0")


class PriceIn(BaseModel):
    symbol: str = Field(..., examples=["ETH"])
    price: float = Field(..., gt=0)
    timestamp: float


class PositionIn(BaseModel):
    user: str
    collateral_symbol: str
    collateral_amount: float = Field(..., gt=0)
    debt_amount: float = Field(..., ge=0)


class LiquidationIn(BaseModel):
    request_id: str
    user: str
    repay_debt: float = Field(..., gt=0)
    liquidator: str = "liquidator"


@app.get("/")
def read_root():
    return {"service": "defi-lending-risk-engine", "status": "ok"}


@app.post("/prices")
def push_price(body: PriceIn):
    result = service.price_feed.push(
        PriceTick(symbol=body.symbol, price=body.price, timestamp=body.timestamp)
    )
    payload = {
        "status": result.status.value,
        "reason": result.reason.value if result.reason else None,
        "symbol": body.symbol,
        "price": body.price,
        "timestamp": body.timestamp,
    }
    if result.status.value == "ACCEPTED":
        return payload
    return JSONResponse(status_code=422, content=payload)


@app.post("/positions")
def open_position(body: PositionIn):
    position = Position(
        user=body.user,
        collateral_symbol=body.collateral_symbol,
        collateral_amount=body.collateral_amount,
        debt_amount=body.debt_amount,
    )
    service.engine.open_position(position)
    return {"status": "OPENED", **body.model_dump()}


@app.get("/positions/{user}/health")
def get_health(user: str):
    position = service.position_store.get(user)
    if position is None:
        return JSONResponse(status_code=404, content={"status": "NOT_FOUND", "user": user})
    report = service.engine.evaluate_health(position)
    return {
        "user": report.user,
        "health_factor": report.health_factor,
        "collateral_value": report.collateral_value,
        "debt_amount": report.debt_amount,
        "price_used": report.price_used,
        "price_timestamp": report.price_timestamp,
        "liquidatable": report.liquidatable,
    }


@app.post("/liquidations")
def submit_liquidation(body: LiquidationIn):
    result = service.engine.liquidate(
        request_id=body.request_id,
        user=body.user,
        repay_debt=body.repay_debt,
        liquidator=body.liquidator,
    )
    payload = {
            "request_id": result.request_id,
            "status": result.status.value,
            "reason": result.reason.value if result.reason else None,
            "health_before": (
                None
                if result.health_before is None
                else {
                    "health_factor": result.health_before.health_factor,
                    "collateral_value": result.health_before.collateral_value,
                    "debt_amount": result.health_before.debt_amount,
                    "price_used": result.health_before.price_used,
                }
            ),
            "settlement": (
                None
                if result.settlement is None
                else {
                    "repaid_debt": result.settlement.repaid_debt,
                    "collateral_seized": result.settlement.collateral_seized,
                    "liquidator_reward": result.settlement.liquidator_reward,
                    "protocol_penalty": result.settlement.protocol_penalty,
                }
            ),
        }
    if result.status.value in ("EXECUTED", "DUPLICATE"):
        return payload
    return JSONResponse(status_code=422, content=payload)


class CreditIn(BaseModel):
    account: str
    asset: str
    amount: float = Field(..., gt=0)


@app.post("/accounts/credit")
def credit_account(body: CreditIn):
    service.ledger.credit(body.account, body.asset, body.amount)
    return {"status": "CREDITED", **body.model_dump()}


@app.get("/accounts/{account}")
def get_account(account: str):
    return {"account": account, "balances": service.ledger.snapshot().get(account, {})}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
