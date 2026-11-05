"""
review_bot.py

A rough first pass at automating PR review. Point it at a diff and it
prints a review comment.

Usage:
    python review_bot.py sample_diff.txt

Written in a hurry by someone on the team who wanted to stop reading every
PR line by line. It "works" but nobody's touched it since.
"""

import sys
import openai  # pip install openai

openai.api_key = "sk-REPLACE-ME"  # TODO: move this somewhere safer

PROMPT = """You are a senior engineer reviewing a pull request. Read the diff
below and write a short review comment covering anything that looks wrong,
risky, or worth changing.

DIFF:
{diff}
"""


def main():
    diff_path = sys.argv[1]
    with open(diff_path) as f:
        diff = f.read()

    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": PROMPT.format(diff=diff)}],
    )

    comment = response["choices"][0]["message"]["content"]
    print(comment)


if __name__ == "__main__":
    main()
