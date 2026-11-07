# billing-service (fixture)

A tiny stand-in for the service `sample_diff.txt` was written against. The
exercise ships only a diff, so this repo gives the review pipeline real code
to run tools on.

It was built on purpose so that every path in the pipeline gets exercised:

- `billing/invoice.py` is the pre-PR version. The diff's hunks land at
  `def total` (line 12) and `def apply_discount` (line 30).
- `Invoice.total()` is pre-tax by design, and `tests/test_invoice.py` pins
  that. The PR breaks the test, which gives a **proven** regression.
- `billing/reports.py` applies tax itself and has no tests, so only a reader
  who looks at the callers (here, the LLM) can spot that the PR now taxes
  revenue twice. That gives a **hypothesis** for a human to judge.
