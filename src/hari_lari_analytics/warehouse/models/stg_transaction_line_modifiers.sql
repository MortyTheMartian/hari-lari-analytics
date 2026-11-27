-- The modifiers (add-ons and choices) of a line. Their prices are already in the line's
-- gross sales.
create or replace view stg_transaction_line_modifiers as
select
    payment_id,
    line_uuid,
    _row as modifier_row,
    modifier_id,
    modifier_option_id,
    trim(modifier_name) as modifier_name,
    trim(modifier_option_name) as modifier_option_name,
    price
from raw_transaction_line_modifiers
