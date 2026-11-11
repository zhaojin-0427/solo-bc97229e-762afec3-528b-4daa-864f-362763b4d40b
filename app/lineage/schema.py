"""解析用户粘贴的源表有序字段与敏感标记。

支持的文本格式（每行一张表，# 为注释，空行忽略）：

    customer(cust_id, name, mobile*, id_no*, city)
    orders(order_id, cust_id, amount, create_time)

规则：
- 表名与字段名均为标识符（可含字母、数字、下划线、$；大小写按原样保留，匹配不敏感）
- 字段名后紧跟 * 表示该字段为敏感字段（如 mobile*）
- 字段顺序即 SELECT * 展开顺序
"""

from .errors import LineageError


def _is_ident(s):
    if not s:
        return False
    if not (s[0].isalpha() or s[0] == "_"):
        return False
    return all(c.isalnum() or c in ("_", "$") for c in s)


def parse_schemas(text):
    """返回 {table_name_lower: {"name": 原名, "columns": [ColumnSpec, ...]}}。"""
    tables = {}
    if text is None:
        raise LineageError("源表结构为空，请先粘贴至少一张源表的字段定义")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(";"):
            line = line[:-1].strip()

        lparen = line.find("(")
        rparen = line.rfind(")")
        if lparen <= 0 or rparen != len(line) - 1:
            raise LineageError(
                f"第 {lineno} 行格式错误，应为：表名(字段1, 字段2*, ...)", lineno, 1)

        table_name = line[:lparen].strip()
        if not _is_ident(table_name):
            raise LineageError(f"第 {lineno} 行表名 {table_name!r} 不是合法标识符",
                               lineno, 1)
        body = line[lparen + 1:rparen].strip()
        if not body:
            raise LineageError(f"第 {lineno} 行表 {table_name} 没有任何字段",
                               lineno, 1)

        columns = []
        seen = set()
        for part in body.split(","):
            item = part.strip()
            if not item:
                raise LineageError(f"第 {lineno} 行存在空字段名（多余的逗号？）",
                                   lineno, 1)
            sensitive = False
            if item.endswith("*"):
                sensitive = True
                item = item[:-1].strip()
            if not _is_ident(item):
                raise LineageError(f"第 {lineno} 行字段名 {item!r} 不是合法标识符",
                                   lineno, 1)
            key = item.lower()
            if key in seen:
                raise LineageError(
                    f"第 {lineno} 行表 {table_name} 中字段 {item} 重复", lineno, 1)
            seen.add(key)
            columns.append(ColumnSpec(item, sensitive, len(columns) + 1))

        tkey = table_name.lower()
        if tkey in tables:
            raise LineageError(f"第 {lineno} 行表 {table_name} 重复定义", lineno, 1)
        tables[tkey] = TableSpec(table_name, columns)

    if not tables:
        raise LineageError("源表结构为空，请先粘贴至少一张源表的字段定义")
    return tables


class ColumnSpec:
    __slots__ = ("name", "sensitive", "position")

    def __init__(self, name, sensitive, position):
        self.name = name
        self.sensitive = sensitive
        self.position = position


class TableSpec:
    __slots__ = ("name", "columns", "_index")

    def __init__(self, name, columns):
        self.name = name
        self.columns = columns
        self._index = {c.name.lower(): c for c in columns}

    def get(self, col_name):
        return self._index.get(col_name.lower())
