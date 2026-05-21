# AlphaFactorService

独立因子服务，负责管理因子定义、因子计算任务，以及查询 ClickHouse 中的因子结果。

## 职责边界

- Studio 负责展示、编辑、创建任务、查看结果。
- AlphaFactorService 负责保存因子定义、管理任务、执行计算、查询结果。
- ClickHouse 的 `ab_factor` 库负责保存因子定义、计算任务和因子结果。
- 不再用本地 SQLite 或 YAML 保存因子库，元数据和结果统一由 ClickHouse 承载。

## 第一阶段能力

- `GET /factors` 查询因子定义。
- `POST /factors` 创建因子定义。
- `PUT /factors/{factor_id}` 更新因子定义。
- `DELETE /factors/{factor_id}` 停用因子定义。
- `POST /factor-jobs` 创建计算任务。
- `GET /factor-jobs` 查询任务。
- `POST /factor-jobs/{job_id}/run` 执行指定任务。
- `POST /factor-jobs/run-pending` 批量执行 pending 任务。
- `GET /factor-values` 查询因子结果。
- `GET /factor-values/coverage` 查询覆盖率框架。

当前计算引擎先支持默认股票日频因子的 ClickHouse SQL 计算，包括均值、区间涨跌幅、涨停次数、N 日首次涨停。结果由 worker 写入 ClickHouse。

## 启动

```bash
cd /Users/zhao/Desktop/git/AlphaFactorService
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
alpha-factor-service
```

默认服务地址：

```text
http://127.0.0.1:8100
```

## PM2 启动

```bash
cd /Users/zhao/Desktop/git/AlphaFactorService
pm2 start ecosystem.config.js
```

默认只启动 API 服务，和 `AlphaBlocksSyncData` 保持同样的单 PM2 进程模型。前端创建计算任务后会调用 `POST /factor-jobs/run-pending` 触发执行。

默认监听：

```text
http://0.0.0.0:8100
```

可通过环境变量覆盖：

```bash
PYTHON_BIN=/path/to/python \
AB_FACTOR_HOST=0.0.0.0 \
AB_FACTOR_PORT=8100 \
pm2 start ecosystem.config.js
```

如果以后需要独立后台消费 pending 任务，可以单独运行：

```bash
python -m factor_service.worker --limit 5 --poll-interval 60
```

## ClickHouse 初始化

ClickHouse 连接信息写在本项目 `.env`，字段和 `AlphaBlocksSyncData` 的 datasource 对齐：

```text
AB_FACTOR_CLICKHOUSE_HOST
AB_FACTOR_CLICKHOUSE_PORT
AB_FACTOR_CLICKHOUSE_USER
AB_FACTOR_CLICKHOUSE_PASSWORD
AB_FACTOR_CLICKHOUSE_SECURE
AB_FACTOR_CLICKHOUSE_DATABASE
```

其中 `AB_FACTOR_CLICKHOUSE_DATABASE` 是因子服务自己的库，默认 `ab_factor`。

因子计算的数据源单独配置，默认读取 `baostock.stock_daily_real`：

```text
AB_FACTOR_SOURCE_DATABASE
AB_FACTOR_STOCK_DAILY_TABLE
AB_FACTOR_STOCK_CODE_COLUMN
AB_FACTOR_STOCK_DATE_COLUMN
AB_FACTOR_STOCK_BASIC_TABLE
AB_FACTOR_STOCK_BASIC_TYPE_COLUMN
AB_FACTOR_STOCK_BASIC_STOCK_TYPE_VALUE
```

```bash
clickhouse-client < scripts/init_clickhouse.sql
```

也可以直接在 ClickHouse 控制台执行 `scripts/init_clickhouse.sql`。

如果需要导入当前 AlphaBlocks 里已有的几个默认因子：

```bash
clickhouse-client < scripts/seed_default_factors.sql
```

## 数据存储

所有数据都落在 ClickHouse 的 `ab_factor` 库：

```text
ab_factor.factor_definitions
ab_factor.factor_compute_jobs
ab_factor.factor_values_daily
```

其中：

- `factor_definitions` 保存因子定义、版本、参数、表达式。
- `factor_compute_jobs` 保存计算任务和执行状态。
- `factor_values_daily` 保存日频因子结果。

后续分钟级结果可以单独增加：

```text
ab_factor.factor_values_intraday
```
