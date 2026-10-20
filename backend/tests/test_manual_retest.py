import asyncio
import io
import json
import sqlite3
from datetime import timedelta
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.models import ManualRetestRequest, Status, Trade
from app.core.store import Store
from app.engine.paper_engine import PaperEngine
from app.ibkr.client import MarketDataError
from app.main import create_app
from conftest import FakeSelector


def retest_request():
    return ManualRetestRequest(
        request_id=uuid4(),
        reason="Observe the 30 percent bot-monitored protection",
        confirmed=True,
    )


async def close_daily(engine, provider):
    trade = await engine.entry(manual=True)
    provider.prices = {"back_put": ("4.87", "4.97"), "back_call": ("4.87", "4.97")}
    await engine.poll()
    provider.prices = {}
    trade = next(row for row in await engine.store.trades() if row.trade_id == trade.trade_id)
    assert trade.status == Status.STOP
    return trade


async def test_retest_preserves_daily_trade_and_is_idempotent(lab):
    engine, provider, _ = lab
    original = await close_daily(engine, provider)
    request = retest_request()
    retest = await engine.entry(manual=True, retest=request)
    assert retest.trade_id != original.trade_id
    assert "-TEST-" in retest.trade_id
    assert retest.manual_retest is True
    assert retest.manual_retest_request_id == request.request_id
    assert retest.calendar_stop_loss_pct == 30
    assert "MANUAL_RETEST" in retest.notes
    assert len(await engine.store.logs("leg")) == 12
    provider.fail = True
    repeated = await engine.entry(manual=True, retest=request)
    assert repeated.trade_id == retest.trade_id
    assert len(await engine.store.logs("leg")) == 12
    trades = await engine.store.trades()
    assert len(trades) == 2
    assert next(row for row in trades if row.trade_id == original.trade_id) == original
    with pytest.raises(ValueError, match="already has"):
        await engine.entry(manual=True)
    with pytest.raises(ValueError, match="positions to be closed"):
        await engine.entry(manual=True, retest=retest_request())
    await engine.store.engine.dispose()
    assert (await engine.entry(manual=True, retest=request)).trade_id == retest.trade_id


async def test_retest_requires_explicit_manual_request_and_original_daily(lab):
    engine, _, _ = lab
    request = retest_request()
    with pytest.raises(ValueError, match="explicitly requested"):
        await engine.entry(retest=request)
    with pytest.raises(ValueError, match="existing normal"):
        await engine.entry(manual=True, retest=request)
    assert await engine.store.trades() == []


async def test_concurrent_retries_create_one_trade_and_return_it_after_close_and_restart(lab):
    engine, provider, clock = lab
    await close_daily(engine, provider)
    request = retest_request()
    results = await asyncio.gather(
        engine.entry(manual=True, retest=request),
        engine.entry(manual=True, retest=request),
    )
    assert results[0].trade_id == results[1].trade_id
    assert len(await engine.store.logs("leg")) == 12
    provider.prices = {"back_put": ("4.87", "4.97"), "back_call": ("4.87", "4.97")}
    await engine.poll()
    clock[0] += timedelta(hours=10)
    config = await engine.store.config()
    config.enabled = False
    await engine.store.save_config(config)
    store = Store(engine.settings.data_dir)
    try:
        await store.initialize()
        restarted = PaperEngine(
            store,
            provider,
            FakeSelector(),
            engine.market,
            engine.csv,
            engine.advisor,
            engine.settings,
        )
        previous = await restarted.entry(manual=True, retest=request)
        assert previous.trade_id == results[0].trade_id
        assert previous.status == Status.STOP
        assert len(await store.trades()) == 2
        assert len(await store.logs("leg")) == 16
    finally:
        await store.engine.dispose()


@pytest.mark.parametrize("blocked", ["disabled", "market_closed", "active_previous_day"])
async def test_retest_preserves_existing_entry_gates(lab, blocked):
    engine, provider, clock = lab
    original = await close_daily(engine, provider)
    if blocked == "disabled":
        config = await engine.store.config()
        config.enabled = False
        await engine.store.save_config(config)
        match = "disabled"
    elif blocked == "market_closed":
        clock[0] += timedelta(hours=10)
        match = "open regular market"
    else:
        prior = original.model_copy(
            update={
                "trade_id": "DC-PRIOR-ACTIVE",
                "entry_session": original.entry_session - timedelta(days=3),
                "status": Status.CALENDAR,
            }
        )
        await engine.store.commit_trade(prior, [])
        match = "positions to be closed"
    with pytest.raises(ValueError, match=match):
        await engine.entry(manual=True, retest=retest_request())
    assert not any(trade.manual_retest for trade in await engine.store.trades())


async def test_retest_keeps_spread_guard_and_allows_same_request_after_rejection(lab):
    engine, provider, _ = lab
    original = await close_daily(engine, provider)
    request = retest_request()
    provider.prices = {"back_put": ("4.87", "5.1"), "back_call": ("4.87", "5.1")}
    with pytest.raises(MarketDataError, match="Entry rejected"):
        await engine.entry(manual=True, retest=request)
    assert len(await engine.store.trades()) == 1
    assert len(await engine.store.logs("leg")) == 8
    rejection = (await engine.store.logs("entry_rejection"))[-1]
    assert rejection["manual_retest"] is True
    assert rejection["manual_retest_request_id"] == str(request.request_id)
    provider.prices = {}
    retest = await engine.entry(manual=True, retest=request)
    assert retest.manual_retest is True
    assert (
        next(row for row in await engine.store.trades() if row.trade_id == original.trade_id)
        == original
    )


