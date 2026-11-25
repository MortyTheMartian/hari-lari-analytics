-- One row per line of a payment, amounts as Moka reports them (a refund payment repeats the
-- refunded lines with positive amounts). Variants are title-cased with Python's rules, so
-- "cold" and "Cold" are one variant. The free-text note may contain names: it stays in the
-- models (plan 10.3) and never goes into an export.
create or replace view stg_transaction_lines as
select
    payment_id,
    payment_no,
    uuid as line_uuid,
    id as line_id,
    _row as line_row,
    item_id,
    trim(item_name) as item,
    title_case(nullif(trim(item_variant_name), '')) as variant,
    item_variant_id as variant_id,
    category_id,
    coalesce(nullif(trim(category_name), ''), 'Uncategorized') as category,
    quantity,
    gross_sales,
    discount_amount,
    net_sales,
    nullif(trim(note), '') as note
from raw_transaction_lines
