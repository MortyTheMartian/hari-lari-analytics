-- Every line of every payment, with refunds turned around (plan 2.3): a refund payment
-- repeats the refunded lines with positive amounts, so here they get a negative quantity,
-- nothing gross, and their amount in refunds, which makes
-- gross_sales - discounts - refunds = net_sales hold on every line. A receipt whose refunds
-- bring its net sales to zero or below is fully refunded and does not count as an order.
-- Several discount codes on one line are joined by ' + ' in name order; several modifiers by
-- ' + ' in option id order, so their ids, names and options line up.
create or replace view int_signed_lines as
with discount_codes as (
    select
        payment_id,
        line_uuid,
        string_agg(distinct discount_name, ' + ' order by discount_name) as discount_code
    from stg_transaction_line_discounts
    where discount_name is not null
    group by payment_id, line_uuid
),

modifiers as (
    select
        payment_id,
        line_uuid,
        string_agg(modifier_id::varchar, ' + ' order by modifier_option_id, modifier_row)
            as modifier_id,
        string_agg(modifier_option_id::varchar, ' + ' order by modifier_option_id, modifier_row)
            as modifier_option_id,
        string_agg(modifier_name, ' + ' order by modifier_option_id, modifier_row)
            as modifier_name,
        string_agg(modifier_option_name, ' + ' order by modifier_option_id, modifier_row)
            as modifier_option_name,
        sum(price)::bigint as modifier_price
    from stg_transaction_line_modifiers
    group by payment_id, line_uuid
),

refund_reasons as (
    select payment_id, first(refund_reason order by refund_row) as refund_reason
    from stg_transaction_refunds
    group by payment_id
),

signed as (
    select
        p.receipt_nr,
        p.sale_ts,
        case when p.is_refund then 'Refund' else 'Payment' end as event_type,
        p.is_refund,
        case when p.is_refund then r.refund_reason end as refund_reason,
        l.category,
        l.item,
        l.variant,
        case when p.is_refund then -l.quantity else l.quantity end as quantity,
        case when p.is_refund then 0 else l.gross_sales end::bigint as gross_sales,
        case when p.is_refund then 0 else l.discount_amount end::bigint as discounts,
        case when p.is_refund then l.net_sales else 0 end::bigint as refunds,
        case when p.is_refund then -l.net_sales else l.net_sales end::bigint as net_sales,
        case when not p.is_refund then d.discount_code end as discount_code,
        p.payment_type,
        l.payment_id,
        l.item_id,
        l.variant_id,
        l.category_id,
        m.modifier_id,
        m.modifier_option_id,
        m.modifier_name,
        m.modifier_option_name,
        m.modifier_price,
        l.line_uuid,
        l.line_id,
        l.line_row,
        l.note
    from stg_transaction_lines as l
    left join stg_transactions as p
        on l.payment_id = p.payment_id and l.payment_no = p.payment_no
    left join discount_codes as d
        on l.payment_id = d.payment_id and l.line_uuid = d.line_uuid
    left join modifiers as m
        on l.payment_id = m.payment_id and l.line_uuid = m.line_uuid
    left join refund_reasons as r
        on l.payment_id = r.payment_id
)

select
    *,
    discount_code is not null or discounts > 0 as discounted,
    not coalesce(
        bool_or(is_refund) over receipt and sum(net_sales) over receipt <= 0, false
    ) as counts_as_order
from signed
window receipt as (partition by receipt_nr)
