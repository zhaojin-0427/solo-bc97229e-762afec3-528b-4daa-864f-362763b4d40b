"""受限 SQLite SELECT 的递归下降解析器。

白名单语法（其余全部拒绝并报错）：
- 非递归 WITH CTE（支持嵌套 CTE，拒绝 RECURSIVE）
- SELECT [ALL] 结果列（支持 *、表名.*、列别名、表别名）
- 表达式：列引用、常量、+ - * / 四则运算、括号、CASE（含简单 CASE）
- 谓词：比较、AND/OR/NOT、[NOT] IN (...)、[NOT] LIKE、IS [NOT] NULL
- FROM 表 [INNER/LEFT [OUTER]] JOIN 表 ON 条件（拒绝逗号连接、CROSS/NATURAL/RIGHT/FULL）
- WHERE、UNION ALL（拒绝 UNION、INTERSECT、EXCEPT）
- 子查询只允许出现在 CTE 体中
解析只构造 AST，不连接数据库、不执行 SQL。
"""

from . import ast_nodes as ast
from .errors import LineageError
from .tokenizer import tokenize

# 裸别名（不带 AS）遇到这些词必须停止
ALIAS_STOP = {
    "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
    "UNION", "INTERSECT", "EXCEPT", "JOIN", "INNER", "LEFT", "RIGHT",
    "FULL", "CROSS", "NATURAL", "ON", "AS", "AND", "OR", "WHEN",
    "THEN", "ELSE", "END", "IN", "IS", "LIKE", "NOT",
}

CMP_OPS = {"=", "==", "<>", "!=", "<", ">", "<=", ">="}
ADD_OPS = {"+", "-"}
MUL_OPS = {"*", "/"}


