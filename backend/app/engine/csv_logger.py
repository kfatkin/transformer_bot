import asyncio
import csv
import io
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.models import LegEvent, Trade, calendar_risk_policy
from app.core.store import Store

SUMMARY_COLUMNS = [
    "trade_id",
    "symbol",
    "entry_timestamp",
    "front_expiry",
    "back_expiry",
    "put_strike",
    "call_strike",
    "wing_width",
    "quantity",
    "calendar_debit_paid",
    "total_capital_committed",
    "transformed",
    "transform_timestamp",
    "minutes_to_transform",
    "transformer_credit_received",
    "transform_gain_pct",
    "guaranteed_floor_profit",
    "max_tent_profit",
    "status",
    "exit_timestamp",
    "final_pnl_dollars",
    "roi_pct",
    "notes",
]
LEG_COLUMNS = [
    "trade_id",
    "timestamp",
    "event_type",
    "con_id",
    "symbol",
    "expiry",
    "strike",
    "right",
    "action",
    "bid",
    "ask",
    "simulated_fill_price",
    "delta",
    "implied_vol",
]
BUTTERFLY_COLUMNS = [
    "trade_id",
    "strategy",
    "symbol",
    "entry_timestamp",
    "expiry",
    "opening_index",
    "signal_price",
    "signal_timestamp",
    "right",
    "long_strike",
    "short_strike",
    "outer_strike",
    "wing_width",
    "quantity",
    "initial_debit",
    "total_capital_committed",
    "transformed",
    "transform_timestamp",
    "minutes_to_transform",
    "transformer_credit_received",
    "transform_gain_pct",
    "guaranteed_floor_profit",
    "max_tent_profit",
    "status",
    "exit_timestamp",
    "final_pnl_dollars",
    "roi_pct",
    "notes",
    "stop_loss_pct",
    "stop_credit",
    "commission_per_leg",
    "commissions_dollars",
    "net_floor_dollars",
    "net_max_tent_dollars",
    "settlement_value",
    "settlement_provenance",
    "settlement_source",
]


def render_csv(columns: list[str], rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formulas in user-provided settlement notes.
        safe = {
            key: "'" + value
            if isinstance(value, str) and value.startswith(("=", "+", "@", "\t", "\r"))
            else value
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
        butterflies = await self.store.butterflies()
        legs = [LegEvent.model_validate(row) for row in await self.store.logs("leg")]
        calendar_ids = {trade.trade_id for trade in trades}
        butterfly_ids = {trade.trade_id for trade in butterflies}
        butterfly_rows = []
        for trade in butterflies:
            row = trade.model_dump(mode="json")
            row.update(
                total_capital_committed=str(trade.capital),
                transformed="TRUE" if trade.transformed else "FALSE",
                **{f"{name}_strike": str(spec.strike) for name, spec in trade.contracts.items()},
                net_floor_dollars=(
                    str(
                        trade.guaranteed_floor_profit * trade.multiplier - trade.commissions_dollars
                    )
                    if trade.guaranteed_floor_profit is not None
                    else None
                ),
                net_max_tent_dollars=(
                    str(trade.max_tent_profit * trade.multiplier - trade.commissions_dollars)
                    if trade.max_tent_profit is not None
                    else None
                ),
            )
            butterfly_rows.append(row)
        return {
            "trades_summary.csv": render_csv(
                SUMMARY_COLUMNS, [summary_row(trade) for trade in trades]
            ),
            "legs_execution_log.csv": render_csv(
                LEG_COLUMNS, [leg_row(leg) for leg in legs if leg.trade_id in calendar_ids]
            ),
            "butterfly_trades_summary.csv": render_csv(BUTTERFLY_COLUMNS, butterfly_rows),
            "butterfly_legs_execution_log.csv": render_csv(
                LEG_COLUMNS + ["quantity"],
                [leg_row(leg) for leg in legs if leg.trade_id in butterfly_ids],
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
            for kind in (
                "evaluation",
                "advisor",
                "error",
                "settlement",
                "entry_rejection",
                "entry_skip",
            ):
                archive.writestr(
                    f"{kind}_audit.json",
                    json.dumps(await self.store.logs(kind), indent=2),
                )
            configuration = (await self.store.config()).model_dump(mode="json")
            configuration["risk_policy"] = calendar_risk_policy()
            configuration["butterfly"] = (await self.store.butterfly_config()).model_dump(
                mode="json"
            )
            archive.writestr("configuration.json", json.dumps(configuration, indent=2))
            archive.writestr(
                "butterfly_trade_audit.json",
                json.dumps(
                    [trade.model_dump(mode="json") for trade in await self.store.butterflies()],
                    indent=2,
                ),
            )
            archive.writestr(
                "manual_retest_audit.json",
                json.dumps(
                    [
                        trade.model_dump(mode="json")
                        for trade in await self.store.trades()
                        if trade.manual_retest
                    ],
                    indent=2,
                ),
            )
        return output.getvalue()
