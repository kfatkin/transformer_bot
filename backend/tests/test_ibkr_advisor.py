import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from ib_async import Index, Ticker
from ib_async.objects import BarData
from ib_async.objects import TickData

from app.advisor.ai_agent import Advisor
from app.core.config import Settings
from app.core.models import Evaluation, OptionSpec, Quote
from app.ibkr.client import IBClient, MarketDataError


class FakeIB:
    def __init__(self, data_type=1, tick_types=(1, 2), old=False, bid=1, ask=1.1):
        self.data_type = data_type
        self.tick_types = tick_types
        self.old = old
        self.bid = bid
        self.ask = ask
        self.cancelled = []

    def isConnected(self):
        return True

    def reqMktData(self, contract, *args):
        ticker = Ticker(
            contract=contract,
            bid=self.bid,
            ask=self.ask,
            bidSize=10,
            askSize=10,
            marketDataType=self.data_type,
        )

        def publish():
            now = datetime.now(timezone.utc)
            ticker.bid = self.bid
            ticker.ask = self.ask
            ticker.bidSize = 10
            ticker.askSize = 10
            if self.old:
                now -= timedelta(minutes=1)
            ticker.ticks = [
                TickData(now, kind, self.bid if kind == 1 else self.ask, 10)
                for kind in self.tick_types
            ]
            ticker.updateEvent.emit(ticker)

        asyncio.get_running_loop().call_soon(publish)
        return ticker

    def cancelMktData(self, contract):
        self.cancelled.append(contract.conId)


@pytest.mark.parametrize(
    "kind,ticks,old,success",
    [
        (1, (1, 2), False, True),
        (3, (1, 2), False, False),
        (1, (1,), False, False),
        (1, (), False, False),
        (1, (1, 2), True, False),
    ],
)
async def test_live_adapter_requires_two_fresh_price_ticks(kind, ticks, old, success):
    client = IBClient(Settings(quote_timeout_seconds=2))
    fake = FakeIB(kind, ticks, old)
    client.ib = fake
    spec = OptionSpec(con_id=10, expiry=date(2026, 10, 30), strike=6000, right="P")
    if success:
        quotes = await client.quotes({"put": spec})
        assert quotes["put"].bid == Decimal(1)
        assert quotes["put"].mid == Decimal("1.05")
        assert quotes["put"].ask_size == 10
    else:
        with pytest.raises(MarketDataError, match="timed out"):
            await client.quotes({"put": spec})
    assert fake.cancelled == [10]


async def test_adapter_rejects_wide_spread():
    client = IBClient(Settings(quote_timeout_seconds=2))
    client.ib = FakeIB(bid=1, ask=4)
    spec = OptionSpec(con_id=10, expiry=date(2026, 10, 30), strike=6000, right="P")
    with pytest.raises(MarketDataError, match="spread"):
        await client.quotes({"put": spec})


async def test_insufficient_size_never_creates_entry(lab):
    engine, provider, _ = lab
    original = provider.quotes

    async def small_quotes(specs, require_delta=False):
        result = await original(specs, require_delta)
        for quote in result.values():
            quote.bid_size = Decimal(0)
        return result

    provider.quotes = small_quotes
    with pytest.raises(MarketDataError, match="size"):
        await engine.entry(manual=True)
    assert await engine.store.trades() == []
    assert await engine.store.logs("leg") == []


@pytest.mark.parametrize(
    "provider,path,payload",
    [
        (
            "ollama",
            "/api/generate",
            {"response": '{"sentences":["Front IV is higher.","Threshold is met."]}'},
        ),
        (
            "openai",
            "/v1/chat/completions",
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"sentences":["Front IV is higher.","Threshold is met."]}'
                        }
                    }
                ]
            },
        ),
        (
            "anthropic",
            "/v1/messages",
            {"content": [{"text": '{"sentences":["Front IV is higher.","Threshold is met."]}'}]},
        ),
    ],
)
async def test_advisor_adapters(provider, path, payload, lab):
    engine, _, clock = lab
    settings = Settings(
        advisor_enabled=True,
        advisor_provider=provider,
        advisor_base_url="http://fixture.invalid",
        advisor_model="fixture",
    )
    advisor = Advisor(settings, engine.store)
    evaluation = Evaluation(
        trade_id="fixture",
        timestamp=clock[0],
        kind="TRANSFORM_ORDER",
        quotes={},
        transformer_credit=Decimal(8),
        target_credit=Decimal(7),
    )

    def handler(request):
        assert request.url.path == path
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await advisor.reason(client, evaluation) == "Front IV is higher. Threshold is met."


async def test_advisor_failure_is_explicit(lab):
    engine, _, clock = lab
    advisor = Advisor(Settings(advisor_enabled=True), engine.store)
    evaluation = Evaluation(
        trade_id="fixture",
        timestamp=clock[0],
        kind="ENTRY_CALENDAR",
        quotes={},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await advisor.reason(client, evaluation)
    quote = Quote(
        contract=OptionSpec(con_id=1, expiry=date(2026, 10, 30), strike=6000, right="P"),
        timestamp=clock[0],
        bid=1,
        ask=2,
    )
    assert quote.mid == Decimal("1.5")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"response": '{"sentences":["Only one."]}'})
        )
    ) as client:
        with pytest.raises(ValueError, match="two complete sentences"):
            await advisor.reason(client, evaluation)


async def test_audit_timestamps_keep_utc_offset(lab):
    engine, _, _ = lab
    await engine.store.log("error", {"component": "fixture", "message": "test"})
    assert (await engine.store.logs("error"))[0]["logged_at"].endswith("+00:00")


@pytest.mark.parametrize("available", [True, False])
async def test_historical_proxy_matches_exact_expiration_date(available):
    client = IBClient(Settings())

    async def qualify(contracts):
        return [Index("SPX", "CBOE", "USD", conId=100)]

    async def historical(contract, **kwargs):
        assert kwargs["useRTH"] is True
        assert kwargs["barSizeSetting"] == "1 day"
        assert kwargs["whatToShow"] == "TRADES"
        assert kwargs["endDateTime"].tzinfo is not None
        return [
            BarData(date=date(2026, 10, 29), close=5800),
            BarData(date=date(2026, 10, 30) if available else date(2026, 10, 28), close=6000),
        ]

    client.qualify = qualify
    fake = FakeIB()
    fake.reqHistoricalDataAsync = historical
    client.ib = fake
    if available:
        assert await client.settlement_close(date(2026, 10, 30)) == Decimal(6000)
    else:
        with pytest.raises(MarketDataError, match="stays pending"):
            await client.settlement_close(date(2026, 10, 30))
