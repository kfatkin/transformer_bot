from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import app.engine.paper_engine as engine_module
from app.advisor.ai_agent import Advisor
from app.core.config import Settings
from app.core.market import MarketCalendar
from app.core.models import OptionSpec, Quote
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.paper_engine import PaperEngine


class FakeProvider:
    def __init__(self, clock):
        self.clock = clock
        self.fail = False
        self.prices = {}

    async def quotes(self, specs, require_delta=False):
        if self.fail:
            raise ConnectionError("Fixture disconnect")
        return {
            name: Quote(
                contract=spec,
                timestamp=self.clock[0],
                bid=self.prices.get(
                    name,
                    (4, 4.1)
                    if name.startswith("short")
                    else ((5, 5.1) if name.startswith("back") else (0.9, 1)),
                )[0],
                ask=self.prices.get(
                    name,
                    (4, 4.1)
                    if name.startswith("short")
                    else ((5, 5.1) if name.startswith("back") else (0.9, 1)),
                )[1],
                delta=-0.35 if spec.right == "P" else 0.35,
                implied_vol=0.25 if name.startswith("short") else 0.20,
                bid_size=100,
                ask_size=100,
            )
            for name, spec in specs.items()
        }

    async def index(self, symbol):
        return Decimal("20") if symbol == "VIX" else Decimal("6000")

    async def settlement_close(self, expiry):
        if self.fail:
            raise ConnectionError("Fixture historical-data disconnect")
        return Decimal("6000")


class FakeSelector:
    async def select(self, today, width):
        contracts = {}
        for index, (name, right, strike, expiry) in enumerate(
            [
                ("short_put", "P", 5900, date(2026, 10, 30)),
                ("short_call", "C", 6100, date(2026, 10, 30)),
                ("back_put", "P", 5900, date(2026, 11, 2)),
                ("back_call", "C", 6100, date(2026, 11, 2)),
                ("outer_put", "P", Decimal(5900) - width, date(2026, 10, 30)),
                ("outer_call", "C", Decimal(6100) + width, date(2026, 10, 30)),
            ],
            1,
        ):
            contracts[name] = OptionSpec(
                con_id=index,
                right=right,
                strike=strike,
                expiry=expiry,
            )
        return contracts


@pytest.fixture
async def lab(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(engine_module, "utcnow", lambda: clock[0])
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path)
    await store.initialize()
    provider = FakeProvider(clock)
    csv = CSVLogger(store, tmp_path / "csv")
    advisor = Advisor(settings, store)
    engine = PaperEngine(
        store,
        provider,
        FakeSelector(),
        MarketCalendar(),
        csv,
        advisor,
        settings,
    )
    yield engine, provider, clock
    await store.engine.dispose()


def transform_prices(provider, credit="7.252"):
    bid = Decimal(credit) / 2 + 1
    provider.prices = {
        "back_put": (bid, bid + Decimal("0.1")),
        "back_call": (bid, bid + Decimal("0.1")),
        "short_put": (Decimal("1.9"), Decimal("2")),
        "short_call": (Decimal("1.9"), Decimal("2")),
    }
