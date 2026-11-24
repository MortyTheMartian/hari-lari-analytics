-- One row per day from the first sale to the anchor. A trading day has at least one order
-- (a line on a receipt that counts as an order), the rule of today's report. The events of
-- config/calendar_events.csv are joined by date range; a day with several events lists their
-- names joined by ' + ', and its event_kind is the most telling one: closure, holiday,
-- promotion, other.
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
),

events_per_day as (
    select
        d.date,
        string_agg(e.name, ' + ' order by e.date_from, e.name) as event_name,
        case max(
            case e.kind when 'closure' then 4 when 'holiday' then 3 when 'promotion' then 2 else 1 end
        )
            when 4 then 'closure' when 3 then 'holiday' when 2 then 'promotion' else 'other'
        end as event_kind,
        bool_or(e.kind = 'holiday') as is_holiday,
        bool_or(e.kind = 'closure') as is_closure,
        bool_or(e.kind = 'promotion') as has_promotion
    from days as d
    join seed_calendar_events as e on d.date between e.date_from and e.date_to
    group by d.date
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
    coalesce(o.orders, 0) > 0 as is_trading_day,
    e.event_kind,
    e.event_name,
    coalesce(e.is_holiday, false) as is_holiday,
    coalesce(e.is_closure, false) as is_closure,
    coalesce(e.has_promotion, false) as has_promotion
from days as d
left join orders_per_day as o on d.date = o.sale_date
left join events_per_day as e on d.date = e.date
