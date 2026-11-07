# PR Review Brief

**Risk: HIGH** · diff `sample_diff.txt` · repo `examples/billing-service` · mode: full · LLM: openrouter/z-ai/glm-5.3-flash, 2 call(s)

Changed: `billing/invoice.py`

> Advisory only. This brief does not approve or merge anything; a human reviewer decides.

## Why this risk level
- [high] test fails after the PR: test_total_excludes_tax
- [high] touches sensitive path billing/**
- [medium] lint S110, BLE001 on a changed line (billing/invoice.py:35)
- [medium] public function Invoice.total changed and has 4 caller(s)
- [medium] new public function Invoice.export_csv has no test

## Proven by tools and rules
1. **[high · safety]** `tests/test_invoice.py:14` Test regression: test_total_excludes_tax passes before this PR and fails after it  
   _pytest (before vs after)_
   - evidence: `assert 110.00000000000001 == 100.0 ± 1.0e-04`
2. **[high · policy]** `billing/invoice.py` Touches billing/**: Moves money. Needs 2 approving reviewers.  
   _rule:sensitive-path_
   - Files: billing/invoice.py
3. **[medium · safety]** `billing/invoice.py:12` Invoice.total() changed; 4 call site(s) depend on it, including billing/reports.py  
   _rule:changed-function-callers (AST, matched by name)_
   - billing/invoice.py:20 in Invoice.payable: return self.total()
   - billing/invoice.py:34 in Invoice.apply_discount: self.total_override = self.total() * (1 - discount)
   - billing/invoice.py:40 in Invoice.export_csv: f.write(f"invoice_id,total\n{self.id},{self.total()}\n")
   - billing/reports.py:12 in monthly_revenue: revenue += inv.total() * (1 + rate)
4. **[medium · quality]** `billing/invoice.py:35` `try`-`except`-`pass` detected, consider logging the exception / Do not catch blind exception: `Exception`  
   _ruff S110, BLE001_
   - evidence: `except Exception:`
   - S110: `try`-`except`-`pass` detected, consider logging the exception
   - BLE001: Do not catch blind exception: `Exception`
5. **[medium · policy]** `billing/invoice.py:38` New public function Invoice.export_csv() has no test that references it  
   _rule:new-public-function-requires-test_

## Needs your judgment (LLM hypotheses, not verified)
1. **[medium · safety]** `billing/reports.py:12` monthly_revenue now double-applies tax because it multiplies inv.total() by (1 + rate) even though total() itself now includes tax.  
   _llm:openrouter/z-ai/glm-5.3-flash · behaviour_change · confidence high_
   - evidence: `revenue += inv.total() * (1 + rate)`
   - The docstring (line 7) states the assumption 'Invoice.total() is pre-tax, so tax is applied here', but after this PR total() returns subtotal * (1 + tax_rate). For any invoice with a nonzero tax_rate the contribution becomes subtotal * (1+rate)^2, so monthly revenue is taxed twice.
2. **[medium · safety]** `billing/invoice.py:34` apply_discount now computes the discount on the tax-inclusive total, so the same discount code produces a different total_override (and thus a different payable) than before for any invoice with a tax rate.  
   _llm:openrouter/z-ai/glm-5.3-flash · behaviour_change · confidence medium_
   - evidence: `self.total_override = self.total() * (1 - discount)`
   - Line 34 sets total_override = self.total() * (1 - discount). total() previously returned the pre-tax subtotal and now returns subtotal * (1 + tax_rate), so the discount silently applies to tax as well; payable() (lines 18-20) returns this override, changing what discounted customers are charged.
3. **[medium · safety]** `billing/invoice.py:40` export_csv writes self.total() instead of self.payable(), so an invoice with an applied discount exports the pre-discount amount, contradicting the class's own definition of what the customer pays.  
   _llm:openrouter/z-ai/glm-5.3-flash · new_method · confidence high_
   - evidence: `f.write(f"invoice_id,total\n{self.id},{self.total()}\n")`
   - apply_discount (lines 30-36) sets total_override, and payable() (lines 16-20) is the method that returns the discounted amount; export_csv bypasses it and reports the raw total, producing a CSV that disagrees with payable() for any discounted invoice.

## Questions the rules raised for the LLM
- [answered] `Invoice.total` (behaviour_change): Invoice.total() changed and has 4 caller(s) in 1 other file(s) (2 grounded concern(s))
- [answered] `Invoice.export_csv` (new_method): new public method Invoice.export_csv (1 grounded concern(s))

## Where to look first
1. `tests/test_invoice.py:14` Test regression: test_total_excludes_tax passes before this PR and fails after it
2. `billing/reports.py:12` monthly_revenue now double-applies tax because it multiplies inv.total() by (1 + rate) even though total() itself now includes tax.
3. `billing/invoice.py:34` apply_discount now computes the discount on the tax-inclusive total, so the same discount code produces a different total_override (and thus a different payable) than before for any invoice with a tax rate.
4. `billing/invoice.py:40` export_csv writes self.total() instead of self.payable(), so an invoice with an applied discount exports the pre-discount amount, contradicting the class's own definition of what the customer pays.
5. `billing/invoice.py:12` Invoice.total() changed; 4 call site(s) depend on it, including billing/reports.py

## Notes
- Policy: policy.yaml.
- Tests: 2/3 pass after the PR; regressions it caused: 1.
