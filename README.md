# SQL 字段溯源 · 敏感列影响分析（作业队列版，Flask + Docker Compose）

面向「客户分群视图交付广告代理商前，评估手机号等敏感字段影响」的静态血缘工具。
粘贴**源表有序字段及敏感标记**和**单条 SQLite SELECT**，逐列展示每个输出列
源自哪些源字段、经由哪些 CTE 列传播，并高亮敏感来源。

**只做静态语法分析与字段级溯源，不连接数据库、不执行 SQL，也不判断表达式能否脱敏。**

本版本把网页分析改为**本地持久化作业**：

- 提交即固定源表说明与 SQL，返回**作业号**；刷新页面后从服务器找回进度；
- 全部 Web 进程共用**一个执行名额**（独立执行器进程），其余作业按提交顺序排队，
  计算期间页面与状态查询始终可响应；
- 排队中、执行中均可**取消**；执行满 **30 秒**强制中止；取消/超时都会停止实际
  计算并立即释放名额，**迟到结果不会改写终态**；
- 重启后已结束作业与排队顺序保留，执行中作业标记为**中断**，队列继续处理；
  中断作业可**以原输入重试为新作业**。

---

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> 。首次打开且队列为空时，页面会自动提交一份
示例作业（含 CTE、INNER/LEFT JOIN、CASE、WHERE、UNION ALL、敏感字段），
可直接观察「排队 → 执行 → 完成」全过程。

停止（作业数据保留在命名卷中）：

```bash
docker compose down        # 保留作业数据
docker compose down -v     # 连作业数据一起清空
```

## 2. 演示脚本（页面上即可完成全部操作）

Compose 默认给每个作业加了 **5 秒人工耗时**（`LINEAGE_JOB_DELAY`，见第 6 节），
让队列与取消可观察；单作业执行上限 **30 秒**（`LINEAGE_JOB_TIMEOUT`）。

1. **提交与排队**：连续点击 3 次「提交溯源作业」。列表中第 1 个作业「执行中」，
   其余「排队中」并显示前方排队数——所有 Web 进程共用一个执行名额。
2. **刷新找回进度**：执行中刷新浏览器，作业列表与选中详情自动恢复。
3. **取消执行中作业**：点击「取消」，状态变为「已取消」，实际计算（子进程）
   被立即终止，名额释放，下一个排队作业开始执行。
4. **取消排队中作业**：直接变为「已取消」，不会占用名额。
5. **30 秒超时**：把人工耗时调到超过 30 秒再提交即可看到「超时中止」：
   ```bash
   LINEAGE_JOB_DELAY=40 docker compose up -d
   ```
   （或反向把时限调小：`LINEAGE_JOB_TIMEOUT=5 docker compose up -d`）
6. **重启恢复**：作业执行中重启执行器——
   ```bash
   docker compose restart lineage-worker
   ```
   执行中的作业变为「已中断」，排队作业继续按提交顺序执行；
   `docker compose down && docker compose up -d` 效果相同（数据在卷中）。
7. **中断重试**：点击中断作业的「重试」，以原输入生成新作业号进入队列。
8. **失败原因**：提交一段非法 SQL（如 `SELECT nope FROM customer`），
   详情区展示具体行列号与原因。

## 3. 架构：一个执行名额如何落地

```
┌────────────────────────────┐      ┌──────────────────────────┐
│ lineage-web (gunicorn)     │      │ lineage-worker (唯一)     │
│ 2 进程 × 4 线程            │      │ 串行认领队首作业           │
│ 只读写作业队列，不做计算    │      │ 每个作业派生独立子进程执行  │
└───────────┬────────────────┘      └────────────┬─────────────┘
            │            共享卷 /data             │
            └────────────► lineage_jobs.db ◄──────┘
                    (SQLite, WAL；作业状态与结果)
```

- **提交**：Web 进程把固定后的输入写入 SQLite（`queued`），返回作业号。
- **执行名额**：全部署只有一个 `lineage-worker` 进程，串行认领队首
  （`UPDATE ... WHERE id = (SELECT ... ORDER BY id LIMIT 1)` 原子占位），
  因此无论多少 Web 进程/线程同时提交，同一时刻只有 1 个作业在执行。
- **真实计算在子进程**：worker 为每个作业派生
  `python -m lineage.job_child` 子进程（复用 `app/lineage/` 引擎），
  取消/超时时直接 SIGKILL，**实际计算立即停止**，名额即刻释放。
- **取消**：Web 侧把 `cancel_requested` 置 1（排队中则直接落 `cancelled`）；
  worker 每 0.1s 轮询，发现后杀子进程并落终态。
