import asyncio
import csv
import io
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.models import LegEvent, Trade
from app.core.store import Store

SUMMARY_COLUMNS = [
    "trade_id", "symbol", "entry_timestamp", "front_expiry", "back_expiry",
    "put_strike", "call_strike", "wing_width", "quantity", "calendar_debit_paid",
    "total_capital_committed", "transformed", "transform_timestamp",
    "minutes_to_transform", "transformer_credit_received", "transform_gain_pct",
    "guaranteed_floor_profit", "max_tent_profit", "status", "exit_timestamp",
    "final_pnl_dollars", "roi_pct", "notes",
]
LEG_COLUMNS = [
    "trade_id", "timestamp", "event_type", "con_id", "symbol", "expiry", "strike",
    "right", "action", "bid", "ask", "simulated_fill_price", "delta", "implied_vol",
]


def render_csv(columns: list[str], rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formulas in user-provided settlement notes.
        safe = {
            key: "'" + value if isinstance(value, str) and value.startswith(
                ("=", "+", "@", "\t", "\r")
            ) else value
            for key, value in row.items()
        }
        writer.writerow(safe)
    return output.getvalue()


def summary_row(trade: Trade) -> dict:
    row = trade.model_dump(mode="json")
    row["transformed"] = "TRUE" if trade.transformed else "FALSE"
    row["total_capital_committed"] = str(trade.capital)
    return row


def leg_row(leg: LegEvent) -> dict:
    row = leg.model_dump(mode="json")
    row.update(row.pop("contract"))
    return row


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class CSVLogger:
    def __init__(self, store: Store, directory: Path) -> None:
        self.store = store
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    async def contents(self) -> dict[str, str]:
        trades = await self.store.trades()
        legs = [LegEvent.model_validate(row) for row in await self.store.logs("leg")]
        return {
            "trades_summary.csv": render_csv(
                SUMMARY_COLUMNS, [summary_row(trade) for trade in trades]
            ),
            "legs_execution_log.csv": render_csv(
                LEG_COLUMNS, [leg_row(leg) for leg in legs]
            ),
        }

    async def export(self) -> None:
        async with self.store.export_lock:
            files = await self.contents()
            for name, content in files.items():
                await asyncio.to_thread(atomic_write, self.directory / name, content)

    async def report(self) -> bytes:
        files = await self.contents()
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            for kind in ("evaluation", "advisor", "error", "settlement"):
                archive.writestr(
                    f"{kind}_audit.json",
                    json.dumps(await self.store.logs(kind), indent=2),
                )
            archive.writestr(
                "configuration.json", (await self.store.config()).model_dump_json(indent=2)
            )
        return output.getvalue()
