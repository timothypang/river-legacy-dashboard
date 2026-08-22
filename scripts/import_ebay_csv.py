#!/usr/bin/env python3
"""
scripts/import_ebay_csv.py — River Legacy standing eBay CSV import workflow

Reads an eBay Seller Hub "All Orders Report" CSV export and appends new,
non-duplicate orders into data/orders.json as RAW FACTS ONLY (no precomputed
profit/fees — the dashboard computes those at read time; see CLAUDE.md's
Data Store section for the canonical formulas).

Usage:
    python3 import_ebay_csv.py <csv_path> <orders_json_path>

Safe to re-run against overlapping exports — dedups against existing entries
by tracking number first, then falls back to (item title + sale date +
sold price) for older rows that predate the orderNumber field.

Flags rows that need Tim's manual review instead of guessing:
  - Missing/blank Custom Label -> can't infer Owner. Row is added with
    owner: "" and included in the FLAGGED output so Tim can assign it.
  - Sold For > $100 -> fee data isn't in this CSV format at all (no FVF
    column), so every row relies on the dashboard's 13.25%/$5.50 defaults.
    That assumption matters more at higher dollar amounts, so these are
    flagged for confirmation even though they're technically importable.
  - Item title doesn't look like a trading card -> per CLAUDE.md, non-card
    items belong in data/other-orders.json, not data/orders.json. Custom
    Label is blank on every row in this CSV export, so category can't be
    read directly off the file; a keyword heuristic flags likely-non-card
    titles for manual re-routing instead of silently misfiling them (this
    happened for real: a "NWT Ann Taylor Cardigan" row, already correctly
    filed in other-orders.json from an earlier import, got re-added to
    orders.json on the first version of this script because the two data
    files were never cross-checked against each other).
"""
import csv
import json
import os
import re
import sys
from datetime import datetime

# Loose signal that a title is a graded/collectible trading card, not a
# heuristic for what IS a card (that's a much longer list) — just enough to
# flag titles that DON'T match anything card-like, so they can be checked
# against data/other-orders.json before being filed into data/orders.json.
CARD_SIGNAL_RE = re.compile(
    r"\b(psa|bgs|cgc|sgc|tcg|pokemon|pok[ée]mon|holo|topps|panini|prizm|"
    r"card|rc\b|rookie)\b",
    re.IGNORECASE,
)


def load_tracking_set(json_path):
    if not json_path or not os.path.exists(json_path):
        return set()
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    out = set()
    for o in data:
        t = o.get("tracking")
        if t:
            out.add(t)
        # other-orders.json stores tracking inside the free-text "notes"
        # field rather than a dedicated column — pull it out defensively.
        notes = o.get("notes", "")
        m = re.search(r"Tracking:\s*(\S+)", notes)
        if m:
            out.add(m.group(1))
    return out


