"""受限 SQL 的抽象语法树节点（仅包含允许的语法结构）。"""


# ---------- 表达式 ----------

class Literal:
    """常量：数值 / 字符串 / NULL。"""

    def __init__(self, value, lit_type, line, col):
        self.value = value          # 原始文本（字符串），不做求值
        self.lit_type = lit_type    # 'number' | 'string' | 'null'
        self.line = line
        self.col = col


class ColumnRef:
    """列引用：table 为 None 时是裸列名。"""

    def __init__(self, name, table, line, col):
        self.name = name            # 规范化为原始大小写文本
        self.table = table          # 限定表名/别名，或 None
        self.line = line
        self.col = col


class BinaryOp:
    def __init__(self, op, left, right, line, col):
        self.op = op
        self.left = left
        self.right = right
        self.line = line
        self.col = col


class UnaryOp:
    """一元运算符：- / + / NOT。"""

    def __init__(self, op, operand, line, col):
        self.op = op
        self.operand = operand
        self.line = line
        self.col = col


class CaseExpr:
    """CASE（searched: CASE WHEN ... THEN ... ELSE ... END，
    以及 simple: CASE 操作数 WHEN ... THEN ...）。"""

    def __init__(self, operand, branches, else_expr, line, col):
        self.operand = operand              # Expr 或 None
        self.branches = branches            # [(when_expr, then_expr), ...]
        self.else_expr = else_expr          # Expr 或 None
        self.line = line
        self.col = col


class InList:
    """expr [NOT] IN (const/expr, ...)。"""

    def __init__(self, expr, negated, items, line, col):
        self.expr = expr
        self.negated = negated
        self.items = items
        self.line = line
        self.col = col


class LikeExpr:
    """expr [NOT] LIKE pattern。"""

    def __init__(self, expr, negated, pattern, line, col):
        self.expr = expr
        self.negated = negated
        self.pattern = pattern
        self.line = line
        self.col = col


class IsNull:
    """expr IS [NOT] NULL。"""

    def __init__(self, expr, negated, line, col):
        self.expr = expr
        self.negated = negated
        self.line = line
        self.col = col


# ---------- FROM / JOIN ----------

class TableRef:
    def __init__(self, name, alias, line, col):
        self.name = name            # 源表名或 CTE 名
        self.alias = alias          # 显式别名或 None
        self.line = line
        self.col = col


class Join:
    def __init__(self, join_type, right, on_expr, line, col):
        self.join_type = join_type  # 'INNER' | 'LEFT'
        self.right = right          # TableRef
        self.on_expr = on_expr      # 表达式
        self.line = line
        self.col = col


# ---------- 结果列 ----------

class Star:
    """裸 *。"""

    def __init__(self, line, col):
        self.line = line
        self.col = col


class TableStar:
    """表名.* / 别名.*。"""

    def __init__(self, table, line, col):
        self.table = table
        self.line = line
        self.col = col


class ResultColumn:
    def __init__(self, expr, alias, line, col):
        self.expr = expr            # 表达式节点
        self.alias = alias          # 显式别名或 None
        self.line = line
        self.col = col


# ---------- 查询结构 ----------

class SelectCore:
    def __init__(self, result_cols, from_table, joins, where, line, col):
        self.result_cols = result_cols   # [Star | TableStar | ResultColumn]
        self.from_table = from_table     # TableRef 或 None
        self.joins = joins               # [Join]
        self.where = where               # 表达式或 None
        self.line = line
        self.col = col


class CTE:
    def __init__(self, name, column_aliases, query, line, col):
        self.name = name
        self.column_aliases = column_aliases  # [str, ...] 或 None
        self.query = query                    # Query
        self.line = line
        self.col = col


class Query:
    """一条 SELECT 语句：cores 长度 >= 1；多于 1 个时以 UNION ALL 连接。"""

    def __init__(self, cores, ctes, line, col):
        self.cores = cores          # [SelectCore, ...]
        self.ctes = ctes            # [CTE, ...]
        self.line = line
        self.col = col
