-- Line grain: the cleaned sales lines up to the anchor, with the attributes of their order,
-- so that filtered metrics such as "orders with a drink" work on lines too, and the calendar
-- event of the day.
create or replace view fct_sales_lines as
select
    l.*,
    o.items as order_items,
    o.lines as order_lines,
    o.has_food,
    o.has_drink,
    o.has_consignment,
    o.has_discount,
    o.has_modifier,
    c.event_kind,
    c.event_name,
    c.is_holiday,
    c.has_promotion
from int_sales_lines as l
left join int_order_attributes as o on l.receipt_nr = o.receipt_nr
left join dim_calendar as c on l.sale_date = c.date
