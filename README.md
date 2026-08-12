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
- `POST /factor-values/sync-states` 批量查询因子规格的真实持久化覆盖范围，供自动全量/增量同步规划使用。

聚宽因子迁移实施参考见 [`docs/joinquant-factor-catalog.md`](docs/joinquant-factor-catalog.md)，可通过
`rtk .venv/bin/python scripts/export_joinquant_factor_catalog.py` 重新生成公开目录与详情快照。

当前数据源和公式引擎的逐因子兼容性检测见
[`docs/joinquant-factor-compatibility.md`](docs/joinquant-factor-compatibility.md)，可通过
`rtk .venv/bin/python scripts/audit_joinquant_factor_compatibility.py` 重新实测字段覆盖率并生成报告。

将审计通过的 84 个聚宽因子幂等导入当前因子库：

```bash
rtk .venv/bin/python -m scripts.import_joinquant_ready_factors
rtk .venv/bin/python -m scripts.import_joinquant_ready_factors --apply
```

第一条命令只校验；第二条才写入定义，不会自动创建计算任务或启动全历史同步。重复执行不会增加版本；只有显式追加
`--update-existing` 才会覆盖同名但定义不同的因子并创建新版本。
- `POST /factor-analysis/jobs` 创建 Alphalens 标准分析任务。
- `POST /factor-analysis/jobs/{analysis_job_id}/run` 执行分析任务。
- `GET /factor-analysis/summary` 查询 IC、分位收益、换手等汇总结果。
- `GET /factor-analysis/ic` 查询每日 IC 序列。
- `GET /factor-analysis/quantile-returns` 查询分位收益序列。
- `GET /factor-analysis/turnover` 查询分位换手和 Rank 自相关序列。

当前计算引擎先支持默认股票日频因子的 ClickHouse SQL 计算，包括均值、区间涨跌幅、涨停次数、N 日首次涨停。结果由 worker 写入 ClickHouse。
因子分析使用 `alphalens-reloaded` 做标准化计算，分析结果仍落在 ClickHouse。

每次日频因子同步会完成整条横截面处理流水线：

```text
raw_value -> rank_value -> percentile -> score
```

- `raw_value` 是公式原始结果。
- `rank_value` 是当日横截面降序名次，原值最大者排名 1，并列值使用相同名次。
- `percentile` 是当日横截面百分位，范围 0 到 1，原值越大越接近 1。
- `score` 是模型输入值。布尔因子保留 0/1；连续因子默认使用 1%/99% 缩尾后的横截面 Z-score，也可由因子的 `data_processing` 配置改为 MAD/中位数去极值、rank 标准化或不标准化。

新批次成功落库后，worker 会清理同一因子版本、参数、实体和日期范围内的旧批次，避免重算后同时读到旧原始值与新标准分。写入失败时不会提前删除旧结果。

当前日频源尚未绑定行业与市值暴露。配置行业或市值中性化的任务会明确失败，避免将未执行的中性化误报为成功。

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
AB_FACTOR_STOCK_PRICE_COLUMN
AB_FACTOR_STOCK_BASIC_TABLE
AB_FACTOR_STOCK_BASIC_TYPE_COLUMN
AB_FACTOR_STOCK_BASIC_STOCK_TYPE_VALUE
```

当前股票日频因子需要从多张实体资产表组合字段：行情来自 `starlight.ad_market_kline_daily`，
涨跌停和状态来自 `starlight.ad_history_stock_status`，换手率等字段来自 `baostock.bs_daily_kline`。
先创建因子计算源视图：

```bash
python - <<'PY'
from pathlib import Path
from factor_service.clickhouse import client

for statement in [item.strip() for item in Path("scripts/create_factor_source_views.sql").read_text().split(";") if item.strip()]:
    client().command(statement)
PY
```

然后将计算源指向视图：

```text
AB_FACTOR_SOURCE_DATABASE=ab_factor
AB_FACTOR_STOCK_DAILY_TABLE=stock_daily_factor_source
AB_FACTOR_STOCK_BASIC_TABLE=stock_basic_factor_source
```

```bash
clickhouse-client < scripts/init_clickhouse.sql
```

也可以直接在 ClickHouse 控制台执行 `scripts/init_clickhouse.sql`。

如果需要导入当前 AlphaBlocks 里已有的几个默认因子：

```bash
clickhouse-client < scripts/seed_default_factors.sql
```

## 因子结果校验

可以用脚本检查源表、公式编译、worker dry-run 和已落库结果是否一致：

```bash
AB_FACTOR_SOURCE_DATABASE=ab_factor \
AB_FACTOR_STOCK_DAILY_TABLE=stock_daily_factor_source \
AB_FACTOR_STOCK_BASIC_TABLE=stock_basic_factor_source \
python scripts/validate_factor_outputs.py
```

## 数据存储

所有数据都落在 ClickHouse 的 `ab_factor` 库：

```text
ab_factor.factor_definitions
ab_factor.factor_compute_jobs
ab_factor.factor_values_daily
ab_factor.factor_analysis_jobs
ab_factor.factor_analysis_summary
ab_factor.factor_analysis_ic_daily
ab_factor.factor_analysis_quantile_returns
ab_factor.factor_analysis_turnover_daily
```

其中：

- `factor_definitions` 保存因子定义、版本、参数、表达式。
- `factor_compute_jobs` 保存计算任务和执行状态。
- `factor_values_daily` 保存日频因子结果。
- `factor_analysis_jobs` 保存因子评价任务。
- `factor_analysis_*` 保存 Alphalens 生成的 IC、分位收益、换手和汇总指标。

`factor_values_daily` 同时保存两套时间：`event_available_at` 是行情事件理论可用时间，`computed_at` 是因子批次实际生成时间。策略查询默认按 `computed_at` 和 DataCutoff 做严格截断；只有显式历史重建研究才按 `event_available_at` 查询。`source_vintage` 记录计算源和任务批次。Alphalens 对日频收盘因子统一延迟一个交易日后再计算 forward return，避免用当天收盘数据又假设能在当天收盘成交。

后续分钟级结果可以单独增加：

```text
ab_factor.factor_values_intraday
```
