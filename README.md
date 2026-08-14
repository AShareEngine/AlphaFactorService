# AlphaFactorService

AlphaBlocks统一的数据研究服务，负责因子、Qlib模型训练与推理、模型信号和快速回测。

## 职责边界

- Studio 负责展示、编辑、创建任务、查看结果。
- AlphaFactorService 负责保存因子定义、管理任务、执行计算、查询结果。
- AlphaFactorService 内置研究调度器，负责LightGBM、XGBoost、CatBoost和PyTorch MLP训练与每日推理。
- 因子API和研究调度器运行在同一个服务进程、共用8100端口；原生模型训练由短生命周期子进程隔离。
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
- `POST /model-inference/availability` 按冻结因子、数据截止时间和历史交易日检查每日推理可用性。
- `GET /model-signals` 返回指定不可变模型版本、交易日的PIT安全TopN信号，供AlphaBlocks正式策略回测读取。
- `POST /model-research/jobs` 创建训练任务，`POST /model-research/jobs/{job_id}/dispatch` 在本服务中调度执行。
- `GET /research/ready` 和 `GET /research/status` 查询内置研究调度器状态。

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
cp config/runtime.example.yaml config/runtime.local.yaml
alpha-factor-service
```

默认服务地址：

```text
http://127.0.0.1:8100
```

## PM2 启动

```bash
cd /Users/zhao/Desktop/git/AlphaFactorService
python3.11 -m venv .venv
.venv/bin/pip install -e .
pm2 start ecosystem.config.js
```

PM2只启动一个常驻进程`alpha-factor-service`，统一提供因子、模型信号、回测、
Qlib多模型训练和每日推理。模型训练在任务期间启动隔离子进程，完成后自动退出。
PyTorch MLP使用显式`hidden_layers`数组冻结逐层宽度，例如`[64, 128, 256]`表示
`输入维度 → 64 → 128 → 256 → 1`；层结构会进入任务配置和训练manifest，保证可复现。
PyTorch LSTM使用Qlib `TSDatasetH`按股票构建因果历史窗口，默认结构为
`60交易日 × 因子维度 → 2层LSTM(128) → 1`。训练、Walk-Forward、测试段预测和每日推理
使用同一`lookback_window`，不允许把不同股票的数据拼成一条序列。
Transformer+LSTM混合模型在同一窗口上先执行带因果遮罩的Transformer Encoder，再把
时序表示交给LSTM输出截面预测。默认结构为`60日 → Transformer 64D/4头/2层 →
LSTM(128) → 1`，所有结构参数随模型版本冻结。

研究能力已经融合到`factor_service.research`模块，不再存在第二个顶层业务包。当前处于开发阶段，
训练产物只接受新的`factor_service.research.models`类路径，不保留旧Worker包兼容层。

默认只监听本机：

```text
http://127.0.0.1:8100
```

如果AlphaBlocks运行在另一台机器，部署机的`runtime.local.yaml`需要改为：

```yaml
service:
  host: 0.0.0.0
  port: 8100
```

同时把AlphaBlocks的`external_services.factor_service.base_url`设为这台部署机的局域网地址。

监听地址、ClickHouse、数据源与研究存储位置统一配置在：

```text
config/runtime.local.yaml
```

`runtime.local.yaml`已被Git忽略。部署时可以用`ALPHA_FACTOR_RUNTIME_CONFIG`显式选择
另一份YAML；除Python解释器选择外，PM2不再通过环境变量覆盖业务配置。

统一服务使用Python 3.11或3.12，并复用同一份`clickhouse`配置。研究配置示例：

```yaml
research:
  scheduler:
    enabled: true
    refresh_seconds: 60
  storage:
    work_root: /Volumes/QuantData/alphafactor/research-work
    model_artifacts_root: /Volumes/QuantData/alphafactor/model-artifacts
```

每日推理计划由同一个AlphaFactorService进程按`research.scheduler.refresh_seconds`检查，
不需要AlphaBlocks再运行独立的模型推理调度进程。

`research.storage.work_root`只保存训练暂存文件、预测Parquet、MLflow记录、日志和任务状态；
`research.storage.model_artifacts_root`保存经过SHA256校验并原子发布的正式模型产物，以及
`datasets/{dataset_hash}`下不可变的训练Parquet快照。Qlib训练只读取正式快照，不读取暂存文件。
两者都由AlphaFactorService管理。相对路径从项目根目录解析；需要
放到外置磁盘或指定数据盘时建议直接填写绝对路径。AlphaBlocks只保存产物元数据。

AlphaBlocks只配置`external_services.factor_service.base_url`。没有单独的研究服务地址或端口。
模型任务、事件、版本和产物元数据由AlphaFactorService直接写入`control_database`，
AlphaFactorService不再配置或回调AlphaBlocks API。

### Walk-Forward滚动评估

训练任务可选开启严格Walk-Forward评估。每个窗口按交易日依次执行
`训练 → 5日隔离 → 验证 → 5日隔离 → 独立测试`，逐窗缺失值中位数仅使用该窗训练段拟合。
各窗口测试预测按时间拼接后写入模型信号库并用于Top20研究回测；正式模型产物仍单独训练和保存，
用于后续每日推理。滚动窗口默认按1年252个交易日、1个月21个交易日换算。

```json
{
  "walk_forward": {
    "enabled": true,
    "strategy": "rolling",
    "train_years": 1,
    "valid_months": 3,
    "test_months": 12,
    "step_months": 12,
    "max_windows": 4,
    "embargo_days": 5
  }
}
```

为保证拼接后的样本外信号唯一，`step_months`不得小于`test_months`。`expanding`策略固定首个
训练日并逐窗扩展训练段；`rolling`策略保持训练长度不变并向前滚动。

安装后可用统一命令执行环境诊断：

```bash
alpha-factor-service doctor
```

如果以后需要独立后台消费 pending 任务，可以单独运行：

```bash
python -m factor_service.worker --limit 5 --poll-interval 60
```

## ClickHouse 初始化

ClickHouse连接信息写在本项目`config/runtime.local.yaml`：

```yaml
clickhouse:
  host: 10.126.126.3
  port: 8123
  username: default
  password: ""
  secure: false
  factor_database: ab_factor
  model_database: ab_model
```

其中`factor_database`是因子服务自己的库，默认`ab_factor`。

因子计算的数据源单独配置，默认读取 `baostock.stock_daily_real`：

```yaml
sources:
  factor:
    database: ab_factor
    stock_daily_table: stock_daily_factor_source
    stock_code_column: code
    stock_date_column: trade_time
    stock_price_column: close
    stock_basic_table: stock_basic_factor_source
    stock_basic_type_column: type
    stock_basic_stock_type_value: "1"
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

模型训练不要求先写入`factor_values_daily`：任务会锁定因子版本、公式参数和`params_hash`，
直接从源数据即时计算，先在`work_root`暂存，再发布为不可变Parquet。因子中心的同步、评价和
单因子回测仍继续使用`factor_values_daily`，两条流程互不替代。

`model-signals`只返回`feature_cutoff_at <= T日15:00`的模型预测，并携带
`dataset_hash`和`inference_run_id`。接口只提供因果安全的信号截面；正式策略仍由AlphaBlocks
执行T+1成交、涨跌停/停牌、费用和持仓规则，FactorService不会在这里复制策略引擎。

后续分钟级结果可以单独增加：

```text
ab_factor.factor_values_intraday
```
