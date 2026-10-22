import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from app.advisor.ai_agent import Advisor
from app.core.butterfly import (
    BUTTERFLY_ACTIVE,
    BUTTERFLY_SESSIONS,
    ButterflyStatus,
    ButterflyTrade,
)
from app.core.config import ButterflyConfig, Settings
from app.core.market import EASTERN, MarketCalendar
from app.core.models import Evaluation, LegEvent, OptionSpec, Quote, utcnow
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.math import intrinsic
from app.engine.paper_engine import DataProvider, execution, validate_quote_set, vix_context
from app.ibkr.client import MarketDataError

logger = logging.getLogger(__name__)


class ButterflyProvider(DataProvider, Protocol):
    async def session_open(self, day: date) -> Decimal: ...


class ButterflySelection(Protocol):
    async def select(
        self, today: date, width: Decimal, right: Literal["P", "C"]
    ) -> dict[str, OptionSpec]: ...


class ButterflyEngine:
    def __init__(
        self,
        store: Store,
        provider: ButterflyProvider,
        selector: ButterflySelection,
        market: MarketCalendar,
        csv: CSVLogger,
        advisor: Advisor,
        settings: Settings,
    ) -> None:
        self.store = store
        self.provider = provider
        self.selector = selector
        self.market = market
        self.csv = csv
        self.advisor = advisor
        self.settings = settings
        self.lock = asyncio.Lock()
        self.entry_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.settlement_attempts: dict[str, datetime] = {}

    async def error(self, component: str, exc: Exception, trade_id: str | None = None) -> None:
        self.last_error = f"{component}: {exc}"
        logger.error(self.last_error, exc_info=exc)
        await self.store.log(
            "error",
            {
                "strategy": "ZERO_DTE_BUTTERFLY",
                "component": component,
                "message": str(exc),
                "trade_id": trade_id,
            },
        )

    def entry_clock(self, now: datetime) -> date:
        local = now.astimezone(EASTERN)
        if not self.market.is_open(now) or (local.hour, local.minute) != (10, 0):
            raise ValueError(
                "0DTE entry requires the 10:00:00-10:00:59 Eastern minute; no catch-up"
            )
        return local.date()

    async def entry(self) -> ButterflyTrade:
        async with self.entry_lock:
            now = utcnow()
            day = self.entry_clock(now)
            config = await self.store.butterfly_config()
            if not config.enabled:
                raise ValueError("0DTE new entries are disabled")
            trades = await self.store.butterflies()
            if any(trade.entry_session == day for trade in trades):
                raise ValueError("0DTE already has an entry this session")
            if (
                trades
                and self.market.sessions_between(min(trade.entry_session for trade in trades), day)
                > BUTTERFLY_SESSIONS
            ):
                raise ValueError("0DTE validation entry window completed; monitoring continues")
            deadline = now.astimezone(EASTERN).replace(second=0, microsecond=0) + timedelta(
                minutes=1
            )
            try:
                async with asyncio.timeout((deadline - now).total_seconds()):
                    opening = await self.provider.session_open(day)
                    signal = await self.provider.index("SPX")
                    signal_at = utcnow()
                    self.entry_clock(signal_at)
                    if (
                        not opening.is_finite()
                        or opening <= 0
                        or not signal.is_finite()
                        or signal <= 0
                    ):
                        raise MarketDataError("Invalid SPX opening baseline or live signal price")
                    if signal == opening:
                        raise ValueError(
                            "SPX unchanged from the 09:30 opening bar; no directional entry"
                        )
                    right: Literal["P", "C"] = "C" if signal < opening else "P"
                    contracts = await self.selector.select(day, Decimal(config.wing_width), right)
                    vix = await vix_context(self.provider, self.error)
                    quotes = await self.provider.quotes(contracts, require_delta=True)
            except TimeoutError as exc:
                raise MarketDataError("0DTE quote selection missed the 10:00 entry window") from exc
            async with self.lock:
                filled_at = utcnow()
                if self.entry_clock(filled_at) != day:
                    raise ValueError("0DTE session changed during sampling; no simulated fill")
                validate_quote_set(quotes, contracts, self.settings, filled_at)
                delta = quotes["long"].delta
                if delta is None or not 0.30 <= abs(delta) <= 0.50 or (delta < 0) != (right == "P"):
                    raise MarketDataError(
                        "0DTE long delta drifted outside its signed 0.30-0.50 band"
                    )
                debit = quotes["long"].ask - quotes["short"].bid
                if not 0 < debit < Decimal(config.wing_width):
                    raise MarketDataError(
                        "0DTE opening debit must be positive and less than spread width"
                    )
                commission = Decimal(str(config.commission_per_leg))
                trade = ButterflyTrade(
                    trade_id=f"BF-{day:%Y%m%d}-SPX",
                    entry_session=day,
                    entry_timestamp=filled_at,
                    signal_timestamp=signal_at,
                    opening_index=opening,
                    signal_price=signal,
                    expiry=day,
                    right=right,
                    wing_width=Decimal(config.wing_width),
                    quantity=config.quantity,
                    initial_debit=debit,
                    commission_per_leg=commission,
                    commissions_dollars=commission * 2 * config.quantity,
                    contracts=contracts,
                    notes="0DTE contrarian debit-to-balanced-butterfly; SPXW PM; natural fills; "
                    "09:30 RTH one-minute opening-bar baseline; bot-monitored 30% loss before fees; "
                    "no broker orders. Untransformed exit 15 minutes before session close.",
                )
                liquidation = quotes["long"].bid - quotes["short"].ask
                if liquidation <= trade.stop_credit:
                    message = (
                        "0DTE entry rejected: initial natural liquidation loss meets or exceeds 30%"
                    )
                    await self.store.log(
                        "entry_rejection",
                        {
                            "strategy": trade.strategy,
                            "timestamp": filled_at.isoformat(),
                            "entry_session": day.isoformat(),
                            "message": message,
                            "opening_index": str(opening),
                            "signal_price": str(signal),
                            "initial_debit": str(debit),
                            "liquidation_credit": str(liquidation),
                            "stop_credit": str(trade.stop_credit),
                            "quotes": {
                                name: quote.model_dump(mode="json")
                                for name, quote in quotes.items()
                            },
                        },
                    )
                    raise MarketDataError(message)
                legs = [
                    execution(trade, quotes["long"], "BUY", "ENTRY_DEBIT_SPREAD", filled_at),
                    execution(trade, quotes["short"], "SELL", "ENTRY_DEBIT_SPREAD", filled_at),
                ]
                trade.mark_price = liquidation
                trade.unrealized_pnl_dollars = (
                    liquidation - debit
                ) * trade.multiplier - trade.commissions_dollars
                trade.current_transform_credit = quotes["short"].bid - quotes["outer"].ask
                trade.last_quote_at = min(quote.timestamp for quote in quotes.values())
                evaluation = self.evaluation(trade, quotes, "ENTRY_DEBIT_SPREAD")
                evaluation.vix = vix
                await self.store.commit_butterfly(trade, legs, evaluation)
                await self.csv.export()
                await self.advisor.submit(evaluation)
                self.last_error = None
                return trade

    def evaluation(self, trade: ButterflyTrade, quotes: dict[str, Quote], kind: str) -> Evaluation:
        return Evaluation(
            strategy=trade.strategy,
            trade_id=trade.trade_id,
            timestamp=utcnow(),
            kind=kind,
            quotes=quotes,
            transformer_credit=quotes["short"].bid - quotes["outer"].ask,
            liquidation_credit=quotes["long"].bid - quotes["short"].ask,
            target_credit=trade.target_credit,
            underlying=trade.signal_price if kind == "ENTRY_DEBIT_SPREAD" else None,
            context={
                "opening_index": str(trade.opening_index),
                "entry_signal_price": str(trade.signal_price),
                "direction": "bullish call debit" if trade.right == "C" else "bearish put debit",
                "long_delta": quotes["long"].delta,
                "maximum_bid_ask_points": float(
                    max(quote.ask - quote.bid for quote in quotes.values())
                ),
                "stop_credit": str(trade.stop_credit),
            },
        )

    async def poll(self) -> None:
        async with self.lock:
            now = utcnow()
            if not self.market.is_open(now):
                return
            for trade in await self.store.butterflies():
                if (
                    trade.status not in BUTTERFLY_ACTIVE
                    or trade.expiry < now.astimezone(EASTERN).date()
                ):
                    continue
                try:
                    vix = await vix_context(self.provider, self.error)
                    quotes = await self.provider.quotes(trade.contracts)
                    now = utcnow()
                    validate_quote_set(quotes, trade.contracts, self.settings, now)
                    if self.market.is_open(now):
                        await self.evaluate(trade, quotes, now, vix)
                except (MarketDataError, ValueError, ConnectionError, TimeoutError, OSError) as exc:
                    await self.error("0DTE quote evaluation", exc, trade.trade_id)
            await self.csv.export()

    async def evaluate(
        self,
        trade: ButterflyTrade,
        quotes: dict[str, Quote],
        now: datetime,
        vix: Decimal | None = None,
    ) -> None:
        liquidation = quotes["long"].bid - quotes["short"].ask
        credit = quotes["short"].bid - quotes["outer"].ask
        trade.last_quote_at = min(quote.timestamp for quote in quotes.values())
        trade.current_transform_credit = credit
        evaluation = self.evaluation(trade, quotes, "BUTTERFLY_EVALUATION")
        evaluation.vix = vix
        legs: list[LegEvent] = []
        if trade.status == ButterflyStatus.DEBIT:
            trade.mark_price = liquidation
            trade.unrealized_pnl_dollars = (
                liquidation - trade.initial_debit
            ) * trade.multiplier - trade.commissions_dollars
            _, close = self.market.bounds(trade.expiry)
            timed_exit = now >= close - timedelta(minutes=15)
            if liquidation <= trade.stop_credit or timed_exit:
                event = "DEBIT_TIME_EXIT" if timed_exit else "DEBIT_STOP_LOSS"
                legs = [
                    execution(trade, quotes["long"], "SELL", event, now),
                    execution(trade, quotes["short"], "BUY", event, now),
                ]
                trade.commissions_dollars += trade.commission_per_leg * 2 * trade.quantity
                self.finish(
                    trade,
                    ButterflyStatus.TIME if timed_exit else ButterflyStatus.STOP,
                    (liquidation - trade.initial_debit) * trade.multiplier,
                    now,
                    f"{event}: natural liquidation credit {liquidation} points.",
                )
                evaluation.kind = event
            elif credit >= trade.target_credit:
                legs = [
                    execution(trade, quotes["short"], "SELL", "TRANSFORM_BUTTERFLY", now),
                    execution(trade, quotes["outer"], "BUY", "TRANSFORM_BUTTERFLY", now),
                ]
                trade.transformed = True
                trade.status = ButterflyStatus.BUTTERFLY
                trade.transform_timestamp = now
                trade.minutes_to_transform = int(
                    (now - trade.entry_timestamp).total_seconds() // 60
                )
                trade.transformer_credit_received = credit
                trade.transform_gain_pct = (
                    (credit - trade.initial_debit) / trade.initial_debit * 100
                )
                trade.guaranteed_floor_profit = credit - trade.initial_debit
                trade.max_tent_profit = credit - trade.initial_debit + trade.wing_width
                trade.commissions_dollars += trade.commission_per_leg * 2 * trade.quantity
                trade.notes += (
                    " Transformed to balanced butterfly; modeled net expiration floor >= 0."
                )
                evaluation.kind = "TRANSFORM_BUTTERFLY"
        if trade.status == ButterflyStatus.BUTTERFLY:
            trade.mark_price = quotes["long"].bid + quotes["outer"].bid - 2 * quotes["short"].ask
            trade.unrealized_pnl_dollars = (
                (trade.transformer_credit_received or Decimal(0))
                - trade.initial_debit
                + trade.mark_price
            ) * trade.multiplier - trade.commissions_dollars
            if evaluation.kind != "TRANSFORM_BUTTERFLY":
                evaluation.kind = "BUTTERFLY_MARK"
        await self.store.commit_butterfly(trade, legs, evaluation)
        if evaluation.kind != "BUTTERFLY_MARK":
            await self.advisor.submit(evaluation)

    @staticmethod
    def finish(
        trade: ButterflyTrade, status: ButterflyStatus, gross: Decimal, now: datetime, note: str
    ) -> None:
        trade.status = status
        trade.exit_timestamp = now
        trade.final_pnl_dollars = gross - trade.commissions_dollars
        trade.roi_pct = trade.final_pnl_dollars / trade.capital * 100
        trade.unrealized_pnl_dollars = Decimal(0)
        trade.notes += f" {note} Total commissions ${trade.commissions_dollars}."

    async def settle(
        self,
        trade_id: str,
        value: Decimal,
        source: str,
        provenance: Literal["verified_official", "ibkr_close_proxy"] = "verified_official",
    ) -> ButterflyTrade:
        async with self.lock:
            trade = next(
                (t for t in await self.store.butterflies() if t.trade_id == trade_id), None
            )
            if trade is None:
                raise ValueError("Unknown 0DTE trade")
            if trade.status not in BUTTERFLY_ACTIVE:
                raise ValueError("0DTE trade already closed")
            _, close = self.market.bounds(trade.expiry)
            now = utcnow()
            if now < close:
                raise ValueError("SPXW PM settlement cannot be applied before expiration close")
            if not value.is_finite() or value <= 0 or len(source.strip()) < 10:
                raise ValueError("A positive SPXW settlement and its source are required")
            names = ["long", "short", "outer"] if trade.transformed else ["long", "short"]
            payout = Decimal(0)
            legs = []
            for name in names:
                spec = trade.contracts[name]
                count = 2 if name == "short" and trade.transformed else 1
                price = intrinsic(spec.strike, spec.right, value)
                payout += price * (-count if name == "short" else count)
                legs.append(
                    LegEvent(
                        trade_id=trade.trade_id,
                        timestamp=close,
                        event_type="EXPIRATION_SETTLEMENT",
                        contract=spec,
                        action="BUY" if name == "short" else "SELL",
                        simulated_fill_price=price,
                        quantity=count * trade.quantity,
                    )
                )
            credit = trade.transformer_credit_received or Decimal(0)
            status = (
                (
                    ButterflyStatus.FLOOR
                    if payout == 0
                    else ButterflyStatus.TENT
                    if payout == trade.wing_width
                    else ButterflyStatus.SLOPE
                )
                if trade.transformed
                else ButterflyStatus.EXPIRED
            )
            trade.settlement_value = value
            trade.settlement_source = source.strip()
            trade.settlement_provenance = provenance
            self.finish(
                trade,
                status,
                (credit + payout - trade.initial_debit) * trade.multiplier,
                now,
                f"{'IBKR closing-index PROXY' if provenance == 'ibkr_close_proxy' else 'Verified SPXW PM settlement'} "
                f"{value}; source: {source.strip()}.",
            )
            await self.store.commit_butterfly(trade, legs)
            await self.store.log(
                "settlement",
                {
                    "strategy": trade.strategy,
                    "trade_id": trade_id,
                    "settlement_value": str(value),
                    "effective_timestamp": close.isoformat(),
                    "source": source.strip(),
                    "provenance": provenance,
                },
            )
            if not trade.transformed:
                await self.error(
                    "0DTE missed time exit",
                    ValueError(
                        "Untransformed spread expired before a valid timed-exit fill; settled intrinsic cashflows"
                    ),
                    trade_id,
                )
            await self.csv.export()
            return trade

    async def settle_due(self) -> None:
        if not self.settings.auto_settle_from_ib_close:
            return
        now = utcnow()
        for trade in await self.store.butterflies():
            if trade.status not in BUTTERFLY_ACTIVE:
                continue
            _, close = self.market.bounds(trade.expiry)
            if now < close + timedelta(minutes=self.settings.settlement_delay_minutes):
                continue
            previous = self.settlement_attempts.get(trade.trade_id)
            if previous and (now - previous).total_seconds() < 300:
                continue
            self.settlement_attempts[trade.trade_id] = now
            try:
                value = await self.provider.settlement_close(trade.expiry)
                await self.settle(
                    trade.trade_id,
                    value,
                    f"IBKR SPX CBOE TRADES/RTH daily close for {trade.expiry}; NOT verified official settlement",
                    provenance="ibkr_close_proxy",
                )
            except (MarketDataError, ValueError, ConnectionError, TimeoutError, OSError) as exc:
                await self.error("0DTE automatic settlement proxy", exc, trade.trade_id)

    async def configure(self, config: ButterflyConfig) -> None:
        async with self.entry_lock, self.lock:
            await self.store.save_butterfly_config(config)
