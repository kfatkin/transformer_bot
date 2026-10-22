import asyncio
import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.engine.butterfly_engine as butterfly_module
from app.api.routes import snapshot
from app.core.butterfly import ButterflyStatus
from app.core.config import ButterflyConfig, Settings
from app.core.models import OptionSpec
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.butterfly_engine import ButterflyEngine
from app.ibkr.client import MarketDataError
from app.main import create_app
from conftest import FakeProvider


class ButterflyProvider(FakeProvider):
    def __init__(self, clock):
        super().__init__(clock)
        self.spot = Decimal(5990)
        self.opening = Decimal(6000)
        self.prices = {"long": ("4", "4.1"), "short": ("2.1", "2.2"), "outer": (".2", ".3")}

    async def session_open(self, day):
        if self.fail:
            raise MarketDataError("No exact 09:30 opening bar")
        return self.opening

    async def index(self, symbol):
        return Decimal(20) if symbol == "VIX" else self.spot


class ButterflySelector:
    async def select(self, today, width, right):
        step = width if right == "C" else -width
        return {
            name: OptionSpec(
                con_id=100 + index, expiry=today, strike=Decimal(6000) + step * index, right=right
            )
            for index, name in enumerate(("long", "short", "outer"))
        }


@pytest.fixture
async def butterfly_lab(lab, monkeypatch):
    calendar, _, clock = lab
    clock[0] = clock[0].replace(minute=0)
    monkeypatch.setattr(butterfly_module, "utcnow", lambda: clock[0])
    provider = ButterflyProvider(clock)
    engine = ButterflyEngine(
        calendar.store,
        provider,
        ButterflySelector(),
        calendar.market,
        calendar.csv,
        calendar.advisor,
        calendar.settings,
    )
    await engine.configure(ButterflyConfig(enabled=True))
    return engine, provider, clock, calendar


def funded(provider, credit="2.026"):
    bid = Decimal(credit) + Decimal(".3")
    provider.prices.update(long=("5", "5.1"), short=(bid, bid + Decimal(".1")))


@pytest.mark.parametrize("spot,right", [(5990, "C"), (6010, "P")])
async def test_contrarian_entry_exact_fills_and_independent_daily_limit(butterfly_lab, spot, right):
    engine, provider, clock, calendar = butterfly_lab
    provider.spot = Decimal(spot)
    trade = await engine.entry()
    assert trade.right == right
    assert trade.initial_debit == 2
    assert trade.stop_credit == Decimal("1.4")
    assert trade.target_credit == Decimal("2.026")
    assert trade.opening_index == 6000
    legs = await engine.store.logs("leg")
    assert [leg["action"] for leg in legs] == ["BUY", "SELL"]
    assert [leg["simulated_fill_price"] for leg in legs] == ["4.1", "2.1"]
    assert all(leg["quantity"] == 1 for leg in legs)
    with pytest.raises(ValueError, match="already"):
        await engine.entry()
    clock[0] = clock[0].replace(minute=30)
    await calendar.entry(manual=True)
    assert len(await engine.store.trades()) == len(await engine.store.butterflies()) == 1
    contents = await engine.csv.contents()
    assert len(list(csv.DictReader(io.StringIO(contents["legs_execution_log.csv"])))) == 4
    assert len(list(csv.DictReader(io.StringIO(contents["butterfly_legs_execution_log.csv"])))) == 2
    state = await snapshot(calendar, True, None, engine)
    assert state["metrics"]["total_trades"] == state["butterfly_metrics"]["total_trades"] == 1
    assert {row["strategy"] for row in state["trades"]} == {"DOUBLE_CALENDAR", "ZERO_DTE_BUTTERFLY"}


@pytest.mark.parametrize("change", ["flat", "missing", "delta", "wide_loss", "size", "late_sample"])
async def test_invalid_entry_never_records_fills(butterfly_lab, change):
    engine, provider, clock, _ = butterfly_lab
    original = provider.quotes
    if change == "flat":
        provider.spot = provider.opening
    elif change == "missing":
        provider.fail = True
    elif change == "wide_loss":
        provider.prices["long"] = ("3.6", "4.1")

    async def quotes(specs, require_delta=False):
        result = await original(specs, require_delta)
        if change == "delta":
            result["long"].delta = -0.4
        elif change == "size":
            result["long"].ask_size = Decimal(0)
        elif change == "late_sample":
            clock[0] += timedelta(minutes=1)
        return result

    provider.quotes = quotes
    with pytest.raises((ValueError, MarketDataError)):
        await engine.entry()
    assert await engine.store.butterflies() == []
    assert await engine.store.logs("leg") == []
    if change == "wide_loss":
        rejection = (await engine.store.logs("entry_rejection"))[0]
        assert rejection["strategy"] == "ZERO_DTE_BUTTERFLY"
        assert len(rejection["quotes"]) == 3


