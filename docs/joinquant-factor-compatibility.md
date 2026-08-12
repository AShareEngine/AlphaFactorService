# 聚宽因子与当前系统同步能力检测

> 检测时间：2026-08-12 23:54:04 CST。
> “可同步”表示当前 ClickHouse 日频源字段存在、表达式能通过现有编译器，
> 不代表数值已与聚宽逐日对齐，也不代表聚宽的行业/市值中性化已经复刻。

## 结论

- 聚宽目录：285 个。
- **现在可同步 raw_value：84 个**，其中全历史核心行情字段 50 个，依赖部分历史字段 34 个。
- 公式口径需人工确认：9 个。
- 数据够、但计算引擎需扩展：15 个。
- 当前缺数据或复合依赖：177 个。
- 真实 ClickHouse 执行探测：93/93 个候选表达式通过。
- 在可同步因子中，聚宽标记为“无数据处理”的有 6 个；其余大多要求行业/市值中性化，当前只能同步原始值，不能声称完全复刻聚宽 score。

## 当前源实测

- 表：`ab_factor.stock_daily_factor_source`
- 行数：12,379,489
- 日期：2013-01-04 至 2026-08-10
- worker 实际股票行数：12,379,489
- worker 实际股票代码数：5,466
- 底层视图未过滤规模：18,114,802 行、29,861 个代码

| 字段 | 非空覆盖率 |
|---|---:|
| `open` | 100.00% |
| `high` | 100.00% |
| `low` | 100.00% |
| `close` | 100.00% |
| `volume` | 100.00% |
| `amount` | 100.00% |
| `pre_close` | 100.00% |
| `turnover_rate` | 99.97% |
| `pct_chg` | 99.97% |
| `pe` | 99.97% |
| `pb` | 99.97% |
| `high_limited` | 95.40% |
| `low_limited` | 95.40% |

## 现在可同步的 84 个聚宽因子

### 风险因子 - 风格因子

- 部分历史：`average_share_turnover_annual`、`average_share_turnover_quarterly`、`book_to_price_ratio`、`earnings_to_price_ratio`、`share_turnover_monthly`

### 情绪类因子

- 完整历史：`AR`、`ARBR`、`ATR14`、`ATR6`、`BR`、`TVMA20`、`TVMA6`、`TVSTD20`、`TVSTD6`、`VDIFF`、`VOSC`、`VROC12`、`VROC6`、`VSTD10`、`VSTD20`、`WVAD`
- 部分历史：`DAVOL10`、`DAVOL20`、`DAVOL5`、`PSY`、`turnover_volatility`、`VOL10`、`VOL120`、`VOL20`、`VOL240`、`VOL5`、`VOL60`、`VR`

### 风险类因子

- 部分历史：`Kurtosis120`、`Kurtosis20`、`Kurtosis60`、`sharpe_ratio_120`、`sharpe_ratio_20`、`sharpe_ratio_60`、`Skewness120`、`Skewness20`、`Skewness60`、`Variance120`、`Variance20`、`Variance60`

### 技术指标因子

- 完整历史：`boll_down`、`boll_up`、`EMA5`、`EMAC10`、`EMAC12`、`EMAC120`、`EMAC20`、`EMAC26`、`MAC10`、`MAC120`、`MAC20`、`MAC5`、`MAC60`

### 动量类因子

- 完整历史：`arron_down_25`、`arron_up_25`、`BBIC`、`bear_power`、`BIAS10`、`BIAS20`、`BIAS5`、`BIAS60`、`bull_power`、`CCI10`、`CCI15`、`CCI20`、`PLRC12`、`PLRC24`、`PLRC6`、`Price1M`、`Price1Y`、`Price3M`、`ROC12`、`ROC20`、`ROC6`
- 部分历史：`single_day_VPT`、`single_day_VPT_12`、`single_day_VPT_6`、`Volume1M`

### 风险因子 - 新风格因子

- 部分历史：`btop`


## 系统已登记因子

