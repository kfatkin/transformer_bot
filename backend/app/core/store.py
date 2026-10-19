import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import TradingConfig
from app.core.models import Evaluation, LegEvent, Trade, utcnow


class Base(DeclarativeBase):
    pass


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("symbol", "entry_session"),)
    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    entry_session: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class LogRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ConfigRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{directory / 'paper.db'}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.export_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.exec_driver_sql("PRAGMA busy_timeout=5000")

    async def trades(self) -> list[Trade]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(TradeRow).order_by(TradeRow.entry_session, TradeRow.trade_id)
            )
            return [Trade.model_validate(row.payload) for row in rows]

    async def commit_trade(
        self, trade: Trade, legs: list[LegEvent], evaluation: Evaluation | None = None
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(TradeRow, trade.trade_id)
            if row is None:
                session.add(
                    TradeRow(
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                        entry_session=trade.entry_session.isoformat(),
                        payload=trade.model_dump(mode="json"),
                    )
                )
            else:
                row.payload = trade.model_dump(mode="json")
            for leg in legs:
                session.add(
                    LogRow(
                        kind="leg",
                        timestamp=leg.timestamp,
                        payload=leg.model_dump(mode="json"),
                    )
                )
            if evaluation:
                session.add(
                    LogRow(
                        kind="evaluation",
                        timestamp=evaluation.timestamp,
                        payload=evaluation.model_dump(mode="json"),
                    )
                )

    async def log(self, kind: str, payload: dict[str, Any]) -> None:
        async with self.sessions.begin() as session:
            session.add(LogRow(kind=kind, timestamp=utcnow(), payload=payload))

    async def logs(self, kind: str, limit: int | None = None) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            query = select(LogRow).where(LogRow.kind == kind)
            if limit:
                query = query.order_by(LogRow.id.desc()).limit(limit)
            else:
                query = query.order_by(LogRow.id)
            rows = await session.scalars(query)
            return [
                {
                    "id": row.id,
                    "logged_at": row.timestamp.replace(tzinfo=timezone.utc).isoformat(),
                    **row.payload,
                }
                for row in rows
            ]

    async def config(self) -> TradingConfig:
        async with self.sessions() as session:
            row = await session.get(ConfigRow, "trading")
            return TradingConfig.model_validate(row.payload) if row else TradingConfig()

    async def save_config(self, config: TradingConfig) -> None:
        async with self.sessions.begin() as session:
            await session.merge(ConfigRow(key="trading", payload=config.model_dump(mode="json")))