class Parser:
    def __init__(self, tokens, sql):
        self.tokens = tokens
        self.sql = sql
        self.pos = 0

    # ---------- 基础工具 ----------

    def peek(self, offset=0):
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def at_end(self):
        return self.pos >= len(self.tokens)

    def err(self, msg, tok=None):
        if tok is None:
            tok = self.peek()
        if tok is not None:
            raise LineageError(msg, tok.line, tok.col)
        raise LineageError(msg + "（SQL 意外结束）")

    def expect_punct(self, value):
        tok = self.peek()
        if tok is None or tok.kind not in ("op", "star", "dot") or tok.value != value:
            self.err(f"缺少 {value!r}")
        return self.next()

    def accept_kw(self, kw):
        tok = self.peek()
        if tok is not None and tok.is_kw(kw):
            return self.next()
        return None

    def expect_kw(self, kw):
        tok = self.peek()
        if tok is None or not tok.is_kw(kw):
            self.err(f"缺少关键字 {kw}")
        return self.next()

    def expect_word(self):
        tok = self.peek()
        if tok is None or tok.kind != "word":
            self.err("需要标识符")
        return self.next()

    # ---------- 入口 ----------

    def parse(self):
        if not self.tokens:
            raise LineageError("SQL 为空，至少需要一条 SELECT 语句")
        query = self.parse_query()
        # 仅允许末尾一个分号
        tok = self.peek()
        if tok is not None:
            if tok.kind == "op" and tok.value == ";":
                self.next()
                if not self.at_end():
                    self.err("只允许一条 SELECT 语句，检测到分号后的多余内容")
            else:
                self.err("只允许一条 SELECT 语句，检测到多余的语法元素")
        return query

    # ---------- 查询 / CTE ----------

    def parse_query(self):
        start = self.peek()
        ctes = []
        if self.accept_kw("WITH"):
            if self.accept_kw("RECURSIVE"):
                self.err("不支持 RECURSIVE WITH（仅允许非递归 CTE）")
            while True:
                cte_start = self.peek()
                name_tok = self.expect_word()
                col_aliases = None
                if self._is_punct("("):
                    self.next()
                    col_aliases = []
                    while True:
                        col_aliases.append(self.expect_word().value)
                        if self._is_punct(")"):
                            self.next()
                            break
                        self.expect_punct(",")
                self.expect_kw("AS")
                self.expect_punct("(")
                inner = self.parse_query()
                self.expect_punct(")")
                ctes.append(ast.CTE(name_tok.value, col_aliases, inner,
                                    cte_start.line, cte_start.col))
                if self._is_punct(","):
                    self.next()
                    continue
                break

        cores = [self.parse_select_core()]
        while True:
            tok = self.peek()
            if tok is None:
                break
            if tok.is_kw("UNION"):
                self.next()
                if not self.accept_kw("ALL"):
                    self.err("仅支持 UNION ALL，不支持普通 UNION（去重语义不在溯源范围内）")
                cores.append(self.parse_select_core())
                continue
            if tok.is_kw("INTERSECT") or tok.is_kw("EXCEPT"):
                self.err("仅支持 UNION ALL，不支持 INTERSECT / EXCEPT")
            break
        return ast.Query(cores, ctes, start.line, start.col)

    def parse_select_core(self):
        start = self.expect_kw("SELECT")
        if self.accept_kw("DISTINCT"):
            self.err("不支持 DISTINCT（去重语义不在溯源范围内）")
        self.accept_kw("ALL")  # SELECT ALL 等同默认行为，接受但无操作

        result_cols = self.parse_result_columns()

        from_table = None
        joins = []
        if self.accept_kw("FROM"):
            from_table = self.parse_table_factor()
            joins = self.parse_joins()

        where = None
        if self.accept_kw("WHERE"):
            where = self.parse_or()

        # 显式拒绝其余子句
        tok = self.peek()
        if tok is not None and tok.kind == "word":
            upper = tok.upper
            if upper in ("GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
                         "WINDOW", "VALUES"):
                self.err(f"不支持 {upper} 子句，允许的子句仅有 FROM / WHERE / JOIN ON")
        return ast.SelectCore(result_cols, from_table, joins, where,
                              start.line, start.col)

    def parse_result_columns(self):
        cols = []
        while True:
            tok = self.peek()
            if tok is None:
                self.err("SELECT 后缺少结果列")
            # 裸 *
            if tok.kind == "star":
                self.next()
                cols.append(ast.Star(tok.line, tok.col))
            elif tok.kind == "word" and self._is_punct_at(1, "."):
                dot = self.tokens[self.pos + 1]
                after = self.tokens[self.pos + 2] if self.pos + 2 < len(self.tokens) else None
                if after is not None and after.kind == "star":
                    self.next()  # 表名
                    self.next()  # .
                    self.next()  # *
                    cols.append(ast.TableStar(tok.value, tok.line, tok.col))
                else:
                    cols.append(self.parse_named_result())
            else:
                cols.append(self.parse_named_result())

            if self._is_punct(","):
                self.next()
                continue
            break
        return cols

    def parse_named_result(self):
        start = self.peek()
        expr = self.parse_or()
        alias = None
        if self.accept_kw("AS"):
            alias = self.expect_word().value
        else:
            tok = self.peek()
            if tok is not None and tok.kind == "word" and tok.upper not in ALIAS_STOP:
                alias = self.next().value
        return ast.ResultColumn(expr, alias, start.line, start.col)

    # ---------- FROM / JOIN ----------

    def parse_table_factor(self):
        tok = self.peek()
        if tok is None:
            self.err("FROM 后缺少表名")
        if self._is_punct("("):
            self.err("不支持 FROM 子查询或括号连接，子查询仅允许定义在 WITH CTE 中")
        name_tok = self.expect_word()
        alias = None
        if self.accept_kw("AS"):
            alias = self.expect_word().value
        else:
            nxt = self.peek()
            if nxt is not None and nxt.kind == "word" and nxt.upper not in ALIAS_STOP:
                alias = self.next().value
        return ast.TableRef(name_tok.value, alias, name_tok.line, name_tok.col)

    def parse_joins(self):
        joins = []
        while True:
            tok = self.peek()
            if tok is None:
                break
            if self._is_punct(","):
                self.err("不支持逗号隐式连接，请改写为 INNER JOIN ... ON ...")
            if not (tok.kind == "word" and tok.upper in
                    ("JOIN", "INNER", "LEFT", "RIGHT", "FULL",
                     "CROSS", "NATURAL")):
                break
            join_start = self.next()
            jtype = "INNER"
            if join_start.is_kw("INNER"):
                self.expect_kw("JOIN")
            elif join_start.is_kw("LEFT"):
                jtype = "LEFT"
                self.accept_kw("OUTER")
                self.expect_kw("JOIN")
            elif join_start.is_kw("JOIN"):
                jtype = "INNER"
            elif join_start.is_kw("RIGHT") or join_start.is_kw("FULL"):
                self.err("仅支持 INNER JOIN 与 LEFT JOIN，不支持 RIGHT/FULL JOIN")
            else:
                self.err("仅支持 INNER JOIN 与 LEFT [OUTER] JOIN，"
                         "不支持 CROSS/NATURAL JOIN")
            right = self.parse_table_factor()
            self.expect_kw("ON")
            on_expr = self.parse_or()
            joins.append(ast.Join(jtype, right, on_expr, join_start.line, join_start.col))
        return joins

    # ---------- 表达式（优先级由低到高） ----------

    def parse_or(self):
        left = self.parse_and()
        while self.accept_kw("OR"):
            op_tok = self.tokens[self.pos - 1]
            right = self.parse_and()
            left = ast.BinaryOp("OR", left, right, op_tok.line, op_tok.col)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.accept_kw("AND"):
            op_tok = self.tokens[self.pos - 1]
            right = self.parse_not()
            left = ast.BinaryOp("AND", left, right, op_tok.line, op_tok.col)
        return left

    def parse_not(self):
        if self.accept_kw("NOT"):
            op_tok = self.tokens[self.pos - 1]
            return ast.UnaryOp("NOT", self.parse_not(), op_tok.line, op_tok.col)
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_postfix()
        tok = self.peek()
        while tok is not None and tok.kind == "op" and tok.value in CMP_OPS:
            self.next()
            right = self.parse_postfix()
            left = ast.BinaryOp(tok.value, left, right, tok.line, tok.col)
            tok = self.peek()
        return left

    def parse_postfix(self):
        expr = self.parse_add()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "word":
                return expr
            upper = tok.upper
            negated = False
            if upper == "NOT":
                nxt = self.peek(1)
                if nxt is not None and nxt.kind == "word" and nxt.upper in ("LIKE", "IN"):
                    negated = True
                    self.next()
                    upper = self.peek().upper
                else:
                    return expr
            if upper == "IS":
                is_tok = self.next()
                is_neg = self.accept_kw("NOT") is not None
                null_tok = self.expect_kw("NULL")
                expr = ast.IsNull(expr, is_neg, is_tok.line, is_tok.col)
                continue
            if upper == "LIKE":
                like_tok = self.next()
                pattern = self.parse_add()
                expr = ast.LikeExpr(expr, negated, pattern, like_tok.line, like_tok.col)
                continue
            if upper == "IN":
                in_tok = self.next()
                self.expect_punct("(")
                if self.peek() is not None and (self.peek().is_kw("SELECT")
                                                or self.peek().is_kw("WITH")):
                    self.err("不支持 IN (子查询)，IN 列表只允许常量/表达式，子查询仅限 CTE")
                items = [self.parse_or()]
                while self._is_punct(","):
                    self.next()
                    items.append(self.parse_or())
                self.expect_punct(")")
                expr = ast.InList(expr, negated, items, in_tok.line, in_tok.col)
                continue
            if upper == "BETWEEN":
                self.err("不支持 BETWEEN，请改写为 >= 与 <= 的 AND 组合")
            if upper == "EXISTS":
                self.err("不支持 EXISTS 子查询，子查询仅允许定义在 WITH CTE 中")
            return expr

    def parse_add(self):
        left = self.parse_mul()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "op" and tok.value in ADD_OPS:
                self.next()
                right = self.parse_mul()
                left = ast.BinaryOp(tok.value, left, right, tok.line, tok.col)
                continue
            if tok is not None and tok.kind == "op" and tok.value == "||":
                self.err("不支持字符串连接 ||，输出表达式仅限四则运算与 CASE")
            return left

    def parse_mul(self):
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok is None:
                return left
            # 词法器把 * 单独标记为 star；在表达式语境下它是乘号
            is_mul = (tok.kind == "op" and tok.value in MUL_OPS) or tok.kind == "star"
            if is_mul:
                self.next()
                right = self.parse_unary()
                left = ast.BinaryOp("*" if tok.kind == "star" else tok.value,
                                    left, right, tok.line, tok.col)
                continue
            # 走到这里的 star 只能是误用（正常的结果列 * 在更上层处理）
            if tok.kind == "star":
                self.err("星号 * 只能作为 SELECT 结果列展开，不能用于表达式")
            return left

    def parse_unary(self):
        tok = self.peek()
        if tok is not None and tok.kind == "op" and tok.value in ("+", "-"):
            self.next()
            operand = self.parse_unary()
            return ast.UnaryOp(tok.value, operand, tok.line, tok.col)
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok is None:
            self.err("表达式不完整")

        # 括号表达式；括号内若是查询则拒绝
        if self._is_punct("("):
            nxt = self.peek(1)
            if nxt is not None and (nxt.is_kw("SELECT") or nxt.is_kw("WITH")):
                self.err("不允许内联子查询，子查询只能定义在 WITH CTE 中")
            self.next()
            expr = self.parse_or()
            self.expect_punct(")")
            return expr

        if tok.kind == "number":
            self.next()
            return ast.Literal(tok.value, "number", tok.line, tok.col)
        if tok.kind == "string":
            self.next()
            return ast.Literal(tok.value, "string", tok.line, tok.col)
        if tok.is_kw("NULL"):
            self.next()
            return ast.Literal("NULL", "null", tok.line, tok.col)
        if tok.is_kw("CASE"):
            return self.parse_case()

        if tok.kind == "word":
            # 函数调用一律拒绝（含 CAST、COALESCE 等）
            nxt = self.peek(1)
            if nxt is not None and nxt.kind == "op" and nxt.value == "(":
                self.err(f"不支持函数调用 {tok.value}(...)，"
                         "输出表达式仅限列引用、常量、四则运算和 CASE")
            self.next()
            if self._is_punct("."):
                self.next()
                col_tok = self.peek()
                if col_tok is None or col_tok.kind != "word":
                    self.err("限定列名格式应为 表名.列名")
                self.next()
                return ast.ColumnRef(col_tok.value, tok.value, tok.line, tok.col)
            return ast.ColumnRef(tok.value, None, tok.line, tok.col)

        if tok.kind == "star":
            self.err("星号 * 只能作为 SELECT 结果列展开")
        self.err(f"意外的语法元素 {tok.value!r}")

    def parse_case(self):
        start = self.expect_kw("CASE")
        operand = None
        if not self.peek().is_kw("WHEN"):
            operand = self.parse_or()
        branches = []
        while self.accept_kw("WHEN"):
            when_expr = self.parse_or()
            self.expect_kw("THEN")
            then_expr = self.parse_or()
            branches.append((when_expr, then_expr))
        if not branches:
            self.err("CASE 表达式至少需要一个 WHEN ... THEN ... 分支")
        else_expr = None
        if self.accept_kw("ELSE"):
            else_expr = self.parse_or()
        self.expect_kw("END")
        return ast.CaseExpr(operand, branches, else_expr, start.line, start.col)

    # ---------- 小工具 ----------

    def _is_punct(self, value):
        tok = self.peek()
        return tok is not None and tok.kind in ("op", "star", "dot") and tok.value == value

    def _is_punct_at(self, offset, value):
        tok = self.peek(offset)
        return tok is not None and tok.kind in ("op", "star", "dot") and tok.value == value


def parse_sql(sql):
    """词法 + 语法分析，返回 Query AST；任何不支持的语法均抛 LineageError。"""
    tokens = tokenize(sql)
    return Parser(tokens, sql).parse()
