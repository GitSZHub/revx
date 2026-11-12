"""Compare what the ledger says the account holds with what the exchange reports."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

ZERO = Decimal(0)


@dataclass(frozen=True)
class Mismatch:
    asset: str
    expected: Decimal
    actual: Decimal

    @property
    def diff(self) -> Decimal:
        return self.actual - self.expected

    def __str__(self) -> str:
        return f"{self.asset}: ledger {self.expected}, exchange {self.actual} (diff {self.diff:+})"


def check(expected: dict[str, Decimal], actual: dict[str, Decimal], assets: Iterable[str], *,
          tolerance_pct: Decimal, abs_eur: Decimal, abs_crypto: Decimal) -> list[Mismatch]:
    """Mismatches for `assets` beyond tolerance. An asset passes if the difference is within the
    absolute tolerance for its kind, or within tolerance_pct of the larger of the two amounts."""
    out = []
    for asset in assets:
        exp, act = expected.get(asset, ZERO), actual.get(asset, ZERO)
        diff = abs(act - exp)
        allowed = max(abs_eur if asset == "EUR" else abs_crypto, tolerance_pct * max(abs(exp), abs(act)))
        if diff > allowed:
            out.append(Mismatch(asset, exp, act))
    return out
