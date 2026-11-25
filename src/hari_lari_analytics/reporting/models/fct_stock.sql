-- One stock-tracked item-variant of today's live catalog with its stock level and alert-at
-- quantity at extraction time (a snapshot, not a history), and its status: negative (more
-- sold than counted in), out of stock (0), low (1 up to the alert-at quantity) or ok. A
-- variant without Moka's stock alert has alert-at 0.
create or replace view fct_stock as
select
    (select anchor from run_context) as stock_date,
    v.variant_id,
    i.item_name as adjusted_item,
    case
        when v.variant is null then i.item_name
        else i.item_name || '-' || v.variant
    end as adjusted_item_variant,
    v.variant,
    coalesce(d.adjusted_category, i.category_name, 'Uncategorized') as adjusted_category,
    coalesce(d.category_group_level_1, 'unmapped') as category_group_level_1,
    v.in_stock,
    case when v.alert_enabled then coalesce(v.alert_at, 0) else 0 end as alert_at,
    v.alert_enabled,
    case
        when v.in_stock < 0 then 'negative'
        when v.in_stock = 0 then 'out'
        when v.in_stock <= (case when v.alert_enabled then coalesce(v.alert_at, 0) else 0 end)
            then 'low'
        else 'ok'
    end as stock_status
from stg_item_variants as v
join stg_items as i on v.item_id = i.item_id
left join dim_items as d on i.item_name = d.adjusted_item
where v.track_stock and not coalesce(v.is_deleted, false) and not i.is_deleted
