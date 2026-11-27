# Metrics and dimensions

All figures are computed from line-level Moka sales data after cleaning, from the Moka API: the
Parquet tables of one extraction run. The report models (`reporting/models/`) apply the rules
below in SQL. Amounts are whole Indonesian Rupiah (IDR).

## Cleaning rules

- **Amounts** are whole Rupiah; a fractional amount stops the run. Every line must satisfy
  `Gross Sales − Discounts − Refunds = Net Sales`, otherwise the run stops.
- **Lines** are the `transaction_lines`, joined to their payment for the receipt number,
  timestamp and payment type (the payment's `payment_type_label`). The discount code is the
  `discount_name` in `transaction_line_discounts`; if a line has several, they are joined
  with ` + `.
- **Refunds.** A refund is a second payment with the same receipt number whose
  `parent_payment_id` points to the original payment. It repeats the refunded lines with
  positive amounts. They become lines with Event Type `Refund`: negative quantity, Gross Sales
  and Discounts 0, the amount in Refunds and the negative amount in Net Sales, so
  `Gross − Discounts − Refunds = Net Sales` holds on every line. The refunded item, variant
  and category are those of the line itself, and the reason comes from `transaction_refunds`.
- **Fully refunded receipts** (a receipt with a refund line and net sales ≤ 0) do not count as
  orders. Their lines stay in the data, so the sales amounts still add up.
- **Identical lines are kept.** The same item can appear twice on one receipt as separate lines.
- **Variants** are capitalized consistently (`cold` → `Cold`).
- **Personal data** (customer, collected by, served by) is not read. The free-text line note may
  contain names: it stays in the models and never goes into an export.
- **Timestamps** are Moka's local time (Bali); there is no timezone conversion. The API labels
  its timestamps `+07:00`, but the clock time already is Bali time, so the extraction drops the
  offset instead of converting it.
- **Ids.** Every line also carries `payment_id`, `item_id`, `variant_id` and `category_id`.
- **Modifiers** (add-ons and choices such as *Chicken* or *Poached*, in use since 21 Nov 2026)
  are on the line as `modifier_id`, `modifier_option_id`, `modifier_name`,
  `modifier_option_name` and `modifier_price`, linked through the payment and the line's uuid
  (`transaction_line_modifiers`). They are NULL for lines without modifiers, which is nearly
  all lines. A line stays one row, because Moka already includes the modifier prices in the
  line's gross sales; `modifier_price` is informational and must not be added to the sales. A
  line with several modifiers lists them joined by ` + `, ordered by option id, so the ids,
  names and option names line up; `modifier_price` is then the sum of their unit prices. The
  Excel export shows the modifier name, option and price.

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
| Category AOV | Net Sales IDR of the category ÷ orders containing the category |
| Contributive AOV | % Category Order Penetration × Category AOV = Net Sales IDR of the category ÷ all orders |
| First Sale Timestamp | Earliest payment line, excluding fully refunded receipts |
| Last Sale Timestamp | Latest payment line, excluding fully refunded receipts |

Orders are distinct counts: they cannot be summed across categories, items or payment types,
because one receipt can contain several. Always count them from the lines.

Penetration always uses the orders of the current filter as its denominator. For example, in
the *last 14 days* section, "Coffee 60%" means 60% of all receipts in those 14 days contained a
coffee; filtered to payment type BCA, it would be the share of BCA receipts.

### Contributive AOV

The Contributive AOVs split the AOV into one slice per category:

```text
Net Sales        = Orders × AOV
AOV              = Σ Contributive AOV over all categories
Contributive AOV = % Category Order Penetration × Category AOV
```

The slices add up to the AOV exactly, and a category's share of the AOV equals its share of
net sales. A change in a slice has two possible causes: the category appears in more or fewer
orders (penetration: an inclusion question), or orders with the category spend more or less
on it (Category AOV: a pricing or basket-size question).

The **Total** line of the Categories page sums Net Sales, Items Sold and Contributive AOV
over the categories. Its Orders are all distinct receipts, not the sum of the category orders.
Its penetration is therefore 100%, and its Category AOV and Contributive AOV both equal the
AOV. When there is a prior period, categories sold only in the prior period stay in the table
with zero sales, so that the changes in Contributive AOV add up to the change in AOV.
A category whose lines all sit on fully refunded receipts has no orders and no Category AOV,
but its (usually zero) net sales still count towards its Contributive AOV.

## Dimensions

| Dimension | Definition |
| --- | --- |
| Sale Timestamp | Date and time of the line, truncated as Date-Hour, Date, Week (Monday of the ISO week, plus an ISO week label like `2026-W44`), Month, Quarter or Year |
| Hour of Day | 0–23 |
| Category | Category recorded at the time of sale |
| Adjusted Category | Category according to today's set of categories, see below |
| Item | Item name recorded at the time of sale |
| Adjusted Item | Today's name of the item, see below |
| Item-Variant | `Item-Variant` as sold, or just `Item` when there is no variant |
| Adjusted Item-Variant | The adjusted item and today's name of the line's variant, found by the variant's id; just the adjusted item when that variant has no name, was deleted or is unknown, so a variant Moka renamed or dropped is still the same item |
| Discounted | TRUE when a discount was applied |
| Discount Code | Name of the applied discount, empty if none |
| Payment Type | Payment method of the receipt |

### Adjusted Item and Adjusted Category

Items are matched by their Moka id, for every item id and the name it was sold under:

1. If `config/item_aliases.csv` lists the name the item was sold under, the alias wins:
   `adjusted_item` replaces the name (empty: keep the sold name) and `adjusted_category` (if
   filled) sets the category.
2. Otherwise a live item gives today's name and category through its item id, so an item
   that was renamed in Moka needs no alias.
3. A deleted item without alias keeps its sold name (unless it matches a live item, ignoring
   capitalization) and the category it last had in Moka. An item that is not in the catalog
   at all keeps its latest category in the sales data and is listed in the run output.

In every case the adjusted name is matched with the live items, ignoring capitalization, to
get today's spelling and category.

Open-price entries (`Custom Amount` in Moka) get their own category, `Custom Amount`, through
an alias row.

## Report periods

All periods end at the **anchor**, the last day of the report. Sales after the anchor are
ignored. The anchor is set with `--anchor`; by default it is yesterday (Bali date), so the
report that is sent on a Monday morning covers up to and including Sunday.

| Section | Period | Trend by | Compared with |
| --- | --- | --- | --- |
| L14D | The 14 days up to and including the anchor | day | the 14 days before |
| L6W | The last 6 complete ISO weeks (Monday–Sunday); the anchor's week counts only if the anchor is a Sunday | ISO week | the 6 weeks before |
| L3M | The last 3 complete calendar months; the anchor's month counts only if the anchor is its last day | month | the 3 months before |

A comparison shows `n/a` when the earlier period starts before the first sale.

The small line charts on the KPI cards show one point per day in L14D. In L6W and L3M they
show one point per 7-day block, counted back from the period's last day so the latest point is
a full week; leftover days at the start of the period (in L3M usually one to three) are left
out of the line chart, but not out of the totals.

### Week-over-week and month-over-month growth

The trend charts of L6W and L3M label every bar with its growth over the bar before:
`net sales of the bucket ÷ net sales of the previous bucket − 1`, and the same for orders.
The first bar compares with the week or month just before the period, but only if that bucket
lies completely within the sales history; otherwise, and whenever the previous bucket has no
sales, the growth is `n/a`. The daily trend (L14D) has no growth labels.
