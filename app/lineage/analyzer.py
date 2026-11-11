"""溯源分析引擎。

对已解析的 AST 做字段级血缘分析，严格区分两类依赖：
- 值依赖（value）：输出表达式实际引用到的列，沿表达式（含 CASE 的条件分支）
  向上下游传播。
- 条件依赖（condition）：沿途 WHERE 与 JOIN ON 中出现的列，作用于所在查询
  块的全部输出列，并随输出继续向下游传递。

一个源字段可同时属于两类。任何未知/歧义字段均直接报错；引擎不执行 SQL，
也不判断某个表达式是否可以脱敏。
"""

from . import ast_nodes as ast
from .errors import LineageError
from .parser import parse_sql
from .schema import parse_schemas

# 允许出现在“输出表达式”中的节点类型
_VALUE_NODES = (
    ast.Literal, ast.ColumnRef, ast.BinaryOp, ast.UnaryOp, ast.CaseExpr,
)
# 允许出现在 WHERE / ON 谓词中的节点类型（在值节点之外增加比较与逻辑运算）
_PREDICATE_EXTRA_NODES = (ast.InList, ast.LikeExpr, ast.IsNull)


# ---------- 内部数据结构 ----------

class ColumnNode:
    """血缘图中的一列（源表列、CTE 输出列或最终输出列）。"""

    _seq = 0

    def __init__(self, node_type, name, table=None, block_label="",
                 expr_text=None, from_star=False, position=None):
        ColumnNode._seq += 1
        self.id = f"{node_type}-{ColumnNode._seq}"
        self.node_type = node_type        # 'base' | 'cte' | 'output' | 'union'
        self.name = name
        self.table = table                # 源表名（base 节点）
        self.block_label = block_label
        self.expr_text = expr_text
        self.from_star = from_star
        self.position = position          # 在所属查询块中的列序号
        self.value_deps = {}              # base_id -> [path, ...]
        self.condition_deps = {}          # base_id -> [path, ...]
        self.sensitive = False
        self.sensitive_kind = set()       # 'value' | 'condition'


class Source:
    """FROM/JOIN 之后在当前查询块可见的一个列来源。"""

    def __init__(self, binding, kind, table_name=None, nodes=None, node_by_name=None):
        self.binding = binding            # 限定名（小写）
        self.kind = kind                  # 'base' | 'cte'
        self.table_name = table_name
        self.nodes = nodes                # [ColumnNode]
        self.node_by_name = node_by_name  # {lower name: ColumnNode}


# ---------- 表达式遍历 ----------

def collect_refs(expr):
    """收集表达式中全部 ColumnRef（包括 CASE 条件、IN、LIKE、IS NULL 内的列）。"""
    refs = []
    if expr is None:
        return refs
    if isinstance(expr, ast.ColumnRef):
        refs.append(expr)
    elif isinstance(expr, ast.Literal):
        pass
    elif isinstance(expr, ast.BinaryOp):
        refs.extend(collect_refs(expr.left))
        refs.extend(collect_refs(expr.right))
    elif isinstance(expr, ast.UnaryOp):
        refs.extend(collect_refs(expr.operand))
    elif isinstance(expr, ast.CaseExpr):
        if expr.operand is not None:
            refs.extend(collect_refs(expr.operand))
        for when_e, then_e in expr.branches:
            refs.extend(collect_refs(when_e))
            refs.extend(collect_refs(then_e))
        if expr.else_expr is not None:
            refs.extend(collect_refs(expr.else_expr))
    elif isinstance(expr, ast.InList):
        refs.extend(collect_refs(expr.expr))
        for item in expr.items:
            refs.extend(collect_refs(item))
    elif isinstance(expr, ast.LikeExpr):
        refs.extend(collect_refs(expr.expr))
        refs.extend(collect_refs(expr.pattern))
    elif isinstance(expr, ast.IsNull):
        refs.extend(collect_refs(expr.expr))
    else:  # pragma: no cover - 解析器不会产生其他节点
        raise LineageError(f"内部错误：未知表达式类型 {type(expr).__name__}")
    return refs


