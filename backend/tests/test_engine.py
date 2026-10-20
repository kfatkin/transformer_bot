import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zipfile import ZipFile

import pytest

from app.core.config import TradingConfig
from app.core.models import Status, Trade
from app.engine.csv_logger import LEG_COLUMNS, SUMMARY_COLUMNS
from app.engine.math import condor_payoff_loss
from app.ibkr.chains import expiry_pairs
from app.ibkr.client import MarketDataError
from conftest import transform_prices


async def test_entry_exact_prices_audit_and_duplicate(lab):
    engine, provider, clock = lab
    trade = await engine.entry(manual=True)
    assert trade.calendar_debit_paid == Decimal("2.2")
    assert trade.capital == 220
    assert trade.commissions_dollars == Decimal("2.60")
    assert len(await engine.store.logs("leg")) == 4
    evaluation = (await engine.store.logs("evaluation"))[0]
    assert evaluation["context"]["front_back_iv_ratio"] == 1.25
    assert len(evaluation["quotes"]) == 6
    assert evaluation["quotes"]["back_put"]["mid"] == "5.05"
    with pytest.raises(ValueError, match="already"):
        await engine.entry(manual=True)
    assert len(await engine.store.trades()) == 1
    files = await engine.csv.contents()
    rows = list(csv.DictReader(io.StringIO(files["trades_summary.csv"])))
    assert list(rows[0]) == SUMMARY_COLUMNS
    assert rows[0]["transformed"] == "FALSE"
    legs = list(csv.DictReader(io.StringIO(files["legs_execution_log.csv"])))
    assert list(legs[0]) == LEG_COLUMNS
    assert legs[0]["simulated_fill_price"] == "5.1"


async def test_transform_threshold_is_fee_aware_and_exact(lab):
    engine, provider, clock = lab
    trade = await engine.entry(manual=True)
    clock[0] += timedelta(minutes=42)
    transform_prices(provider, "7.2519")
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.CALENDAR
    transform_prices(provider, "7.252")
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.CONDOR
    assert trade.minutes_to_transform == 42
    assert trade.guaranteed_floor_profit == Decimal(".052")
    assert trade.guaranteed_floor_profit * trade.multiplier - trade.commissions_dollars == 0
    assert trade.transform_gain_pct == (Decimal("7.252") - Decimal("2.2")) / Decimal("2.2") * 100
    assert len(await engine.store.logs("leg")) == 8
    await engine.poll()
    assert len(await engine.store.logs("leg")) == 8
    assert (await engine.store.logs("evaluation"))[-1]["kind"] == "CONDOR_MARK"


@pytest.mark.parametrize(
    "settlement,status,loss",
    [
        ("6000", Status.TENT, "0"),
        ("5900", Status.TENT, "0"),
        ("6100", Status.TENT, "0"),
        ("5897.5", Status.SLOPE, "2.5"),
        ("6102.5", Status.SLOPE, "2.5"),
        ("5895", Status.FLOOR, "5"),
        ("6105", Status.FLOOR, "5"),
        ("6200", Status.FLOOR, "5"),
    ],
)
async def test_official_settlement_all_payoff_regions(lab, settlement, status, loss):
    engine, provider, clock = lab
    trade = await engine.entry(manual=True)
    transform_prices(provider)
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert condor_payoff_loss(trade, Decimal(settlement)) == Decimal(loss)
    with pytest.raises(ValueError, match="before"):
        await engine.settle(trade.trade_id, Decimal(settlement), "Verified fixture reference")
    clock[0] = datetime(2026, 10, 30, 20, 1, tzinfo=timezone.utc)
    result = await engine.settle(trade.trade_id, Decimal(settlement), "Verified fixture reference")
    assert result.status == status
    assert result.final_pnl_dollars == (Decimal("5") - Decimal(loss)) * 100
    assert len(await engine.store.logs("leg")) == 12
    assert len(await engine.store.logs("settlement")) == 1
    with pytest.raises(ValueError, match="already closed"):
        await engine.settle(trade.trade_id, Decimal(settlement), "Verified fixture reference")


async def test_calendar_stop_loss_at_exact_thirty_percent(lab):
    engine, provider, clock = lab
    trade = await engine.entry(manual=True)
    provider.prices = {"back_put": ("4.87", "4.97"), "back_call": ("4.87", "4.97")}
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.STOP
    assert trade.calendar_stop_loss_pct == 30
    assert trade.calendar_stop_credit == Decimal("1.54")
    assert trade.final_pnl_dollars == Decimal("-71.20")
    assert trade.roi_pct == Decimal("-71.20") / 220 * 100
    assert len(await engine.store.logs("leg")) == 8
    assert (await engine.store.logs("leg"))[-1]["event_type"] == "STOP_LOSS"


@pytest.mark.parametrize("liquidation", ["1.76", "1.65", "1.55", "1.5401"])
async def test_calendar_remains_open_before_thirty_percent_loss(lab, liquidation):
    engine, provider, _ = lab
    await engine.entry(manual=True)
    bid = Decimal("4.1") + Decimal(liquidation) / 2
    provider.prices = {
        "back_put": (bid, bid + Decimal("0.1")),
        "back_call": (bid, bid + Decimal("0.1")),
        "outer_put": ("1.9", "2"),
        "outer_call": ("1.9", "2"),
    }
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.CALENDAR
    assert len(await engine.store.logs("leg")) == 4


