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


class ManualIB(FakeIB):
    def __init__(self):
        super().__init__()
        self.tickers = {}
        self.requests = []
        self.connected = True

    def isConnected(self):
        return self.connected

    def reqMktData(self, contract, *args):
        self.requests.append(contract.conId)
        ticker = Ticker(contract=contract, marketDataType=1)
        self.tickers[contract.conId] = ticker
        return ticker

    def publish(self, con_id):
        ticker = self.tickers[con_id]
        ticker.bid, ticker.ask = 1, 1.1
        ticker.bidSize = ticker.askSize = 10
        now = datetime.now(timezone.utc)
        ticker.ticks = [TickData(now, 1, 1, 10), TickData(now, 2, 1.1, 10)]
        ticker.updateEvent.emit(ticker)


async def test_shared_subscription_cancellation_preserves_other_sampler():
    client = IBClient(Settings(quote_timeout_seconds=2))
    fake = ManualIB()
    client.ib = fake
    spec = OptionSpec(con_id=10, expiry=date(2026, 10, 30), strike=6000, right="P")
    first = asyncio.create_task(client.quotes({"put": spec}))
    second = asyncio.create_task(client.quotes({"put": spec}))
    try:
        await asyncio.sleep(0)
        assert fake.requests == [10]
        assert client.subscriptions[10].users == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert fake.cancelled == []
        assert client.subscriptions[10].users == 1
        fake.publish(10)
        assert (await second)["put"].bid == 1
        assert fake.cancelled == [10]
        assert client.subscriptions == {}
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_quote_disconnect_cleans_pool_and_allows_fresh_subscription():
    client = IBClient(Settings(quote_timeout_seconds=2))
    fake = ManualIB()
    client.ib = fake
    spec = OptionSpec(con_id=10, expiry=date(2026, 10, 30), strike=6000, right="P")
    first = asyncio.create_task(client.quotes({"put": spec}))
    await asyncio.sleep(0)
    fake.connected = False
    with pytest.raises(ConnectionError, match="disconnected"):
        await first
    assert client.subscriptions == {}
    fake.connected = True
    second = asyncio.create_task(client.quotes({"put": spec}))
    await asyncio.sleep(0)
    fake.publish(10)
    assert (await second)["put"].ask == Decimal("1.1")
    assert fake.requests == [10, 10]


@pytest.mark.parametrize(
    "timestamp,value,valid",
    [
        (datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc), 6000, True),
        (datetime(2026, 10, 19, 13, 31, tzinfo=timezone.utc), 6000, False),
        (datetime(2026, 10, 19, 13, 30), 6000, False),
        (datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc), 6000, False),
        (datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc), float("nan"), False),
    ],
)
async def test_exact_opening_bar_no_fallback(timestamp, value, valid):
    client = IBClient(Settings())

    async def qualify(contracts):
        return [Index("SPX", "CBOE", "USD", conId=100)]

    async def historical(contract, **kwargs):
        assert kwargs["barSizeSetting"] == "1 min"
        assert kwargs["useRTH"] is True
        assert kwargs["formatDate"] == 2
        assert kwargs["endDateTime"].hour == 9
        assert kwargs["endDateTime"].minute == 31
        return [BarData(date=timestamp, open=value, close=9999)]

    client.qualify = qualify
    fake = FakeIB()
    fake.reqHistoricalDataAsync = historical
    client.ib = fake
    if valid:
        assert await client.session_open(date(2026, 10, 19)) == 6000
    else:
        with pytest.raises(MarketDataError, match="09:30"):
            await client.session_open(date(2026, 10, 19))
