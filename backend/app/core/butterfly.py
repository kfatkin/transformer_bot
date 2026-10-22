from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from app.core.models import OptionSpec

BUTTERFLY_SESSIONS = 14


class ButterflyStatus(StrEnum):
    DEBIT = "ACTIVE_DEBIT_SPREAD"
    BUTTERFLY = "RISK_FREE_BUTTERFLY"
    STOP = "CLOSED_STOP_LOSS"
    TIME = "CLOSED_0DTE_TIME_EXIT"
    EXPIRED = "CLOSED_UNTRANSFORMED_EXPIRY"
    TENT = "CLOSED_MAX_PROFIT"
    FLOOR = "CLOSED_FLOOR"
    SLOPE = "CLOSED_PARTIAL_PROFIT"


BUTTERFLY_ACTIVE = {ButterflyStatus.DEBIT, ButterflyStatus.BUTTERFLY}


class ButterflyTrade(BaseModel):
    strategy: Literal["ZERO_DTE_BUTTERFLY"] = "ZERO_DTE_BUTTERFLY"
    trade_id: str
    symbol: Literal["SPX"] = "SPX"
    entry_session: date
    entry_timestamp: datetime
    signal_timestamp: datetime
    opening_index: Decimal = Field(gt=0, allow_inf_nan=False)
    signal_price: Decimal = Field(gt=0, allow_inf_nan=False)
    expiry: date
    right: Literal["P", "C"]
    wing_width: Decimal = Field(gt=0)
    quantity: int = Field(ge=1, le=100)
    initial_debit: Decimal = Field(gt=0)
    stop_loss_pct: Decimal = Field(default=Decimal("30"), gt=0, lt=100)
    commission_per_leg: Decimal = Field(ge=0)
    commissions_dollars: Decimal = Field(ge=0)
    contracts: dict[str, OptionSpec]
    status: ButterflyStatus = ButterflyStatus.DEBIT
    transformed: bool = False
    transform_timestamp: datetime | None = None
    minutes_to_transform: int | None = None
    transformer_credit_received: Decimal | None = None
    transform_gain_pct: Decimal | None = None
    guaranteed_floor_profit: Decimal | None = None
    max_tent_profit: Decimal | None = None
    exit_timestamp: datetime | None = None
    final_pnl_dollars: Decimal | None = None
    roi_pct: Decimal | None = None
    mark_price: Decimal | None = None
    unrealized_pnl_dollars: Decimal | None = None
    current_transform_credit: Decimal | None = None
    last_quote_at: datetime | None = None
    settlement_value: Decimal | None = None
    settlement_source: str | None = None
    settlement_provenance: Literal["verified_official", "ibkr_close_proxy"] | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_structure(self) -> "ButterflyTrade":
        if self.expiry != self.entry_session:
            raise ValueError("0DTE options must expire on the entry session")
        if self.signal_timestamp.tzinfo is None or self.entry_timestamp.tzinfo is None:
            raise ValueError("Signal and entry timestamps must be timezone-aware")
        expected = "C" if self.signal_price < self.opening_index else "P"
        if self.signal_price == self.opening_index or self.right != expected:
            raise ValueError("Spread direction must oppose the move from the opening index")
        if set(self.contracts) != {"long", "short", "outer"}:
            raise ValueError("Expected long, short, and outer butterfly contracts")
        if any(
            spec.right != self.right or spec.expiry != self.expiry
            for spec in self.contracts.values()
        ):
            raise ValueError("Butterfly legs must share the same right and same-day expiry")
        step = self.wing_width if self.right == "C" else -self.wing_width
        if (
            self.contracts["short"].strike != self.contracts["long"].strike + step
            or self.contracts["outer"].strike != self.contracts["short"].strike + step
        ):
            raise ValueError("Butterfly strikes must be equally spaced")
        return self

    @property
    def multiplier(self) -> Decimal:
        return Decimal(100 * self.quantity)

    @property
    def capital(self) -> Decimal:
        return self.initial_debit * self.multiplier

    @property
    def target_credit(self) -> Decimal:
        return self.initial_debit + self.commission_per_leg * 4 / 100

    @computed_field
    @property
    def stop_credit(self) -> Decimal:
        return self.initial_debit * (1 - self.stop_loss_pct / 100)