async def test_manual_retest_api_validation_and_duplicate_request(lab, tmp_path):
    _, provider, _ = lab
    app = create_app(Settings(data_dir=tmp_path / "api"), run_scheduler=False)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        engine.provider = provider
        engine.selector = FakeSelector()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            body = retest_request().model_dump(mode="json")
            assert (
                await client.post("/api/entry/retest", json={**body, "confirmed": False})
            ).status_code == 422
            assert (
                await client.post("/api/entry/retest", json={**body, "request_id": "bad"})
            ).status_code == 422
            assert (await client.post("/api/entry/retest", json=body)).status_code == 409
            await close_daily(engine, provider)
            first = await client.post("/api/entry/retest", json=body)
            assert first.status_code == 200
            repeated = await client.post("/api/entry/retest", json=body)
            assert repeated.json()["trade_id"] == first.json()["trade_id"]
            assert (
                await client.post(
                    "/api/entry/retest", json={**body, "reason": "A different test reason"}
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/api/entry/retest", json=retest_request().model_dump(mode="json")
                )
            ).status_code == 409
            assert (await client.post("/api/entry", json={})).status_code == 409
            state = (await client.get("/api/state")).json()
            assert state["metrics"]["total_trades"] == 2
            assert state["metrics"]["normal_trade_count"] == 1
            assert state["metrics"]["manual_retest_count"] == 1
            report = await client.get("/api/report")
            assert report.status_code == 200
            with ZipFile(io.BytesIO(report.content)) as archive:
                audit = json.loads(archive.read("manual_retest_audit.json"))
                assert len(audit) == 1
                assert audit[0]["manual_retest_request_id"] == body["request_id"]
                assert b"MANUAL_RETEST" in archive.read("trades_summary.csv")


def create_legacy_database(directory, trade):
    directory.mkdir()
    payload = trade.model_dump(mode="json")
    for field in ("manual_retest", "manual_retest_request_id", "manual_retest_reason"):
        payload.pop(field, None)
    raw = json.dumps(payload)
    with sqlite3.connect(directory / "paper.db") as connection:
        connection.execute(
            "CREATE TABLE trades (trade_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL, "
            "entry_session VARCHAR NOT NULL, payload JSON NOT NULL, UNIQUE(symbol, entry_session))"
        )
        connection.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?)",
            (
                trade.trade_id,
                trade.symbol,
                trade.entry_session.isoformat(),
                raw,
            ),
        )
        connection.execute("CREATE TABLE settings (key VARCHAR PRIMARY KEY, payload JSON NOT NULL)")
        connection.execute(
            "INSERT INTO settings VALUES ('trading', ?)",
            (json.dumps({"quantity": 3, "wing_width": 10}),),
        )
        connection.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, kind VARCHAR NOT NULL, "
            "timestamp DATETIME NOT NULL, payload JSON NOT NULL)"
        )
        connection.execute(
            "INSERT INTO audit_log VALUES (1, 'migration_fixture', "
            "'2026-10-19 14:30:00.000000', '{\"preserved\": true}')"
        )
    return raw


async def test_legacy_migration_preserves_payload_settings_and_daily_constraint(lab, tmp_path):
    engine, provider, _ = lab
    original = await close_daily(engine, provider)
    directory = tmp_path / "legacy"
    raw = create_legacy_database(directory, original)
    store = Store(directory)
    try:
        await store.initialize()
        await store.initialize()
        with sqlite3.connect(directory / "paper.db") as connection:
            assert connection.execute("SELECT payload FROM trades").fetchone()[0] == raw
            assert connection.execute("SELECT manual_retest FROM trades").fetchone()[0] == 0
        assert (await store.config()).quantity == 3
        assert (await store.logs("migration_fixture"))[0]["preserved"] is True
        duplicate = original.model_copy(update={"trade_id": "duplicate-daily"})
        with pytest.raises(IntegrityError):
            await store.commit_trade(duplicate, [])
        request = retest_request()
        retest = Trade.model_validate(
            {
                **original.model_dump(mode="json"),
                "trade_id": f"DC-TEST-{request.request_id.hex}",
                "manual_retest": True,
                "manual_retest_request_id": str(request.request_id),
                "manual_retest_reason": request.reason,
            }
        )
        await store.commit_trade(retest, [])
        assert len(await store.trades()) == 2
    finally:
        await store.engine.dispose()


async def test_migration_failure_rolls_back_table_rebuild_without_losing_original(lab, tmp_path):
    engine, provider, _ = lab
    original = await close_daily(engine, provider)
    directory = tmp_path / "legacy-failure"
    raw = create_legacy_database(directory, original)
    store = Store(directory)

    def fail_copy(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO trades "):
            raise RuntimeError("Simulated migration failure")

    event.listen(store.engine.sync_engine, "before_cursor_execute", fail_copy)
    try:
        with pytest.raises(RuntimeError, match="Simulated migration failure"):
            await store.initialize()
    finally:
        await store.engine.dispose()
    with sqlite3.connect(directory / "paper.db") as connection:
        assert connection.execute("SELECT payload FROM trades").fetchone()[0] == raw
        assert "manual_retest" not in {
            row[1] for row in connection.execute("PRAGMA table_info(trades)")
        }
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='trades_before_retests'"
            ).fetchone()[0]
            == 0
        )
