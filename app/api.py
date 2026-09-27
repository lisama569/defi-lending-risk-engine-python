"""FastAPI HTTP 层：将请求模型转换为领域调用。

金额以 Decimal（JSON 中为数字/字符串）传输；被拒绝的价格/清算不抛 5xx，
而是返回 200 + accepted=false/outcome，便于调用方区分具体原因。
找不到仓位等客户端错误返回 404/400。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.clock import Clock
from app.engine import RiskEngine
from app.errors import PositionNotFound
from app.models import PriceTick, Position


class PricePushIn(BaseModel):
    symbol: str
    price: Decimal
    timestamp: float
    source: str = "oracle"


class PositionIn(BaseModel):
    position_id: str
    owner: str
    collateral_symbol: str
    collateral_amount: Decimal
    debt_symbol: str
    debt_amount: Decimal
    liquidation_threshold: Decimal = Decimal("1.0")
    max_close_factor: Decimal = Decimal("0.5")


class DepositIn(BaseModel):
    party: str
    symbol: str
    amount: Decimal = Field(gt=0)


class LiquidateIn(BaseModel):
    liquidation_id: str
    position_id: str
    liquidator: str
    repay_debt_amount: Decimal = Field(gt=0)


def _snapshot_dict(snap) -> dict:
    return {"party": snap.party, "balances": {k: str(v) for k, v in snap.balances.items()}}


def create_app(engine: RiskEngine | None = None) -> FastAPI:
    app = FastAPI(title="Local DeFi Lending Risk Engine", version="0.1.0")
    engine = engine or RiskEngine(clock=Clock())

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"service": "defi-lending-risk-engine", "status": "ok"}

    @app.post("/prices")
    def push_price(body: PricePushIn) -> dict:
        tick = PriceTick(
            symbol=body.symbol,
            price=body.price,
            timestamp=body.timestamp,
            source=body.source,
        )
        d = engine.push_price(tick)
        return {
            "accepted": d.accepted,
            "reason": d.reason.value if d.reason else None,
            "detail": d.detail,
        }

    @app.get("/prices/{symbol}")
    def get_price(symbol: str) -> dict:
        view = engine.price_view(symbol)
        if view is None:
            return {"symbol": symbol, "available": False}
        return {
            "symbol": symbol,
            "available": True,
            "price": str(view.price),
            "timestamp": view.timestamp,
            "stale": view.stale,
        }

    @app.post("/accounts/deposit")
    def deposit(body: DepositIn) -> dict:
        engine.deposit(body.party, body.symbol, body.amount)
        return {"ok": True}

    @app.post("/positions")
    def open_position(body: PositionIn) -> dict:
        position = Position(
            position_id=body.position_id,
            owner=body.owner,
            collateral_symbol=body.collateral_symbol,
            collateral_amount=body.collateral_amount,
            debt_symbol=body.debt_symbol,
            debt_amount=body.debt_amount,
            liquidation_threshold=body.liquidation_threshold,
            max_close_factor=body.max_close_factor,
        )
        engine.open_position(position)
        return {"ok": True, "position_id": body.position_id}

    @app.get("/positions/{position_id}/health")
    def get_health(position_id: str) -> dict:
        try:
            report = engine.health(position_id)
        except PositionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "position_id": report.position_id,
            "rating": report.rating.value,
            "healthy": report.healthy,
            "health_factor": str(report.health_factor) if report.health_factor is not None else None,
            "threshold": str(report.threshold),
            "collateral_value_usd": str(report.collateral_value_usd),
            "debt_value_usd": str(report.debt_value_usd),
            "detail": report.detail,
            "evaluated_at": report.evaluated_at,
        }

    @app.post("/liquidations")
    def liquidate(body: LiquidateIn) -> dict:
        try:
            result = engine.liquidate(
                liquidation_id=body.liquidation_id,
                position_id=body.position_id,
                liquidator=body.liquidator,
                repay_debt_amount=body.repay_debt_amount,
            )
        except PositionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        payload = {
            "liquidation_id": result.liquidation_id,
            "position_id": result.position_id,
            "outcome": result.outcome.value,
            "detail": result.detail,
            "repay_debt_amount": str(result.repay_debt_amount),
            "collateral_seized": str(result.collateral_seized),
            "liquidator_bonus": str(result.liquidator_bonus),
            "pool_penalty": str(result.pool_penalty),
        }
        if result.before is not None and result.after is not None:
            parties = ("liquidator", "borrower", "pool")
            payload["before"] = {
                name: _snapshot_dict(snap)
                for name, snap in zip(parties, result.before)
            }
            payload["after"] = {
                name: _snapshot_dict(snap)
                for name, snap in zip(parties, result.after)
            }
        return payload

    return app


app = create_app()