| 因子 | 最新版本 | 同步条件 | 最新版本持久化 | 字段 |
|---|---:|---|---|---|
| `first_limit_up_window`（N日首次涨停） | v3 | 可同步 | 0 行（需重算最新版本） | `close`, `high_limited` |
| `limit_up_count`（N日涨停次数） | v3 | 可同步 | 0 行（需重算最新版本） | `close`, `high_limited` |
| `mean_amount`（N日平均成交额） | v2 | 可同步 | 0 行（需重算最新版本） | `amount` |
| `mean_turnover_rate`（N日平均换手率） | v2 | 可同步 | 0 行（需重算最新版本） | `turnover_rate` |
| `mean_volume`（N日平均成交量） | v2 | 可同步 | 0 行（需重算最新版本） | `volume` |
| `period_return`（N日涨跌幅） | v3 | 可同步 | 0 行（需重算最新版本） | `close` |
| `stock_fear_proxy`（个股恐慌度） | v2 | 可同步 | 0 行（需重算最新版本） | `close`, `pct_chg`, `volume` |

## 285 个聚宽因子的逐项结果

状态说明：`可同步-完整` 的依赖字段在 worker 股票范围内 100% 非空；`可同步-部分` 至少有一个依赖字段存在历史缺失。

### 基础科目及衍生类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `administration_expense_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `asset_impairment_loss_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cash_flow_to_price_ratio` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `circulating_market_cap` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 行业中性化 -> zscore标准化 |
| `EBIT` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EBITDA` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `financial_assets` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `financial_expense_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `financial_liability` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `goods_sale_and_service_render_cash_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `gross_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `interest_carry_current_liability` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `interest_free_current_liability` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `market_cap` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 行业中性化 -> zscore标准化 |
| `net_debt` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_finance_cash_flow_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_interest_expense` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_invest_cash_flow_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_working_capital` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `non_operating_net_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `non_recurring_gain_loss` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `np_parent_company_owners_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `OperateNetIncome` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_assets` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_cost_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_liability` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_revenue_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `retained_earnings` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sale_expense_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sales_to_price_ratio` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_operating_cost_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_operating_revenue_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `value_change_profit_ttm` | 缺数据/依赖 | - | 缺财务报表、现金流、市值等源字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 质量类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `ACCA` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `account_receivable_turnover_days` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `account_receivable_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `accounts_payable_turnover_days` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `accounts_payable_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `adjusted_profit_to_total_profit` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `admin_expense_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `asset_turnover_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cash_rate_of_sales` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cash_to_current_liability` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cfo_to_ev` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `current_asset_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `current_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `debt_to_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `debt_to_equity_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `debt_to_tangible_equity_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DEGM` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DEGM_8y` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DSRI` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `equity_to_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `equity_to_fixed_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `equity_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `financial_expense_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `fixed_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `fixed_assets_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `GMI` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `goods_service_cash_to_operating_revenue_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `gross_income_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `intangible_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `inventory_turnover_days` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `inventory_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `invest_income_associates_to_total_profit` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `long_debt_to_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `long_debt_to_working_capital_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `long_term_debt_to_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `LVGI` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `margin_stability` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `maximum_margin` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MLEV` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_non_operating_income_to_total_profit` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 过滤值为0的因子 -> 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_to_asset` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_to_net_debt` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_to_operate_income` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_to_total_current_liability` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_to_total_liability` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operating_cash_flow_coverage` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_profit_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_profit_to_total_operate_revenue_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `non_current_asset_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_cost_to_operating_revenue_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_growth_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_to_operating_revenue` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_to_total_profit` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_tax_to_operating_revenue_ratio_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `OperatingCycle` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `profit_margin_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `quick_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `rnoa_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `roa_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `roa_ttm_8y` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROAEBITTTM` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `roe_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `roe_ttm_8y` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `roic_ttm` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sale_expense_to_operating_revenue` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `SGAI` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `SGI` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `super_quick_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_asset_turnover_rate` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_profit_to_cost_ratio` | 缺数据/依赖 | - | 缺资产负债表、利润表、现金流量表等财务字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 每股指标因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `capital_reserve_fund_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cash_and_equivalents_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `cashflow_per_share_ttm` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `eps_ttm` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_asset_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cash_flow_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_profit_per_share_ttm` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_revenue_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_revenue_per_share_ttm` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `retained_earnings_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `retained_profit_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `surplus_reserve_fund_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_operating_revenue_per_share` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_operating_revenue_per_share_ttm` | 缺数据/依赖 | - | 缺财务科目和总股本字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 风险因子 - 风格因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `average_share_turnover_annual` | 可同步-部分 | `turnover_rate` | `Log(Sum($turnover_rate, 252) / 12)` | 无 |
| `average_share_turnover_quarterly` | 可同步-部分 | `turnover_rate` | `Log(Sum($turnover_rate, 63) / 3)` | 无 |
| `beta` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `book_leverage` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `book_to_price_ratio` | 可同步-部分 | `pb` | `1 / NullIf($pb, 0)` | 无 |
| `cash_earnings_to_price_ratio` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `cube_of_size` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `cumulative_range` | 需扩展引擎 | - | 需要月频重采样与区间累计收益 | 无 |
| `daily_standard_deviation` | 需扩展引擎 | - | 需要半衰期指数加权标准差 | 无 |
| `debt_to_assets` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `earnings_growth` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `earnings_to_price_ratio` | 可同步-部分 | `pe` | `1 / NullIf($pe, 0)` | 无 |
| `earnings_yield` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `growth` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `historical_sigma` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `leverage` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `liquidity` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `long_term_predicted_earnings_growth` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `market_leverage` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `momentum` | 需扩展引擎 | - | 需要跳过最近 21 日并支持半衰期指数权重 | 无 |
| `natural_log_of_market_cap` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `non_linear_size` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `predicted_earnings_to_price_ratio` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `raw_beta` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `relative_strength` | 需扩展引擎 | - | 需要半衰期指数加权对数收益 | 无 |
| `residual_volatility` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `sales_growth` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `share_turnover_monthly` | 可同步-部分 | `turnover_rate` | `Log(Sum($turnover_rate, 21))` | 无 |
| `short_term_predicted_earnings_growth` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |
| `size` | 缺数据/依赖 | - | 缺市场指数、行业、市值、分析师预测或复合因子依赖 | 无 |

