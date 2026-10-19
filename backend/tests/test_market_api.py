from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.market import MarketCalendar
from app.core.models import OptionSpec, Quote
from app.main import create_app


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


async def test_stale_quote_rejected(lab):
    engine, provider, clock = lab
    specs = await engine.selector.select(clock[0].date(), Decimal(5))
    quotes = await provider.quotes(specs)
    clock[0] = clock[0].replace(minute=32)
    with pytest.raises(RuntimeError, match="stale"):
        engine.validate_quotes(quotes, specs)
