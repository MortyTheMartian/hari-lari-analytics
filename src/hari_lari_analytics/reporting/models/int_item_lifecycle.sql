-- First and last sale of every adjusted item-variant up to the anchor, for new, rising and
-- fading items (plan 10.15). Refunds do not count as sales.
create or replace view int_item_lifecycle as
select
    adjusted_item,
    adjusted_item_variant,
    arg_max(adjusted_category, sale_ts) as adjusted_category,
    min(sale_ts) as first_sale_ts,
    max(sale_ts) as last_sale_ts,
    min(sale_date) as first_sale_date,
    max(sale_date) as last_sale_date
from int_sales_lines
where not is_refund
group by adjusted_item, adjusted_item_variant
