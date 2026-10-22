import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ib_async import IB, Contract, Index, Option, StartupFetchNONE, Ticker
from ib_async.wrapper import RequestError

from app.core.config import Settings
from app.core.market import EASTERN
from app.core.models import OptionSpec, Quote, utcnow

logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


@dataclass
class Subscription:
    contract: Contract
    ticker: Ticker
    seen: dict[int, datetime]
    handler: Callable[[Ticker], None]
    users: int = 1


class IBClient:
    """Market-data-only adapter: deliberately exposes no order API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ib = IB()
        self.ib.RaiseRequestErrors = True
        self.last_error: str | None = None
        self.ib.errorEvent += self._on_error
        self.connection_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.subscriptions: dict[int, Subscription] = {}

    @property
    def connected(self) -> bool:
        return self.ib.isConnected()

    def _on_error(self, req_id: int, code: int, message: str, *args: object) -> None:
        if code not in {2104, 2106, 2107, 2108, 2158}:
            self.last_error = f"IBKR {code} (request {req_id}): {message}"
            logger.warning(self.last_error)

    async def connect(self) -> None:
        async with self.connection_lock:
            if self.connected:
                return
            await self.ib.connectAsync(
                self.settings.ib_host,
                self.settings.ib_port,
                clientId=self.settings.ib_client_id,
                readonly=True,
                timeout=5,
                fetchFields=StartupFetchNONE,
            )
            self.ib.reqMarketDataType(1)
            self.last_error = None

    async def qualify(self, contracts: list[Contract]) -> list[Contract]:
        await self.connect()
        try:
            result = await asyncio.wait_for(self.ib.qualifyContractsAsync(*contracts), timeout=30)
        except RequestError as exc:
            raise MarketDataError(f"IBKR qualification failed: {exc}") from exc
        if len(result) != len(contracts) or any(
            not isinstance(item, Contract) or item.conId <= 0 for item in result
        ):
            raise MarketDataError("IBKR could not uniquely qualify every contract")
        return [item for item in result if isinstance(item, Contract)]

    @staticmethod
    def contract(spec: OptionSpec) -> Contract:
        return Option(
            symbol="SPX",
            lastTradeDateOrContractMonth=spec.expiry.strftime("%Y%m%d"),
            strike=float(spec.strike),
            right=spec.right,
            exchange="SMART",
            multiplier="100",
            currency="USD",
            tradingClass="SPXW",
            conId=spec.con_id,
        )

    @staticmethod
    def spec(contract: Contract) -> OptionSpec:
        if (
            contract.symbol != "SPX"
            or contract.tradingClass != "SPXW"
            or contract.multiplier != "100"
            or contract.secType != "OPT"
        ):
            raise MarketDataError("Only 100-multiplier SPXW options are permitted")
        if contract.right not in {"P", "C"}:
            raise MarketDataError("Expected a put or call")
        return OptionSpec(
            con_id=contract.conId,
            expiry=datetime.strptime(contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date(),
            strike=Decimal(str(contract.strike)),
            right="P" if contract.right == "P" else "C",
        )

    def _subscribe(self, contract: Contract) -> Subscription:
        existing = self.subscriptions.get(contract.conId)
        if existing:
            existing.users += 1
            return existing
        ticker = self.ib.reqMktData(contract, "", False, False)
        seen: dict[int, datetime] = {}

        def on_update(updated: Ticker) -> None:
            for tick in updated.ticks:
                if tick.tickType in {1, 2, 4}:
                    seen[tick.tickType] = tick.time

        ticker.updateEvent.connect(on_update)
        subscription = Subscription(contract, ticker, seen, on_update)
        self.subscriptions[contract.conId] = subscription
        return subscription

    def _release(self, subscription: Subscription) -> None:
        # A contract may be shared by entry and monitoring; never cancel the other sampler.
        subscription.users -= 1
        if subscription.users == 0:
            subscription.ticker.updateEvent.disconnect(subscription.handler)
            del self.subscriptions[subscription.contract.conId]
            if self.connected:
                self.ib.cancelMktData(subscription.contract)

    async def _sample(
        self, contracts: list[Contract], require_delta: bool = False
    ) -> list[tuple[Ticker, datetime]]:
        await self.connect()
        acquired: list[Subscription] = []
        try:
            for contract in contracts:
                acquired.append(self._subscribe(contract))
            deadline = asyncio.get_running_loop().time() + self.settings.quote_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if not self.connected:
                    raise ConnectionError("IBKR disconnected during quote sampling")
                now = utcnow()
                ready = True
                for sample in acquired:
                    ticker = sample.ticker
                    required = {1, 2} if sample.contract.secType == "OPT" else {4}
                    if (
                        ticker.marketDataType != 1
                        or any(
                            sample.seen.get(kind) is None
                            or not 0
                            <= (now - sample.seen[kind]).total_seconds()
                            <= self.settings.max_quote_age_seconds
                            for kind in required
                        )
                        or (
                            require_delta
                            and (
                                ticker.modelGreeks is None
                                or ticker.modelGreeks.delta is None
                                or not math.isfinite(ticker.modelGreeks.delta)
                            )
                        )
                    ):
                        ready = False
                        break
                if ready:
                    return [
                        (
                            sample.ticker,
                            min(
                                sample.seen[kind]
                                for kind in ({1, 2} if sample.contract.secType == "OPT" else {4})
                            ),
                        )
                        for sample in acquired
                    ]
                await asyncio.sleep(0.1)
            raise MarketDataError(
                "Live bid/ask or Greeks timed out; check SPX subscriptions and API permissions"
                + (f". {self.last_error}" if self.last_error else "")
            )
        finally:
            for sample in acquired:
                self._release(sample)

    async def quotes(
        self, specs: dict[str, OptionSpec], require_delta: bool = False
    ) -> dict[str, Quote]:
        contracts = [self.contract(spec) for spec in specs.values()]
        samples = await self._sample(contracts, require_delta)
        output = {}
        for (name, spec), (ticker, timestamp) in zip(specs.items(), samples, strict=True):
            if not all(math.isfinite(value) for value in (ticker.bid, ticker.ask)):
                raise MarketDataError(f"{name}: non-finite bid or ask")
            if ticker.bid < 0 or ticker.ask <= 0 or ticker.ask < ticker.bid:
                raise MarketDataError(f"{name}: missing or crossed bid/ask")
            if ticker.ask - ticker.bid > self.settings.max_spread_points:
                raise MarketDataError(f"{name}: spread exceeds configured quality limit")
            greeks = ticker.modelGreeks
            output[name] = Quote(
                contract=spec,
                timestamp=timestamp,
                bid=Decimal(str(ticker.bid)),
                ask=Decimal(str(ticker.ask)),
                delta=greeks.delta
                if greeks and greeks.delta is not None and math.isfinite(greeks.delta)
                else None,
                implied_vol=greeks.impliedVol
                if greeks and greeks.impliedVol is not None and math.isfinite(greeks.impliedVol)
                else None,
                market_data_type=ticker.marketDataType,
                bid_size=Decimal(str(ticker.bidSize))
                if math.isfinite(ticker.bidSize) and ticker.bidSize >= 0
                else None,
                ask_size=Decimal(str(ticker.askSize))
                if math.isfinite(ticker.askSize) and ticker.askSize >= 0
                else None,
            )
        return output

    async def index(self, symbol: str) -> Decimal:
        contracts = await self.qualify([Index(symbol, "CBOE", "USD")])
        samples = await self._sample(contracts)
        value = samples[0][0].last
        if not math.isfinite(value) or value <= 0:
            raise MarketDataError(f"Invalid live {symbol} index price")
        return Decimal(str(value))

    async def settlement_close(self, expiry: date) -> Decimal:
        contracts = await self.qualify([Index("SPX", "CBOE", "USD")])
        end = datetime.combine(expiry + timedelta(days=1), datetime.min.time(), EASTERN)
        end = min(end, utcnow())
        async with self.request_lock:
            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    contracts[0],
                    endDateTime=end,
                    durationStr="3 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    timeout=20,
                )
            except RequestError as exc:
                raise MarketDataError(f"IBKR settlement proxy request failed: {exc}") from exc
        for bar in bars:
            bar_day = bar.date
            if isinstance(bar_day, datetime):
                if bar_day.tzinfo is None:
                    raise MarketDataError("Historical datetime lacks a timezone")
                bar_day = bar_day.astimezone(EASTERN).date()
            if bar_day == expiry:
                if not math.isfinite(bar.close) or bar.close <= 0:
                    raise MarketDataError("Invalid historical SPX closing-index proxy")
                return Decimal(str(bar.close))
        raise MarketDataError(
            f"No historical SPX daily close for {expiry}; settlement stays pending"
        )

    async def session_open(self, day: date) -> Decimal:
        contracts = await self.qualify([Index("SPX", "CBOE", "USD")])
        opening = datetime.combine(day, datetime.min.time(), EASTERN).replace(hour=9, minute=30)
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                contracts[0],
                endDateTime=opening + timedelta(minutes=1),
                durationStr="120 S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                timeout=20,
            )
        except RequestError as exc:
            raise MarketDataError(f"IBKR opening-index request failed: {exc}") from exc
        for bar in bars:
            if not isinstance(bar.date, datetime) or bar.date.tzinfo is None:
                continue
            if bar.date.astimezone(EASTERN) == opening:
                if not math.isfinite(bar.open) or bar.open <= 0:
                    raise MarketDataError("Invalid 09:30 SPX opening-index value")
                return Decimal(str(bar.open))
        raise MarketDataError(f"No exact 09:30 regular-session SPX opening bar for {day}")

    def disconnect(self) -> None:
        self.ib.disconnect()
