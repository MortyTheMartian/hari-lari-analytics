-- The discounts applied to a line; a line can have several.
create or replace view stg_transaction_line_discounts as
select
    payment_id,
    line_uuid,
    nullif(trim(discount_name), '') as discount_name
from raw_transaction_line_discounts
