"""Build the spec index from a vendor workbook.

    uv run python -m trading.rag.build_index [workbook] [-o out.json]

Parsing the 339-sheet workbook takes a few seconds, so the agent reads the JSON
index rather than the xlsx at runtime.
"""

from __future__ import annotations

import argparse
from collections import Counter

from trading.rag.spec_store import DEFAULT_INDEX, SpecStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workbook", nargs="?", default="KIWOOM_API.xlsx")
    ap.add_argument("-o", "--out", default=str(DEFAULT_INDEX))
    args = ap.parse_args()

    store = SpecStore.from_workbook(args.workbook)
    path = store.save(args.out)

    missing = [e.api_id for e in store.catalog if e.api_id not in store.specs]
    empty = [s.api_id for s in store.specs.values() if not s.request and not s.response]
    by_cat = Counter(e.category for e in store.catalog)

    print(f"catalog: {len(store.catalog)} apis, specs parsed: {len(store.specs)}")
    for cat, n in by_cat.most_common():
        print(f"  {cat or '(uncategorised)'}: {n}")
    if missing:
        print(f"no sheet for {len(missing)} catalog entries: {missing[:10]}")
    if empty:
        print(f"parsed but no fields for {len(empty)}: {empty[:10]}")
    print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