def parse_money(s):
    if not s:
        return None
    s = s.strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_ebay_date(s):
    """'Aug-12-26' -> '2026-08-12'"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        d = datetime.strptime(s, "%b-%d-%y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return s  # leave as-is if format is unexpected, rather than silently dropping


def load_ebay_rows(csv_path):
    """eBay's export has 2 junk lines before the header, a blank line after
    it, then data rows, then a trailing 'N,record(s) downloaded,' + Seller ID
    footer. Parse defensively rather than assuming fixed line numbers."""
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))

    header_idx = None
    for i, row in enumerate(reader):
        if row and row[0].strip() == "Sales Record Number":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Could not find header row in {csv_path}")

    headers = reader[header_idx]
    rows = []
    for row in reader[header_idx + 1:]:
        if not row or not row[0].strip():
            continue  # blank spacer row
        if "record(s) downloaded" in ",".join(row):
            break  # footer reached
        obj = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
        if not obj.get("Order Number"):
            continue
        # Combined-shipment orders emit one order-level "summary" row (blank
        # Item Title/Item Number, aggregate Sold For across all items) plus
        # one row per actual item underneath it, all sharing the same Sales
        # Record Number and Order Number. The summary row isn't a real line
        # item — skip it, or it becomes a phantom duplicate-looking order on
        # top of the real per-item rows. (Seen in the wild: 2026-05-18
        # Charizard TG03/TG30 + Gengar Fossil combined order.)
        if not obj.get("Item Title", "").strip():
            continue
        rows.append(obj)
    return rows


def existing_dedup_keys(orders):
    # NOTE: order number alone is NOT a safe dedup key. Combined-shipment
    # orders (multiple items, one buyer, one shipping label) share a single
    # eBay Order Number across several rows/items, so "order number already
    # seen" would wrongly treat the second item in a legit multi-item order
    # as a duplicate of the first. Dedup by (order number, item title) pairs
    # instead, plus tracking number and an item+date+price fallback for
    # legacy rows that predate the orderNumber field.
    by_tracking = set()
    by_fallback = set()
    by_order_item = set()
    for o in orders:
        if o.get("tracking"):
            by_tracking.add(o["tracking"])
        if o.get("orderNumber"):
            by_order_item.add((o["orderNumber"], o.get("item", "").strip()))
        fk = (o.get("item", "").strip(), o.get("date", ""), o.get("itemSubtotal"))
        by_fallback.add(fk)
    return by_order_item, by_tracking, by_fallback


def import_csv(csv_path, orders_json_path, other_orders_json_path=None):
    with open(orders_json_path, encoding="utf-8") as f:
        orders = json.load(f)

    if other_orders_json_path is None:
        # Default to the sibling other-orders.json in the same data/ folder.
        candidate = os.path.join(os.path.dirname(os.path.abspath(orders_json_path)), "other-orders.json")
        other_orders_json_path = candidate if os.path.exists(candidate) else None

    by_order_item, by_tracking, by_fallback = existing_dedup_keys(orders)
    other_orders_tracking = load_tracking_set(other_orders_json_path)
    seen_this_run = set()  # guard against dupes across multiple CSVs in one run — keyed by (order_num, item), not tracking alone (combined-shipment orders share one tracking # across items)

    ebay_rows = load_ebay_rows(csv_path)
    added, skipped_dupe, flagged_no_owner, flagged_high_value, flagged_non_card = [], [], [], [], []

    for r in ebay_rows:
        order_num = r.get("Order Number", "").strip()
        tracking = r.get("Tracking Number", "").strip()
        item = r.get("Item Title", "").strip()
        date = parse_ebay_date(r.get("Sale Date", ""))
        sold_for = parse_money(r.get("Sold For", ""))
        fallback_key = (item, date, sold_for)
        order_item_key = (order_num, item)

        if order_num and order_item_key in by_order_item:
            skipped_dupe.append((item, date, "order number + item already present"))
            continue
        if fallback_key in by_fallback:
            skipped_dupe.append((item, date, "item+date+price match already present"))
            continue
        if tracking and tracking in by_tracking and fallback_key not in by_fallback:
            # Tracking matches an existing row but item+date+price doesn't —
            # likely a sibling item in a combined shipment that's already
            # been imported under this tracking number. Treat as a probable
            # duplicate rather than silently importing; surface it instead
            # of guessing.
            skipped_dupe.append((item, date, "tracking number already present (different item — verify manually)"))
            continue
        if tracking and tracking in other_orders_tracking:
            # Already recorded as a non-card item in other-orders.json —
            # don't re-file it into orders.json too.
            skipped_dupe.append((item, date, "tracking number already present in other-orders.json"))
            continue
        if order_item_key in seen_this_run:
            skipped_dupe.append((item, date, "duplicate within this CSV/run"))
            continue

        custom_label = r.get("Custom Label", "").strip()
        owner = custom_label if custom_label else ""
        if not owner:
            flagged_no_owner.append((item, date, sold_for))

        if sold_for is not None and sold_for > 100:
            flagged_high_value.append((item, date, sold_for))

        if not CARD_SIGNAL_RE.search(item):
            # Doesn't contain any obvious card/grading keyword. Per
            # CLAUDE.md, non-card items (clothing, gear, etc.) belong in
            # data/other-orders.json, not data/orders.json. Still added here
            # (better to have it recorded somewhere than dropped), but
            # flagged so Tim can move it if it's genuinely not a card.
            flagged_non_card.append((item, date, sold_for))

        ebay_tax = parse_money(r.get("eBay Collected Tax", "")) or 0
        seller_tax = parse_money(r.get("Seller Collected Tax", "")) or 0
        # Seller Collected Tax is a rare case for River Legacy (usually $0);
        # fold it in additively since both count toward the FVF-taxable total
        # per eBay's actual fee policy (see CLAUDE.md Data Store formulas).
        total_tax = round(ebay_tax + seller_tax, 2)

        new_row = {
            "date": date,
            "item": item,
            "owner": owner,
            "itemSubtotal": sold_for,
            "buyerShipping": parse_money(r.get("Shipping And Handling", "")) or 0,
            "ebayTax": total_tax,
            "otherFees": None,       # dashboard applies $0.40 default
            "shippingLabel": None,   # dashboard applies $5.50 default
            "refund": None,
            "buyer": r.get("Buyer Username", "").strip(),
            "shipState": r.get("Ship To State", "").strip(),
            "tracking": tracking,
            "status": "Shipped",
            "paidOut": None,
            "orderNumber": order_num,  # new field, enables cleaner dedup next time
        }
        orders.append(new_row)
        added.append(new_row)
        seen_this_run.add(order_item_key)

    with open(orders_json_path, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)

    return {
        "added": added,
        "skipped_dupe": skipped_dupe,
        "flagged_no_owner": flagged_no_owner,
        "flagged_high_value": flagged_high_value,
        "flagged_non_card": flagged_non_card,
        "total_orders_now": len(orders),
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 import_ebay_csv.py <csv_path> <orders_json_path>")
        sys.exit(1)
    result = import_csv(sys.argv[1], sys.argv[2])
    print(f"Added: {len(result['added'])}")
    print(f"Skipped as duplicate: {len(result['skipped_dupe'])}")
    print(f"Flagged (no owner/Custom Label): {len(result['flagged_no_owner'])}")
    print(f"Flagged (>$100, verify FVF assumption): {len(result['flagged_high_value'])}")
    print(f"Flagged (doesn't look like a card, verify data file): {len(result['flagged_non_card'])}")
    print(f"Total orders in file now: {result['total_orders_now']}")
