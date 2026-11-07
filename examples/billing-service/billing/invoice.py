"""Invoice model for the billing service."""
from billing.discounts import DISCOUNT_CODES


class Invoice:
    def __init__(self, id, line_items, tax_rate=None):
        self.id = id
        self.line_items = line_items
        self.tax_rate = tax_rate  # applied by callers, see billing/reports.py
        self.total_override = None

    def total(self):
        subtotal = sum(item.price * item.qty for item in self.line_items)
        return subtotal

    def payable(self):
        """What the customer pays: the discounted total if a code was applied."""
        if self.total_override is not None:
            return self.total_override
        return self.total()

    def add_item(self, item):
        """Add a line item (anything with .price and .qty)."""
        self.line_items.append(item)

    def item_count(self):
        """Number of units across all line items."""
        return sum(item.qty for item in self.line_items)

    def apply_discount(self, code):
        discount = DISCOUNT_CODES.get(code)
        if discount:
            self.total_override = self.total() * (1 - discount)


def invoice_from_dict(data):
    """Build an Invoice from the API payload shape."""
    return Invoice(data["id"], list(data.get("line_items", [])), data.get("tax_rate"))