@pytest.mark.parametrize(
    "delta,allowed", [(0.30, True), (0.50, True), (0.299, False), (0.501, False)]
)
async def test_fresh_long_delta_inclusive_boundaries(butterfly_lab, delta, allowed):
    engine, provider, _, _ = butterfly_lab
    original = provider.quotes

    async def quotes(specs, require_delta=False):
        result = await original(specs, require_delta)
        result["long"].delta = delta
        return result

    provider.quotes = quotes
    if allowed:
        assert (await engine.entry()).status == ButterflyStatus.DEBIT
    else:
        with pytest.raises(MarketDataError, match="delta"):
            await engine.entry()
        assert await engine.store.logs("leg") == []


@pytest.mark.parametrize(
    "at,allowed",
    [
        (datetime(2026, 10, 19, 13, 59, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 19, 14, 0, 59, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 19, 14, 1, tzinfo=timezone.utc), False),
        (datetime(2026, 12, 25, 15, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 18, 14, tzinfo=timezone.utc), False),
    ],
)
async def test_exact_entry_minute_holidays_and_no_catchup(butterfly_lab, at, allowed):
    engine, _, clock, _ = butterfly_lab
    clock[0] = at
    if allowed:
        assert (await engine.entry()).entry_timestamp == at
    else:
        with pytest.raises(ValueError, match="10:00"):
            await engine.entry()


async def test_fee_funded_boundary_and_quantity_aware_settlement(butterfly_lab):
    engine, provider, clock, _ = butterfly_lab
    await engine.configure(ButterflyConfig(enabled=True, quantity=3))
    trade = await engine.entry()
    funded(provider, "2.025")
    await engine.poll()
    assert (await engine.store.butterflies())[0].status == ButterflyStatus.DEBIT
    funded(provider)
    clock[0] += timedelta(seconds=20)
    await engine.poll()
    trade = (await engine.store.butterflies())[0]
    assert trade.status == ButterflyStatus.BUTTERFLY
    assert trade.guaranteed_floor_profit * trade.multiplier - trade.commissions_dollars == 0
    assert trade.commissions_dollars == Decimal("7.8")
    assert len(await engine.store.logs("leg")) == 4
    await engine.poll()
    assert len(await engine.store.logs("leg")) == 4
    clock[0] = clock[0].replace(hour=20)
    settled = await engine.settle(trade.trade_id, Decimal(6005), "Fixture verified SPXW PM")
    assert settled.status == ButterflyStatus.TENT
    assert settled.final_pnl_dollars == 1500
    assert settled.settlement_provenance == "verified_official"
    legs = (await engine.store.logs("leg"))[-3:]
    assert [leg["quantity"] for leg in legs] == [3, 6, 3]
    with pytest.raises(ValueError, match="closed"):
        await engine.settle(trade.trade_id, Decimal(6005), "Fixture verified SPXW PM")


@pytest.mark.parametrize(
    "spot,right,value,pnl,status",
    [
        (5990, "C", 5990, "0", ButterflyStatus.FLOOR),
        (5990, "C", 6015, "0", ButterflyStatus.FLOOR),
        (5990, "C", 6002, "200", ButterflyStatus.SLOPE),
        (6010, "P", 5995, "500", ButterflyStatus.TENT),
        (6010, "P", 5992, "200", ButterflyStatus.SLOPE),
        (6010, "P", 5985, "0", ButterflyStatus.FLOOR),
    ],
)
async def test_both_rights_entire_butterfly_payoff(butterfly_lab, spot, right, value, pnl, status):
    engine, provider, clock, _ = butterfly_lab
    provider.spot = Decimal(spot)
    trade = await engine.entry()
    assert trade.right == right
    funded(provider)
    await engine.poll()
    clock[0] = clock[0].replace(hour=20)
    result = await engine.settle(trade.trade_id, Decimal(value), "Fixture verified SPXW PM")
    assert result.status == status
    assert result.final_pnl_dollars == Decimal(pnl)


