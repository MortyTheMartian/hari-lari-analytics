-- The cleaned sales lines up to the anchor: the signed lines with the adjusted item and
-- category, the time truncations, the item-variant labels and the category groups (the
-- pandas pipeline: load_api_sales, add_dimensions, add_category_groups, the anchor cut).
-- Category groups are joined by adjusted category name, never by category id, because Moka
-- reuses ids with a new meaning (decision D10). A category without a group is 'unmapped'.
create or replace view int_sales_lines as
select
    l.receipt_nr,
    l.sale_ts,
    l.event_type,
    l.is_refund,
    l.refund_reason,
    l.category,
    l.item,
    l.variant,
    l.quantity,
    l.gross_sales,
    l.discounts,
    l.refunds,
    l.net_sales,
    l.discount_code,
    l.payment_type,
    l.payment_id,
    l.item_id,
    l.variant_id,
    l.category_id,
    l.modifier_id,
    l.modifier_option_id,
    l.modifier_name,
    l.modifier_option_name,
    l.modifier_price,
    l.discounted,
    l.counts_as_order,
    r.adjusted_item,
    r.adjusted_category,
    date_trunc('hour', l.sale_ts) as sale_hour,
    l.sale_ts::date as sale_date,
    date_trunc('week', l.sale_ts)::date as sale_week,
    isoyear(l.sale_ts)::varchar || '-W' || lpad(week(l.sale_ts)::varchar, 2, '0') as sale_iso_week,
    date_trunc('month', l.sale_ts)::date as sale_month,
    year(l.sale_ts)::varchar || '-Q' || quarter(l.sale_ts)::varchar as sale_quarter,
    year(l.sale_ts) as sale_year,
    hour(l.sale_ts) as hour_of_day,
    isodow(l.sale_ts) as weekday,
    case when l.variant is null then l.item else l.item || '-' || l.variant end as item_variant,
    case
        when l.variant is null then r.adjusted_item
        else r.adjusted_item || '-' || l.variant
    end as adjusted_item_variant,
    coalesce(g.category_group_level_1, 'unmapped') as category_group_level_1,
    nullif(g.category_group_level_2, '') as category_group_level_2,
    l.line_uuid,
    l.line_id,
    l.line_row,
    l.note
from int_signed_lines as l
left join int_item_resolution as r
    on l.item_id is not distinct from r.item_id and l.item is not distinct from r.item
left join seed_category_groups as g
    on r.adjusted_category = g.category
where l.sale_ts < (select anchor from run_context) + interval 1 day
