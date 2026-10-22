from decimal import Decimal
from datetime import datetime
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import ButterflyConfig, TradingConfig
from app.core.butterfly import BUTTERFLY_ACTIVE, BUTTERFLY_SESSIONS, ButterflyTrade
from app.core.market import EASTERN
from app.core.models import ACTIVE, ManualRetestRequest, Trade, calendar_risk_policy, utcnow
from app.core.market import MarketCalendar
from app.engine.butterfly_engine import ButterflyEngine
from app.engine.paper_engine import PaperEngine
from app.ibkr.client import MarketDataError

router = APIRouter(prefix="/api")


def engine_for(request: Request) -> PaperEngine:
    return request.app.state.engine


def performance(
    trades: Sequence[Trade | ButterflyTrade], market: MarketCalendar, now: datetime, sessions: int
) -> dict:
    closed = [
        trade
        for trade in trades
        if trade.status not in ACTIVE and trade.status not in BUTTERFLY_ACTIVE
    ]
    pnls = [trade.final_pnl_dollars or Decimal(0) for trade in closed]
    wins = sum((pnl for pnl in pnls if pnl > 0), Decimal(0))
    losses = -sum((pnl for pnl in pnls if pnl < 0), Decimal(0))
    transformed = [trade for trade in trades if trade.transformed]
    days = (
        market.sessions_between(
            min(trade.entry_session for trade in trades), now.astimezone(EASTERN).date()
        )
        if trades
        else 0
    )
    return {
        "total_trades": len(trades),
        "normal_trade_count": sum(
            not isinstance(trade, Trade) or not trade.manual_retest for trade in trades
        ),
        "manual_retest_count": sum(
            isinstance(trade, Trade) and trade.manual_retest for trade in trades
        ),
        "closed_trades": len(closed),
        "proxy_settled_trades": sum(
            trade.settlement_provenance == "ibkr_close_proxy" for trade in closed
        ),
        "transform_success_rate": len(transformed) / len(trades) * 100 if trades else 0,
        "average_minutes_to_transform": (
            sum(trade.minutes_to_transform or 0 for trade in transformed) / len(transformed)
            if transformed
            else None
        ),
        "profit_factor": str(wins / losses) if losses else None,
        "profit_factor_label": "∞" if wins and not losses else "N/A",
        "total_pnl": str(sum(pnls, Decimal(0))),
        "max_winner": str(max([Decimal(0), *pnls])),
        "max_loser": str(min([Decimal(0), *pnls])),
        "win_rate": sum(pnl > 0 for pnl in pnls) / len(pnls) * 100 if pnls else None,
        "sessions_elapsed": days,
        "entry_window_sessions": sessions,
        "entry_window_complete": days > sessions,
    }


async def snapshot(
    engine: PaperEngine,
    connected: bool,
    broker_error: str | None,
    butterfly: ButterflyEngine | None = None,
) -> dict:
    now = utcnow()
    trades = await engine.store.trades()
    butterflies = await engine.store.butterflies()
    positions: list[Trade | ButterflyTrade] = [*trades, *butterflies]
    rows = []
    for trade in positions:
        row = trade.model_dump(mode="json")
        row["strategy"] = "DOUBLE_CALENDAR" if isinstance(trade, Trade) else trade.strategy
        _, close = engine.market.bounds(
            trade.front_expiry if isinstance(trade, Trade) else trade.expiry
        )
        row.update(
            target_credit=str(trade.target_credit),
            net_floor_dollars=(
                str(
                    (trade.guaranteed_floor_profit or Decimal(0)) * trade.multiplier
                    - trade.commissions_dollars
                )
                if trade.transformed
                else None
            ),
            settlement_pending=(trade.status in ACTIVE or trade.status in BUTTERFLY_ACTIVE)
            and now >= close,
            quote_stale=(
                trade.last_quote_at is None
                or (now - trade.last_quote_at).total_seconds()
                > engine.settings.max_quote_age_seconds
            ),
        )
        rows.append(row)
    return {
        "server_time": now.isoformat(),
        "market_time": now.astimezone(EASTERN).isoformat(),
        "market_open": engine.market.is_open(now),
        "next_entry": engine.market.next_entry(now).isoformat(),
        "next_butterfly_entry": engine.market.next_entry(now, minute=0).isoformat(),
        "connected": connected,
        "error": engine.last_error or (butterfly.last_error if butterfly else None) or broker_error,
        "mode": "SIMULATED FILLS • SPXW ONLY • NO BROKER ORDERS",
        "advisor_enabled": engine.settings.advisor_enabled,
        "automatic_settlement": engine.settings.auto_settle_from_ib_close,
        "config": (await engine.store.config()).model_dump(),
        "butterfly_config": (await engine.store.butterfly_config()).model_dump(),
        "poll_seconds": engine.settings.poll_seconds,
        "risk_policy": calendar_risk_policy(),
        "trades": rows,
        "metrics": performance(trades, engine.market, now, engine.settings.evaluation_sessions),
        "butterfly_metrics": performance(butterflies, engine.market, now, BUTTERFLY_SESSIONS),
        "feed": await engine.store.logs("advisor", 20),
        "errors": await engine.store.logs("error", 10),
    }


