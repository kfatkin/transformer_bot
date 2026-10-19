import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from app.advisor.ai_agent import Advisor
from app.core.config import Settings, TradingConfig
from app.core.market import EASTERN, MarketCalendar
from app.core.models import (
    ACTIVE,
    CALENDAR_STOP_LOSS_PCT,
    Evaluation,
    LegEvent,
    ManualRetestRequest,
    OptionSpec,
    Quote,
    Status,
    Trade,
    utcnow,
)
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.math import (
    calendar_debit,
    condor_close_debit,
    condor_payoff_loss,
    intrinsic,
    liquidation_credit,
    transformer_credit,
)
from app.ibkr.client import MarketDataError

logger = logging.getLogger(__name__)


class DataProvider(Protocol):
    async def quotes(
        self, specs: dict[str, OptionSpec], require_delta: bool = False
    ) -> dict[str, Quote]: ...

    async def index(self, symbol: str) -> Decimal: ...

    async def settlement_close(self, expiry: date) -> Decimal: ...


class Selector(Protocol):
    async def select(self, today: date, width: Decimal) -> dict[str, OptionSpec]: ...


def execution(trade: Trade, quote: Quote, action: str, event: str, now: datetime) -> LegEvent:
    size = quote.ask_size if action == "BUY" else quote.bid_size
    if size is None or size < trade.quantity:
        raise MarketDataError(
            f"{quote.contract.con_id}: insufficient displayed {action} size for {trade.quantity} contracts"
        )
    return LegEvent(
        trade_id=trade.trade_id,
        timestamp=now,
        event_type=event,
        contract=quote.contract,
        action="BUY" if action == "BUY" else "SELL",
        bid=quote.bid,
        ask=quote.ask,
        simulated_fill_price=quote.ask if action == "BUY" else quote.bid,
        delta=quote.delta,
        implied_vol=quote.implied_vol,
    )


