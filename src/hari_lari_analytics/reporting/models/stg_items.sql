-- Today's item catalog, including deleted items.
create or replace view stg_items as
select
    id as item_id,
    _row as item_row,
    name as item_name,
    "category.name" as category_name,
    is_deleted
from raw_items
