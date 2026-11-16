"""Structural look at the title shape of A2 articles that gave no base (counts only).

Part of the catalyst-feasibility-v1 A2 diagnostic (docs/research/
catalyst-feasibility-a2-diagnostic-2026-11-16.md). The classes and their order are
fixed here, before the one run on the sealed holdout, and are not changed after the
counts are seen. The look reads no market value, fetches nothing and contacts no
exchange; it reads the kept receipts of a finished run read-only.

Output is the total and one count per class. No title text, ticker, article id or
date is printed or written anywhere, and a mismatch reports counts only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import catalyst_feasibility as cf

CLASSES = (
    "usdt_pair",
    "paren_ticker",
    "slash_pair",
    "spaced_pair",
    "usdc_pair",
    "multiple_no_ticker",
    "index",
    "other",
)
SLASH_PAIR = re.compile(r"\b[A-Z0-9]{1,20}/USDT\b")
SPACED_PAIR = re.compile(r"\b[A-Z0-9]{1,20}\s+USDT\b")
USDC_PAIR = re.compile(r"\b[A-Z0-9]{1,20}/?USDC\b")
MULTIPLE = re.compile(r"\bMultiple\b", re.IGNORECASE)
INDEX = re.compile(r"\bIndex\b", re.IGNORECASE)


def shape_class(title: str) -> str:
    """The first matching class, in the order of CLASSES.

    usdt_pair: a ...USDT token the current A2 rule would read. paren_ticker: a ticker
    in parentheses. slash_pair: AAA/USDT. spaced_pair: AAA USDT. usdc_pair: AAAUSDC or
    AAA/USDC. multiple_no_ticker: the word Multiple and none of the forms above.
    index: the word Index and none of the above. other: everything else, including
    coin-margined products.
    """
    if cf._pairs(title):
        return "usdt_pair"
    if cf._parens(title):
        return "paren_ticker"
    if SLASH_PAIR.search(title):
        return "slash_pair"
    if SPACED_PAIR.search(title):
        return "spaced_pair"
    if USDC_PAIR.search(title):
        return "usdc_pair"
    if MULTIPLE.search(title):
        return "multiple_no_ticker"
    if INDEX.search(title):
        return "index"
    return "other"


def _load_receipts(root: Path) -> list[dict[str, Any]]:
    """The run's ok receipts as the report reads them, from files, never writing."""
    receipts: list[dict[str, Any]] = []
    for path in sorted((root / "inherited").glob("*.json")):
        receipts += json.loads(path.read_text())["receipts"]
    for path in sorted((root / "receipts").glob("*.json")):
        receipts.append(json.loads(path.read_text()))
    return receipts


def no_base_a2_titles(receipts: list[dict[str, Any]], window: str) -> list[str]:
    """Titles of A2 articles in a window that the A2 rule read no base from."""
    wanted = {
        a["id"]
        for a in cf.articles(receipts)
        if a["source"] == "binance" and a["bucket"] == "A2" and a["window"] == window
        if not a["bases"]
    }
    titles: dict[str, str] = {}
    for r in receipts:
        if r["outcome"] == "ok" and r["series"].startswith("binance:"):
            for item in r["kept"]:
                if str(item["id"]) in wanted:
                    titles[str(item["id"])] = str(item["title"])
    return list(titles.values())


def count_shapes(titles: list[str], expect_total: int | None = None) -> dict[str, Any]:
    counts = Counter(shape_class(t) for t in titles)
    total = sum(counts.values())
    if expect_total is not None and total != expect_total:
        raise ValueError(f"total {total} differs from the expected {expect_total}")
    return {"total": total, "classes": {c: counts.get(c, 0) for c in CLASSES}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--window", choices=sorted(cf.WINDOWS), required=True)
    parser.add_argument("--expect-total", type=int)
    args = parser.parse_args(argv)
    titles = no_base_a2_titles(_load_receipts(args.root), args.window)
    sys.stdout.write(json.dumps(count_shapes(titles, args.expect_total), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