### 情绪类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `AR` | 可同步-完整 | `high`, `low`, `open` | `(Sum(Greater($high - $open, 0), 26)) / NullIf((Sum(Greater($open - $low, 0), 26)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ARBR` | 可同步-完整 | `high`, `low`, `open`, `pre_close` | `((Sum(Greater($high - $open, 0), 26)) / NullIf((Sum(Greater($open - $low, 0), 26)), 0) * 100) - ((Sum(Greater($high - $pre_close, 0), 26)) / NullIf((Sum(Greater($pre_close - $low, 0), 26)), 0) * 100)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ATR14` | 可同步-完整 | `high`, `low`, `pre_close` | `Mean(Greater(Greater($high - $low, Abs($high - $pre_close)), Abs($low - $pre_close)), 14)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ATR6` | 可同步-完整 | `high`, `low`, `pre_close` | `Mean(Greater(Greater($high - $low, Abs($high - $pre_close)), Abs($low - $pre_close)), 6)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BR` | 可同步-完整 | `high`, `low`, `pre_close` | `(Sum(Greater($high - $pre_close, 0), 26)) / NullIf((Sum(Greater($pre_close - $low, 0), 26)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DAVOL10` | 可同步-部分 | `turnover_rate` | `(Mean($turnover_rate, 10)) / NullIf((Mean($turnover_rate, 120)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DAVOL20` | 可同步-部分 | `turnover_rate` | `(Mean($turnover_rate, 20)) / NullIf((Mean($turnover_rate, 120)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `DAVOL5` | 可同步-部分 | `turnover_rate` | `(Mean($turnover_rate, 5)) / NullIf((Mean($turnover_rate, 120)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAWVAD` | 需扩展引擎 | - | 需要先算 WVAD 再做移动平均，当前禁止嵌套窗口 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `money_flow_20` | 口径待确认 | `close`, `high`, `low`, `volume` | `Mean((($high + $low + $close) / 3) * $volume, 20)`；公开说明只定义单日资金流，未说明 20 日使用均值还是求和 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `PSY` | 可同步-部分 | `pct_chg` | `Sum(Gt($pct_chg, 0), 12) / 12 * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `turnover_volatility` | 可同步-部分 | `turnover_rate` | `Std($turnover_rate, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TVMA20` | 可同步-完整 | `amount` | `Mean($amount, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TVMA6` | 可同步-完整 | `amount` | `Mean($amount, 6)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TVSTD20` | 可同步-完整 | `amount` | `Std($amount, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TVSTD6` | 可同步-完整 | `amount` | `Std($amount, 6)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VDEA` | 需扩展引擎 | - | 需要对 VDIFF 再做 EMA，当前禁止嵌套窗口 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VDIFF` | 可同步-完整 | `volume` | `EMA($volume, 12) - EMA($volume, 26)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VEMA10` | 口径待确认 | `volume` | `EMA($volume, 10)`；聚宽详情没有给出计算逻辑，当前表达式按因子名推定 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VEMA12` | 口径待确认 | `volume` | `EMA($volume, 12)`；聚宽详情没有给出计算逻辑，当前表达式按因子名推定 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VEMA26` | 口径待确认 | `volume` | `EMA($volume, 26)`；聚宽详情没有给出计算逻辑，当前表达式按因子名推定 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VEMA5` | 口径待确认 | `volume` | `EMA($volume, 5)`；聚宽详情没有给出计算逻辑，当前表达式按因子名推定 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VMACD` | 需扩展引擎 | - | 依赖 VDIFF/VDEA 的多阶段计算 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL10` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 10)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL120` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 120)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL20` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL240` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 240)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL5` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 5)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOL60` | 可同步-部分 | `turnover_rate` | `Mean($turnover_rate, 60)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VOSC` | 可同步-完整 | `volume` | `(EMA($volume, 12) - EMA($volume, 26)) / NullIf((EMA($volume, 12)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VR` | 可同步-部分 | `pct_chg`, `volume` | `(Sum(If(Gt($pct_chg, 0), $volume, 0), 26) + 0.5 * Sum(If(Eq($pct_chg, 0), $volume, 0), 26)) / NullIf((Sum(If(Lt($pct_chg, 0), $volume, 0), 26) + 0.5 * Sum(If(Eq($pct_chg, 0), $volume, 0), 26)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VROC12` | 可同步-完整 | `volume` | `PeriodReturn($volume, 12) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VROC6` | 可同步-完整 | `volume` | `PeriodReturn($volume, 6) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VSTD10` | 可同步-完整 | `volume` | `Std($volume, 10)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `VSTD20` | 可同步-完整 | `volume` | `Std($volume, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `WVAD` | 可同步-完整 | `close`, `high`, `low`, `open`, `volume` | `Sum((($close - $open) / NullIf($high - $low, 0)) * $volume, 6)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 成长类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `financing_cash_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_asset_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_operate_cashflow_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `net_profit_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `np_parent_company_owners_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `operating_revenue_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `PEG` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_asset_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `total_profit_growth_rate` | 缺数据/依赖 | - | 缺多期财务与同比增长字段 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 风险类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `Kurtosis120` | 可同步-部分 | `pct_chg` | `Kurt(($pct_chg / 100), 120)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Kurtosis20` | 可同步-部分 | `pct_chg` | `Kurt(($pct_chg / 100), 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Kurtosis60` | 可同步-部分 | `pct_chg` | `Kurt(($pct_chg / 100), 60)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sharpe_ratio_120` | 可同步-部分 | `pct_chg` | `(Mean(($pct_chg / 100), 120) * 252 - 0.04) / NullIf((Std(($pct_chg / 100), 120) * Power(252, 0.5)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sharpe_ratio_20` | 可同步-部分 | `pct_chg` | `(Mean(($pct_chg / 100), 20) * 252 - 0.04) / NullIf((Std(($pct_chg / 100), 20) * Power(252, 0.5)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `sharpe_ratio_60` | 可同步-部分 | `pct_chg` | `(Mean(($pct_chg / 100), 60) * 252 - 0.04) / NullIf((Std(($pct_chg / 100), 60) * Power(252, 0.5)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Skewness120` | 可同步-部分 | `pct_chg` | `Skew(($pct_chg / 100), 120)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Skewness20` | 可同步-部分 | `pct_chg` | `Skew(($pct_chg / 100), 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Skewness60` | 可同步-部分 | `pct_chg` | `Skew(($pct_chg / 100), 60)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Variance120` | 可同步-部分 | `pct_chg` | `Var(($pct_chg / 100), 120)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Variance20` | 可同步-部分 | `pct_chg` | `Var(($pct_chg / 100), 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Variance60` | 可同步-部分 | `pct_chg` | `Var(($pct_chg / 100), 60)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 技术指标因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `boll_down` | 可同步-完整 | `close` | `(Mean($close, 20) - 2 * Std($close, 20)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `boll_up` | 可同步-完整 | `close` | `(Mean($close, 20) + 2 * Std($close, 20)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMA5` | 可同步-完整 | `close` | `(EMA($close, 5)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMAC10` | 可同步-完整 | `close` | `(EMA($close, 10)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMAC12` | 可同步-完整 | `close` | `(EMA($close, 12)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMAC120` | 可同步-完整 | `close` | `(EMA($close, 120)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMAC20` | 可同步-完整 | `close` | `(EMA($close, 20)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `EMAC26` | 可同步-完整 | `close` | `(EMA($close, 26)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAC10` | 可同步-完整 | `close` | `(Mean($close, 10)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAC120` | 可同步-完整 | `close` | `(Mean($close, 120)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAC20` | 可同步-完整 | `close` | `(Mean($close, 20)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAC5` | 可同步-完整 | `close` | `(Mean($close, 5)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MAC60` | 可同步-完整 | `close` | `(Mean($close, 60)) / NullIf(($close), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MACDC` | 需扩展引擎 | - | 需要对 DIF 再做信号线 EMA，当前禁止嵌套窗口 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MFI14` | 需扩展引擎 | - | 需要昨日典型价参与滚动条件求和，当前禁止窗口嵌套 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `price_no_fq` | 口径待确认 | `close` | `$close`；需要先确认 starlight.ad_market_kline_daily.close 是否确为不复权价格 | 无 |