@router.get("/state")
async def state(request: Request) -> dict:
    return await snapshot(
        engine_for(request),
        request.app.state.ib.connected,
        request.app.state.ib.last_error,
        request.app.state.butterfly,
    )


@router.get("/health")
async def health(request: Request) -> dict:
    await engine_for(request).store.config()
    return {
        "application": "ready",
        "ibkr_connected": request.app.state.ib.connected,
        "orders_enabled": False,
    }


@router.put("/config")
async def configure(config: TradingConfig, request: Request) -> dict:
    await engine_for(request).configure(config)
    return config.model_dump()


@router.put("/config/butterfly")
async def configure_butterfly(config: ButterflyConfig, request: Request) -> dict:
    await request.app.state.butterfly.configure(config)
    return config.model_dump()


async def execute_manual_entry(request: Request, retest: ManualRetestRequest | None = None) -> dict:
    engine = engine_for(request)
    try:
        trade = await engine.entry(manual=True, retest=retest)
        return trade.model_dump(mode="json")
    except (ValueError, MarketDataError, ConnectionError, TimeoutError, OSError) as exc:
        await engine.error("manual retest" if retest else "manual entry", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/entry")
async def entry(request: Request) -> dict:
    return await execute_manual_entry(request)


@router.post("/entry/retest")
async def manual_retest(body: ManualRetestRequest, request: Request) -> dict:
    return await execute_manual_entry(request, body)


class SettlementInput(BaseModel):
    value: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    source: str = Field(min_length=10, max_length=1000)
    confirmed_official_spxw_pm: bool


@router.post("/trades/{trade_id}/settlement")
async def settle(trade_id: str, body: SettlementInput, request: Request) -> dict:
    if not body.confirmed_official_spxw_pm:
        raise HTTPException(422, "Official SPXW PM settlement confirmation is required")
    engine = engine_for(request)
    if any(trade.trade_id == trade_id for trade in await engine.store.butterflies()):
        return await settle_butterfly(request.app.state.butterfly, trade_id, body)
    try:
        return (await engine.settle(trade_id, body.value, body.source)).model_dump(mode="json")
    except (ValueError, MarketDataError, ConnectionError, TimeoutError, OSError) as exc:
        await engine.error("settlement", exc, trade_id)
        raise HTTPException(409, str(exc)) from exc


async def settle_butterfly(engine: ButterflyEngine, trade_id: str, body: SettlementInput) -> dict:
    try:
        return (await engine.settle(trade_id, body.value, body.source)).model_dump(mode="json")
    except (ValueError, MarketDataError, ConnectionError, TimeoutError, OSError) as exc:
        await engine.error("manual 0DTE settlement", exc, trade_id)
        raise HTTPException(409, str(exc)) from exc


@router.get("/report")
async def report(request: Request) -> Response:
    engine = engine_for(request)
    async with engine.lock, request.app.state.butterfly.lock:
        content = await engine.csv.report()
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="dc_validation_report.zip"'},
    )


@router.get("/csv/{name}")
async def csv_download(name: str, request: Request) -> Response:
    if name not in {
        "trades_summary.csv",
        "legs_execution_log.csv",
        "butterfly_trades_summary.csv",
        "butterfly_legs_execution_log.csv",
    }:
        raise HTTPException(404, "Unknown report")
    engine = engine_for(request)
    async with engine.lock, request.app.state.butterfly.lock:
        files = await engine.csv.contents()
    return Response(
        files[name],
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    import asyncio
    from urllib.parse import urlsplit

    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    app = websocket.app
    try:
        while True:
            await websocket.send_json(
                await snapshot(
                    app.state.engine,
                    app.state.ib.connected,
                    app.state.ib.last_error,
                    app.state.butterfly,
                )
            )
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