@pytest.mark.parametrize("bid,pnl", [("3.6", "-62.6"), ("3.0", "-122.6")])
async def test_stop_is_natural_sampled_liquidation_before_fees(butterfly_lab, bid, pnl):
    engine, provider, _, _ = butterfly_lab
    await engine.entry()
    provider.prices["long"] = (bid, Decimal(bid) + Decimal(".1"))
    await engine.poll()
    trade = (await engine.store.butterflies())[0]
    assert trade.status == ButterflyStatus.STOP
    assert trade.final_pnl_dollars == Decimal(pnl)
    assert [leg["action"] for leg in (await engine.store.logs("leg"))[-2:]] == ["SELL", "BUY"]


@pytest.mark.parametrize("day,close_hour", [(date(2026, 10, 19), 20), (date(2026, 11, 27), 18)])
async def test_timed_exit_uses_actual_close_and_precedes_transform(butterfly_lab, day, close_hour):
    engine, provider, clock, _ = butterfly_lab
    clock[0] = datetime(
        day.year, day.month, day.day, 14 if day.month == 10 else 15, tzinfo=timezone.utc
    )
    await engine.entry()
    clock[0] = clock[0].replace(hour=close_hour - 1, minute=44, second=59)
    await engine.poll()
    assert (await engine.store.butterflies())[0].status == ButterflyStatus.DEBIT
    funded(provider)
    clock[0] += timedelta(seconds=1)
    await engine.poll()
    trade = (await engine.store.butterflies())[0]
    assert trade.status == ButterflyStatus.TIME
    assert not trade.transformed


async def test_proxy_settlement_missed_time_exit_is_explicit_not_fake_fill(butterfly_lab):
    engine, provider, clock, _ = butterfly_lab
    await engine.entry()
    clock[0] = clock[0].replace(hour=20, minute=30)
    await engine.settle_due()
    trade = (await engine.store.butterflies())[0]
    assert trade.status == ButterflyStatus.EXPIRED
    assert trade.settlement_provenance == "ibkr_close_proxy"
    assert trade.final_pnl_dollars == Decimal("-201.30")
    assert (await engine.store.logs("error"))[0]["component"] == "0DTE missed time exit"
    assert all(leg["event_type"] != "DEBIT_TIME_EXIT" for leg in await engine.store.logs("leg"))


async def test_disabled_entries_restart_and_entry_window_do_not_disable_monitoring(butterfly_lab):
    engine, provider, clock, _ = butterfly_lab
    first = await engine.entry()
    await engine.configure(ButterflyConfig(enabled=False))
    restarted = ButterflyEngine(
        engine.store,
        provider,
        engine.selector,
        engine.market,
        engine.csv,
        engine.advisor,
        engine.settings,
    )
    funded(provider)
    await restarted.poll()
    assert (await engine.store.butterflies())[0].transformed
    await engine.configure(ButterflyConfig(enabled=True))
    # Session 14 is allowed; session 15 is not, measured from first successful entry.
    sessions = engine.market.calendar.sessions_in_range(
        first.entry_session.isoformat(), "2026-11-20"
    )
    clock[0] = datetime.combine(sessions[13].date(), datetime.min.time(), timezone.utc).replace(
        hour=15
    )
    await engine.entry()
    clock[0] = datetime.combine(sessions[14].date(), datetime.min.time(), timezone.utc).replace(
        hour=15
    )
    with pytest.raises(ValueError, match="window completed"):
        await engine.entry()


async def test_new_store_loads_persisted_butterfly_and_frozen_trade_parameters(butterfly_lab):
    engine, provider, _, _ = butterfly_lab
    original = await engine.entry()
    await engine.configure(
        ButterflyConfig(enabled=False, quantity=3, wing_width=10, commission_per_leg=1)
    )
    store = Store(engine.settings.data_dir)
    await store.initialize()
    try:
        restarted = ButterflyEngine(
            store,
            provider,
            engine.selector,
            engine.market,
            CSVLogger(store, engine.settings.data_dir / "csv"),
            engine.advisor,
            engine.settings,
        )
        funded(provider)
        await restarted.poll()
        trade = (await store.butterflies())[0]
        assert trade.trade_id == original.trade_id
        assert trade.quantity == 1 and trade.wing_width == 5
        assert trade.commission_per_leg == Decimal(".65")
        assert trade.status == ButterflyStatus.BUTTERFLY
        assert (await store.butterfly_config()).enabled is False
    finally:
        await store.engine.dispose()