class PaperEngine:
    def __init__(
        self,
        store: Store,
        provider: DataProvider,
        selector: Selector,
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
        self.prepared: tuple[date, int, dict[str, OptionSpec]] | None = None
        self.last_error: str | None = None
        self.settlement_attempts: dict[str, datetime] = {}

    async def error(self, component: str, exc: Exception, trade_id: str | None = None) -> None:
        self.last_error = f"{component}: {exc}"
        logger.error(self.last_error, exc_info=exc)
        await self.store.log(
            "error",
            {
                "component": component,
                "message": str(exc),
                "trade_id": trade_id,
            },
        )

    def validate_quotes(self, quotes: dict[str, Quote], specs: dict[str, OptionSpec]) -> None:
        if set(quotes) != set(specs):
            raise MarketDataError("Incomplete quote set")
        now = utcnow()
        for name, quote in quotes.items():
            if quote.contract != specs[name]:
                raise MarketDataError(f"{name}: contract mismatch")
            if (
                not 0
                <= (now - quote.timestamp).total_seconds()
                <= self.settings.max_quote_age_seconds
            ):
                raise MarketDataError(f"{name}: stale quote")
            if quote.ask - quote.bid > Decimal(str(self.settings.max_spread_points)):
                raise MarketDataError(f"{name}: spread exceeds quality limit")

    async def prepare(self) -> None:
        async with self.lock:
            now = utcnow()
            if not self.market.is_open(now):
                return
            config = await self.store.config()
            if not config.enabled:
                return
            day = now.astimezone(EASTERN).date()
            contracts = await self.selector.select(day, Decimal(config.wing_width))
            self.prepared = (day, config.wing_width, contracts)

    async def vix(self) -> Decimal | None:
        try:
            return await self.provider.index("VIX")
        except (MarketDataError, ConnectionError, TimeoutError, OSError) as exc:
            await self.error("VIX context unavailable (trading math unaffected)", exc)
            return None

    async def entry(
        self, manual: bool = False, *, retest: ManualRetestRequest | None = None
    ) -> Trade:
        async with self.lock:
            if retest is not None and not manual:
                raise ValueError("Retests must be explicitly requested as manual entries")
            trades = await self.store.trades()
            if retest is not None:
                existing = next(
                    (
                        trade
                        for trade in trades
                        if trade.manual_retest_request_id == retest.request_id
                    ),
                    None,
                )
                if existing:
                    if existing.manual_retest_reason != retest.reason:
                        raise ValueError("Retest request ID was reused with a different reason")
                    return existing
            now = utcnow()
            local = now.astimezone(EASTERN)
            if not self.market.is_open(now):
                raise ValueError("Entry requires an open regular market session and live quotes")
            if not manual and (local.hour, local.minute) != (10, 30):
                raise ValueError(
                    "Scheduled entry outside 10:30 minute rejected; no catch-up trades"
                )
            config = await self.store.config()
            if not config.enabled:
                raise ValueError("New entries are disabled")
            daily_exists = any(
                trade.entry_session == local.date() and not trade.manual_retest for trade in trades
            )
            if retest is not None:
                if not daily_exists:
                    raise ValueError("Manual retest requires an existing normal SPX entry today")
                if any(trade.status in ACTIVE for trade in trades):
                    raise ValueError(
                        "Manual retest requires all existing SPX positions to be closed"
                    )
            elif daily_exists:
                raise ValueError("SPX already has an entry this session")
            if (
                trades
                and self.market.sessions_between(
                    min(trade.entry_session for trade in trades), local.date()
                )
                > self.settings.evaluation_sessions
            ):
                raise ValueError(
                    "Validation entry window completed; existing trades remain monitored"
                )
            if (
                self.prepared
                and self.prepared[0] == local.date()
                and self.prepared[1] == config.wing_width
            ):
                contracts = self.prepared[2]
            elif manual:
                contracts = await self.selector.select(local.date(), Decimal(config.wing_width))
            else:
                raise ValueError(
                    "10:25 preflight unavailable; scheduled entry skipped, not entered late"
                )
            # Collect optional context before executable quotes, never after them.
            vix = await self.vix()
            quotes = await self.provider.quotes(contracts, require_delta=True)
            self.validate_quotes(quotes, contracts)
            for side in ("put", "call"):
                delta = quotes[f"short_{side}"].delta
                if delta is None or not 0.30 <= abs(delta) <= 0.40:
                    raise MarketDataError("Selected short delta drifted outside 30–40 at entry")
            filled_at = utcnow()
            if not self.market.is_open(filled_at):
                raise ValueError("Market closed during entry selection; no simulated fill")
            if not manual and (
                filled_at.astimezone(EASTERN).hour,
                filled_at.astimezone(EASTERN).minute,
            ) != (10, 30):
                raise ValueError("Quote sampling missed 10:30 entry window; no simulated fill")
            debit = calendar_debit(quotes)
            if debit <= 0:
                raise MarketDataError("Opening calendar debit must be positive")
            commission = Decimal(str(config.commission_per_leg))
            trade = Trade(
                trade_id=(
                    f"DC-{local.date():%Y%m%d}-SPX-TEST-{retest.request_id.hex}"
                    if retest
                    else f"DC-{local.date():%Y%m%d}-SPX"
                ),
                entry_session=local.date(),
                entry_timestamp=filled_at,
                manual_retest=retest is not None,
                manual_retest_request_id=retest.request_id if retest else None,
                manual_retest_reason=retest.reason if retest else None,
                front_expiry=contracts["short_put"].expiry,
                back_expiry=contracts["back_put"].expiry,
                put_strike=contracts["short_put"].strike,
                call_strike=contracts["short_call"].strike,
                wing_width=Decimal(config.wing_width),
                quantity=config.quantity,
                calendar_debit_paid=debit,
                calendar_stop_loss_pct=CALENDAR_STOP_LOSS_PCT,
                commission_per_leg=commission,
                commissions_dollars=commission * 4 * config.quantity,
                contracts=contracts,
                notes="SPXW PM cash-settled; conservative natural fills. "
                + (
                    f"MANUAL_RETEST: {retest.reason}. Extra test, not a scheduled study entry. "
                    if retest
                    else ("Manual entry. " if manual else "Scheduled entry. ")
                )
                + f"Commission ${commission}/contract/leg; no broker orders. "
                + f"Bot-monitored calendar stop: -{CALENDAR_STOP_LOSS_PCT}% of opening "
                + "debit before fees; reject entries already at this loss.",
            )
            liquidation = liquidation_credit(quotes)
            if liquidation <= trade.calendar_stop_credit:
                initial_loss_pct = (debit - liquidation) / debit * 100
                message = (
                    f"Entry rejected: initial natural liquidation loss {initial_loss_pct:.2f}% "
                    f"meets or exceeds the {trade.calendar_stop_loss_pct}% bot threshold "
                    f"(debit {debit}, liquidation credit {liquidation} points)."
                )
                await self.store.log(
                    "entry_rejection",
                    {
                        "symbol": trade.symbol,
                        "timestamp": filled_at.isoformat(),
                        "entry_session": local.date().isoformat(),
                        "manual_retest": trade.manual_retest,
                        "manual_retest_request_id": str(retest.request_id) if retest else None,
                        "initial_loss_pct": str(initial_loss_pct),
                        "calendar_stop_loss_pct": str(trade.calendar_stop_loss_pct),
                        "calendar_debit": str(debit),
                        "liquidation_credit": str(liquidation),
                        "stop_credit": str(trade.calendar_stop_credit),
                        "message": message,
                        "quotes": {
                            name: quote.model_dump(mode="json") for name, quote in quotes.items()
                        },
                    },
                )
                raise MarketDataError(message)
            legs = [
                execution(trade, quotes[name], action, "ENTRY_CALENDAR", filled_at)
                for side in ("put", "call")
                for name, action in ((f"back_{side}", "BUY"), (f"short_{side}", "SELL"))
            ]
            evaluation = self.evaluation(trade, quotes, "ENTRY_CALENDAR", vix)
            trade.mark_price = liquidation
            trade.unrealized_pnl_dollars = (
                trade.mark_price - debit
            ) * trade.multiplier - trade.commissions_dollars
            trade.current_transform_credit = transformer_credit(quotes)
            trade.last_quote_at = min(quote.timestamp for quote in quotes.values())
            await self.store.commit_trade(trade, legs, evaluation)
            await self.csv.export()
            await self.advisor.submit(evaluation)
            self.last_error = None
            return trade

    def evaluation(
        self, trade: Trade, quotes: dict[str, Quote], kind: str, vix: Decimal | None
    ) -> Evaluation:
        front_iv = [
            quotes[f"short_{side}"].implied_vol
            for side in ("put", "call")
            if f"short_{side}" in quotes and quotes[f"short_{side}"].implied_vol is not None
        ]
        back_iv = [
            quotes[f"back_{side}"].implied_vol
            for side in ("put", "call")
            if f"back_{side}" in quotes and quotes[f"back_{side}"].implied_vol is not None
        ]
        front = (
            sum(value for value in front_iv if value is not None) / 2
            if len(front_iv) == 2
            else None
        )
        back = (
            sum(value for value in back_iv if value is not None) / 2 if len(back_iv) == 2 else None
        )
        credit = transformer_credit(quotes) if "back_put" in quotes else None
        return Evaluation(
            trade_id=trade.trade_id,
            timestamp=utcnow(),
            kind=kind,
            quotes=quotes,
            transformer_credit=credit,
            liquidation_credit=liquidation_credit(quotes) if "back_put" in quotes else None,
            target_credit=trade.target_credit,
            vix=vix,
            context={
                "front_iv": front,
                "back_iv": back,
                "front_back_iv_ratio": front / back if front is not None and back else None,
                "maximum_bid_ask_points": float(
                    max(quote.ask - quote.bid for quote in quotes.values())
                ),
                "calendar_stop_loss_pct": str(trade.calendar_stop_loss_pct),
                "calendar_stop_credit": str(trade.calendar_stop_credit),
            },
        )

    async def poll(self) -> None:
        async with self.lock:
            now = utcnow()
            if not self.market.is_open(now):
                return
            active = [trade for trade in await self.store.trades() if trade.status in ACTIVE]
            if not active:
                return
            vix = await self.vix()
            for trade in active:
                if trade.front_expiry < now.astimezone(EASTERN).date():
                    # Do not manufacture historical fills after missed expiration.
                    continue
                try:
                    specs = (
                        trade.contracts
                        if trade.status == Status.CALENDAR
                        else {
                            name: spec
                            for name, spec in trade.contracts.items()
                            if not name.startswith("back")
                        }
                    )
                    quotes = await self.provider.quotes(specs)
                    self.validate_quotes(quotes, specs)
                    if not self.market.is_open(utcnow()):
                        continue
                    await self.evaluate(trade, quotes, vix)
                except (MarketDataError, ValueError, ConnectionError, TimeoutError, OSError) as exc:
                    await self.error("quote evaluation", exc, trade.trade_id)
            await self.csv.export()

    async def evaluate(
        self, trade: Trade, quotes: dict[str, Quote], vix: Decimal | None = None
    ) -> None:
        now = utcnow()
        legs: list[LegEvent] = []
        evaluation = self.evaluation(trade, quotes, "TRANSFORM_EVALUATION", vix)
        trade.last_quote_at = min(quote.timestamp for quote in quotes.values())
        if trade.status == Status.CALENDAR:
            credit = transformer_credit(quotes)
            liquidation = liquidation_credit(quotes)
            trade.mark_price = liquidation
            trade.current_transform_credit = credit
            trade.unrealized_pnl_dollars = (
                liquidation - trade.calendar_debit_paid
            ) * trade.multiplier - trade.commissions_dollars
            _, close = self.market.bounds(trade.front_expiry)
            must_exit = (close - now).total_seconds() <= 300
            # Stop-loss takes priority over transform on the same sampled quote.
            if liquidation <= trade.calendar_stop_credit or must_exit:
                event = "STOP_LOSS" if not must_exit else "CALENDAR_TIME_EXIT"
                status = Status.STOP if not must_exit else Status.TIME
                legs = [
                    execution(trade, quotes[name], action, event, now)
                    for side in ("put", "call")
                    for name, action in ((f"back_{side}", "SELL"), (f"short_{side}", "BUY"))
                ]
                trade.commissions_dollars += trade.commission_per_leg * 4 * trade.quantity
                self.finish(
                    trade,
                    status,
                    (liquidation - trade.calendar_debit_paid) * trade.multiplier,
                    now,
                    f"{event}: liquidation credit {liquidation} points. "
                    + (
                        f"Bot-monitored loss threshold {trade.calendar_stop_loss_pct}% before fees."
                        if status == Status.STOP
                        else ""
                    ),
                )
                evaluation.kind = event
            elif credit >= trade.target_credit:
                trade.status = Status.CONDOR
                trade.transformed = True
                trade.transform_timestamp = now
                trade.minutes_to_transform = int(
                    (now - trade.entry_timestamp).total_seconds() // 60
                )
                trade.transformer_credit_received = credit
                trade.transform_gain_pct = (
                    (credit - trade.calendar_debit_paid) / trade.calendar_debit_paid * 100
                )
                trade.guaranteed_floor_profit = (
                    credit - trade.calendar_debit_paid - trade.wing_width
                )
                trade.max_tent_profit = credit - trade.calendar_debit_paid
                trade.commissions_dollars += trade.commission_per_leg * 4 * trade.quantity
                legs = [
                    execution(trade, quotes[name], action, "TRANSFORM_ORDER", now)
                    for side in ("put", "call")
                    for name, action in ((f"back_{side}", "SELL"), (f"outer_{side}", "BUY"))
                ]
                trade.mark_price = condor_close_debit(quotes)
                trade.unrealized_pnl_dollars = (
                    credit - trade.calendar_debit_paid - trade.mark_price
                ) * trade.multiplier - trade.commissions_dollars
                evaluation.kind = "TRANSFORM_ORDER"
        elif trade.status == Status.CONDOR:
            trade.mark_price = condor_close_debit(quotes)
            trade.unrealized_pnl_dollars = (
                (trade.transformer_credit_received or Decimal(0))
                - trade.calendar_debit_paid
                - trade.mark_price
            ) * trade.multiplier - trade.commissions_dollars
            evaluation.kind = "CONDOR_MARK"
        else:
            raise ValueError("Cannot evaluate a closed trade")
        await self.store.commit_trade(trade, legs, evaluation)
        if evaluation.kind != "CONDOR_MARK":
            await self.advisor.submit(evaluation)

    @staticmethod
    def finish(trade: Trade, status: Status, gross: Decimal, now: datetime, note: str) -> None:
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
    ) -> Trade:
        async with self.lock:
            trade = next(
                (item for item in await self.store.trades() if item.trade_id == trade_id), None
            )
            if trade is None:
                raise ValueError("Unknown trade")
            if trade.status not in ACTIVE:
                raise ValueError("Trade already closed")
            now = utcnow()
            _, close = self.market.bounds(trade.front_expiry)
            if now < close:
                raise ValueError("Official PM settlement cannot be applied before expiration close")
            if not value.is_finite() or value <= 0 or len(source.strip()) < 10:
                raise ValueError("A positive official SPXW settlement and its source are required")
            legs: list[LegEvent] = []
            if trade.status == Status.CONDOR:
                loss = condor_payoff_loss(trade, value)
                gross = (
                    (trade.transformer_credit_received or Decimal(0))
                    - trade.calendar_debit_paid
                    - loss
                ) * trade.multiplier
                status = (
                    Status.TENT
                    if loss == 0
                    else (Status.FLOOR if loss == trade.wing_width else Status.SLOPE)
                )
                names = [name for name in trade.contracts if not name.startswith("back")]
            else:
                # Recovery path: settle expired shorts, liquidate still-live back longs now.
                if not self.market.is_open(now):
                    raise ValueError(
                        "Expired calendar recovery requires an open session for back-long liquidation"
                    )
                _, back_close = self.market.bounds(trade.back_expiry)
                if now >= back_close:
                    raise ValueError(
                        "Back longs also expired; historical cashflow reconciliation required, no fabricated fill"
                    )
                specs = {
                    name: spec for name, spec in trade.contracts.items() if name.startswith("back")
                }
                quotes = await self.provider.quotes(specs)
                self.validate_quotes(quotes, specs)
                now = utcnow()
                if not self.market.is_open(now) or now >= back_close:
                    raise ValueError("Back-long liquidation window ended during quote sampling")
                sale = sum((quote.bid for quote in quotes.values()), Decimal(0))
                obligation = sum(
                    (
                        intrinsic(trade.contracts[f"short_{side}"].strike, kind, value)
                        for side, kind in (("put", "P"), ("call", "C"))
                    ),
                    Decimal(0),
                )
                gross = (sale - obligation - trade.calendar_debit_paid) * trade.multiplier
                legs.extend(
                    execution(trade, quote, "SELL", "CALENDAR_RECOVERY", now)
                    for quote in quotes.values()
                )
                trade.commissions_dollars += trade.commission_per_leg * 2 * trade.quantity
                status = Status.TIME
                names = ["short_put", "short_call"]
            for name in names:
                spec = trade.contracts[name]
                legs.append(
                    LegEvent(
                        trade_id=trade.trade_id,
                        timestamp=close,
                        event_type="EXPIRATION_SETTLEMENT",
                        contract=spec,
                        action="BUY" if name.startswith("short") else "SELL",
                        simulated_fill_price=intrinsic(spec.strike, spec.right, value),
                    )
                )
            trade.settlement_value = value
            trade.settlement_source = source.strip()
            trade.settlement_provenance = provenance
            label = (
                "Official SPXW PM settlement"
                if provenance == "verified_official"
                else "SPXW PM settlement PROXY"
            )
            self.finish(
                trade,
                status,
                gross,
                now,
                f"{label} {value} for {trade.front_expiry}; source: {source.strip()}.",
            )
            await self.store.commit_trade(trade, legs)
            await self.store.log(
                "settlement",
                {
                    "trade_id": trade_id,
                    "settlement_value": str(value),
                    "effective_timestamp": close.isoformat(),
                    "source": source.strip(),
                    "provenance": provenance,
                },
            )
            await self.csv.export()
            return trade

    async def settle_due(self) -> None:
        if not self.settings.auto_settle_from_ib_close:
            return
        now = utcnow()
        for trade in await self.store.trades():
            if trade.status not in ACTIVE:
                continue
            _, close = self.market.bounds(trade.front_expiry)
            if (now - close).total_seconds() < self.settings.settlement_delay_minutes * 60:
                continue
            if trade.status == Status.CALENDAR and not self.market.is_open(now):
                # Back longs must be sold using a real, current market quote.
                continue
            previous = self.settlement_attempts.get(trade.trade_id)
            if previous and (now - previous).total_seconds() < 300:
                continue
            self.settlement_attempts[trade.trade_id] = now
            try:
                value = await self.provider.settlement_close(trade.front_expiry)
                await self.settle(
                    trade.trade_id,
                    value,
                    f"IBKR SPX CBOE TRADES/RTH daily close for {trade.front_expiry}; "
                    "automatic closing-index proxy, NOT verified official SPXW exercise settlement",
                    provenance="ibkr_close_proxy",
                )
            except (MarketDataError, ValueError, ConnectionError, TimeoutError, OSError) as exc:
                await self.error("automatic settlement proxy", exc, trade.trade_id)

    async def configure(self, config: TradingConfig) -> None:
        async with self.lock:
            await self.store.save_config(config)
