"""SQL 血缘与污染传播分析。

输入：源表字段清单 + 各模型的 DuckDB SQL（普通 SELECT、非递归 CTE、
INNER JOIN、WHERE，无星号，列引用均带表名/别名）。

输出：被选字段影响到的模型输出列，以及每列的一条依赖路径。
字段一旦进入 JOIN/筛选条件，即视为污染整个查询的全部输出列，
并向真正读取该查询结果的下游查询传播。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

DIALECT = "duckdb"

# 明确拒绝、且给出可读提示的语句/节点。
# 其余表达式节点（各类函数、运算、类型转换、CASE 等）一律放行：
# 列级血缘只依赖 Column 引用与作用域结构，函数内部不改变取值依赖的传递口径。
_REJECTED = {
    "Union": "UNION / 集合运算",
    "Except": "EXCEPT 集合运算",
    "Intersect": "INTERSECT 集合运算",
    "Group": "GROUP BY 聚合分组",
    "Having": "HAVING 筛选",
    "Window": "窗口函数",
    "Pivot": "PIVOT 透视",
    "Merge": "MERGE 语句",
    "Insert": "INSERT 语句",
    "Update": "UPDATE 语句",
    "Delete": "DELETE 语句",
    "Create": "CREATE 语句",
    "Drop": "DROP 语句",
    "AlterTable": "ALTER TABLE 语句",
}


class AnalysisError(Exception):
    """分析无法继续时抛出，message 为可直接展示给用户的中文说明。"""


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    """叶子依赖：某个可读关系（源表/上游模型/CTE/子查询）的一列。"""
    relation: str
    column: str


@dataclass
class QueryNode:
    """一个 SELECT 作用域（根查询 / CTE / FROM 子查询 / WHERE 子查询）。

    node_id 在模型内全局唯一，节点之间（含同名 CTE）一律通过 node_id 引用，
    名称（name）仅用于展示；同名 CTE 各自处于不同作用域，由 depth / scope_path
    / parent_id 区分实际定义位置。
    """
    node_id: str
    kind: str  # root | cte | subquery
    name: str  # cte 名 / 子查询别名 / 根为模型名
    output_columns: list[str]
    # 每个输出列 -> 依赖的叶子集合
    output_leaves: dict[str, set[Leaf]]
    # 关系限定名 -> (类型, 实际对象)：
    #   table   -> 源表名；model -> 上游模型名；
    #   cte/subquery -> 目标 QueryNode 的 node_id
    sources: dict[str, tuple[str, str]]
    # JOIN ON / WHERE 中引用到的叶子
    predicate_leaves: set[Leaf]
    # 谓词中的 IN (SELECT ...) 子查询节点 id（其污染输出触发本节点条件污染）
    predicate_subqueries: list[str] = field(default_factory=list)
    # 对应的 sqlglot 作用域表达式，用于子查询匹配
    scope_expr: object = None
    # 作用域层级：根查询为 0，每嵌套一层（派生表/CTE/谓词子查询）加 1
    depth: int = 0
    # 父查询节点 id（根节点为 None）
    parent_id: str | None = None
    # 从根到本节点的查询名链路，如 ["m1", "x", "flow"]，用于标注同名定义所在层
    scope_path: list[str] = field(default_factory=list)
    # 运行期：被污染的叶子集合
    tainted_leaves: set[Leaf] = field(default_factory=set)
    # 运行期：被污染的输出列（值依赖或条件污染）
    tainted_outputs: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def _parse_sql(sql: str, model_name: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except sqlglot.errors.ParseError as exc:
        raise AnalysisError(f"模型「{model_name}」SQL 解析失败：{exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise AnalysisError(
            f"模型「{model_name}」必须只包含一条 SELECT 语句，当前解析出 {len(statements)} 条。"
        )
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise AnalysisError(f"模型「{model_name}」只允许普通 SELECT，不支持 {type(tree).__name__}。")
    _validate_structure(tree, model_name)
    return tree


def _validate_structure(tree: exp.Expression, model_name: str) -> None:
    # 递归 CTE
    with_node = tree.find(exp.With)
    if with_node is not None and with_node.args.get("recursive"):
        raise AnalysisError(f"模型「{model_name}」使用了递归 CTE，本工具仅支持非递归 CTE。")

    for node in tree.walk():
        tname = type(node).__name__
        if tname in _REJECTED:
            raise AnalysisError(
                f"模型「{model_name}」包含不支持的语法：{_REJECTED[tname]}。"
            )
        # 星号（SELECT * 与 t.*）
        is_star = isinstance(node, exp.Star) or (
            isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
        )
        if is_star:
            raise AnalysisError(
                f"模型「{model_name}」不允许使用星号 SELECT * / t.*（{node.sql()}），"
                "请显式列出列。"
            )
        # JOIN 类型：只接受显式 INNER JOIN；空 kind 对应逗号隐式连接/CROSS，拒绝
        if isinstance(node, exp.Join):
            kind = (node.kind or "").upper()
            if kind != "INNER":
                raise AnalysisError(
                    f"模型「{model_name}」仅允许显式 INNER JOIN，"
                    f"发现 {kind or '逗号隐式连接/CROSS'} JOIN，请改写为 INNER JOIN ... ON ...。"
                )
            if node.args.get("side") or node.args.get("method"):
                raise AnalysisError(
                    f"模型「{model_name}」仅允许普通 INNER JOIN，发现特殊 JOIN 修饰。"
                )


# ---------------------------------------------------------------------------
# 列引用解析
# ---------------------------------------------------------------------------

def _resolve_column(col: exp.Column, scope, node: QueryNode, model_name: str) -> Leaf:
    """把一个带限定名的列引用解析为某个可读关系的叶子列。"""
    qualifier = col.table
    if not qualifier:
        raise AnalysisError(
            f"模型「{model_name}」的查询「{node.name}」中列「{col.name}」未带表名或别名，"
            "请为所有列引用补充限定名。"
        )
    if qualifier not in scope.sources:
        # 限定名既可能是拼写错误，也可能是关联子查询引用了外层作用域的别名
        outer = scope.parent
        in_outer = outer is not None and qualifier in outer.sources
        if in_outer:
            raise AnalysisError(
                f"模型「{model_name}」的子查询「{node.name}」引用了外层查询的表/别名"
                f"「{qualifier}」（列「{col.sql()}」），属于关联子查询，本工具不支持该写法。"
            )
        raise AnalysisError(
            f"模型「{model_name}」的查询「{node.name}」中引用了未定义的表名/别名「{qualifier}」"
            f"（列「{col.sql()}」），无法解析引用，分析已停止。"
        )
    source = scope.sources[qualifier]
    if isinstance(source, exp.Table):
        kind = "table"
    else:
        parent_type = type(source.expression.parent).__name__ if source.expression.parent else ""
        if parent_type == "CTE":
            kind = "cte"
        else:
            kind = "subquery"
            sub = source.expression.parent
            if not sub.alias:
                raise AnalysisError(
                    f"模型「{model_name}」存在未起别名的子查询，请为 FROM 中的子查询添加别名。"
                )
    # Leaf 的 relation 统一使用查询内的限定名（node.sources 的 key），
    # 实际的表名/模型名通过 node.sources 映射得到。
    return Leaf(relation=qualifier, column=col.name)


def _collect_leaves(expression: exp.Expression | None, scope, node: QueryNode,
                    model_name: str) -> set[Leaf]:
    """收集表达式中本作用域的全部列引用叶子。

    子查询内部的列由其自身的 QueryNode 负责处理，这里剪枝跳过；
    谓词中的 IN (SELECT ...) 子查询在 _build_model 中单独登记。
    若子查询引用了外层别名，_resolve_column 会按关联子查询报错。
    """
    leaves: set[Leaf] = set()
    if expression is None:
        return leaves
    stack = [expression]
    while stack:
        child = stack.pop()
        if isinstance(child, exp.Expression):
            pass
        elif isinstance(child, list):
            stack.extend(child)
            continue
        else:
            continue
        # 剪枝：子查询内部由其自身作用域处理
        # （IN 的内层是 Subquery，EXISTS 的内层直接是嵌套 Select）
        if isinstance(child, exp.Subquery):
            continue
        if isinstance(child, exp.Select) and child is not expression:
            continue
        if isinstance(child, exp.Column) and not isinstance(child.this, exp.Star):
            leaves.add(_resolve_column(child, scope, node, model_name))
            continue
        stack.extend(child.args.values())
    return leaves


def _predicate_subquery_exprs(expression: exp.Expression) -> list[exp.Expression]:
    """提取谓词表达式中 IN/EXISTS 子查询的内层 SELECT 表达式。"""
    result: list[exp.Expression] = []
    for in_node in expression.find_all(exp.In):
        query = in_node.args.get("query")
        if isinstance(query, exp.Subquery):
            result.append(query.this)
    for exists_node in expression.find_all(exp.Exists):
        inner = exists_node.this
        if isinstance(inner, exp.Subquery):
            inner = inner.this
        if isinstance(inner, exp.Select):
            result.append(inner)
    return result


def _layer_label(node: "QueryNode") -> str:
    """节点所在层的可读说明。scope_path[0] 恒为模型名，末位是节点自身名。"""
    model_name = node.scope_path[0]
    if node.kind == "root":
        return f"模型「{model_name}」根查询（第 0 层）"
    if node.kind == "cte":
        if node.depth == 0:
            return f"模型「{model_name}」根查询的 WITH（第 0 层，外层定义）"
        chain = " > ".join(node.scope_path[1:-1]) or "根查询"
        return (
            f"第 {node.depth} 层嵌套作用域（模型「{model_name}」中 {chain} 内的 WITH，"
            "内层定义，遮蔽外层同名 CTE）"
        )
    # 派生表 / 谓词子查询
    chain = " > ".join(node.scope_path[1:-1]) or "根查询"
    return f"第 {node.depth} 层嵌套子查询（模型「{model_name}」中 {chain} 内的「{node.name}」）"


# ---------------------------------------------------------------------------
# 单模型构建
# ---------------------------------------------------------------------------

def _build_model(model_name: str, sql: str,
                 source_schemas: dict[str, list[str]],
                 model_names: set[str]) -> tuple[list[QueryNode], dict[str, QueryNode], list[dict]]:
    tree = _parse_sql(sql, model_name)
    scopes = list(traverse_scope(tree))
    nodes: list[QueryNode] = []
    by_id: dict[str, QueryNode] = {}
    expr_to_node: dict[exp.Expression, QueryNode] = {}
    sub_seq = 0
    node_seq = 0
    # 延迟登记的谓词子查询：(所属节点, 内层 SELECT 表达式)
    pending_predicate_subs: list[tuple[QueryNode, exp.Expression]] = []

    # ---- 第一遍：建立节点并收集投影/谓词叶子（此时只认限定名，不解析目标） ----
    for scope in scopes:
        expr = scope.expression
        parent = expr.parent
        if scope.is_root or parent is None:
            kind, qname = "root", model_name
            node_id = f"{model_name}#root"
        elif isinstance(parent, exp.CTE):
            kind, qname = "cte", parent.alias
            node_seq += 1
            node_id = f"{model_name}#n{node_seq}:cte:{qname}"
        else:
            # FROM 中的派生表必须有别名；WHERE 中的谓词子查询（IN/EXISTS）不是关系，
            # 不能被外层按名引用，使用自动生成的内部名称。
            # IN 的作用域父节点是 Subquery，EXISTS 的父节点直接是 Exists。
            is_in_sub = isinstance(parent, exp.Subquery) and parent.find_ancestor(exp.In) is not None
            is_exists = isinstance(parent, exp.Exists)
            in_predicate = is_in_sub or is_exists
            # SELECT 投影中的标量子查询：Subquery 父级不是 From/Join/In
            # （FROM 与 INNER JOIN 中的派生表、IN 谓词子查询均为合法写法）
            is_scalar = (
                isinstance(parent, exp.Subquery)
                and not isinstance(parent.parent, (exp.From, exp.Join))
                and parent.find_ancestor(exp.In) is None
            )
            if is_scalar:
                raise AnalysisError(
                    f"模型「{model_name}」在 SELECT 投影中使用了标量子查询"
                    f"（{parent.sql()[:60]}），本工具不支持该写法，请改写为 INNER JOIN。"
                )
            if in_predicate:
                kind = "subquery"
                sub_seq += 1
                pred = "EXISTS" if is_exists else "IN"
                qname = f"{pred} 子查询#{sub_seq}"
            else:
                kind = "subquery"
                if not isinstance(parent, exp.Subquery):
                    raise AnalysisError(
                        f"模型「{model_name}」存在无法归类的子查询（父节点 {type(parent).__name__}），"
                        "本工具仅支持 FROM 派生表与 IN/EXISTS 谓词子查询。"
                    )
                qname = parent.alias
                if not qname:
                    raise AnalysisError(
                        f"模型「{model_name}」存在未起别名的 FROM 子查询，请为其添加别名。"
                    )
            node_seq += 1
            node_id = f"{model_name}#n{node_seq}:{kind}:{qname}"

        # 同一 WITH 子句内的 CTE 重名：真重名，必须报错。
        # 不同作用域（外层 WITH vs 派生表内层 WITH）的同名 CTE 不在此列；
        # 同一作用域内表/派生表别名与 CTE 同名属于 SQL 合法的名称遮蔽，
        # 由 sqlglot 的 scope.sources 按实际定义解析，不在此处报错。
        cte_aliases = [cte.alias for cte in scope.ctes]
        dup_ctes = sorted({a for a in cte_aliases if cte_aliases.count(a) > 1})
        if dup_ctes:
            raise AnalysisError(
                f"模型「{model_name}」的同一个 WITH 子句中 CTE 名「{ '、'.join(dup_ctes) }」"
                "重复定义。同一 WITH 内不允许重名；仅不同作用域（如外层 WITH 与 FROM "
                "派生表内层 WITH）允许使用同名 CTE，内层定义会遮蔽外层定义。"
            )

        node = QueryNode(
            node_id=node_id,
            kind=kind,
            name=qname,
            output_columns=[],
            output_leaves={},
            sources={},
            predicate_leaves=set(),
            scope_expr=expr,
        )

        for projection in expr.selects:
            out_name = projection.alias_or_name
            if not out_name:
                raise AnalysisError(
                    f"模型「{model_name}」的查询「{qname}」存在无法命名的输出表达式，请显式加别名。"
                )
            if out_name in node.output_leaves:
                raise AnalysisError(
                    f"模型「{model_name}」的查询「{qname}」输出列「{out_name}」重复。"
                )
            leaves = _collect_leaves(projection, scope, node, model_name)
            node.output_columns.append(out_name)
            node.output_leaves[out_name] = leaves

        # WHERE
        where = expr.args.get("where")
        if where is not None:
            node.predicate_leaves |= _collect_leaves(where, scope, node, model_name)
            for inner in _predicate_subquery_exprs(where):
                pending_predicate_subs.append((node, inner))

        # INNER JOIN ON
        for join in expr.args.get("joins", []):
            on = join.args.get("on")
            if on is not None:
                node.predicate_leaves |= _collect_leaves(on, scope, node, model_name)
                for inner in _predicate_subquery_exprs(on):
                    pending_predicate_subs.append((node, inner))
            using = join.args.get("using")
            if using:
                raise AnalysisError(
                    f"模型「{model_name}」使用了 JOIN ... USING，请改写为 ON 等值条件并带限定名。"
                )

        # 关联子查询校验（本工具只接受非关联子查询）
        if scope.is_correlated_subquery:
            raise AnalysisError(
                f"模型「{model_name}」的子查询「{qname}」引用了外层查询的列（关联子查询），"
                "本工具不支持该写法。"
            )

        nodes.append(node)
        by_id[node.node_id] = node
        expr_to_node[expr] = node

    # ---- 第二遍：按 SQL 作用域解析 sources，并建立层级/作用域链路 ----
    # 嵌套深度：根查询为 0；FROM 派生表 / 谓词子查询每包一层 +1；
    # CTE 内层查询与其定义者同层（CTE 只是命名作用域，不构成查询嵌套）。
    # IN 的内层 SELECT 有 Subquery 包裹；EXISTS 的内层直接挂在 Exists 下，
    # 两者都要计数，否则 EXISTS 子查询会被误算为第 0 层。
    # 注意 CTE 内层 SELECT 的 scope.parent 是“定义该 CTE 的 SELECT”，
    # 直接沿 AST 父链数子查询层数即可得到正确深度。
    _NEST_WRAPPERS = (exp.Subquery, exp.Exists)

    def _nest_depth(expr: exp.Expression) -> int:
        depth = 0
        cur = expr.parent
        while cur is not None:
            if isinstance(cur, _NEST_WRAPPERS):
                depth += 1
            cur = cur.parent
        return depth

    def _definer_node(expr: exp.Expression) -> QueryNode | None:
        """包裹该 SELECT 的最近查询节点：CTE 找定义它的 SELECT，子查询找外层 SELECT。"""
        cur = expr.parent
        while cur is not None and not isinstance(cur, exp.Select):
            cur = cur.parent
        return expr_to_node.get(cur) if cur is not None else None

    for scope, node in zip(scopes, nodes):
        definer = _definer_node(scope.expression)
        node.parent_id = definer.node_id if definer is not None else None
        node.depth = _nest_depth(scope.expression)

    # 作用域链路自顶向下计算（traverse_scope 为后序，子节点先于定义者出现）：
    # 恒以模型名（根查询名）开头，末位是本节点名，使同名 CTE 能通过
    # “定义者链路 + 层级”区分各自定义位置。
    roots = [n for n in nodes if n.parent_id is None]
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur.parent_id is None:
            cur.scope_path = [cur.name]
        else:
            cur.scope_path = list(by_id[cur.parent_id].scope_path) + [cur.name]
        stack.extend(n for n in nodes if n.parent_id == cur.node_id)

    # 第三遍：按 SQL 作用域解析 sources 并校验列存在性。
    # scope.sources 已由 sqlglot 按 SQL 作用域规则解析：
    # 内层同名 CTE 遮蔽外层，外层查询读不到内层定义。
    for scope, node in zip(scopes, nodes):
        for sname, source in scope.sources.items():
            if isinstance(source, exp.Table):
                actual = source.name
                if actual in model_names:
                    node.sources[sname] = ("model", actual)
                else:
                    if actual not in source_schemas:
                        raise AnalysisError(
                            f"模型「{model_name}」读取了未知表「{actual}」："
                            "它既不在源表清单中，也不是已定义的模型。无法解析引用，分析已停止。"
                        )
                    node.sources[sname] = ("table", actual)
            else:
                target = expr_to_node.get(source.expression)
                if target is None:
                    raise AnalysisError(
                        f"模型「{model_name}」存在无法登记作用域的子查询，请检查 SQL 写法。"
                    )
                ptype = type(source.expression.parent).__name__ if source.expression.parent else ""
                stype = "cte" if ptype == "CTE" else "subquery"
                # 保存目标节点的 node_id 而非名字：同名 CTE 各自独立、按作用域解析
                node.sources[sname] = (stype, target.node_id)

        # 源表列存在性 + CTE/子查询输出列存在性（按实际读取的作用域节点校验）
        all_leaves: set[Leaf] = set(node.predicate_leaves)
        for ls in node.output_leaves.values():
            all_leaves |= ls
        for leaf in all_leaves:
            stype, ref = _source_of(node, leaf.relation)
            if stype == "table":
                if leaf.column not in source_schemas.get(ref, []):
                    raise AnalysisError(
                        f"模型「{model_name}」引用了源表「{ref}」上不存在的列「{leaf.column}」，"
                        "无法解析引用，分析已停止。"
                    )
            elif stype == "model":
                # 上游模型的输出列在拓扑构建后统一校验
                continue
            else:
                upstream = by_id[ref]
                if leaf.column not in upstream.output_columns:
                    raise AnalysisError(
                        f"模型「{model_name}」的查询「{node.name}」引用了"
                        f"{'CTE' if stype == 'cte' else '子查询'}「{upstream.name}」"
                        f"（{_layer_label(upstream)}）上不存在的列「{leaf.column}」，"
                        "无法解析引用，分析已停止。"
                    )

    # 所有作用域节点已入表，统一登记谓词中的 IN/EXISTS 子查询
    for owner, inner_select in pending_predicate_subs:
        target = expr_to_node.get(inner_select)
        if target is None:
            raise AnalysisError(
                f"模型「{model_name}」存在无法登记的谓词子查询，请检查 SQL 写法。"
            )
        owner.predicate_subqueries.append(target.node_id)

    # 同名 CTE 汇总：仅跨作用域的同名定义（同一 WITH 重名已在上面报错）
    cte_groups: dict[str, list[QueryNode]] = {}
    for n in nodes:
        if n.kind == "cte":
            cte_groups.setdefault(n.name, []).append(n)
    shadowed_ctes = [
        {
            "name": name,
            "definitions": [
                {
                    "depth": n.depth,
                    "layer": _layer_label(n),
                    "scope_path": list(n.scope_path),
                    "node_id": n.node_id,
                }
                for n in sorted(group, key=lambda x: x.depth)
            ],
        }
        for name, group in sorted(cte_groups.items())
        if len(group) > 1
    ]

    return nodes, by_id, shadowed_ctes


def _source_of(node: QueryNode, relation: str) -> tuple[str, str]:
    entry = node.sources.get(relation)
    if entry is None:
        raise AnalysisError(f"内部错误：查询「{node.name}」找不到关系「{relation}」。")
    return entry


# ---------------------------------------------------------------------------
# 模型 DAG
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    name: str
    sql: str
    nodes: list[QueryNode]
    by_id: dict[str, QueryNode]
    upstream: set[str]  # 直接读取的上游模型
    # 跨作用域同名 CTE 的定义位置说明（同一 WITH 重名属于错误，不会出现在这里）
    shadowed_ctes: list[dict] = field(default_factory=list)
    condition_tainted: bool = False


def build_models(payload: dict) -> tuple[dict[str, list[str]], dict[str, ModelInfo], list[str]]:
    """构建全部模型，返回（源表 schema、模型信息、模型拓扑序）。"""
    source_tables = payload.get("source_tables")
    models = payload.get("models")
    if not isinstance(source_tables, list) or not isinstance(models, list):
        raise AnalysisError("JSON 需包含数组字段 source_tables 和 models。")

    source_schemas: dict[str, list[str]] = {}
    for i, tbl in enumerate(source_tables):
        if not isinstance(tbl, dict):
            raise AnalysisError(f"source_tables[{i}] 必须是对象。")
        tname = tbl.get("table")
        cols = tbl.get("columns")
        if not tname or not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            raise AnalysisError(
                f"source_tables[{i}] 需包含字符串 table 和字符串数组 columns。"
            )
        if tname in source_schemas:
            raise AnalysisError(f"源表「{tname}」在清单中重复出现。")
        source_schemas[tname] = list(cols)

    model_names = {m.get("name") for m in models if isinstance(m, dict)}
    if len(model_names) != len(models) or any(not n for n in model_names):
        raise AnalysisError("models 中每个模型需要唯一的字符串 name。")

    infos: dict[str, ModelInfo] = {}
    for m in models:
        mname, sql = m["name"], m.get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            raise AnalysisError(f"模型「{mname}」缺少非空 sql。")
        nodes, by_id, shadowed_ctes = _build_model(
            mname, sql, source_schemas, model_names
        )
        upstream = {
            ref
            for node in nodes
            for stype, ref in node.sources.values()
            if stype == "model"
        }
        if mname in upstream:
            raise AnalysisError(f"模型「{mname}」不能读取自身。")
        infos[mname] = ModelInfo(
            mname, sql, nodes, by_id, upstream, shadowed_ctes=shadowed_ctes
        )

    # DAG 校验与拓扑序
    order = _topological_order(infos)

    # 上游模型输出列存在性校验（此时所有模型已构建完成）
    for info in infos.values():
        for node in info.nodes:
            all_leaves = set(node.predicate_leaves)
            for ls in node.output_leaves.values():
                all_leaves |= ls
            for leaf in all_leaves:
                stype, ref = _source_of(node, leaf.relation)
                if stype == "model":
                    up = infos[ref]
                    root = up.nodes[-1]  # traverse_scope 为后序，根在最后
                    if leaf.column not in root.output_columns:
                        raise AnalysisError(
                            f"模型「{info.name}」引用了上游模型「{ref}」上不存在的输出列"
                            f"「{leaf.column}」，无法解析引用，分析已停止。"
                        )
    return source_schemas, infos, order


def _topological_order(infos: dict[str, ModelInfo]) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def visit(name: str, stack: list[str]) -> None:
        s = state.get(name, 0)
        if s == 2:
            return
        if s == 1:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            raise AnalysisError(f"模型依赖存在循环：{cycle}，本工具仅支持有向无环的模型血缘。")
        state[name] = 1
        for up in sorted(infos[name].upstream):
            visit(up, stack + [name])
        state[name] = 2
        order.append(name)

    for name in sorted(infos):
        visit(name, [])
    return order


# ---------------------------------------------------------------------------
# 污染传播
# ---------------------------------------------------------------------------

def _propagate_within_model(info: ModelInfo,
                            incoming: dict[str, set[str]],
                            upstream_paths: dict[str, dict[str, list[dict]]]) -> dict[str, list[dict]]:
    """在单个模型内部做定点传播。

    incoming: 上游模型名 -> 被污染的上游输出列集合
    upstream_paths: 上游模型根输出列 -> 完整依赖路径（用于跨模型串联）
    返回：本模型根查询每个被污染输出列 -> 一条依赖路径
    """
    nodes = info.nodes  # traverse_scope 后序：被依赖的 CTE/子查询在前

    def seed_leaves(node: QueryNode) -> set[Leaf]:
        seeds: set[Leaf] = set()
        for sname, (stype, actual) in node.sources.items():
            if stype == "model":
                for col in incoming.get(actual, set()):
                    seeds.add(Leaf(relation=sname, column=col))
        return seeds

    # 多轮定点：节点按后序排列，CTE/子查询间若存在引用链也可收敛
    by_id = info.by_id
    for _ in range(len(nodes) + 1):
        changed = False
        for node in nodes:
            before = (frozenset(node.tainted_leaves), frozenset(node.tainted_outputs))

            # 1) 上游传入的污染叶子
            node.tainted_leaves |= seed_leaves(node)

            # 2) 内部 CTE/子查询输出污染 -> 对应叶子污染。
            #    sources 中的引用按 SQL 作用域解析为具体 node_id，
            #    同名 CTE 各自独立，内层遮蔽外层。
            for sname, (stype, ref) in node.sources.items():
                if stype in ("cte", "subquery"):
                    up = by_id[ref]
                    for col in up.tainted_outputs:
                        node.tainted_leaves.add(Leaf(relation=sname, column=col))

            # 3) 谓词子查询（IN (SELECT ...)）任一输出被污染 -> 条件命中
            predicate_sub_hit = any(
                bool(by_id[sid].tainted_outputs) for sid in node.predicate_subqueries
            )
            condition_hit = bool(node.predicate_leaves & node.tainted_leaves) or predicate_sub_hit

            # 4) 值依赖传播
            for out_col, leaves in node.output_leaves.items():
                if leaves & node.tainted_leaves:
                    node.tainted_outputs.add(out_col)

            # 5) 条件污染：JOIN/WHERE/IN 子查询命中 -> 全部输出列
            if condition_hit:
                node.tainted_outputs.update(node.output_columns)

            after = (frozenset(node.tainted_leaves), frozenset(node.tainted_outputs))
            if after != before:
                changed = True
        if not changed:
            break
    else:
        raise AnalysisError(f"模型「{info.name}」内部依赖无法收敛，请检查 CTE/子查询引用。")

    root = nodes[-1]
    info.condition_tainted = any(
        bool(n.predicate_leaves & n.tainted_leaves)
        or any(by_id[sid].tainted_outputs for sid in n.predicate_subqueries)
        for n in nodes
    )

    # 为每个污染输出列记录一条依赖路径
    paths = _build_paths(info, upstream_paths)
    return paths


def _build_paths(info: ModelInfo,
                 upstream_paths: dict[str, dict[str, list[dict]]]) -> dict[str, list[dict]]:
    """为根查询每个污染输出列构造一条回溯到源表字段的依赖路径。

    upstream_paths: 上游模型名 -> (该模型根输出列 -> 完整路径)。
    路径由“步骤”组成，每一步描述一个查询节点中 输入列 -> 输出列 的传递，
    via 标记该步是值依赖(value)还是条件污染(condition)。
    """
    by_id = info.by_id

    def find_path(node: QueryNode, col: str, seen: frozenset) -> list[dict] | None:
        key = (node.node_id, col)
        if key in seen:
            return None
        seen = seen | {key}

        step = {
            "type": "node",
            "model": info.name,
            "query": node.name,
            "output": col,
            "depth": node.depth,
            "scope_path": list(node.scope_path),
            "layer": _layer_label(node),
        }
        cond_leaf_hit = bool(node.predicate_leaves & node.tainted_leaves)
        value_leaves = [lf for lf in node.output_leaves[col] if lf in node.tainted_leaves]

        # 优先值依赖，其次条件污染；保证路径反映真实的传递方式
        ordered: list[tuple[Leaf, str]] = [(lf, "value") for lf in value_leaves]
        if cond_leaf_hit:
            for lf in sorted(node.predicate_leaves & node.tainted_leaves,
                             key=lambda x: (x.relation, x.column)):
                if not any(lf == v for v, _ in ordered):
                    ordered.append((lf, "condition"))

        for leaf, via in ordered:
            stype, ref = node.sources[leaf.relation]
            cur = dict(step)
            cur.update({
                "via": via,
                "input_relation": ref,
                "input_qualifier": leaf.relation,
                "input_column": leaf.column,
            })
            if stype in ("cte", "subquery"):
                up = by_id[ref]
                cur["input_relation"] = up.name
                cur["input_layer"] = _layer_label(up)
                if leaf.column in up.tainted_outputs:
                    sub = find_path(up, leaf.column, seen)
                    if sub is not None:
                        return sub + [cur]
            elif stype == "model":
                up_paths = upstream_paths.get(ref, {})
                if leaf.column in up_paths:
                    return up_paths[leaf.column] + [cur]
            else:  # 源表：路径起点
                return [{
                    "type": "source",
                    "relation": ref,
                    "column": leaf.column,
                }, cur]

        # 条件污染经由 IN/EXISTS 谓词子查询：穿过子查询的任一污染输出
        for sid in node.predicate_subqueries:
            sub_node = by_id[sid]
            pred_kind = "EXISTS" if sub_node.name.startswith("EXISTS") else "IN"
            for sub_col in sub_node.output_columns:
                if sub_col not in sub_node.tainted_outputs:
                    continue
                sub_path = find_path(sub_node, sub_col, seen)
                if sub_path is not None:
                    cur = dict(step)
                    cur.update({
                        "via": "condition",
                        "input_relation": sub_node.name,
                        "input_qualifier": sub_node.name,
                        "input_column": sub_col,
                        "input_layer": _layer_label(sub_node),
                        "predicate": pred_kind,
                    })
                    return sub_path + [cur]
        return None

    root = info.nodes[-1]
    result: dict[str, list[dict]] = {}
    for col in root.output_columns:
        if col not in root.tainted_outputs:
            continue
        path = find_path(root, col, frozenset())
        if path is None:
            path = [{"type": "node", "model": info.name, "query": root.name,
                     "output": col, "via": "unknown"}]
        result[col] = path
    return result


def analyze(payload: dict, target_table: str, target_column: str) -> dict:
    source_schemas, infos, order = build_models(payload)

    if target_table not in source_schemas:
        raise AnalysisError(f"源表「{target_table}」不在清单中。")
    if target_column not in source_schemas[target_table]:
        raise AnalysisError(
            f"源表「{target_table}」上不存在列「{target_column}」。"
        )

    # 跨模型传播：incoming[model] = {上游模型: 污染输出列}
    incoming: dict[str, dict[str, set[str]]] = {name: {} for name in infos}
    # 每个模型根输出列 -> 完整依赖路径（供下游模型串联）
    all_paths: dict[str, dict[str, list[dict]]] = {}

    for name in order:
        info = infos[name]
        # 只保留真正被本模型读取的上游污染列
        for up in info.upstream:
            up_root = infos[up].nodes[-1]
            read_cols: set[str] = set()
            for node in info.nodes:
                for sname, (stype, actual) in node.sources.items():
                    if stype == "model" and actual == up:
                        all_leaves = set(node.predicate_leaves)
                        for ls in node.output_leaves.values():
                            all_leaves |= ls
                        read_cols |= {lf.column for lf in all_leaves if lf.relation == sname}
            tainted = up_root.tainted_outputs & read_cols
            if tainted:
                incoming[name][up] = set(tainted)

        # 种子：目标源表字段被本模型直接读取，将污染注入真正读取该列的节点
        for node in info.nodes:
            for sname, (stype, actual) in node.sources.items():
                if stype != "table" or actual != target_table:
                    continue
                all_leaves = set(node.predicate_leaves)
                for ls in node.output_leaves.values():
                    all_leaves |= ls
                if any(lf.relation == sname and lf.column == target_column
                       for lf in all_leaves):
                    node.tainted_leaves.add(Leaf(relation=sname, column=target_column))

        paths = _propagate_within_model(info, incoming[name], all_paths)
        if paths:
            all_paths[name] = paths

    affected = []
    for name in order:
        info = infos[name]
        root = info.nodes[-1]
        if not root.tainted_outputs:
            continue
        cols = []
        for col in root.output_columns:
            if col not in root.tainted_outputs:
                continue
            path = all_paths.get(name, {}).get(col, [])
            via = _summarize_via(path)
            cols.append({
                "column": col,
                "via": via,
                "path": path,
            })
        # 条件污染判定（根查询或任一内部查询命中条件，含 IN 谓词子查询）
        by_id = info.by_id
        # 仅对确实存在跨作用域同名的 CTE 名称做层级消歧，其余保持简短查询名
        shadow_names = {item["name"] for item in info.shadowed_ctes}

        def _query_label(n: QueryNode) -> str:
            if n.depth == 0 or n.name not in shadow_names:
                return n.name
            chain = " > ".join(n.scope_path[1:-1]) or "根查询"
            return f"{n.name}（{chain} 内，第 {n.depth} 层）"

        condition_queries = sorted({
            _query_label(n) for n in info.nodes
            if (n.predicate_leaves & n.tainted_leaves)
            or any(by_id[sid].tainted_outputs for sid in n.predicate_subqueries)
        })
        affected.append({
            "model": name,
            "condition_tainted": info.condition_tainted,
            "condition_queries": condition_queries,
            "columns": cols,
        })

    return {
        "target": {"table": target_table, "column": target_column},
        "affected_models": affected,
        "model_count": len(affected),
        "column_count": sum(len(m["columns"]) for m in affected),
        # 跨作用域同名 CTE：写明每个名字分别在哪一层定义（同一 WITH 重名在校验阶段即报错）
        "shadowed_ctes": [
            {"model": name, **item}
            for name in order for item in infos[name].shadowed_ctes
        ],
    }


def _summarize_via(path: list[dict]) -> str:
    if not path:
        return "unknown"
    vias = {step.get("via") for step in path if "via" in step}
    if "condition" in vias and "value" in vias:
        return "mixed"
    if "condition" in vias:
        return "condition"
    return "value"
