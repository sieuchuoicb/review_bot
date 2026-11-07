## Review notes

Risk assessed by rules: **HIGH**

### Confirmed by tools
- `tests/test_invoice.py:14` Test regression: test_total_excludes_tax passes before this PR and fails after it _(pytest (before vs after))_
- `billing/invoice.py` Touches billing/**: Moves money. Needs 2 approving reviewers. _(rule:sensitive-path)_
- `billing/invoice.py:35` `try`-`except`-`pass` detected, consider logging the exception / Do not catch blind exception: `Exception` _(ruff S110, BLE001)_
- `billing/invoice.py:38` New public function Invoice.export_csv() has no test that references it _(rule:new-public-function-requires-test)_
  - **Reviewer:** Please add a test that exports a discounted invoice

### Raised for discussion
- `billing/reports.py:12` monthly_revenue now double-applies tax because it multiplies inv.total() by (1 + rate) even though total() itself now includes tax. _(llm:openrouter/z-ai/glm-5.3-flash · behaviour_change · confidence high)_
  - The docstring (line 7) states the assumption 'Invoice.total() is pre-tax, so tax is applied here', but after this PR total() returns subtotal * (1 + tax_rate). For any invoice with a nonzero tax_rate the contribution becomes subtotal * (1+rate)^2, so monthly revenue is taxed twice.
- `billing/invoice.py:34` apply_discount now computes the discount on the tax-inclusive total, so the same discount code produces a different total_override (and thus a different payable) than before for any invoice with a tax rate. _(llm:openrouter/z-ai/glm-5.3-flash · behaviour_change · confidence medium)_
  - Line 34 sets total_override = self.total() * (1 - discount). total() previously returned the pre-tax subtotal and now returns subtotal * (1 + tax_rate), so the discount silently applies to tax as well; payable() (lines 18-20) returns this override, changing what discounted customers are charged.
  - **Reviewer:** Confirm with finance whether discounts apply before or after tax
- `billing/invoice.py:40` export_csv writes self.total() instead of self.payable(), so an invoice with an applied discount exports the pre-discount amount, contradicting the class's own definition of what the customer pays. _(llm:openrouter/z-ai/glm-5.3-flash · new_method · confidence high)_
  - apply_discount (lines 30-36) sets total_override, and payable() (lines 16-20) is the method that returns the discounted amount; export_csv bypasses it and reports the raw total, producing a CSV that disagrees with payable() for any discounted invoice.

---
_Advisory, drafted by review-brief. Each item above was accepted by reviewer-1. This comment is not an approval._
