-- Receipt grain: one row per receipt up to the anchor, dated at its payment. A refund on a
-- later day therefore counts on the day of the sale here, but on the day of the refund in
-- fct_sales_lines (the rule of today's report).
create or replace view fct_orders as
select
    o.receipt_nr,
    o.sale_ts,
    date_trunc('hour', o.sale_ts) as sale_hour,
    o.sale_ts::date as sale_date,
    week_start(o.sale_ts) as sale_week,
    isoyear(o.sale_ts)::varchar || '-W' || lpad(week(o.sale_ts)::varchar, 2, '0') as sale_iso_week,
    date_trunc('month', o.sale_ts)::date as sale_month,
    year(o.sale_ts)::varchar || '-Q' || quarter(o.sale_ts)::varchar as sale_quarter,
    year(o.sale_ts) as sale_year,
    hour(o.sale_ts) as hour_of_day,
    isodow(o.sale_ts) as weekday,
    o.payment_type,
    o.gross_sales,
    o.discounts,
    o.refunds,
    o.net_sales,
    o.items,
    o.lines,
    o.has_food,
    o.has_drink,
    o.has_consignment,
    o.has_discount,
    o.has_modifier,
    o.has_refund,
    o.counts_as_order,
    c.event_kind,
    c.event_name,
    c.is_holiday,
    c.has_promotion
from int_order_attributes as o
left join dim_calendar as c on o.sale_ts::date = c.date
