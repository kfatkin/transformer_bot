from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ib_host: str = "127.0.0.1"
    ib_port: Literal[7497, 7496] = 7497
    ib_client_id: int = Field(default=71, ge=1)
    data_dir: Path = Path("./data")
    poll_seconds: int = Field(default=20, ge=15, le=30)
    quote_timeout_seconds: int = Field(default=12, ge=2, le=30)
    max_quote_age_seconds: int = Field(default=30, ge=5, le=60)
    max_spread_points: float = Field(default=2, gt=0)
    strike_scan_count: int = Field(default=16, ge=6, le=30)
    evaluation_sessions: int = Field(default=14, ge=10, le=14)
    auto_settle_from_ib_close: bool = True
    settlement_delay_minutes: int = Field(default=15, ge=5, le=120)
    advisor_enabled: bool = False
    advisor_provider: Literal["openai", "anthropic", "ollama"] = "ollama"
    advisor_base_url: str = "http://127.0.0.1:11434"
    advisor_model: str = "llama3.2"
    advisor_api_key: str = ""

    @field_validator("ib_port", mode="before")
    @classmethod
    def parse_ib_port(cls, value: object) -> object:
        return int(value) if isinstance(value, str) else value


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    symbol: Literal["SPX"] = "SPX"
    quantity: int = Field(default=1, ge=1, le=100)
    wing_width: Literal[5, 10] = 5
    commission_per_leg: float = Field(default=0.65, ge=0, le=10)


class ButterflyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    quantity: int = Field(default=1, ge=1, le=100)
    wing_width: Literal[5, 10] = 5
    commission_per_leg: float = Field(default=0.65, ge=0, le=10)
