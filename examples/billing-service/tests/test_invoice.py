from dataclasses import dataclass

import pytest

from billing.invoice import Invoice


@dataclass
class Item:
    price: float
    qty: int


def test_total_excludes_tax():
    inv = Invoice("INV-1", [Item(50.0, 2)], tax_rate=0.1)
    assert inv.total() == pytest.approx(100.0)


def test_apply_discount_sets_payable():
    inv = Invoice("INV-2", [Item(100.0, 1)])
    inv.apply_discount("WELCOME10")
    assert inv.payable() == pytest.approx(90.0)


def test_unknown_code_is_ignored():
    inv = Invoice("INV-3", [Item(100.0, 1)])
    inv.apply_discount("NOPE")
    assert inv.payable() == pytest.approx(100.0)