@pytest.mark.parametrize("bid", ["4.87", "4.80"])
async def test_entry_guard_rejects_thirty_percent_or_worse_spread_loss(lab, bid):
    engine, provider, _ = lab
    provider.prices = {"back_put": (bid, "5.1"), "back_call": (bid, "5.1")}
    with pytest.raises(MarketDataError, match="Entry rejected"):
        await engine.entry(manual=True)
    assert await engine.store.trades() == []
    assert await engine.store.logs("leg") == []
    audit = await engine.store.logs("entry_rejection")
    assert len(audit) == 1
    assert Decimal(audit[0]["initial_loss_pct"]) >= 30
    assert len(audit[0]["quotes"]) == 6
    with ZipFile(io.BytesIO(await engine.csv.report())) as archive:
        assert "entry_rejection_audit.json" in archive.namelist()
        assert list(csv.DictReader(io.StringIO(archive.read("trades_summary.csv").decode()))) == []
        assert (
            list(csv.DictReader(io.StringIO(archive.read("legs_execution_log.csv").decode()))) == []
        )


async def test_entry_guard_allows_just_under_thirty_percent(lab):
    engine, provider, _ = lab
    provider.prices = {"back_put": ("4.87005", "5.1"), "back_call": ("4.87005", "5.1")}
    trade = await engine.entry(manual=True)
    assert trade.status == Status.CALENDAR
    assert trade.mark_price == Decimal("1.54010")
    assert trade.calendar_stop_loss_pct == 30
    assert await engine.store.logs("entry_rejection") == []


async def test_legacy_closed_trade_keeps_original_stop_policy(lab):
    engine, provider, _ = lab
    trade = await engine.entry(manual=True)
    payload = trade.model_dump(mode="json")
    payload.pop("calendar_stop_loss_pct", None)
    payload.pop("calendar_stop_credit", None)
    payload.update(status=Status.STOP, final_pnl_dollars="-49.20")
    legacy = Trade.model_validate(payload)
    assert legacy.calendar_stop_loss_pct == 20
    assert legacy.calendar_stop_credit == Decimal("1.76")
    assert legacy.final_pnl_dollars == Decimal("-49.20")


async def test_bot_stop_scales_with_quantity_and_uses_actual_overshoot(lab):
    engine, provider, _ = lab
    await engine.configure(TradingConfig(quantity=3))
    await engine.entry(manual=True)
    provider.prices = {"back_put": ("4.85", "4.95"), "back_call": ("4.85", "4.95")}
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.STOP
    assert trade.final_pnl_dollars == Decimal("-225.60")


async def test_no_fill_on_disconnect_or_closed_market(lab):
    engine, provider, clock = lab
    provider.fail = True
    with pytest.raises(ConnectionError):
        await engine.entry(manual=True)
    assert await engine.store.trades() == []
    provider.fail = False
    clock[0] = datetime(2026, 10, 18, 14, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="open"):
        await engine.entry(manual=True)


async def test_scheduled_entry_requires_preflight_and_no_catchup(lab):
    engine, _, clock = lab
    with pytest.raises(ValueError, match="preflight"):
        await engine.entry()
    await engine.prepare()
    assert (await engine.entry()).status == Status.CALENDAR
    clock[0] += timedelta(days=1, minutes=1)
    with pytest.raises(ValueError, match="10:30"):
        await engine.entry()


async def test_expiration_calendar_exits_five_minutes_before_close(lab):
    engine, _, clock = lab
    await engine.entry(manual=True)
    clock[0] = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
    await engine.poll()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.TIME
    assert trade.final_pnl_dollars == Decimal("-45.20")
    assert (await engine.store.logs("leg"))[-1]["event_type"] == "CALENDAR_TIME_EXIT"


async def test_restart_csv_projection_is_idempotent_and_bundle_complete(lab):
    engine, provider, _ = lab
    await engine.entry(manual=True)
    transform_prices(provider)
    await engine.poll()
    before = await engine.csv.contents()
    await engine.csv.export()
    await engine.csv.export()
    assert await engine.csv.contents() == before
    assert len(list(csv.DictReader(io.StringIO(before["trades_summary.csv"])))) == 1
    assert len(list(csv.DictReader(io.StringIO(before["legs_execution_log.csv"])))) == 8
    with ZipFile(io.BytesIO(await engine.csv.report())) as archive:
        assert set(archive.namelist()) == {
            "trades_summary.csv",
            "legs_execution_log.csv",
            "evaluation_audit.json",
            "advisor_audit.json",
            "error_audit.json",
            "settlement_audit.json",
            "entry_rejection_audit.json",
            "manual_retest_audit.json",
            "configuration.json",
        }
        assert json.loads(archive.read("manual_retest_audit.json")) == []
    await engine.store.engine.dispose()
    reloaded = (await engine.store.trades())[0]
    assert reloaded.status == Status.CONDOR
    assert reloaded.calendar_stop_loss_pct == 30
    assert await engine.csv.contents() == before