def check_value_expr(expr, context, err_tok=None):
    """输出表达式白名单：仅列引用、常量、四则运算和 CASE（含其内部比较）。"""
    if expr is None:
        return
    if isinstance(expr, _VALUE_NODES):
        pass
    else:
        kind = {"where": "WHERE", "on": "ON"}.get(context, "输出")
        raise LineageError(
            f"{kind} 表达式中出现了不允许的结构 {type(expr).__name__}；"
            "输出表达式仅限列引用、常量、四则运算和 CASE",
            getattr(expr, "line", None), getattr(expr, "col", None))

    if isinstance(expr, ast.BinaryOp):
        if expr.op in ("AND", "OR") or expr.op in _CMP_SET:
            raise LineageError(
                "比较/逻辑运算只能出现在 CASE 的 WHEN 条件、WHERE 或 ON 中，"
                "不能作为输出值表达式",
                expr.line, expr.col)
        check_value_expr(expr.left, context)
        check_value_expr(expr.right, context)
    elif isinstance(expr, ast.UnaryOp):
        if expr.op == "NOT":
            raise LineageError("NOT 不能用于输出值表达式", expr.line, expr.col)
        check_value_expr(expr.operand, context)
    elif isinstance(expr, ast.CaseExpr):
        # CASE 内部允许比较/逻辑/IN/LIKE/IS
        sub = collect_refs(expr)  # 先做可达性检查由解析阶段保证
        for sub_expr in _case_sub_expressions(expr):
            check_predicate_or_value(sub_expr)
    elif isinstance(expr, ast.ColumnRef) or isinstance(expr, ast.Literal):
        pass


_CMP_SET = {"=", "==", "<>", "!=", "<", ">", "<=", ">="}


def _case_sub_expressions(case):
    out = []
    if case.operand is not None:
        out.append(case.operand)
    for w, t in case.branches:
        out.append(w)
        out.append(t)
    if case.else_expr is not None:
        out.append(case.else_expr)
    return out


def check_predicate_or_value(expr):
    """CASE 内部表达式：允许值表达式 + 比较/逻辑/IN/LIKE/IS 的完整谓词集合。"""
    if isinstance(expr, (ast.Literal, ast.ColumnRef)):
        return
    if isinstance(expr, ast.BinaryOp):
        check_predicate_or_value(expr.left)
        check_predicate_or_value(expr.right)
    elif isinstance(expr, ast.UnaryOp):
        check_predicate_or_value(expr.operand)
    elif isinstance(expr, ast.CaseExpr):
        for sub in _case_sub_expressions(expr):
            check_predicate_or_value(sub)
    elif isinstance(expr, (ast.InList, ast.LikeExpr, ast.IsNull)):
        for ref_expr in collect_refs(expr):
            check_predicate_or_value(ref_expr)
    else:  # pragma: no cover
        raise LineageError(f"内部错误：CASE 内出现未支持的表达式 {type(expr).__name__}")


def check_predicate(expr, clause):
    """WHERE / ON 谓词白名单校验。"""
    allowed = _VALUE_NODES + _PREDICATE_EXTRA_NODES
    if not isinstance(expr, allowed):
        raise LineageError(
            f"{clause} 中出现了不允许的结构 {type(expr).__name__}",
            getattr(expr, "line", None), getattr(expr, "col", None))
    if isinstance(expr, ast.BinaryOp):
        check_predicate(expr.left, clause)
        check_predicate(expr.right, clause)
    elif isinstance(expr, ast.UnaryOp):
        check_predicate(expr.operand, clause)
    elif isinstance(expr, ast.CaseExpr):
        for sub in _case_sub_expressions(expr):
            check_predicate(sub, clause)
    elif isinstance(expr, ast.InList):
        check_predicate(expr.expr, clause)
        for item in expr.items:
            check_predicate(item, clause)
    elif isinstance(expr, ast.LikeExpr):
        check_predicate(expr.expr, clause)
        check_predicate(expr.pattern, clause)
    elif isinstance(expr, ast.IsNull):
        check_predicate(expr.expr, clause)


