-- One row per receipt up to the anchor: its sale time, payment type, amounts and what is in
-- it. The sale time and payment type are those of the payment, not of a later refund. The
-- has_* flags look at the sold lines only; items is the net quantity (refunds subtract).
create or replace view int_order_attributes as
select
    receipt_nr,
    coalesce(min(sale_ts) filter (where not is_refund), min(sale_ts)) as sale_ts,
    coalesce(
        first(payment_type order by sale_ts) filter (where not is_refund),
        first(payment_type order by sale_ts)
    ) as payment_type,
    sum(gross_sales)::bigint as gross_sales,
    sum(discounts)::bigint as discounts,
    sum(refunds)::bigint as refunds,
    sum(net_sales)::bigint as net_sales,
    sum(quantity)::bigint as items,
    count(*) filter (where not is_refund) as lines,
    coalesce(bool_or(category_group_level_1 = 'food') filter (where not is_refund), false)
        as has_food,
    coalesce(bool_or(category_group_level_1 = 'drink') filter (where not is_refund), false)
        as has_drink,
    coalesce(bool_or(category_group_level_1 = 'consignment') filter (where not is_refund), false)
        as has_consignment,
    bool_or(discounted) as has_discount,
    coalesce(bool_or(modifier_id is not null) filter (where not is_refund), false)
        as has_modifier,
    bool_or(is_refund) as has_refund,
    bool_and(counts_as_order) as counts_as_order
from int_sales_lines
group by receipt_nr
