"""Replay an explicitly synthetic scenario into isolated example reports."""

import argparse
import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.advisor.ai_agent import Advisor
from app.core.config import Settings
from app.core.market import MarketCalendar
from app.core.models import OptionSpec, Quote
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.paper_engine import PaperEngine

ROOT = Path(__file__).resolve().parents[2]


class Replay:
    def __init__(self, fixture: dict) -> None:
        self.fixture = fixture
        self.now = datetime.fromisoformat(fixture["entry_time"])
        self.phase = "entry_quotes"

    async def select(self, today: date, width: Decimal) -> dict[str, OptionSpec]:
        return {
            name: OptionSpec.model_validate(spec)
            for name, spec in self.fixture["contracts"].items()
        }

    async def index(self, symbol: str) -> Decimal:
        return Decimal("20")

    async def settlement_close(self, expiry: date) -> Decimal:
        return Decimal(self.fixture["settlement"])

    async def quotes(
        self, specs: dict[str, OptionSpec], require_delta: bool = False
    ) -> dict[str, Quote]:
        return {
            name: Quote(
                contract=spec,
                timestamp=self.now,
                bid=self.fixture[self.phase][name][0],
                ask=self.fixture[self.phase][name][1],
                delta=-0.35 if spec.right == "P" else 0.35,
                implied_vol=0.25 if name.startswith("short") else 0.20,
                bid_size=10,
                ask_size=10,
            )
            for name, spec in specs.items()
        }


async def generate(output: Path) -> None:
    fixture = json.loads((ROOT / "samples" / "market_fixture.json").read_text())
    output = output.resolve()
    if output == (ROOT / "data").resolve() or (ROOT / "data").resolve() in output.parents:
        raise ValueError("Sample output must not be written to live application data")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="dc-sample-") as temporary:
        store = Store(Path(temporary))
        await store.initialize()
        settings = Settings(data_dir=Path(temporary), advisor_enabled=False)
        replay = Replay(fixture)
        csv = CSVLogger(store, output)
        engine = PaperEngine(
            store,
            replay,
            replay,
            MarketCalendar(),
            csv,
            Advisor(settings, store),
            settings,
        )
        try:
            with (
                patch("app.engine.paper_engine.utcnow", lambda: replay.now),
                patch("app.core.store.utcnow", lambda: replay.now),
            ):
                trade = await engine.entry(manual=True)
                replay.phase = "transform_quotes"
                replay.now = datetime.fromisoformat(fixture["transform_time"])
                await engine.poll()
                replay.now = datetime.fromisoformat(fixture["settlement_time"])
                result = await engine.settle(
                    trade.trade_id,
                    Decimal(fixture["settlement"]),
                    "SYNTHETIC FIXTURE ONLY; not an official market settlement",
                )
                assert result.final_pnl_dollars == Decimal("504.80")
                print(f"SYNTHETIC sample exported to {output}; net P&L $504.80, 12 leg events.")
        finally:
            await store.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "samples" / "generated")
    asyncio.run(generate(parser.parse_args().output))