async def test_config_and_session_cap(lab):
    engine, _, clock = lab
    await engine.configure(TradingConfig(enabled=False))
    with pytest.raises(ValueError, match="disabled"):
        await engine.entry(manual=True)
    await engine.configure(TradingConfig(quantity=2, wing_width=10))
    trade = await engine.entry(manual=True)
    assert trade.quantity == 2
    assert trade.capital == 440
    assert trade.contracts["outer_put"].strike == 5890
    clock[0] = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="window completed"):
        await engine.entry(manual=True)


def test_expiry_selection_real_listing_and_weekend_preference():
    today = date(2026, 10, 19)
    pairs = expiry_pairs(
        [
            date(2026, 10, 23),
            date(2026, 10, 26),
            date(2026, 10, 27),
            date(2026, 10, 30),
            date(2026, 11, 2),
        ],
        today,
    )
    assert pairs[0] == (date(2026, 10, 30), date(2026, 11, 2))
    assert all(
        6 <= (front - today).days <= 12 and 1 <= (back - front).days <= 4 for front, back in pairs
    )


async def test_missed_calendar_expiry_recovers_back_longs_on_monday(lab):
    engine, _, clock = lab
    trade = await engine.entry(manual=True)
    clock[0] = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)
    await engine.poll()
    assert (await engine.store.trades())[0].status == Status.CALENDAR
    result = await engine.settle(
        trade.trade_id,
        Decimal(6000),
        "Verified Friday SPXW PM fixture source",
    )
    assert result.status == Status.TIME
    assert result.commissions_dollars == Decimal("3.90")
    assert result.final_pnl_dollars == Decimal("776.10")
    logs = await engine.store.logs("leg")
    assert len(logs) == 8
    assert sum(row["event_type"] == "CALENDAR_RECOVERY" for row in logs) == 2
    assert sum(row["event_type"] == "EXPIRATION_SETTLEMENT" for row in logs) == 2


async def test_both_calendar_expiries_missed_does_not_invent_fills(lab):
    engine, _, clock = lab
    trade = await engine.entry(manual=True)
    clock[0] = datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="also expired"):
        await engine.settle(trade.trade_id, Decimal(6000), "Verified Friday fixture source")
    assert (await engine.store.trades())[0].status == Status.CALENDAR
    assert len(await engine.store.logs("leg")) == 4


async def test_disconnect_at_calendar_time_exit_keeps_position_unresolved(lab):
    engine, provider, clock = lab
    await engine.entry(manual=True)
    clock[0] = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
    provider.fail = True
    await engine.poll()
    assert (await engine.store.trades())[0].status == Status.CALENDAR
    assert len(await engine.store.logs("leg")) == 4
    assert (await engine.store.logs("error"))[-1]["component"] == "quote evaluation"


async def test_automatic_proxy_settlement_delay_and_provenance(lab):
    engine, provider, clock = lab
    await engine.entry(manual=True)
    transform_prices(provider)
    await engine.poll()
    clock[0] = datetime(2026, 10, 30, 20, 14, tzinfo=timezone.utc)
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.CONDOR
    clock[0] += timedelta(minutes=1)
    await engine.settle_due()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.TENT
    assert trade.settlement_provenance == "ibkr_close_proxy"
    assert trade.settlement_value == 6000
    assert "NOT verified official" in trade.notes
    assert (await engine.store.logs("settlement"))[0]["provenance"] == "ibkr_close_proxy"
    await engine.settle_due()
    assert len(await engine.store.logs("leg")) == 12


async def test_missing_proxy_data_stays_pending_and_retries_after_five_minutes(lab):
    engine, provider, clock = lab
    await engine.entry(manual=True)
    transform_prices(provider)
    await engine.poll()
    clock[0] = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)
    provider.fail = True
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.CONDOR
    assert (await engine.store.logs("error"))[-1]["component"] == "automatic settlement proxy"
    provider.fail = False
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.CONDOR
    clock[0] += timedelta(minutes=5)
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.TENT


async def test_automatic_proxy_calendar_recovery_waits_for_open_session(lab):
    engine, _, clock = lab
    await engine.entry(manual=True)
    # Missed Friday liquidation: recovery must wait for back-long market hours.
    clock[0] = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.CALENDAR
    clock[0] = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)
    await engine.settle_due()
    trade = (await engine.store.trades())[0]
    assert trade.status == Status.TIME
    assert trade.settlement_provenance == "ibkr_close_proxy"


async def test_automatic_settlement_can_be_disabled(lab):
    engine, provider, clock = lab
    await engine.entry(manual=True)
    transform_prices(provider)
    await engine.poll()
    engine.settings.auto_settle_from_ib_close = False
    clock[0] = datetime(2026, 10, 30, 20, 30, tzinfo=timezone.utc)
    await engine.settle_due()
    assert (await engine.store.trades())[0].status == Status.CONDOR
