-- Every adjusted item: today's live catalog plus the deleted or renamed items that still
-- appear in sales, with the first and last sale and whether it is live today. The category
-- is the one of its latest sale, or today's catalog category if it was never sold.
create or replace view dim_items as
with sold as (
    select
        adjusted_item,
        arg_max(adjusted_category, last_sale_ts) as adjusted_category,
        min(first_sale_date) as first_sale_date,
        max(last_sale_date) as last_sale_date
    from int_item_lifecycle
    group by adjusted_item
),

live as (
    select item_name as adjusted_item, first(category_name order by item_row) as category_name
    from stg_items
    where not is_deleted
    group by item_name
),

items as (
    select
        coalesce(s.adjusted_item, lv.adjusted_item) as adjusted_item,
        coalesce(s.adjusted_category, lv.category_name, 'Uncategorized') as adjusted_category,
        lv.adjusted_item is not null as is_live,
        s.first_sale_date,
        s.last_sale_date
    from sold as s
    full join live as lv on s.adjusted_item = lv.adjusted_item
)

select
    i.adjusted_item,
    i.adjusted_category,
    coalesce(g.category_group_level_1, 'unmapped') as category_group_level_1,
    nullif(g.category_group_level_2, '') as category_group_level_2,
    i.is_live,
    i.first_sale_date,
    i.last_sale_date
from items as i
left join seed_category_groups as g on i.adjusted_category = g.category
