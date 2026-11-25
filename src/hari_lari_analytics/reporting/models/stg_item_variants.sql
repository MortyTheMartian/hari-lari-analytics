-- One variant of today's item catalog with its stock settings and level, as the API returned
-- them at extraction time. An item without variants has one variant with an empty name.
create or replace view stg_item_variants as
select
    id as variant_id,
    item_id,
    title_case(nullif(trim(name), '')) as variant,
    in_stock,
    stock_alert as alert_at,
    track_stock,
    alert as alert_enabled,
    is_deleted
from raw_item_variants
