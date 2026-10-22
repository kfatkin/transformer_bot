import asyncio
from datetime import date
from decimal import Decimal
from typing import Literal

from ib_async import Index, Option, OptionChain
from ib_async.wrapper import RequestError

from app.core.market import MarketCalendar
from app.core.models import OptionSpec
from app.ibkr.client import IBClient, MarketDataError


async def listed_chain(client: IBClient) -> OptionChain:
    underlying = (await client.qualify([Index("SPX", "CBOE", "USD")]))[0]
    async with client.request_lock:
        try:
            chains = await asyncio.wait_for(
                client.ib.reqSecDefOptParamsAsync("SPX", "", "IND", underlying.conId),
                timeout=30,
            )
        except RequestError as exc:
            raise MarketDataError(f"IBKR chain request failed: {exc}") from exc
    candidates = [
        chain for chain in chains if chain.tradingClass == "SPXW" and chain.exchange == "SMART"
    ]
    if not candidates:
        raise MarketDataError("No SMART SPXW chain is available")
    return candidates[0]


def scan_strikes(
    chain: OptionChain, spot: float, right: str, limit: int, *, nearest: bool = False
) -> list[float]:
    strikes = sorted(
        strike
        for strike in chain.strikes
        if (spot * 0.94 <= strike <= spot if right == "P" else spot <= strike <= spot * 1.06)
    )
    count = min(len(strikes), limit)
    if count < 2:
        raise MarketDataError("Insufficient listed SPXW strikes")
    if nearest:
        return sorted(strikes, key=lambda strike: abs(strike - spot))[:count]
    return [strikes[round(index * (len(strikes) - 1) / (count - 1))] for index in range(count)]


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
        chain = await listed_chain(self.client)
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
            strikes = scan_strikes(chain, spot, right, self.client.settings.strike_scan_count)
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


class ButterflySelector:
    def __init__(self, client: IBClient) -> None:
        self.client = client

    async def select(
        self, today: date, width: Decimal, right: Literal["P", "C"]
    ) -> dict[str, OptionSpec]:
        chain = await listed_chain(self.client)
        expiry = today.strftime("%Y%m%d")
        if expiry not in chain.expirations:
            raise MarketDataError(f"No listed SPXW same-day expiry for {today}")
        spot = float(await self.client.index("SPX"))
        strikes = scan_strikes(
            chain, spot, right, self.client.settings.strike_scan_count, nearest=True
        )
        contracts = await self.client.qualify(
            [
                Option(
                    "SPX",
                    expiry,
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
            and 0.30 <= abs(quote.delta) <= 0.50
            and (quote.delta < 0 if right == "P" else quote.delta > 0)
        ]
        if not valid:
            raise MarketDataError(f"No sampled {right} long has live 0.30-0.50 absolute delta")
        long = min(valid, key=lambda quote: abs(abs(quote.delta or 0) - 0.40)).contract
        step = width if right == "C" else -width
        wings = await self.client.qualify(
            [
                Option(
                    "SPX",
                    expiry,
                    float(long.strike + step * offset),
                    right,
                    "SMART",
                    multiplier="100",
                    currency="USD",
                    tradingClass="SPXW",
                )
                for offset in (1, 2)
            ]
        )
        return {
            "long": long,
            "short": self.client.spec(wings[0]),
            "outer": self.client.spec(wings[1]),
        }