def canonical_expr(expr):
    """生成表达式的规范化文本（用于未命名表达式列的默认列名）。"""
    if isinstance(expr, ast.Literal):
        if expr.lit_type == "string":
            return "'" + expr.value.replace("'", "''") + "'"
        return expr.value
    if isinstance(expr, ast.ColumnRef):
        return f"{expr.table}.{expr.name}" if expr.table else expr.name
    if isinstance(expr, ast.BinaryOp):
        return f"({canonical_expr(expr.left)} {expr.op} {canonical_expr(expr.right)})"
    if isinstance(expr, ast.UnaryOp):
        return f"{expr.op}{canonical_expr(expr.operand)}"
    if isinstance(expr, ast.CaseExpr):
        parts = ["CASE"]
        if expr.operand is not None:
            parts.append(canonical_expr(expr.operand))
        for w, t in expr.branches:
            parts.append(f"WHEN {canonical_expr(w)} THEN {canonical_expr(t)}")
        if expr.else_expr is not None:
            parts.append(f"ELSE {canonical_expr(expr.else_expr)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(expr, ast.InList):
        kw = "NOT IN" if expr.negated else "IN"
        items = ", ".join(canonical_expr(i) for i in expr.items)
        return f"{canonical_expr(expr.expr)} {kw} ({items})"
    if isinstance(expr, ast.LikeExpr):
        kw = "NOT LIKE" if expr.negated else "LIKE"
        return f"{canonical_expr(expr.expr)} {kw} {canonical_expr(expr.pattern)}"
    if isinstance(expr, ast.IsNull):
        kw = "IS NOT NULL" if expr.negated else "IS NULL"
        return f"{canonical_expr(expr.expr)} {kw}"
    raise LineageError("内部错误：无法生成表达式文本")  # pragma: no cover


# ---------- 依赖合并工具 ----------

def _merge_deps(target, source_deps, extra_hop=None):
    """把 source_deps（base_id -> paths）并入 target；可选地给每条路径追加一跳。"""
    def _same_path(a, b):
        if len(a) != len(b):
            return False
        return all(x == y for x, y in zip(a, b))

    for base_id, paths in source_deps.items():
        bucket = target.setdefault(base_id, [])
        for path in paths:
            new_path = list(path)
            if extra_hop is not None:
                new_path = new_path + [extra_hop]
            if not any(_same_path(existing, new_path) for existing in bucket):
                bucket.append(new_path)


def _extend_paths(paths, hop):
    return [list(p) + [hop] for p in paths]


def _recolor_paths(deps, new_kind, clause=None, ref=None):
    """把一组依赖路径上所有 hop 的 kind 改写（深拷贝，避免污染原值依赖）。

    用于条件依赖：来源列沿值路径传播而来，但在 WHERE/ON 语境下应展示为条件链路。
    """
    out = {}
    for base_id, paths in deps.items():
        new_paths = []
        for path in paths:
            new_path = []
            for hop in path:
                h = dict(hop)
                h["kind"] = new_kind
                if clause is not None:
                    h["clause"] = clause
                if ref is not None:
                    h["ref"] = ref
                new_path.append(h)
            new_paths.append(new_path)
        out[base_id] = new_paths
    return out


# ---------- 分析器主体 ----------

class Analyzer:
    def __init__(self, tables):
        self.tables = tables                       # schema 解析结果
        self.base_nodes = {}                      # (table_lower, col_lower) -> ColumnNode
        self.cte_records = []                     # 视图模型用 CTE 信息
        self._init_base_nodes()

    def _init_base_nodes(self):
        for tkey, spec in self.tables.items():
            for col in spec.columns:
                node = ColumnNode("base", col.name, table=spec.name,
                                  block_label=f"源表 {spec.name}",
                                  position=col.position)
                node.value_deps[node.id] = [[]]
                if col.sensitive:
                    node.sensitive = True
                    node.sensitive_kind.add("value")
                self.base_nodes[(tkey, col.name.lower())] = node
        self._base_by_id = {node.id: node for node in self.base_nodes.values()}

    # ---- 入口 ----

    def analyze(self, query):
        final_nodes = self._analyze_query(query, block_prefix="最终查询", depth=0)
        return self._build_view(query, final_nodes)

    # ---- 查询（含 CTE 与 UNION ALL）----

    def _analyze_query(self, query, block_prefix, depth, visible_ctes=None):
        if visible_ctes is None:
            visible_ctes = {}
        # 本层 CTE 映射：内层可引用外层，但同名时内层遮蔽外层
        cte_map = dict(visible_ctes)
        for idx, cte in enumerate(query.ctes, start=1):
            label = f"CTE {cte.name}"
            inner_nodes = self._analyze_query(
                cte.query, block_prefix=f"CTE {cte.name}", depth=depth + 1,
                visible_ctes=cte_map)
            nodes = self._apply_cte_aliases(cte, inner_nodes, label)
            key = cte.name.lower()
            if key in cte_map and key not in visible_ctes:
                raise LineageError(f"CTE 名称 {cte.name} 在同一 WITH 中重复定义",
                                   cte.line, cte.col)
            cte_map[key] = nodes
            self.cte_records.append({
                "name": cte.name,
                "label": label,
                "columns": [self._node_brief(n) for n in nodes],
            })

        branch_outputs = []
        for bi, core in enumerate(query.cores, start=1):
            if len(query.cores) > 1:
                label = f"{block_prefix} / UNION ALL 分支 {bi}"
            else:
                label = block_prefix
            branch_outputs.append(
                self._analyze_core(core, cte_map, label, depth,
                                   branch_index=bi, union_count=len(query.cores),
                                   is_cte=block_prefix.startswith("CTE ")))

        if len(branch_outputs) == 1:
            return branch_outputs[0]

        # UNION ALL：按列位置合并
        width = len(branch_outputs[0])
        for bi, nodes in enumerate(branch_outputs[1:], start=2):
            if len(nodes) != width:
                raise LineageError(
                    f"UNION ALL 各分支列数不一致：第 1 个分支 {width} 列，"
                    f"第 {bi} 个分支 {len(nodes)} 列")
        merged = []
        for pos in range(width):
            peers = [branch[pos] for branch in branch_outputs]
            name = peers[0].name
            node = ColumnNode("union", name, block_label=block_prefix,
                              position=pos + 1)
            for bi, peer in enumerate(peers, start=1):
                hop = {"block": peer.block_label, "name": peer.name,
                       "kind": "union-branch", "branch": bi}
                _merge_deps(node.value_deps, peer.value_deps, hop)
                cond_hop = {"block": peer.block_label, "name": peer.name,
                            "kind": "union-branch", "branch": bi}
                _merge_deps(node.condition_deps, peer.condition_deps, cond_hop)
            self._refresh_sensitivity(node)
            merged.append(node)
        return merged

    def _apply_cte_aliases(self, cte, nodes, label):
        if not cte.column_aliases:
            for n in nodes:
                n.block_label = label
            return nodes
        aliases = cte.column_aliases
        if len(aliases) != len(nodes):
            raise LineageError(
                f"CTE {cte.name} 的列别名清单有 {len(aliases)} 列，"
                f"但其查询输出 {len(nodes)} 列", cte.line, cte.col)
        out = []
        for alias, old in zip(aliases, nodes):
            hop = {"block": label, "name": alias, "kind": "cte-output"}
            node = ColumnNode("cte", alias, block_label=label,
                              expr_text=old.expr_text, position=old.position)
            _merge_deps(node.value_deps, old.value_deps, hop)
            _merge_deps(node.condition_deps, old.condition_deps, hop)
            self._refresh_sensitivity(node)
            out.append(node)
        return out

    # ---- 单个 SELECT 块 ----

    def _analyze_core(self, core, cte_map, label, depth, branch_index,
                      union_count, is_cte):
        sources, first_binding = self._bind_sources(core, cte_map, label)
        condition_entries = self._build_conditions(core, sources, label)

        outputs = []
        position = 0
        for rc in core.result_cols:
            if isinstance(rc, ast.Star):
                for src in sources:
                    for src_node in src.nodes:
                        position += 1
                        outputs.append(self._make_passthrough_node(
                            src_node, label, position, star=True,
                            condition_entries=condition_entries, is_cte=is_cte))
            elif isinstance(rc, ast.TableStar):
                src = self._find_source(rc.table, sources, rc)
                for src_node in src.nodes:
                    position += 1
                    outputs.append(self._make_passthrough_node(
                        src_node, label, position, star=True,
                        condition_entries=condition_entries, is_cte=is_cte))
            else:
                position += 1
                outputs.append(self._make_expr_node(
                    rc, sources, label, position, condition_entries, is_cte))

        if not outputs:
            raise LineageError("SELECT 结果列为空", core.line, core.col)
        return outputs

    def _bind_sources(self, core, cte_map, label):
        if core.from_table is None:
            if core.joins:
                raise LineageError("存在 JOIN 却没有 FROM 主表",  # pragma: no cover
                                   core.line, core.col)
            return [], None

        sources = []
        bindings = set()

        def bind(ref):
            key = ref.name.lower()
            if key in cte_map:
                nodes = cte_map[key]
                kind = "cte"
                table_name = None
            elif key in self.tables:
                spec = self.tables[key]
                nodes = [self.base_nodes[(key, c.name.lower())]
                         for c in spec.columns]
                kind = "base"
                table_name = spec.name
            else:
                raise LineageError(
                    f"FROM 中的 {ref.name} 既不是已定义的 CTE，也不是已粘贴的源表",
                    ref.line, ref.col)
            binding = (ref.alias or ref.name).lower()
            if binding in bindings:
                raise LineageError(
                    f"表别名 {binding} 在查询块 {label} 中重复", ref.line, ref.col)
            bindings.add(binding)
            node_by_name = {n.name.lower(): n for n in nodes}
            sources.append(Source(binding, kind, table_name, nodes, node_by_name))

        bind(core.from_table)
        for join in core.joins:
            bind(join.right)
        return sources, core.from_table.name.lower()

    def _build_conditions(self, core, sources, label):
        """返回 [(clause, ColumnRef, resolved_node), ...]，按 WHERE/各 ON 顺序。"""
        entries = []

        def resolve_clause(expr, clause):
            check_predicate(expr, clause)
            for ref in collect_refs(expr):
                node = self._resolve_ref(ref, sources)
                entries.append((clause, ref, node))

        if core.where is not None:
            resolve_clause(core.where, "WHERE")
        for join in core.joins:
            resolve_clause(join.on_expr, "ON")
        return entries

    def _resolve_ref(self, ref, sources):
        if ref.table is not None:
            key = ref.table.lower()
            for src in sources:
                if src.binding == key:
                    node = src.node_by_name.get(ref.name.lower())
                    if node is None:
                        raise LineageError(
                            f"{ref.table}.{ref.name} 字段不存在",
                            ref.line, ref.col)
                    return node
            raise LineageError(
                f"限定名 {ref.table}. 找不到对应的表或别名", ref.line, ref.col)

        matches = []
        for src in sources:
            node = src.node_by_name.get(ref.name.lower())
            if node is not None:
                matches.append((src, node))
        if not matches:
            raise LineageError(
                f"字段 {ref.name} 在当前查询块的任何来源中都不存在",
                ref.line, ref.col)
        if len(matches) > 1:
            names = ", ".join(src.binding for src, _ in matches)
            raise LineageError(
                f"字段 {ref.name} 存在歧义，同时出现在 {names}，"
                "请使用 表别名.列名 限定", ref.line, ref.col)
        return matches[0][1]

    def _make_passthrough_node(self, src_node, label, position, star,
                               condition_entries, is_cte):
        node = ColumnNode("cte" if is_cte else "output",
                          src_node.name, block_label=label,
                          expr_text=src_node.name, from_star=star,
                          position=position)
        hop = {"block": label, "name": src_node.name,
               "kind": "star" if star else "ref"}
        _merge_deps(node.value_deps, src_node.value_deps, hop)
        self._inherit_upstream_conditions(node, [src_node], label)
        self._apply_conditions(node, condition_entries, label)
        self._refresh_sensitivity(node)
        return node

    def _make_expr_node(self, rc, sources, label, position,
                        condition_entries, is_cte):
        check_value_expr(rc.expr, "output")
        refs = collect_refs(rc.expr)
        resolved = [self._resolve_ref(ref, sources) for ref in refs]

        if rc.alias:
            name = rc.alias
        elif isinstance(rc.expr, ast.ColumnRef):
            name = rc.expr.name
        else:
            name = canonical_expr(rc.expr)

        node = ColumnNode("cte" if is_cte else "output",
                          name, block_label=label,
                          expr_text=canonical_expr(rc.expr),
                          position=position)
        for src_node in resolved:
            hop = {"block": label, "name": name, "kind": "value",
                   "expr": canonical_expr(rc.expr)}
            _merge_deps(node.value_deps, src_node.value_deps, hop)
        self._inherit_upstream_conditions(node, resolved, label)
        self._apply_conditions(node, condition_entries, label)
        self._refresh_sensitivity(node)
        return node

    def _inherit_upstream_conditions(self, node, src_nodes, label):
        """来源列在其上游查询块中累积的条件依赖，沿值引用继续向下游传递。"""
        for src_node in src_nodes:
            hop = {"block": label, "name": node.name, "kind": "condition",
                   "clause": "INHERITED", "ref": src_node.name}
            _merge_deps(node.condition_deps, src_node.condition_deps, hop)

    def _apply_conditions(self, node, condition_entries, label):
        # 条件依赖作用于“所在查询块全部输出”：本块 WHERE/ON 引用到的源字段，
        # 无论输出表达式是否引用，都计入每个输出列的条件依赖。
        for clause, ref, src_node in condition_entries:
            ref_text = f"{ref.table}.{ref.name}" if ref.table else ref.name
            out_hop = {"block": label, "name": node.name, "kind": "condition",
                       "clause": clause, "ref": ref_text}
            # 来源列的值路径在条件语境下重标为 condition，避免前端误显示为值传递
            value_deps = _recolor_paths(src_node.value_deps, "condition",
                                        clause=clause, ref=ref_text)
            _merge_deps(node.condition_deps, value_deps, out_hop)
            # 来源列自身携带的上游条件依赖继续向下游传播
            up_hop = {"block": label, "name": node.name, "kind": "condition",
                      "clause": clause, "ref": ref_text}
            _merge_deps(node.condition_deps, src_node.condition_deps, up_hop)

    def _find_source(self, table_name, sources, star_node):
        key = table_name.lower()
        for src in sources:
            if src.binding == key:
                return src
        raise LineageError(
            f"{table_name}.* 中的表名/别名在 FROM/JOIN 中不存在",
            star_node.line, star_node.col)

    # ---- 敏感标记 ----

    def _refresh_sensitivity(self, node):
        """节点依赖装配完成后立即重算敏感性（值/条件分别判定，可兼有）。"""
        for base_id in node.value_deps:
            base = self._base_by_id.get(base_id)
            if base is not None and base.sensitive:
                node.sensitive = True
                node.sensitive_kind.add("value")
        for base_id in node.condition_deps:
            base = self._base_by_id.get(base_id)
            if base is not None and base.sensitive:
                node.sensitive = True
                node.sensitive_kind.add("condition")

    # ---- 视图模型 ----

    def _node_brief(self, node):
        return {
            "name": node.name,
            "sensitive": node.sensitive,
            "sensitive_kind": sorted(node.sensitive_kind),
            "expr_text": node.expr_text,
        }

    def _build_view(self, query, final_nodes):
        base_lookup = {node.id: node for node in self.base_nodes.values()}
        outputs = []
        for node in final_nodes:
            value_paths = self._format_deps(node.value_deps, base_lookup)
            condition_paths = self._format_deps(node.condition_deps, base_lookup)
            outputs.append({
                "position": node.position,
                "name": node.name,
                "expr_text": node.expr_text,
                "from_star": node.from_star,
                "sensitive": node.sensitive,
                "sensitive_kind": sorted(node.sensitive_kind),
                "value_sources": value_paths,
                "condition_sources": condition_paths,
            })

        base_tables = []
        for spec in self.tables.values():
            base_tables.append({
                "name": spec.name,
                "columns": [
                    {"name": c.name, "sensitive": c.sensitive,
                     "position": c.position}
                    for c in spec.columns
                ],
            })

        return {
            "outputs": outputs,
            "ctes": self.cte_records,
            "base_tables": base_tables,
        }

    def _format_deps(self, deps, base_lookup):
        result = []
        for base_id, paths in deps.items():
            base = base_lookup[base_id]
            result.append({
                "base_id": base_id,
                "table": base.table,
                "column": base.name,
                "sensitive": base.sensitive,
                "paths": paths,
            })
        result.sort(key=lambda d: (d["table"], d["column"]))
        return result


def analyze(schema_text, sql_text):
    """对外主入口：源表结构文本 + 单条 SELECT 文本 -> 溯源视图模型。"""
    tables = parse_schemas(schema_text)
    query = parse_sql(sql_text)
    analyzer = Analyzer(tables)
    return analyzer.analyze(query)
