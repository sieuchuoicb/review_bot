"""Revenue reporting consumed by the finance team's month-end close."""


def monthly_revenue(invoices):
    """Revenue for the month, including tax.

    Invoice.total() is pre-tax, so tax is applied here, per invoice.
    """
    revenue = 0.0
    for inv in invoices:
        rate = inv.tax_rate or 0.0
        revenue += inv.total() * (1 + rate)
    return round(revenue, 2)
