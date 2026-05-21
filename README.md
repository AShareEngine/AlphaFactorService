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
- `GET /factor-values` 查询因子结果。
- `GET /factor-values/coverage` 查询覆盖率框架。

计算引擎当前先预留接口，后续接入真实 pandas / polars / SQL 计算逻辑后，由 worker 将结果写入 ClickHouse。

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

默认监听：

```text
http://0.0.0.0:8100
```

可通过环境变量覆盖：

```bash
PYTHON_BIN=/path/to/python \
AB_FACTOR_HOST=0.0.0.0 \
AB_FACTOR_PORT=8100 \
AB_FACTOR_RUNTIME_CONFIG=/path/to/AlphaBlocksSyncData/config/runtime.local.yaml \
pm2 start ecosystem.config.js
```

## ClickHouse 初始化

数据库连接默认复用 `AlphaBlocksSyncData` 的 runtime 配置：

```text
/Users/zhao/Desktop/git/AlphaBlocksSyncData/config/runtime.local.yaml
```

读取的是其中的 `datasource.host`、`datasource.port`、`datasource.username`、`datasource.password` 和 `datasource.secure`。
因子服务自己的目标库名仍然是 `ab_factor`，可通过 `AB_FACTOR_CLICKHOUSE_DATABASE` 覆盖。

如果部署路径不同，可以设置：

```bash
AB_FACTOR_RUNTIME_CONFIG=/path/to/AlphaBlocksSyncData/config/runtime.local.yaml
```

也兼容 `AlphaBlocksSyncData` 已经使用的这些环境变量：

```text
SYNC_DATA_RUNTIME_CONFIG
ALPHABLOCKS_SYNC_DATA_RUNTIME_CONFIG
ALPHABLOCKS_RUNTIME_CONFIG
RUNTIME_CONFIG_PATH
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
