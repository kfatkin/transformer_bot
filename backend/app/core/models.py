from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Status(StrEnum):
    CALENDAR = "ACTIVE_CALENDAR"
    CONDOR = "RISK_FREE_CONDOR"
    TENT = "CLOSED_MAX_PROFIT"
    FLOOR = "CLOSED_FLOOR"
    SLOPE = "CLOSED_PARTIAL_PROFIT"
    STOP = "CLOSED_STOP_LOSS"
    TIME = "CLOSED_CALENDAR_TIME_EXIT"


ACTIVE = {Status.CALENDAR, Status.CONDOR}


class OptionSpec(BaseModel):
    con_id: int = Field(gt=0)
    symbol: Literal["SPX"] = "SPX"
    expiry: date
    strike: Decimal
    right: Literal["P", "C"]


class Quote(BaseModel):
    contract: OptionSpec
    timestamp: datetime
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(gt=0)
    delta: float | None = None
    implied_vol: float | None = None
    bid_size: Decimal | None = Field(default=None, ge=0)
    ask_size: Decimal | None = Field(default=None, ge=0)
    market_data_type: int = 1

    @model_validator(mode="after")
    def validate_market(self) -> "Quote":
        if self.timestamp.tzinfo is None:
            raise ValueError("Quote timestamps must be timezone-aware")
        if self.ask < self.bid:
            raise ValueError("Crossed market")
        if self.market_data_type != 1:
            raise ValueError("Only live market data is accepted")
        return self

    @computed_field
    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class LegEvent(BaseModel):
    trade_id: str
    timestamp: datetime
    event_type: str
    contract: OptionSpec
    action: Literal["BUY", "SELL"]
    bid: Decimal | None = None
    ask: Decimal | None = None
    simulated_fill_price: Decimal
    delta: float | None = None
    implied_vol: float | None = None


class Trade(BaseModel):
    trade_id: str
    symbol: Literal["SPX"] = "SPX"
    entry_session: date
    entry_timestamp: datetime
    front_expiry: date
    back_expiry: date
    put_strike: Decimal
    call_strike: Decimal
    wing_width: Decimal
    quantity: int
    calendar_debit_paid: Decimal
    commission_per_leg: Decimal
    commissions_dollars: Decimal
    contracts: dict[str, OptionSpec]
    status: Status = Status.CALENDAR
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

    @property
    def multiplier(self) -> Decimal:
        return Decimal(100 * self.quantity)

    @property
    def target_credit(self) -> Decimal:
        return self.calendar_debit_paid + self.wing_width + self.commission_per_leg * 8 / 100

    @property
    def capital(self) -> Decimal:
        return self.calendar_debit_paid * self.multiplier


class Evaluation(BaseModel):
    trade_id: str
    timestamp: datetime
    kind: str
    quotes: dict[str, Quote]
    transformer_credit: Decimal | None = None
    liquidation_credit: Decimal | None = None
    target_credit: Decimal | None = None
    underlying: Decimal | None = None
    vix: Decimal | None = None
    context: dict[str, float | str | None] = Field(default_factory=dict)
