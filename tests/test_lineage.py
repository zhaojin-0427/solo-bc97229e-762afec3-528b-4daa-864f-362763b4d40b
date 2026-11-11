"""溯源引擎测试：仅依赖 Python 标准库。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from lineage import analyze  # noqa: E402
from lineage.errors import LineageError  # noqa: E402

SCHEMA = """
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status, create_time)
log(id, cust_id, mobile*, event)
"""


def names(result):
    return [o["name"] for o in result["outputs"]]


def out_by_name(result, name):
    for o in result["outputs"]:
        if o["name"] == name:
            return o
    raise KeyError(name)


def dep_tables(sources):
    return {(d["table"], d["column"]) for d in sources}


class BasicLineageTests(unittest.TestCase):

    def test_simple_select_qualified_and_alias(self):
        r = analyze(SCHEMA, "SELECT c.cust_id AS id, c.name FROM customer AS c")
        self.assertEqual(names(r), ["id", "name"])
        col = out_by_name(r, "id")
        self.assertEqual(dep_tables(col["value_sources"]),
                         {("customer", "cust_id")})
        self.assertFalse(col["sensitive"])
        self.assertEqual(col["value_sources"][0]["paths"][0][0]["kind"], "value")

    def test_constant_column_has_no_value_dependency(self):
        r = analyze(SCHEMA, "SELECT cust_id, 'VIP' AS tag, 100 AS n FROM customer")
        tag = out_by_name(r, "tag")
        self.assertEqual(tag["value_sources"], [])
        self.assertFalse(tag["sensitive"])
        n = out_by_name(r, "n")
        self.assertEqual(n["value_sources"], [])

    def test_arithmetic_propagates_value_dependency(self):
        sql = "SELECT amount * 0.9 + 10 AS amt, -amount AS neg FROM orders"
        r = analyze(SCHEMA, sql)
        amt = out_by_name(r, "amt")
        self.assertEqual(dep_tables(amt["value_sources"]),
                         {("orders", "amount")})

    def test_case_condition_counts_as_value_dependency(self):
        sql = """
        SELECT CASE WHEN amount > 1000 THEN amount * 0.9
                    WHEN status IN ('A', 'B') THEN 0
                    ELSE amount END AS final_amt
          FROM orders
        """
        r = analyze(SCHEMA, sql)
        col = out_by_name(r, "final_amt")
        self.assertEqual(dep_tables(col["value_sources"]),
                         {("orders", "amount"), ("orders", "status")})
        self.assertFalse(col["sensitive"])

    def test_simple_case_and_like_isnull(self):
        sql = """
        SELECT CASE city WHEN 'BJ' THEN 1 ELSE 0 END AS bj,
               CASE WHEN name LIKE 'A%' THEN 1 ELSE 0 END AS a,
               CASE WHEN mobile IS NOT NULL THEN 1 ELSE 0 END AS has_m
          FROM customer
        """
        r = analyze(SCHEMA, sql)
        self.assertEqual(dep_tables(out_by_name(r, "bj")["value_sources"]),
                         {("customer", "city")})
        self.assertEqual(dep_tables(out_by_name(r, "a")["value_sources"]),
                         {("customer", "name")})
        hm = out_by_name(r, "has_m")
        self.assertEqual(dep_tables(hm["value_sources"]),
                         {("customer", "mobile")})
        self.assertIn("value", hm["sensitive_kind"])

    def test_star_expands_in_field_order(self):
        r = analyze(SCHEMA, "SELECT * FROM customer")
        self.assertEqual(names(r),
                         ["cust_id", "name", "mobile", "id_no", "city"])
        mobile = out_by_name(r, "mobile")
        self.assertTrue(mobile["from_star"])
        self.assertIn("value", mobile["sensitive_kind"])
        self.assertTrue(all(o["from_star"] for o in r["outputs"]))

    def test_table_star_expansion(self):
        sql = "SELECT c.*, o.order_id FROM customer c INNER JOIN orders o ON c.cust_id = o.cust_id"
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r),
                         ["cust_id", "name", "mobile", "id_no", "city", "order_id"])

    def test_where_applies_condition_dependency_to_all_outputs(self):
        sql = "SELECT cust_id, name FROM customer WHERE mobile IS NOT NULL"
        r = analyze(SCHEMA, sql)
        for nm in ("cust_id", "name"):
            col = out_by_name(r, nm)
            self.assertIn(("customer", "mobile"),
                          dep_tables(col["condition_sources"]))
            self.assertIn("condition", col["sensitive_kind"])
        # name 本身不引用 mobile，因此没有值依赖
        name_col = out_by_name(r, "name")
        self.assertNotIn(("customer", "mobile"),
                         dep_tables(name_col["value_sources"]))

    def test_inner_join_on_condition_propagation(self):
        sql = """
        SELECT o.order_id, c.name
          FROM customer c
          INNER JOIN orders o ON c.cust_id = o.cust_id
         WHERE c.mobile LIKE '1%'
        """
        r = analyze(SCHEMA, sql)
        oid = out_by_name(r, "order_id")
        cond = dep_tables(oid["condition_sources"])
        self.assertIn(("customer", "cust_id"), cond)
        self.assertIn(("orders", "cust_id"), cond)
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn("condition", oid["sensitive_kind"])
        self.assertNotIn("value", oid["sensitive_kind"])

    def test_left_join_supported(self):
        sql = ("SELECT c.cust_id, o.order_id FROM customer c "
               "LEFT OUTER JOIN orders o ON c.cust_id = o.cust_id")
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["cust_id", "order_id"])

    def test_cte_chain_path_hops(self):
        sql = """
        WITH c1 AS (SELECT cust_id, mobile FROM customer WHERE city = 'BJ'),
             c2 AS (SELECT cust_id, mobile FROM c1)
        SELECT cust_id, mobile FROM c2
        """
        r = analyze(SCHEMA, sql)
        mobile = out_by_name(r, "mobile")
        self.assertIn("value", mobile["sensitive_kind"])
        # 路径至少经过 c1、c2 两个 CTE 块
        path = mobile["value_sources"][0]["paths"][0]
        blocks = [hop["block"] for hop in path]
        self.assertTrue(any("CTE c1" in b for b in blocks))
        self.assertTrue(any("CTE c2" in b for b in blocks))
        # WHERE 中的 city 作为条件依赖贯穿到最终输出
        self.assertIn(("customer", "city"),
                      dep_tables(mobile["condition_sources"]))

    def test_cte_column_alias_list(self):
        sql = "WITH c(id, phone) AS (SELECT cust_id, mobile FROM customer) SELECT id, phone FROM c"
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["id", "phone"])
        phone = out_by_name(r, "phone")
        self.assertEqual(dep_tables(phone["value_sources"]),
                         {("customer", "mobile")})

    def test_union_all_merges_by_position(self):
        sql = """
        SELECT cust_id, mobile FROM customer
        UNION ALL
        SELECT cust_id, mobile FROM log
        """
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["cust_id", "mobile"])
        mobile = out_by_name(r, "mobile")
        self.assertEqual(dep_tables(mobile["value_sources"]),
                         {("customer", "mobile"), ("log", "mobile")})
        # 每个来源路径上都有 union-branch 跳
        for dep in mobile["value_sources"]:
            for path in dep["paths"]:
                self.assertTrue(any(h["kind"] == "union-branch" for h in path))

    def test_nested_cte_can_reference_outer(self):
        sql = """
        WITH outer_c AS (
            WITH inner_c AS (SELECT cust_id, mobile FROM customer)
            SELECT cust_id, mobile FROM inner_c
        )
        SELECT cust_id, mobile FROM outer_c
        """
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["cust_id", "mobile"])
        self.assertTrue(out_by_name(r, "mobile")["sensitive"])

    def test_field_can_be_both_value_and_condition(self):
        sql = "SELECT mobile FROM customer WHERE mobile IS NOT NULL"
        r = analyze(SCHEMA, sql)
        col = out_by_name(r, "mobile")
        self.assertEqual(sorted(col["sensitive_kind"]),
                         ["condition", "value"])

    def test_quoted_identifiers_and_comments(self):
        sql = '''
        -- 行注释
        SELECT "cust_id" AS "编号", [name] AS nm /* 块注释 */
          FROM customer
         WHERE `mobile` IS NULL
        '''
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["编号", "nm"])
        nm = out_by_name(r, "nm")
        self.assertIn(("customer", "mobile"),
                      dep_tables(nm["condition_sources"]))

    def test_semicolon_allowed_at_end(self):
        r = analyze(SCHEMA, "SELECT cust_id FROM customer;")
        self.assertEqual(names(r), ["cust_id"])

    def test_cte_sensitive_overview(self):
        sql = ("WITH c AS (SELECT cust_id, mobile FROM customer) "
               "SELECT cust_id FROM c")
        r = analyze(SCHEMA, sql)
        cte_cols = {c["name"]: c for c in r["ctes"][0]["columns"]}
        self.assertTrue(cte_cols["mobile"]["sensitive"])
        self.assertFalse(cte_cols["cust_id"]["sensitive"])


class RejectionTests(unittest.TestCase):

    def assert_rejected(self, sql, schema=SCHEMA):
        with self.assertRaises(LineageError):
            analyze(schema, sql)

    def test_recursive_rejected(self):
        self.assert_rejected(
            "WITH RECURSIVE c(x) AS (SELECT 1) SELECT x FROM c")

    def test_subquery_in_from_rejected(self):
        self.assert_rejected(
            "SELECT t.cust_id FROM (SELECT cust_id FROM customer) t")

    def test_in_subquery_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id IN (SELECT cust_id FROM orders)")

    def test_function_call_rejected(self):
        self.assert_rejected("SELECT coalesce(name, 'x') FROM customer")

    def test_group_by_rejected(self):
        self.assert_rejected(
            "SELECT cust_id, count(1) FROM orders GROUP BY cust_id")

    def test_order_by_rejected(self):
        self.assert_rejected("SELECT cust_id FROM customer ORDER BY cust_id")

    def test_distinct_rejected(self):
        self.assert_rejected("SELECT DISTINCT city FROM customer")

    def test_union_without_all_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer UNION SELECT cust_id FROM log")

    def test_intersect_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer INTERSECT SELECT cust_id FROM log")

    def test_cross_join_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c CROSS JOIN orders o")

    def test_comma_join_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c, orders o WHERE c.cust_id = o.cust_id")

    def test_join_without_on_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c JOIN orders o")

    def test_comparison_in_output_rejected(self):
        self.assert_rejected("SELECT amount > 100 FROM orders")

    def test_string_concat_rejected(self):
        self.assert_rejected("SELECT name || city FROM customer")

    def test_between_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id BETWEEN 1 AND 9")

    def test_exists_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE EXISTS (SELECT 1)")

    def test_cast_rejected(self):
        self.assert_rejected("SELECT CAST(cust_id AS TEXT) FROM customer")

    def test_having_rejected(self):
        self.assert_rejected(
            "SELECT city FROM customer HAVING count(1) > 1")

    def test_scalar_subquery_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id = (SELECT 1)")

    def test_unclosed_string_rejected(self):
        self.assert_rejected("SELECT cust_id FROM customer WHERE name = 'x")

    def test_unknown_column_rejected(self):
        self.assert_rejected("SELECT not_a_col FROM customer")

    def test_unknown_table_rejected(self):
        self.assert_rejected("SELECT cust_id FROM nope")

    def test_ambiguous_bare_column_rejected(self):
        sql = ("SELECT cust_id FROM customer c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id")
        self.assert_rejected(sql)

    def test_unknown_qualified_column_rejected(self):
        self.assert_rejected("SELECT c.nope FROM customer c")

    def test_bad_qualifier_rejected(self):
        self.assert_rejected("SELECT x.cust_id FROM customer c")

    def test_multiple_statements_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer; SELECT order_id FROM orders")

    def test_update_rejected(self):
        self.assert_rejected("UPDATE customer SET name = 'x'")

    def test_union_arity_mismatch_rejected(self):
        self.assert_rejected(
            "SELECT cust_id, name FROM customer UNION ALL SELECT cust_id FROM log")

    def test_cte_alias_arity_mismatch_rejected(self):
        self.assert_rejected(
            "WITH c(a) AS (SELECT cust_id, name FROM customer) SELECT a FROM c")

    def test_bad_schema_line_rejected(self):
        with self.assertRaises(LineageError):
            analyze("customer(cust_id, mobile*) garbage",
                    "SELECT cust_id FROM customer")

    def test_duplicate_schema_column_rejected(self):
        with self.assertRaises(LineageError):
            analyze("customer(cust_id, cust_id)",
                    "SELECT cust_id FROM customer")

    def test_empty_inputs_rejected(self):
        with self.assertRaises(LineageError):
            analyze("", "SELECT 1")
        with self.assertRaises(LineageError):
            analyze(SCHEMA, "")

    def test_modulo_rejected(self):
        self.assert_rejected("SELECT amount % 2 FROM orders")

    def test_table_star_unknown_table_rejected(self):
        self.assert_rejected("SELECT x.* FROM customer c")

    def test_no_from_constant_select(self):
        r = analyze(SCHEMA, "SELECT 1 AS x, 'y' AS y, NULL AS z")
        self.assertEqual(names(r), ["x", "y", "z"])
        for o in r["outputs"]:
            self.assertEqual(o["value_sources"], [])
            self.assertFalse(o["sensitive"])

    def test_negative_and_parenthesized_arithmetic(self):
        r = analyze(SCHEMA, "SELECT (amount + 1) * 2 - -amount AS k FROM orders")
        self.assertEqual(
            dep_tables(r["outputs"][0]["value_sources"]),
            {("orders", "amount")})

    def test_negated_predicates(self):
        sql = ("SELECT cust_id FROM customer "
               "WHERE mobile NOT IN ('1', '2') AND name NOT LIKE 'A%' "
               "AND NOT (cust_id = 1)")
        r = analyze(SCHEMA, sql)
        cond = dep_tables(r["outputs"][0]["condition_sources"])
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn(("customer", "name"), cond)
        self.assertIn(("customer", "cust_id"), cond)

    def test_join_star_expands_all_sources_in_order(self):
        schema = "t(a, b*, c)\nt2(a, d)"
        r = analyze(schema,
                    "SELECT * FROM t INNER JOIN t2 ON t.a = t2.a")
        self.assertEqual(names(r), ["a", "b", "c", "a", "d"])

    def test_duplicate_output_names_allowed(self):
        r = analyze(SCHEMA, "SELECT cust_id, cust_id FROM customer")
        self.assertEqual(names(r), ["cust_id", "cust_id"])

    def test_string_literal_with_sql_keywords_inside(self):
        r = analyze(SCHEMA, "SELECT cust_id FROM customer WHERE name = 'SELECT; FROM'")
        self.assertEqual(names(r), ["cust_id"])

    def test_inherited_condition_kept_when_downstream_has_no_where(self):
        sql = ("WITH c AS (SELECT cust_id, name FROM customer "
               "WHERE mobile IS NOT NULL) "
               "SELECT name FROM c")
        r = analyze(SCHEMA, sql)
        self.assertIn(("customer", "mobile"),
                      dep_tables(r["outputs"][0]["condition_sources"]))


class ConditionPropagationTests(unittest.TestCase):

    def test_condition_propagates_through_cte_and_union(self):
        sql = """
        WITH c AS (SELECT cust_id, name FROM customer WHERE mobile IS NOT NULL)
        SELECT cust_id, name FROM c
        UNION ALL
        SELECT cust_id, name FROM customer WHERE id_no IS NULL
        """
        r = analyze(SCHEMA, sql)
        name = out_by_name(r, "name")
        cond = dep_tables(name["condition_sources"])
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn(("customer", "id_no"), cond)
        self.assertIn("condition", name["sensitive_kind"])

    def test_on_does_not_become_value_dependency(self):
        sql = ("SELECT c.name FROM customer c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id")
        r = analyze(SCHEMA, sql)
        val = dep_tables(out_by_name(r, "name")["value_sources"])
        self.assertEqual(val, {("customer", "name")})

    def test_in_list_constants_only(self):
        sql = "SELECT cust_id FROM customer WHERE city IN ('BJ', 'SH') AND amount_check(cust_id) IS NULL"
        # 后半句含函数，整体应被拒
        with self.assertRaises(LineageError):
            analyze(SCHEMA, sql)
        ok = analyze(SCHEMA,
                     "SELECT cust_id FROM customer WHERE city IN ('BJ', 'SH')")
        col = out_by_name(ok, "cust_id")
        self.assertIn(("customer", "city"),
                      dep_tables(col["condition_sources"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