### 动量类因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `arron_down_25` | 可同步-完整 | `low` | `IdxMin($low, 25) / 25 * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `arron_up_25` | 可同步-完整 | `high` | `IdxMax($high, 25) / 25 * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BBIC` | 可同步-完整 | `close` | `(Mean($close, 3) + Mean($close, 6) + Mean($close, 12) + Mean($close, 24)) / 4 / NullIf($close, 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `bear_power` | 可同步-完整 | `close`, `low` | `($low - EMA($close, 13)) / NullIf($close, 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BIAS10` | 可同步-完整 | `close` | `($close - Mean($close, 10)) / NullIf((Mean($close, 10)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BIAS20` | 可同步-完整 | `close` | `($close - Mean($close, 20)) / NullIf((Mean($close, 20)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BIAS5` | 可同步-完整 | `close` | `($close - Mean($close, 5)) / NullIf((Mean($close, 5)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `BIAS60` | 可同步-完整 | `close` | `($close - Mean($close, 60)) / NullIf((Mean($close, 60)), 0) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `bull_power` | 可同步-完整 | `close`, `high` | `($high - EMA($close, 13)) / NullIf($close, 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `CCI10` | 可同步-完整 | `close`, `high`, `low` | `((($high + $low + $close) / 3) - Mean((($high + $low + $close) / 3), 10)) / NullIf((0.015 * Mad((($high + $low + $close) / 3), 10)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `CCI15` | 可同步-完整 | `close`, `high`, `low` | `((($high + $low + $close) / 3) - Mean((($high + $low + $close) / 3), 15)) / NullIf((0.015 * Mad((($high + $low + $close) / 3), 15)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `CCI20` | 可同步-完整 | `close`, `high`, `low` | `((($high + $low + $close) / 3) - Mean((($high + $low + $close) / 3), 20)) / NullIf((0.015 * Mad((($high + $low + $close) / 3), 20)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `CCI88` | 需扩展引擎 | - | 现有 Mad 展开后的 SQL 超过 ClickHouse 默认 256 KiB 查询长度限制 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `CR20` | 需扩展引擎 | - | 需要昨日中间价参与滚动求和，当前禁止窗口嵌套 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `fifty_two_week_close_rank` | 口径待确认 | `close` | `1 - Rank($close, 250)`；当前时序 Rank 可计算，但聚宽未公开并列值与名次归一化口径 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `MASS` | 需扩展引擎 | - | 需要两层 EMA 后再滚动求和 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `PLRC12` | 可同步-完整 | `close` | `(Slope($close, 12)) / NullIf((Mean($close, 12)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `PLRC24` | 可同步-完整 | `close` | `(Slope($close, 24)) / NullIf((Mean($close, 24)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `PLRC6` | 可同步-完整 | `close` | `(Slope($close, 6)) / NullIf((Mean($close, 6)), 0)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Price1M` | 可同步-完整 | `close` | `($close) / NullIf((Mean($close, 21)), 0) - 1` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Price1Y` | 可同步-完整 | `close` | `($close) / NullIf((Mean($close, 250)), 0) - 1` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Price3M` | 可同步-完整 | `close` | `($close) / NullIf((Mean($close, 61)), 0) - 1` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Rank1M` | 需扩展引擎 | - | 需要对 20 日收益做每日横截面排名；现有 rank_value 不能替代 raw_value | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROC12` | 可同步-完整 | `close` | `PeriodReturn($close, 12) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROC120` | 口径待确认 | `close` | `PeriodReturn($close, 120) * 100`；聚宽公开公式与因子名冲突：两个详情都写成 20 日差值除以 60 日前价格 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROC20` | 可同步-完整 | `close` | `PeriodReturn($close, 20) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROC6` | 可同步-完整 | `close` | `PeriodReturn($close, 6) * 100` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `ROC60` | 口径待确认 | `close` | `PeriodReturn($close, 60) * 100`；聚宽公开公式与因子名冲突：两个详情都写成 20 日差值除以 60 日前价格 | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `single_day_VPT` | 可同步-部分 | `pct_chg`, `volume` | `($pct_chg / 100) * $volume` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `single_day_VPT_12` | 可同步-部分 | `pct_chg`, `volume` | `Mean(($pct_chg / 100) * $volume, 12)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `single_day_VPT_6` | 可同步-部分 | `pct_chg`, `volume` | `Mean(($pct_chg / 100) * $volume, 6)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TRIX10` | 需扩展引擎 | - | 需要三重 EMA | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `TRIX5` | 需扩展引擎 | - | 需要三重 EMA | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |
| `Volume1M` | 可同步-部分 | `pct_chg`, `volume` | `($volume) / NullIf((Mean($volume, 20)), 0) * Mean($pct_chg / 100, 20)` | 中位数去极值 -> 行业市值对数中性化 -> zscore标准化 |

