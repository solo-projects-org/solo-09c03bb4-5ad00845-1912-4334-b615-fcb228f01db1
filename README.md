# 公交清分数仓 · 金额/换乘口径调整影响分析

清分工程师核对**扣款与退款的金额口径**、或调整「换乘标志」字段口径时，需要先找出
**需要复核的结算明细列**。本工具用 [SQLGlot](https://github.com/tobymao/sqlglot)
解析 DuckDB SQL，从选中的源表字段出发，沿模型血缘传播，给出：

- 受影响的**模型**与每个模型的**受影响输出列**；
- 每列**一条可回溯到源表字段的依赖路径**，逐跳标注是「值传递」还是「条件污染」；
- 当模型中存在**跨作用域同名 CTE**（外层 WITH 与 FROM 派生表内层 WITH 同名）时，
  按 SQL 作用域解析每处引用实际读取的定义，并写明同名 CTE 各自在哪一层定义。

纯本地运行，**不需要连接数仓，也不执行任何 SQL**；网页可一键载入清分样例。

## 分析口径

### 支持的 SQL 范围

- 普通 `SELECT`、**非递归** CTE（`WITH`）、`INNER JOIN`、`WHERE`；
- 禁止星号（`SELECT *` / `t.*`），所有列引用必须带表名或别名；
- 支持 `WHERE ... IN (SELECT ...)` / `WHERE EXISTS (SELECT ...)` 形式的非关联谓词子查询、
  FROM 派生表（需别名，含 INNER JOIN 中的派生表）；投影中允许任意函数、运算、
  `CASE`、类型转换等表达式；
- 不支持递归 CTE、关联子查询、SELECT 投影中的标量子查询、`UNION` 等集合运算、
  `GROUP BY`、窗口函数、`LEFT/RIGHT/FULL/CROSS JOIN`、逗号隐式连接、DML 等，
  遇到即报错指出所在模型并停止分析。

### CTE 名称与 SQL 作用域

- **每处列引用按 SQL 作用域解析到实际读取的 CTE**：`FROM` 派生表内层 `WITH` 定义的
  CTE 与外层 `WITH` 的 CTE 同名时，内层查询里的引用命中**内层定义**（名称遮蔽），
  外层查询只能读到**外层定义**，读不到内层的同名 CTE。
- 因此**不同作用域的同名 CTE 是合法查询**，不会被判重名；分析结果的
  `shadowed_ctes` 与依赖路径中会标注每个同名定义所在的层级（第 0 层外层 /
  第 N 层嵌套作用域）与作用域链路。
- **同一个 `WITH` 子句里真的定义了两个同名 CTE 仍然报错**；
  同一作用域内表/派生表别名与 CTE 同名属于 SQL 合法的名称遮蔽，按实际定义解析。

### 传播规则

1. **取值依赖按列引用传递**：输出表达式引用了某列，该列的污染传到这个输出列。
2. **条件污染整表扩散**：字段一旦进入某查询的 `JOIN ON`、`WHERE` 或
   `IN (SELECT ...)` 谓词（含谓词子查询的污染结果集），该查询的**全部输出列**都标记受影响。
3. **跨模型只走真实读取**：下游模型实际 `FROM/JOIN` 读取了上游模型的哪些输出列，
   才接收对应污染；模型可读取上游模型，模型依赖必须是有向无环图。
4. 任一列引用无法解析（未知表/别名、源表或上游模型上不存在的列、未限定列名等）时，
   指出所在模型与具体位置并**停止分析**。

## 目录结构

```
.
├── docker-compose.yml          # 一键启动
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py                  # Flask 路由（/、/api/sample、/api/validate、/api/analyze）
    ├── lineage.py                  # SQLGlot 解析、作用域感知血缘构建、污染传播与路径构造
    ├── data/
    │   ├── sample_clearing.json     # 换乘标志清分样例（3 源表 + 3 模型，含 CTE 与多跳链路）
    │   └── sample_shadow_cte.json   # 扣款/退款同名 CTE 作用域样例（外层与派生表内层同名 flow）
    ├── templates/
    │   └── index.html          # 单页前端（无外部 CDN 依赖）
    └── tests/
        ├── test_lineage.py     # 传播规则、作用域解析与错误处理测试
        └── test_api.py         # Flask 路由与端到端测试
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>：

1. 点击「载入清分样例」，或点击「载入同名CTE样例」查看扣款/退款金额口径，
   也可以上传自己的 JSON；
2. 在左侧选择源表与待核对字段（如 `ods_debit_detail.debit_amt` 或
   `ods_refund_detail.refund_amt`）；
3. 点击「分析受影响模型列」，右侧展示受影响模型、结算输出列与逐跳依赖路径；
   同名 CTE 样例左侧会显示内外两层 `flow` 各自的定义位置。

停止：`docker compose down`。

## 输入 JSON 格式

```json
{
  "source_tables": [
    { "table": "ods_card_transaction",
      "columns": ["transaction_id", "card_id", "transfer_flag"] }
  ],
  "models": [
    { "name": "dwd_card_transaction_detail",
      "description": "可选说明",
      "sql": "SELECT t.transaction_id AS transaction_id, t.transfer_flag AS transfer_flag FROM ods_card_transaction t WHERE t.transfer_flag = 0" }
  ]
}
```

- `source_tables[].table` 为源表名，`columns` 为该表全部字段；
- `models[].name` 为模型名，模型 SQL 中 `FROM/JOIN` 同名关系即视为读取该上游模型；
- 其余名字按源表处理，必须在 `source_tables` 中声明。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 返回内置换乘标志清分样例 JSON |
| GET | `/api/sample/shadow-cte` | 返回扣款/退款同名 CTE 作用域样例 JSON |
| POST | `/api/validate` | 校验 JSON 与全部 SQL，返回源表字段清单、模型读取关系与同名 CTE 分层（`shadowed_ctes`） |
| POST | `/api/analyze?table=<源表>&column=<字段>` | 返回受影响模型、输出列、依赖路径与同名 CTE 分层 |

错误统一返回 HTTP 400 与 `{"ok": false, "error": "中文原因（含模型名）"}`。
工具只做静态解析与血缘分析，**不会连接或执行任何 SQL**。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin
pip install -r app/requirements.txt
python app/app.py                    # 监听 8000
python app/tests/test_lineage.py     # 血缘/作用域/传播规则测试
python app/tests/test_api.py         # HTTP 接口端到端测试
```
