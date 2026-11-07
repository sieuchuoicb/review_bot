"""
review_bot.py

Kept as a compatibility entry point, because people quietly rely on it:

    python review_bot.py sample_diff.txt                 # same call as before
    python review_bot.py sample_diff.txt --repo PATH     # full checks

The original sent the whole diff to one LLM call and printed free text, with
an API key in the source. It now runs the review_brief pipeline instead:
rules detect, the LLM answers only the narrow questions the rules raise, and
a human decides (`review-brief triage`). See README.md.

Keys come from the environment or .env (OPEN_ROUTER_API_KEY, LLM_MODEL_NAME).
"""

import sys

from review_brief.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["review", *sys.argv[1:]]))
