-- The reason given for a refund payment.
create or replace view stg_transaction_refunds as
select
    payment_id,
    _row as refund_row,
    nullif(trim(refunded_reason), '') as refund_reason
from raw_transaction_refunds
