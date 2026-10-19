from decimal import Decimal

from app.core.models import Quote, Trade


def calendar_debit(quotes: dict[str, Quote]) -> Decimal:
    return sum(
        (quotes[f"back_{right}"].ask - quotes[f"short_{right}"].bid for right in ("put", "call")),
        Decimal(0),
    )


def liquidation_credit(quotes: dict[str, Quote]) -> Decimal:
    return sum(
        (quotes[f"back_{right}"].bid - quotes[f"short_{right}"].ask for right in ("put", "call")),
        Decimal(0),
    )


def transformer_credit(quotes: dict[str, Quote]) -> Decimal:
    return sum(
        (quotes[f"back_{right}"].bid - quotes[f"outer_{right}"].ask for right in ("put", "call")),
        Decimal(0),
    )


def condor_close_debit(quotes: dict[str, Quote]) -> Decimal:
    return sum(
        (quotes[f"short_{right}"].ask - quotes[f"outer_{right}"].bid for right in ("put", "call")),
        Decimal(0),
    )


def intrinsic(strike: Decimal, right: str, settlement: Decimal) -> Decimal:
    return max(Decimal(0), strike - settlement if right == "P" else settlement - strike)


def condor_payoff_loss(trade: Trade, settlement: Decimal) -> Decimal:
    return sum(
        (
            intrinsic(trade.contracts[f"short_{right}"].strike, kind, settlement)
            - intrinsic(trade.contracts[f"outer_{right}"].strike, kind, settlement)
            for right, kind in (("put", "P"), ("call", "C"))
        ),
        Decimal(0),
    )