### 风险因子 - 新风格因子

| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |
|---|---|---|---|---|
| `btop` | 可同步-部分 | `pb` | `1 / NullIf($pb, 0)` | 无 |
| `dividend_yield_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `divyild` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `earnqlty` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `earnvar` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `earnyild` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `financial_leverage` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `growth_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `invsqlty` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `liquidity_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `liquidty` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `long_growth` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `ltrevrsl` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `market_beta` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `market_size` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `midcap` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `momentum_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `profit` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `quality_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `relative_momentum` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `resvol` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `sentiment_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `size_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `value_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |
| `volatility_v2` | 缺数据/依赖 | - | 缺组成因子、行业/市值暴露或公开公式 | 无 |

## 检测口径与下一步

1. 本报告验证的是表达式可编译、字段真实存在以及字段非空覆盖率；没有批量创建 85 个因子，也没有启动大规模同步任务。
2. 可同步因子的 `raw_value` 可以先落库；若沿用系统默认处理，`score` 是当前系统的横截面缩尾 Z-score，不等于聚宽含行业/市值中性化的 score。
3. 建议先从 100% 行情覆盖组挑 5 至 10 个代表因子做逐日抽样对齐，再发布全量定义。
4. 重新检测：`rtk .venv/bin/python scripts/audit_joinquant_factor_compatibility.py`。
