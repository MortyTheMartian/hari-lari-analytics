-- The adjusted item and category of every sold item: one row per item id and name as sold
-- (plan 2.4).
--
-- 1. config/item_aliases.csv wins when it lists the sales name: adjusted_item replaces the
--    name (empty: keep the sales name), adjusted_category sets the category.
-- 2. Otherwise a live item gives today's name and category, matched by its id, so renamed
--    items follow their new name without an alias.
-- 3. A deleted item without alias keeps its sales name, unless the name matches a live item
--    (ignoring capitalization), and the category it last had in Moka. An item that is not in
--    the catalog at all uses its latest sales category (is_unmatched, a data note).
--
-- Today's spelling and category of the adjusted name always come from the live items.
create or replace view int_item_resolution as
with sold as (
    select distinct item_id, item from int_signed_lines
),

live as (  -- the first live item of each name
    select item_name, category_name, item_row
    from stg_items
    where not is_deleted
    qualify row_number() over (partition by item_name order by item_row) = 1
),

live_spelling as (  -- the last live name of each spelling, ignoring capitalization
    select lower(item_name) as name_key, item_name
    from live
    qualify row_number() over (partition by lower(item_name) order by item_row desc) = 1
),

catalog as (
    select * from stg_items
    qualify row_number() over (partition by item_id order by item_row) = 1
),

latest_category as (  -- the item's category on its most recent categorised sale
    select item, category
    from int_signed_lines
    where not is_refund and category <> 'Uncategorized'
    qualify row_number() over (partition by item order by sale_ts desc, line_row desc) = 1
),

candidate as (
    select
        s.item_id,
        s.item,
        a.sales_item is not null as has_alias,
        nullif(a.adjusted_category, '') as alias_category,
        c.item_id is not null as in_catalog,
        c.category_name as catalog_category,
        case
            when a.sales_item is not null then coalesce(nullif(a.adjusted_item, ''), s.item)
            when c.item_id is not null and not c.is_deleted then c.item_name
            else s.item
        end as candidate_item
    from sold as s
    left join seed_item_aliases as a on s.item = a.sales_item
    left join catalog as c on s.item_id = c.item_id
),

resolved as (
    select
        k.*,
        sp.item_name as canonical_item,
        coalesce(sp.item_name, k.candidate_item) as adjusted_item
    from candidate as k
    left join live_spelling as sp on lower(k.candidate_item) = sp.name_key
)

select
    r.item_id,
    r.item,
    r.adjusted_item,
    case
        when r.alias_category is not null then r.alias_category
        when r.canonical_item is not null then lv.category_name
        when r.in_catalog and r.catalog_category <> 'Uncategorized' then r.catalog_category
        else coalesce(lc.category, 'Uncategorized')
    end as adjusted_category,
    case
        when r.has_alias then 'alias'
        when r.canonical_item is not null then 'catalog'
        when r.in_catalog then 'deleted item'
        else 'sales fallback'
    end as resolved_by,
    r.canonical_item is null and not r.in_catalog and r.alias_category is null as is_unmatched
from resolved as r
left join live as lv on r.canonical_item = lv.item_name
left join latest_category as lc on r.item = lc.item
