import asyncio
from datetime import date
from decimal import Decimal

from ib_async import Index, Option
from ib_async.wrapper import RequestError

from app.core.market import MarketCalendar
from app.core.models import OptionSpec
from app.ibkr.client import IBClient, MarketDataError


def expiry_pairs(expiries: list[date], today: date) -> list[tuple[date, date]]:
    pairs = [
        (front, back)
        for front in expiries
        if 6 <= (front - today).days <= 12
        for back in expiries
        if 1 <= (back - front).days <= 4
    ]
    return sorted(
        pairs,
        key=lambda pair: (
            not (pair[0].weekday() == 4 and pair[1].weekday() == 0),
            pair[0].weekday() != 4,
            pair[0],
            pair[1],
        ),
    )


class ChainSelector:
    def __init__(self, client: IBClient, market: MarketCalendar) -> None:
        self.client = client
        self.market = market

    async def select(self, today: date, width: Decimal) -> dict[str, OptionSpec]:
        underlying = (await self.client.qualify([Index("SPX", "CBOE", "USD")]))[0]
        async with self.client.request_lock:
            try:
                chains = await asyncio.wait_for(
                    self.client.ib.reqSecDefOptParamsAsync("SPX", "", "IND", underlying.conId),
                    timeout=30,
                )
            except RequestError as exc:
                raise MarketDataError(f"IBKR chain request failed: {exc}") from exc
        candidates = [
            chain for chain in chains if chain.tradingClass == "SPXW" and chain.exchange == "SMART"
        ]
        if not candidates:
            raise MarketDataError("No SMART SPXW chain is available")
        chain = candidates[0]
        expiries = [
            date(int(item[:4]), int(item[4:6]), int(item[6:8])) for item in chain.expirations
        ]
        pairs = expiry_pairs(
            [expiry for expiry in expiries if self.market.is_session(expiry)], today
        )
        if not pairs:
            raise MarketDataError("No listed SPXW expiry pair satisfies 6–12 / 1–4 DTE")
        front, back = pairs[0]
        spot = float(await self.client.index("SPX"))
        selected: dict[str, OptionSpec] = {}
        for right in ("P", "C"):
            strikes = sorted(
                strike
                for strike in chain.strikes
                if (
                    spot * 0.94 <= strike <= spot if right == "P" else spot <= strike <= spot * 1.06
                )
            )
            count = min(len(strikes), self.client.settings.strike_scan_count)
            if count < 2:
                raise MarketDataError("Insufficient listed SPXW strikes")
            strikes = [
                strikes[round(index * (len(strikes) - 1) / (count - 1))] for index in range(count)
            ]
            contracts = await self.client.qualify(
                [
                    Option(
                        "SPX",
                        front.strftime("%Y%m%d"),
                        strike,
                        right,
                        "SMART",
                        multiplier="100",
                        currency="USD",
                        tradingClass="SPXW",
                    )
                    for strike in strikes
                ]
            )
            quotes = await self.client.quotes(
                {str(contract.conId): self.client.spec(contract) for contract in contracts},
                require_delta=True,
            )
            valid = [
                quote
                for quote in quotes.values()
                if quote.delta is not None
                and 0.30 <= abs(quote.delta) <= 0.40
                and (quote.delta < 0 if right == "P" else quote.delta > 0)
            ]
            if not valid:
                raise MarketDataError(f"No sampled {right} strike has live 30–40 delta")
            best = min(valid, key=lambda quote: abs(abs(quote.delta or 0) - 0.35))
            name = "put" if right == "P" else "call"
            selected[f"short_{name}"] = best.contract
            outer = best.contract.strike + (-width if right == "P" else width)
            required = await self.client.qualify(
                [
                    Option(
                        "SPX",
                        expiry.strftime("%Y%m%d"),
                        float(strike),
                        right,
                        "SMART",
                        multiplier="100",
                        currency="USD",
                        tradingClass="SPXW",
                    )
                    for expiry, strike in ((back, best.contract.strike), (front, outer))
                ]
            )
            selected[f"back_{name}"] = self.client.spec(required[0])
            selected[f"outer_{name}"] = self.client.spec(required[1])
        if selected["short_put"].strike >= selected["short_call"].strike:
            raise MarketDataError("Put strike must be below call strike")
        return selected