@pytest.mark.parametrize("stage", ["prepare", "entry"])
async def test_calendar_selection_does_not_block_existing_position_monitoring(butterfly_lab, stage):
    engine, _, clock, calendar = butterfly_lab
    await calendar.entry(manual=True)
    clock[0] += timedelta(days=1, minutes=30)
    entered, release = asyncio.Event(), asyncio.Event()
    original = calendar.selector.select

    async def blocked_select(today, width):
        entered.set()
        await release.wait()
        return await original(today, width)

    calendar.selector.select = blocked_select
    task = asyncio.create_task(
        calendar.prepare() if stage == "prepare" else calendar.entry(manual=True)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock[0] += timedelta(seconds=20)
        await asyncio.wait_for(calendar.poll(), timeout=1)
        assert (await calendar.store.trades())[0].last_quote_at == clock[0]
    finally:
        release.set()
        await task


async def test_butterfly_selection_does_not_hold_position_lock(butterfly_lab):
    engine, _, _, _ = butterfly_lab
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.selector.select

    async def blocked_select(today, width, right):
        entered.set()
        await release.wait()
        return await original(today, width, right)

    engine.selector.select = blocked_select
    task = asyncio.create_task(engine.entry())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(engine.poll(), timeout=1)
        assert not engine.lock.locked()
    finally:
        release.set()
        await task


async def test_failed_adjustment_is_atomic_and_preserves_debit_position(butterfly_lab):
    engine, provider, _, _ = butterfly_lab
    await engine.entry()
    funded(provider)
    original = provider.quotes

    async def no_outer_size(specs, require_delta=False):
        result = await original(specs, require_delta)
        result["outer"].ask_size = Decimal(0)
        return result

    provider.quotes = no_outer_size
    await engine.poll()
    trade = (await engine.store.butterflies())[0]
    assert trade.status == ButterflyStatus.DEBIT
    assert trade.commissions_dollars == Decimal("1.3")
    assert len(await engine.store.logs("leg")) == 2
    assert "size" in (await engine.store.logs("error"))[0]["message"]


def test_api_routes_butterfly_settlement_and_preserves_strategy_audit(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 19, 14, tzinfo=timezone.utc)]
    monkeypatch.setattr(butterfly_module, "utcnow", lambda: clock[0])
    application = create_app(Settings(data_dir=tmp_path), run_scheduler=False)
    with TestClient(application, base_url="http://localhost") as client:
        engine = application.state.butterfly
        provider = ButterflyProvider(clock)
        engine.provider, engine.selector = provider, ButterflySelector()
        assert client.portal is not None
        client.portal.call(engine.configure, ButterflyConfig(enabled=True))
        trade = client.portal.call(engine.entry)
        funded(provider)
        client.portal.call(engine.poll)
        body = {
            "value": "6005",
            "source": "Fixture verified SPXW PM",
            "confirmed_official_spxw_pm": True,
        }
        endpoint = f"/api/trades/{trade.trade_id}/settlement"
        assert client.post(endpoint, json=body).status_code == 409
        clock[0] = clock[0].replace(hour=20)
        response = client.post(endpoint, json=body)
        assert response.status_code == 200
        assert Decimal(response.json()["final_pnl_dollars"]) == 500
        assert response.json()["settlement_provenance"] == "verified_official"
        assert client.get("/api/state").json()["metrics"]["total_trades"] == 0
        assert (
            len(
                list(
                    csv.DictReader(
                        io.StringIO(client.get("/api/csv/butterfly_legs_execution_log.csv").text)
                    )
                )
            )
            == 7
        )


async def test_scheduler_has_independent_twenty_second_jobs_and_ten_am_entry(butterfly_lab):
    from app.engine.scheduler import Scheduler

    engine, _, _, calendar = butterfly_lab

    class RecordingScheduler:
        def __init__(self):
            self.jobs = {}

        def add_job(self, action, *args, **kwargs):
            self.jobs[kwargs["id"]] = (action, args, kwargs)

        def start(self):
            pass

    scheduler = Scheduler(calendar, None, engine)
    scheduler.scheduler = RecordingScheduler()
    scheduler.start()
    jobs = scheduler.scheduler.jobs
    assert jobs["poll"][2]["seconds"] == jobs["butterfly-poll"][2]["seconds"] == 20
    assert jobs["poll"][0] != jobs["butterfly-poll"][0]
    assert "minute='0'" in str(jobs["butterfly-entry"][1][0])
    assert "minute='30'" in str(jobs["entry"][1][0])
