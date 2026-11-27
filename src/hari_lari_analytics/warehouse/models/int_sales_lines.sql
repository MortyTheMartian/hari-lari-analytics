-- The cleaned sales lines up to the anchor: the signed lines with the adjusted item and
-- category, the time truncations, the item-variant labels and the category groups.
-- Category groups are joined by adjusted category name, never by category id, because Moka
-- reuses ids with a new meaning (decision D10). A category without a group is 'unmapped'.
-- Items keep their identity by id (plan 9.8): `adjusted_item_variant` is today's label of the
-- line's variant (adjusted_item plus the live variant's name), or just adjusted_item when the
-- variant is deleted, unknown or absent; `item_variant` stays as sold.
create or replace view int_sales_lines as
with live_variants as (  -- the variants of today's catalog that are not deleted
    select distinct on (variant_id) variant_id, item_id, variant
    from stg_item_variants
    where not coalesce(is_deleted, false)
    order by variant_id, item_id
),

variant_catalog as (  -- whether the run has the catalog of variants at all
    select count(*) > 0 as has_catalog from stg_item_variants
)

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
    week_start(l.sale_ts) as sale_week,
    isoyear(l.sale_ts)::varchar || '-W' || lpad(week(l.sale_ts)::varchar, 2, '0') as sale_iso_week,
    date_trunc('month', l.sale_ts)::date as sale_month,
    year(l.sale_ts)::varchar || '-Q' || quarter(l.sale_ts)::varchar as sale_quarter,
    year(l.sale_ts) as sale_year,
    hour(l.sale_ts) as hour_of_day,
    isodow(l.sale_ts) as weekday,
    case when l.variant is null then l.item else l.item || '-' || l.variant end as item_variant,
    case
        -- a run without the item_variants table keeps the label of the time of the sale
        when not (select has_catalog from variant_catalog) then
            case
                when l.variant is null then r.adjusted_item
                else r.adjusted_item || '-' || l.variant
            end
        -- a live variant gives today's name (none: the item alone); a deleted or unknown
        -- variant, or a line without one, falls back to the item, so that a variant Moka
        -- dropped or renamed is still the same item (plan 9.8)
        when v.variant_id is not null and v.variant is not null
            then r.adjusted_item || '-' || v.variant
        else r.adjusted_item
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
left join live_variants as v on l.variant_id = v.variant_id and l.item_id = v.item_id
left join seed_category_groups as g
    on r.adjusted_category = g.category
where l.sale_ts < (select anchor from run_context) + interval 1 day
