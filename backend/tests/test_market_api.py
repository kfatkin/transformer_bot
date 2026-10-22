import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.market import MarketCalendar
from app.core.models import OptionSpec, Quote
from app.main import create_app
from conftest import FakeProvider, FakeSelector


def test_holiday_dst_and_half_day():
    market = MarketCalendar()
    assert not market.is_session(date(2026, 12, 25))
    assert not market.is_session(date(2026, 10, 18))
    assert market.next_entry(datetime(2026, 10, 30, 16, tzinfo=timezone.utc)).hour == 10
    assert (
        market.next_entry(datetime(2026, 10, 30, 16, tzinfo=timezone.utc))
        .utcoffset()
        .total_seconds()
        == -18000
    )
    _, close = market.bounds(date(2026, 11, 27))
    assert close.hour == 18  # 1 PM EST
    assert not market.is_open(datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "bid,ask,kind", [("2", "1", 1), ("1", "2", 3), ("-1", "2", 1), ("1", "0", 1)]
)
def test_invalid_market_quotes_rejected(bid, ask, kind):
    with pytest.raises(ValidationError):
        Quote(
            contract=OptionSpec(con_id=1, expiry=date(2026, 10, 30), strike=6000, right="P"),
            timestamp=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
            market_data_type=kind,
        )


def test_api_dashboard_config_export_and_same_origin(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), run_scheduler=False)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        assert "DC Time Machine" in client.get("/").text
        assert client.get("/app.js").status_code == 200
        assert client.get("/api/health").json()["orders_enabled"] is False
        state = client.get("/api/state").json()
        assert state["metrics"]["total_trades"] == 0
        assert state["connected"] is False
        assert state["butterfly_config"]["enabled"] is False
        assert state["butterfly_metrics"]["total_trades"] == 0
        assert datetime.fromisoformat(state["next_butterfly_entry"]).minute == 0
        assert state["risk_policy"]["calendar_stop_loss_pct"] == "30"
        assert state["risk_policy"]["broker_stop_orders"] is False
        assert state["risk_policy"]["reject_at_stop_on_entry"] is True
        response = client.put(
            "/api/config",
            json={
                "symbol": "SPX",
                "quantity": 3,
                "wing_width": 10,
                "commission_per_leg": 0.65,
            },
        )
        assert response.status_code == 200
        assert client.get("/api/state").json()["config"]["quantity"] == 3
        assert client.put("/api/config", json={"symbol": "SPY"}).status_code == 422
        assert client.put("/api/config", json={"quantity": 0}).status_code == 422
        assert (
            client.put(
                "/api/config/butterfly",
                json={"enabled": True, "quantity": 2, "wing_width": 5, "commission_per_leg": 0.75},
            ).status_code
            == 200
        )
        assert client.put("/api/config/butterfly", json={"wing_width": 1}).status_code == 422
        assert client.put("/api/config/butterfly", json={"quantity": 0}).status_code == 422
        assert client.get("/api/state").json()["config"]["quantity"] == 3
        assert client.get("/api/csv/butterfly_trades_summary.csv").text.startswith(
            "trade_id,strategy,"
        )
        assert client.get("/api/csv/butterfly_legs_execution_log.csv").status_code == 200
        assert (
            client.post(
                "/api/entry", json={}, headers={"Origin": "http://evil.invalid"}
            ).status_code
            == 403
        )
        assert client.post("/api/entry", content="").status_code == 415
        assert client.get("/api/csv/trades_summary.csv").text.startswith("trade_id,symbol,")
        assert client.get("/api/csv/unknown.csv").status_code == 404
        assert client.get("/api/report").content[:2] == b"PK"
        assert (
            client.post(
                "/api/trades/missing/settlement",
                json={
                    "value": "6000",
                    "source": "Test verified source",
                    "confirmed_official_spxw_pm": False,
                },
            ).status_code
            == 422
        )
        assert client.get("/api/state", headers={"Host": "evil.invalid"}).status_code == 400
        with client.websocket_connect("ws://localhost/api/stream") as websocket:
            assert websocket.receive_json()["config"]["quantity"] == 3
    with TestClient(
        create_app(Settings(data_dir=tmp_path), run_scheduler=False),
        base_url="http://localhost",
    ) as client:
        assert client.get("/api/state").json()["config"]["quantity"] == 3
        assert client.get("/api/state").json()["butterfly_config"]["quantity"] == 2
        assert client.get("/api/state").json()["butterfly_config"]["enabled"] is True


async def test_stale_quote_rejected(lab):
    engine, provider, clock = lab
    specs = await engine.selector.select(clock[0].date(), Decimal(5))
    quotes = await provider.quotes(specs)
    clock[0] = clock[0].replace(minute=32)
    with pytest.raises(RuntimeError, match="stale"):
        engine.validate_quotes(quotes, specs)


def test_entry_guard_returns_explicit_error_without_recording_a_trade(tmp_path, monkeypatch):
    import app.engine.paper_engine as engine_module

    clock = [datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(engine_module, "utcnow", lambda: clock[0])
    app = create_app(Settings(data_dir=tmp_path), run_scheduler=False)
    with TestClient(app, base_url="http://localhost") as client:
        provider = FakeProvider(clock)
        provider.prices = {
            "back_put": ("4.87", "5.1"),
            "back_call": ("4.87", "5.1"),
        }
        app.state.engine.provider = provider
        app.state.engine.selector = FakeSelector()
        response = client.post("/api/entry", json={})
        assert response.status_code == 409
        assert "Entry rejected" in response.json()["detail"]
        state = client.get("/api/state").json()
        assert state["trades"] == []
        assert state["errors"][0]["component"] == "manual entry"
        with ZipFile(io.BytesIO(client.get("/api/report").content)) as archive:
            rejections = json.loads(archive.read("entry_rejection_audit.json"))
            assert len(rejections) == 1
            assert rejections[0]["initial_loss_pct"] == "30.0"
            assert len(rejections[0]["quotes"]) == 6
            config = json.loads(archive.read("configuration.json"))
            assert config["risk_policy"]["calendar_stop_loss_pct"] == "30"
