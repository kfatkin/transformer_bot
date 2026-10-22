import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ib_async import Index, OptionChain

from app.core.config import Settings
from app.core.models import Quote, utcnow
from app.ibkr.chains import ButterflySelector, scan_strikes
from app.ibkr.client import IBClient, MarketDataError


class ChainClient:
    spec = staticmethod(IBClient.spec)

    def __init__(self, expiry="20261019", wrong_sign=False):
        self.settings = Settings()
        self.request_lock = asyncio.Lock()
        self.wrong_sign = wrong_sign
        self.chain = OptionChain(
            exchange="SMART",
            underlyingConId=1,
            tradingClass="SPXW",
            multiplier="100",
            expirations=[expiry],
            strikes=list(range(5650, 6360, 5)),
        )
        self.ib = SimpleNamespace(reqSecDefOptParamsAsync=self.chains)

    async def chains(self, *args):
        return [self.chain]

    async def qualify(self, contracts):
        for contract in contracts:
            contract.conId = 1 if isinstance(contract, Index) else int(contract.strike * 10)
        return contracts

    async def index(self, symbol):
        return Decimal(6000)

    async def quotes(self, specs, require_delta=False):
        assert require_delta
        output = {}
        for name, spec in specs.items():
            distance = abs(spec.strike - 6000)
            delta = (
                0.49
                if distance == 0
                else 0.39
                if distance == 5
                else 0.35
                if distance == 10
                else 0.1
            )
            sign = -1 if spec.right == "P" else 1
            output[name] = Quote(
                contract=spec,
                timestamp=utcnow(),
                bid=2,
                ask=2.1,
                delta=delta * sign * (-1 if self.wrong_sign else 1),
                bid_size=100,
                ask_size=100,
            )
        return output


@pytest.mark.parametrize("right,expected", [("C", [6005, 6010, 6015]), ("P", [5995, 5990, 5985])])
async def test_selector_closest_live_delta_and_mirrored_geometry(right, expected):
    client = ChainClient()
    result = await ButterflySelector(client).select(date(2026, 10, 19), Decimal(5), right)
    assert [spec.strike for spec in result.values()] == expected
    assert all(
        spec.right == right and spec.expiry == date(2026, 10, 19) for spec in result.values()
    )


@pytest.mark.parametrize(
    "expiry,wrong_sign,message",
    [
        ("20261020", False, "same-day"),
        ("20261019", True, "delta"),
    ],
)
async def test_selector_never_substitutes_expiry_or_wrong_signed_delta(expiry, wrong_sign, message):
    with pytest.raises(MarketDataError, match=message):
        await ButterflySelector(ChainClient(expiry, wrong_sign)).select(
            date(2026, 10, 19), Decimal(5), "C"
        )


def test_dense_zero_dte_scan_preserves_near_money_strikes():
    chain = ChainClient().chain
    assert scan_strikes(chain, 6000, "C", 4, nearest=True) == [6000, 6005, 6010, 6015]
    assert scan_strikes(chain, 6000, "P", 4, nearest=True) == [6000, 5995, 5990, 5985]
    assert scan_strikes(chain, 6000, "C", 4)[-1] == 6355
