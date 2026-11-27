-- One row per payment. A refund is a payment of its own with the same receipt number and
-- parent_payment_id pointing at the refunded payment. Timestamps are naive Bali clock time
-- (the extraction drops the +07:00 offset); like the back-office export, whole seconds.
-- Personal data (customer, collected_by, served_by, ...) is not selected.
create or replace view stg_transactions as
select
    id as payment_id,
    payment_no,
    trim(payment_no) as receipt_nr,
    date_trunc('second', created_at) as sale_ts,
    parent_payment_id,
    id is distinct from parent_payment_id as is_refund,
    nullif(trim(payment_type_label), '') as payment_type,
    refund_amount
from raw_transactions
