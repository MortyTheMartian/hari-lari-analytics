-- Every adjusted category that was sold or is in today's catalog, with its category groups
-- (joined by name, decision D10) and its first and last sale.
create or replace view dim_categories as
with sold as (
    select adjusted_category, min(sale_date) as first_sale_date, max(sale_date) as last_sale_date
    from int_sales_lines
    where not is_refund
    group by adjusted_category
),

live as (
    select distinct category_name as adjusted_category
    from stg_items
    where not is_deleted and category_name is not null
),

names as (
    select adjusted_category from sold
    union
    select adjusted_category from live
)

select
    n.adjusted_category,
    coalesce(g.category_group_level_1, 'unmapped') as category_group_level_1,
    nullif(g.category_group_level_2, '') as category_group_level_2,
    lv.adjusted_category is not null as is_live,
    s.first_sale_date,
    s.last_sale_date
from names as n
left join sold as s on n.adjusted_category = s.adjusted_category
left join live as lv on n.adjusted_category = lv.adjusted_category
left join seed_category_groups as g on n.adjusted_category = g.category
