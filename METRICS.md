# Metrics and dimensions

All figures are computed from the line-level Moka *Item Details* export after cleaning.
Amounts are whole Indonesian Rupiah (IDR).

## Cleaning rules

- **Amounts** are read as integers. Every line must satisfy
  `Gross Sales − Discounts − Refunds = Net Sales`, otherwise the run stops.
- **Refunds** appear in Moka as an extra line on the original receipt with Event Type
  `Refund`, a negative quantity, Gross Sales 0 and the refunded amount in Refunds. The item
  name of such a line (`Full Refund for: <receipt> - <item>`) is replaced by the refunded item,
  and its category and variant are copied from the original line.
- **Fully refunded receipts** (a receipt with a refund line and net sales ≤ 0) do not count as
  orders. Their lines stay in the data, so the sales amounts still add up.
- **Identical lines are kept.** Moka has no line ID, and the same item can appear twice on one
  receipt as separate lines.
- **Variants** are capitalized consistently (`cold` → `Cold`).
- **Personal data** (Customer, Collected By, Served By) is dropped.
- **Timestamps** are Moka's local time; there is no timezone conversion.

## Metrics

| Metric | Definition |
| --- | --- |
| Gross Sales IDR | Sum of Gross Sales |
| Discounts IDR | Sum of Discounts (positive numbers) |
| Refunds IDR | Sum of Refunds (positive numbers) |
| Net Sales IDR | Sum of Net Sales = Gross − Discounts − Refunds |
| Orders | Distinct count of receipt numbers, excluding fully refunded receipts |
| Average Order Value | Net Sales IDR ÷ Orders |
| Items Sold | Sum of Quantity (refund lines subtract) |
| % Category Order Penetration | Orders containing at least one line of the category ÷ all orders, both within the same filter |
| First Sale Timestamp | Earliest payment line, excluding fully refunded receipts |
| Last Sale Timestamp | Latest payment line, excluding fully refunded receipts |

Orders are distinct counts: they cannot be summed across categories, items or payment types,
because one receipt can contain several. Always count them from the lines.

Penetration always uses the orders of the current filter as its denominator. For example, in
the *last 14 days* section, "Coffee 60%" means 60% of all receipts in those 14 days contained a
coffee; filtered to payment type BCA, it would be the share of BCA receipts.

## Dimensions

| Dimension | Definition |
| --- | --- |
| Sale Timestamp | Date and time of the line, truncated as Date-Hour, Date, Week (Monday of the ISO week, plus an ISO week label like `2026-W44`), Month, Quarter or Year |
| Hour of Day | 0–23 |
| Category | Category recorded at the time of sale |
| Adjusted Category | Category according to today's set of categories, see below |
| Item | Item name recorded at the time of sale |
| Adjusted Item | Today's name of the item, see below |
| Item-Variant | `Item-Variant`, or just `Item` when there is no variant (also as Adjusted Item-Variant) |
| Discounted | TRUE when a discount was applied |
| Discount Code | Name of the applied discount, empty if none |
| Payment Type | Payment method of the receipt |

### Adjusted Item and Adjusted Category

For every item name in the sales export, in this order:

1. If `config/item_aliases.csv` lists it, `adjusted_item` replaces the name and
   `adjusted_category` (if filled) sets the category.
2. The (adjusted) name is looked up in the item library, ignoring capitalization; a match
   gives today's spelling and, unless overridden in step 1, today's category.
3. Otherwise the item keeps its most recent category in the sales export (preferring any real
   category over `Uncategorized`). These items are listed in the run output.

Open-price entries (`Custom Amount` in Moka) get their own category, `Custom Amount`.

## Report periods

All periods end at the **date of the last sale** in the export (the anchor).

| Section | Period | Trend by | Compared with |
| --- | --- | --- | --- |
| L14D | The 14 days up to and including the anchor | day | the 14 days before |
| L6W | The last 6 complete ISO weeks (Monday–Sunday); the anchor's week counts only if the anchor is a Sunday | ISO week | the 6 weeks before |
| L3M | The last 3 complete calendar months; the anchor's month counts only if the anchor is its last day | month | the 3 months before |

A comparison shows `n/a` when the earlier period starts before the first sale in the export.
