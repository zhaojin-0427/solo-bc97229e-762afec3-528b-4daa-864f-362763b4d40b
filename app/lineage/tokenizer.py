"""受限 SQLite 方言的词法分析器。

职责：把 SQL 文本切分为 Token。不做语法判断，不执行任何 SQL。
支持：
- 普通标识符与双引号 / 反引号 / 方括号引用标识符
- 整数、小数、单引号字符串（支持 '' 转义）
- 关键字（大小写不敏感）、多字符运算符
- -- 行注释与 /* ... */ 块注释（不支持嵌套，SQLite 同样不支持嵌套）
"""

from .errors import LineageError

# 多字符运算符，按长度优先匹配
MULTI_OPS = ("<=", ">=", "<>", "!=", "||", "==")
SINGLE_OPS = set("+-*/(),.;=<>")

KEYWORDS = {
    "with", "as", "select", "from", "where", "join", "inner", "left",
    "outer", "on", "union", "all", "and", "or", "not", "case", "when",
    "then", "else", "end", "in", "is", "null", "like", "distinct",
    "group", "having", "order", "by", "limit", "offset", "recursive",
    "intersect", "except", "cross", "natural", "right", "full",
    "between", "exists", "cast",
}


class Token:
    __slots__ = ("kind", "value", "line", "col", "upper")

    def __init__(self, kind, value, line, col):
        self.kind = kind          # 'word' | 'number' | 'string' | 'op' | 'star' | 'dot'
        self.value = value
        self.line = line
        self.col = col
        self.upper = value.upper() if kind == "word" else None

    def is_kw(self, kw):
        return self.kind == "word" and self.upper == kw

    def __repr__(self):
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"


def tokenize(sql):
    tokens = []
    i = 0
    line = 1
    col = 1
    n = len(sql)

    def advance(ch):
        nonlocal line, col
        if ch == "\n":
            line += 1
            col = 1
        else:
            col += 1

    while i < n:
        ch = sql[i]

        # 空白
        if ch.isspace():
            advance(ch)
            i += 1
            continue

        start_line, start_col = line, col

        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                advance(sql[i])
                i += 1
            continue

        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            advance(sql[i]); i += 1
            advance(sql[i]); i += 1
            closed = False
            while i < n:
                if sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    advance(sql[i]); i += 1
                    advance(sql[i]); i += 1
                    closed = True
                    break
                advance(sql[i])
                i += 1
            if not closed:
                raise LineageError("块注释 /* ... */ 没有闭合", start_line, start_col)
            continue

        # 标识符
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_" or sql[j] == "$"):
                j += 1
            word = sql[i:j]
            for _ in range(j - i):
                advance(sql[i]); i += 1
            tokens.append(Token("word", word, start_line, start_col))
            continue

        # 引用标识符："..." / `...` / [ ... ]
        if ch in ('"', "`"):
            quote = ch
            j = i + 1
            buf = []
            advance(sql[i]); i += 1
            closed = False
            while i < n:
                c = sql[i]
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # 双写转义
                        buf.append(quote)
                        advance(sql[i]); i += 1
                        advance(sql[i]); i += 1
                        continue
                    advance(sql[i]); i += 1
                    closed = True
                    break
                buf.append(c)
                advance(sql[i]); i += 1
            if not closed:
                raise LineageError(f"引用标识符缺少闭合的 {quote}", start_line, start_col)
            name = "".join(buf)
            if not name:
                raise LineageError("引用标识符不能为空", start_line, start_col)
            tokens.append(Token("word", name, start_line, start_col))
            continue

        if ch == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise LineageError("方括号标识符 [ ... ] 没有闭合", start_line, start_col)
            name = sql[i + 1:j]
            if not name:
                raise LineageError("方括号标识符不能为空", start_line, start_col)
            for _ in range(j + 1 - i):
                advance(sql[i]); i += 1
            tokens.append(Token("word", name, start_line, start_col))
            continue

        # 数字
        if ch.isdigit():
            j = i + 1
            dot_seen = False
            while j < n and (sql[j].isdigit() or (sql[j] == "." and not dot_seen)):
                if sql[j] == ".":
                    dot_seen = True
                j += 1
            num = sql[i:j]
            for _ in range(j - i):
                advance(sql[i]); i += 1
            tokens.append(Token("number", num, start_line, start_col))
            continue

        # 字符串
        if ch == "'":
            j = i + 1
            buf = []
            advance(sql[i]); i += 1
            closed = False
            while i < n:
                c = sql[i]
                if c == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        advance(sql[i]); i += 1
                        advance(sql[i]); i += 1
                        continue
                    advance(sql[i]); i += 1
                    closed = True
                    break
                if c == "\n":
                    raise LineageError("字符串字面量不能跨行", start_line, start_col)
                buf.append(c)
                advance(sql[i]); i += 1
            if not closed:
                raise LineageError("字符串字面量缺少闭合的单引号", start_line, start_col)
            tokens.append(Token("string", "".join(buf), start_line, start_col))
            continue

        # 运算符与标点
        matched = False
        for op in MULTI_OPS:
            if sql.startswith(op, i):
                for c in op:
                    advance(c)
                i += len(op)
                tokens.append(Token("op", op, start_line, start_col))
                matched = True
                break
        if matched:
            continue

        if ch == "*":
            tokens.append(Token("star", "*", start_line, start_col))
            advance(ch); i += 1
            continue
        if ch == ".":
            tokens.append(Token("dot", ".", start_line, start_col))
            advance(ch); i += 1
            continue
        if ch in SINGLE_OPS:
            tokens.append(Token("op", ch, start_line, start_col))
            advance(ch); i += 1
            continue

        raise LineageError(f"无法识别的字符 {ch!r}", start_line, start_col)

    return tokens
