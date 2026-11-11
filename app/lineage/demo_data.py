"""内置演示数据：源表结构与示例 SELECT，供页面默认填充与测试复用。"""

DEMO_SCHEMA = """# 字段名后加 * 表示敏感字段，顺序即 SELECT * 的展开顺序
customer(cust_id, name, mobile*, id_no*, city, gender)
orders(order_id, cust_id, amount, status, create_time)
"""

DEMO_SQL = """WITH active_cust AS (
    SELECT cust_id,
           name,
           mobile,
           id_no,
           city
      FROM customer
     WHERE city IN ('BJ', 'SH')
),
cust_order AS (
    SELECT c.cust_id,
           c.name,
           c.mobile,
           c.id_no,
           o.order_id,
           CASE WHEN o.amount > 1000 THEN o.amount * 0.9
                ELSE o.amount
           END AS final_amount
      FROM active_cust AS c
      INNER JOIN orders AS o
              ON c.cust_id = o.cust_id
     WHERE o.status = 'PAID'
)
SELECT cust_id,
       name,
       'VIP' AS vip_tag,
       final_amount + 0 AS amount
  FROM cust_order
 WHERE mobile IS NOT NULL
UNION ALL
SELECT cust_id,
       name,
       'NORMAL' AS vip_tag,
       0 AS amount
  FROM active_cust
 WHERE id_no IS NULL;
"""