- **迟到结果不改写终态**：所有终态迁移都是条件更新
  （`UPDATE ... WHERE status='running'`），作业一旦进入终态，
  任何迟到的结果写入都会因 0 行命中而被丢弃。
- **重启恢复**：worker 启动时把遗留 `running` 作业标记为 `interrupted`，
  `queued` 作业保持原顺序继续执行；已结束作业不受影响。
- **响应性**：Web 进程从不参与计算，状态接口只是轻量 SQLite 读，
  计算期间页面与查询始终可响应。

### 作业状态机

```
queued ──认领──► running ──成功──► succeeded
  │                ├────失败────► failed
  │                ├────取消────► cancelled
  └──取消────────► ├────超时────► timeout      （默认 30s，可配）
     cancelled     └────重启────► interrupted ──重试──► 新作业(queued)
```

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/jobs` | 提交作业。体：`{"schema": "...", "sql": "..."}`；`202` 返回 `{"ok": true, "job_id": N}` |
| GET | `/api/jobs` | 作业列表（倒序）：状态、排队位置、取消标记、时间戳、执行中作业号 |
| GET | `/api/jobs/<id>` | 作业详情：状态、失败原因、提交时固定的输入、逐列溯源结果 |
| POST | `/api/jobs/<id>/cancel` | 取消。排队中→`cancelled`；执行中→`cancelling`（执行器随即中止）；终态→`409` |
| POST | `/api/jobs/<id>/retry` | 以原输入重试为新作业（仅终态作业），`201` 返回新作业号 |

成功作业的 `result` 结构：`{"outputs": [...], "ctes": [...], "base_tables": [...]}`，
与逐列展示一一对应。

## 5. 本地直接运行（可选，需自行准备 Python 3.11 与 Flask）

```bash
pip install -r app/requirements.txt
./run-local.sh        # 同时启动执行器与 Web（8000 端口）
```

核心溯源引擎（`app/lineage/`）与作业层（`app/jobstore.py`、`app/worker.py`）
仅依赖 Python 标准库，无需 Flask 即可测试：

```bash
python -m unittest discover -s tests -v     # 79 个用例
```

## 6. 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `LINEAGE_DB` | `app/../data/lineage_jobs.db`（容器内 `/data/...`） | 作业库 SQLite 路径 |
| `LINEAGE_JOB_TIMEOUT` | `30` | 单作业执行上限（秒），超时强制中止并释放名额 |
| `LINEAGE_JOB_DELAY` | `0`（compose 演示设为 `5`） | 演示/测试用人工耗时，让排队、取消、超时可在页面观察到；生产置 0 |
| `LINEAGE_WORKER_POLL` | `0.2` | 执行器轮询队列间隔（秒） |

Compose 下可用环境变量覆盖，例如：`LINEAGE_JOB_DELAY=40 docker compose up -d`。

---

## 7. 输入格式

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

## 8. 支持的语法白名单（其余一律拒绝并报错）

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

## 9. 两类依赖（分别追踪、分别传递，字段可兼有）

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

## 10. 目录结构

```
.
├── docker-compose.yml          # web（gunicorn）+ worker（唯一执行器）+ 共享卷
├── run-local.sh                # 本地运行：同时启动执行器与 Web
├── app
│   ├── Dockerfile              # python:3.11-slim + gunicorn（worker 复用同镜像）
│   ├── requirements.txt
│   ├── server.py               # Flask：页面 + 作业 API（不做计算）
│   ├── jobstore.py             # SQLite 作业队列：提交/认领/取消/终态/恢复（标准库）
│   ├── worker.py               # 执行器：单名额、子进程执行、取消/超时/重启恢复
│   ├── lineage
│   │   ├── job_child.py        # 作业子进程：读输入文件→analyze→原子写结果文件
│   │   ├── tokenizer.py        # 词法分析
│   │   ├── ast_nodes.py        # AST 定义
│   │   ├── parser.py           # 递归下降解析（白名单语法）
│   │   ├── schema.py           # 源表结构与敏感标记解析
│   │   ├── analyzer.py         # 值/条件双轨依赖传播引擎
│   │   ├── demo_data.py        # 内置示例
│   │   └── errors.py           # 带行列号的错误类型
│   ├── templates/index.html    # 单页界面（输入 + 作业队列 + 详情）
│   └── static/{style.css,app.js}
└── tests
    ├── test_lineage.py         # 62 个溯源引擎用例
    └── test_jobs.py            # 17 个作业队列/执行器用例（真实子进程）
```
