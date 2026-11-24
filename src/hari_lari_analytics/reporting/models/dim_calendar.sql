-- One row per day from the first sale to the anchor. A trading day has at least one order
-- (a line on a receipt that counts as an order), the rule of today's report.
create or replace view dim_calendar as
with days as (
    select unnest(generate_series(
        (select min(sale_date) from int_sales_lines)::timestamp,
        (select anchor from run_context)::timestamp,
        interval 1 day
    ))::date as date
),

orders_per_day as (
    select sale_date, count(distinct receipt_nr) filter (where counts_as_order) as orders
    from int_sales_lines
    group by sale_date
)

select
    d.date,
    date_trunc('week', d.date)::date as week_start,
    isoyear(d.date)::varchar || '-W' || lpad(week(d.date)::varchar, 2, '0') as iso_week,
    date_trunc('month', d.date)::date as month_start,
    year(d.date)::varchar || '-Q' || quarter(d.date)::varchar as quarter,
    year(d.date) as year,
    isodow(d.date) as weekday,
    strftime(d.date, '%a') as weekday_name,
    coalesce(o.orders, 0) as orders,
    coalesce(o.orders, 0) > 0 as is_trading_day
from days as d
left join orders_per_day as o on d.date = o.sale_date
