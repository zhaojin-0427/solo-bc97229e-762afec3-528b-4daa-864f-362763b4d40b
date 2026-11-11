# SQL 字段溯源 · 敏感列影响分析（Flask + Docker Compose）

面向「客户分群视图交付广告代理商前，评估手机号等敏感字段影响」的静态血缘工具。
粘贴**源表有序字段及敏感标记**和**单条 SQLite SELECT**，逐列展示每个输出列
源自哪些源字段、经由哪些 CTE 列传播，并高亮敏感来源。

**只做静态语法分析与字段级溯源，不连接数据库、不执行 SQL，也不判断表达式能否脱敏。**

---

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ，页面已预填可演示的示例（含 CTE、
INNER/LEFT JOIN、CASE、WHERE、UNION ALL、敏感字段），打开即自动分析。

停止：

```bash
docker compose down
```

容器内使用 gunicorn 启动 `server:app`，健康检查访问 `/`。

## 2. 本地直接运行（可选，需自行准备 Python 3.11 与 Flask）

```bash
pip install -r app/requirements.txt
python app/server.py        # 监听 8000
```

核心溯源引擎（`app/lineage/`）仅依赖 Python 标准库，无需 Flask 即可测试：

```bash
python -m unittest discover -s tests -v
```

---

## 3. 输入格式

### 源表结构（左栏，每行一张表，`#` 注释，空行忽略）

```
表名(字段1, 字段2*, 字段3, ...)
```

- 字段名后紧跟 `*` 表示**敏感字段**，如 `mobile*`、`id_no*`
- 字段书写顺序即 `SELECT *` 的展开顺序
- 标识符支持字母/数字/`_`/`$`；SQL 中还支持 `"..."`、`` `...` ``、`[ ... ]` 引用

示例：

```
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status)
```

### SQL（右栏，单条 SELECT，末尾允许一个分号）

## 4. 支持的语法白名单（其余一律拒绝并报错）

| 类别 | 支持 |
| --- | --- |
| CTE | 非递归 `WITH`（可多个、可嵌套，内层可见外层）、CTE 列别名清单；拒绝 `RECURSIVE` |
| SELECT | `SELECT [ALL]`、列别名（`AS` 或裸别名）、`*`、`表名.*`/`别名.*` |
| 输出表达式 | 列引用、常量（数字/字符串/NULL）、`+ - * /` 四则运算与括号、`CASE`（简单与搜索式） |
| 谓词（WHERE / ON / CASE WHEN） | 比较、`AND`/`OR`/`NOT`、`[NOT] IN (常量/表达式列表)`、`[NOT] LIKE`、`IS [NOT] NULL` |
| 表连接 | `INNER JOIN`、`LEFT [OUTER] JOIN ... ON` |
| 集合 | `UNION ALL`（按列位置合并依赖） |
| 子查询 | **仅允许出现在 CTE 体内**；FROM 内联子查询、IN/标量子查询等全部拒绝 |

明确拒绝：普通 `UNION`、`INTERSECT`、`EXCEPT`、`DISTINCT`、`GROUP BY`、`HAVING`、
`ORDER BY`、`LIMIT/OFFSET`、窗口、`RIGHT/FULL/CROSS/NATURAL JOIN`、逗号隐式连接、
无 `ON` 的连接、函数调用（含 `CAST`/`COALESCE`）、`BETWEEN`、`EXISTS`、
字符串连接 `||`、取模 `%`、多语句、任何非 SELECT 语句。

---

## 5. 两类依赖（分别追踪、分别传递，字段可兼有）

- **值依赖（value）**：输出表达式实际引用的列。沿输出表达式传播；
  `CASE` 的 `WHEN` 条件、简单 CASE 的操作数、`THEN/ELSE` 全部计入。
- **条件依赖（condition）**：沿途 `WHERE` 与 `JOIN ON` 中出现的列，
  **作用于所在查询块的全部输出列**（即使该输出表达式并未引用它们），
  并随 CTE/UNION 继续向下游传递。

一个源字段可同时属于两类（如 `SELECT mobile FROM t WHERE mobile IS NOT NULL`）。
前端用红色（值敏感）、橙色（条件敏感）、紫色（兼有）区分。

### 传播规则要点

- 星号按源表字段顺序展开；`表名.*` 仅展开该来源。
- `UNION ALL` 按**列位置**对齐，合并各分支同位置列的全部依赖；列数不一致报错。
- 未知表/列、裸列名在多表中产生歧义、限定名不存在等情况一律报错，不做猜测。
- 常量输出列没有值依赖；但只要所在查询块存在 WHERE/ON，仍会带条件依赖。
- 工具不推断「某个表达式脱敏后是否安全」，只如实呈现来源与路径。

---

## 6. 目录结构

```
.
├── docker-compose.yml          # 服务编排，端口 8000
├── app
│   ├── Dockerfile              # python:3.11-slim + gunicorn
│   ├── requirements.txt
│   ├── server.py               # Flask 路由：/ 与 POST /api/analyze
│   ├── lineage
│   │   ├── tokenizer.py        # 词法分析
│   │   ├── ast_nodes.py        # AST 定义
│   │   ├── parser.py           # 递归下降解析（白名单语法）
│   │   ├── schema.py           # 源表结构与敏感标记解析
│   │   ├── analyzer.py         # 值/条件双轨依赖传播引擎
│   │   ├── demo_data.py        # 内置示例
│   │   └── errors.py           # 带行列号的错误类型
│   ├── templates/index.html    # 单页界面
│   └── static/{style.css,app.js}
└── tests/test_lineage.py       # 62 个标准库 unittest 用例
```

## 7. HTTP 接口

`POST /api/analyze`，请求体：

```json
{ "schema": "customer(cust_id, mobile*)", "sql": "SELECT cust_id FROM customer" }
```

成功 `200`：`{"ok": true, "result": { "outputs": [...], "ctes": [...], "base_tables": [...] }}`；
语法/溯源错误 `400`：`{"ok": false, "error": "第 x 行第 y 列：..."}`。
