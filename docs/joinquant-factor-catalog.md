# 聚宽因子库目录与详情快照

> 本文档用于 AlphaFactorService 的因子迁移与实施分析。内容来自聚宽公开因子看板接口，
> 是元数据与自然语言计算说明的快照，不代表已经取得聚宽的可执行源码或历史因子值。

## 快照信息

- 生成时间：2026-08-12 23:30:09 CST
- 来源页面：[https://www.joinquant.com/view/factorlib/list](https://www.joinquant.com/view/factorlib/list)
- 列表接口：`GET /factorlib/index/getList`
- 详情接口：`GET /factorlib/index/getInfo`
- 快照筛选：中证500、近3年、无佣金、跳过停牌
- 因子总数：285
- 成功取得详情：285/285
- 带计算逻辑说明：271/285
- 重新生成：`rtk .venv/bin/python scripts/export_joinquant_factor_catalog.py`

### 重要实施约束

1. 聚宽列表返回的 `factor_id` 会随股票池、时间范围等筛选条件变化，本文只记录快照 ID；正式导入应以英文因子名作为外部稳定标识。
2. `algorithmIntro` 多数是自然语言说明，不是可直接执行的 QLib/Python 源码；迁移时必须单独翻译、校验和回测对齐。
3. 财务、分析师预测、行业、市值暴露等因子依赖的数据不一定存在于当前股票日线源中，不能直接批量发布到正式因子库。
4. `processMethods` 记录了聚宽的去极值、中性化和标准化流程；这部分与原始公式同样影响最终因子值。
5. 详情页链接包含快照 ID，未来若聚宽重建 ID，旧链接可能失效。

## 分类统计

| 分类 | 因子数量 |
|---|---:|
| 基础科目及衍生类因子 | 37 |
| 质量类因子 | 71 |
| 每股指标因子 | 15 |
| 风险因子 - 风格因子 | 30 |
| 情绪类因子 | 36 |
| 成长类因子 | 9 |
| 风险类因子 | 12 |
| 技术指标因子 | 16 |
| 动量类因子 | 34 |
| 风险因子 - 新风格因子 | 25 |

## 聚宽看板参数快照

- 股票池：{'hs300': ['沪深300', '000300'], 'zz500': ['中证500', '000905'], 'zz800': ['中证800', '000906'], 'zz1000': ['中证1000', '000852'], 'zzqz': ['中证全指', '000985']}
- 时间范围：{'3m': '近3个月', '1y': '近1年', '3y': '近3年', '10y': '近10年'}

## 因子总目录

### 基础科目及衍生类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 1 | `administration_expense_ttm` | 管理费用TTM | 计算过去12个月 管理费用 之和 | [查看](#factor-001) |
| 2 | `asset_impairment_loss_ttm` | 资产减值损失TTM | 计算过去12个月 资产减值损失 之和 | [查看](#factor-002) |
| 3 | `cash_flow_to_price_ratio` | 现金流市值比 | 1 / pcf_ratio (ttm) | [查看](#factor-003) |
| 4 | `circulating_market_cap` | 流通市值 | 流通市值 | [查看](#factor-004) |
| 5 | `EBIT` | 息税前利润 | =净利润+所得税+财务费用 | [查看](#factor-005) |
| 6 | `EBITDA` | 息税折旧摊销前利润 | =营业收入－经营成本－营业税及附加 | [查看](#factor-006) |
| 7 | `financial_assets` | 金融资产 | 货币资金 + 交易性金融资产 + 应收票据 + 应收利息 + 应收股利 + 可供出售金融资产 + 持有至到期投资 | [查看](#factor-007) |
| 8 | `financial_expense_ttm` | 财务费用TTM | 计算过去12个月 财务费用 之和 | [查看](#factor-008) |
| 9 | `financial_liability` | 金融负债 | (流动负债合计-无息流动负债)+(有息非流动负债)=(流动负债合计-应付账款-预收款项-应付职工薪酬-应交税费-其他应付款-一年内的递延收益-其它流动负债)+(长期借款+应付债券) | [查看](#factor-009) |
| 10 | `goods_sale_and_service_render_cash_ttm` | 销售商品提供劳务收到的现金 | 计算过去12个月 销售商品提供劳务收到的现金 之和 | [查看](#factor-010) |
| 11 | `gross_profit_ttm` | 毛利TTM | 计算过去12个月毛利之和 | [查看](#factor-011) |
| 12 | `interest_carry_current_liability` | 带息流动负债 | 流动负债合计 - 无息流动负债 | [查看](#factor-012) |
| 13 | `interest_free_current_liability` | 无息流动负债 | 应付票据+应付账款+预收账款(用 预售款项 代替)+应交税费+应付利息+其他应付款+其他流动负债 | [查看](#factor-013) |
| 14 | `market_cap` | 市值 | 市值 | [查看](#factor-014) |
| 15 | `net_debt` | 净债务 | 总债务-期末现金及现金等价物余额 | [查看](#factor-015) |
| 16 | `net_finance_cash_flow_ttm` | 筹资活动现金流量净额TTM | 计算过去12个月 筹资活动现金流量净额 之和 | [查看](#factor-016) |
| 17 | `net_interest_expense` | 净利息费用 | 利息支出-利息收入 | [查看](#factor-017) |
| 18 | `net_invest_cash_flow_ttm` | 投资活动现金流量净额TTM | 计算过去12个月 投资活动现金流量净额 之和 | [查看](#factor-018) |
| 19 | `net_operate_cash_flow_ttm` | 经营活动现金流量净额TTM | 计算过去12个月 经营活动产生的现金流量净值 之和 | [查看](#factor-019) |
| 20 | `net_profit_ttm` | 净利润TTM | 计算过去12个月 净利润 之和 | [查看](#factor-020) |
| 21 | `net_working_capital` | 净运营资本 | 流动资产 － 流动负债 | [查看](#factor-021) |
| 22 | `non_operating_net_profit_ttm` | 营业外收支净额TTM | 营业外收入（TTM） - 营业外支出（TTM） | [查看](#factor-022) |
| 23 | `non_recurring_gain_loss` | 非经常性损益 | 净利润-扣除非经常损益后的净利润(元) | [查看](#factor-023) |
| 24 | `np_parent_company_owners_ttm` | 归属于母公司股东的净利润TTM | 计算过去12个月 归属于母公司股东的净利润 之和 | [查看](#factor-024) |
| 25 | `OperateNetIncome` | 经营活动净收益 | 经营活动净收益/利润总额(%) * 利润总额 | [查看](#factor-025) |
| 26 | `operating_assets` | 经营性资产 | 总资产 - 金融资产 | [查看](#factor-026) |
| 27 | `operating_cost_ttm` | 营业成本TTM | 计算过去12个月的 营业成本 之和 | [查看](#factor-027) |
| 28 | `operating_liability` | 经营性负债 | 总负债 - 金融负债 | [查看](#factor-028) |
| 29 | `operating_profit_ttm` | 营业利润TTM | 计算过去12个月 营业利润 之和 | [查看](#factor-029) |
| 30 | `operating_revenue_ttm` | 营业收入TTM | 计算过去12个月的 营业收入 之和 | [查看](#factor-030) |
| 31 | `retained_earnings` | 留存收益 | =盈余公积金+未分配利润 | [查看](#factor-031) |
| 32 | `sale_expense_ttm` | 销售费用TTM | 计算过去12个月 销售费用 之和 | [查看](#factor-032) |
| 33 | `sales_to_price_ratio` | 营收市值比 | 1 / ps_ratio (ttm) | [查看](#factor-033) |
| 34 | `total_operating_cost_ttm` | 营业总成本TTM | 计算过去12个月的 营业总成本 之和 | [查看](#factor-034) |
| 35 | `total_operating_revenue_ttm` | 营业总收入TTM | 计算过去12个月的 营业总收入 之和 | [查看](#factor-035) |
| 36 | `total_profit_ttm` | 利润总额TTM | 计算过去12个月 利润总额 之和 | [查看](#factor-036) |
| 37 | `value_change_profit_ttm` | 价值变动净收益TTM | 计算过去12个月 价值变动净收益 之和 | [查看](#factor-037) |

### 质量类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 38 | `ACCA` | 现金流资产比和资产回报率之差 | 现金流资产比-资产回报率,其中现金流资产比=经营活动产生的现金流量净额/总资产 | [查看](#factor-038) |
| 39 | `account_receivable_turnover_days` | 应收账款周转天数 | 应收账款周转天数=360/应收账款周转率 | [查看](#factor-039) |
| 40 | `account_receivable_turnover_rate` | 应收账款周转率 | 即，TTM(营业收入,0)/（AvgQ(应收账款,4,0) + AvgQ(应收票据,4,0) + AvgQ(预收账款,4,0) ） | [查看](#factor-040) |
| 41 | `accounts_payable_turnover_days` | 应付账款周转天数 | 应付账款周转天数 = 360 / 应付账款周转率 | [查看](#factor-041) |
| 42 | `accounts_payable_turnover_rate` | 应付账款周转率 | TTM(营业成本,0)/（AvgQ(应付账款,4,0) + AvgQ(应付票据,4,0) + AvgQ(预付款项,4,0) ） | [查看](#factor-042) |
| 43 | `adjusted_profit_to_total_profit` | 扣除非经常损益后的净利润/净利润 | 扣除非经常损益后的净利润/净利润 | [查看](#factor-043) |
| 44 | `admin_expense_rate` | 管理费用与营业总收入之比 | 管理费用与营业总收入之比=管理费用（TTM）/营业总收入（TTM） | [查看](#factor-044) |
| 45 | `asset_turnover_ttm` | 经营资产周转率TTM | 营业收入TTM/近4个季度期末净经营性资产均值; 净经营性资产=经营资产-经营负债 | [查看](#factor-045) |
| 46 | `cash_rate_of_sales` | 经营活动产生的现金流量净额与营业收入之比 | 经营活动产生的现金流量净额（TTM） / 营业收入（TTM） | [查看](#factor-046) |
| 47 | `cash_to_current_liability` | 现金比率 | 期末现金及现金等价物余额/流动负债合计的12个月均值 | [查看](#factor-047) |
| 48 | `cfo_to_ev` | 经营活动产生的现金流量净额与企业价值之比TTM | 经营活动产生的现金流量净额TTM / 企业价值。其中，企业价值=司市值+负债合计-货币资金 | [查看](#factor-048) |
| 49 | `current_asset_turnover_rate` | 流动资产周转率TTM | 过去12个月的营业收入/过去12个月的平均流动资产合计 | [查看](#factor-049) |
| 50 | `current_ratio` | 流动比率(单季度) | 流动比率=流动资产合计/流动负债合计 | [查看](#factor-050) |
| 51 | `debt_to_asset_ratio` | 债务总资产比 | 债务总资产比=负债合计/总资产 | [查看](#factor-051) |
| 52 | `debt_to_equity_ratio` | 产权比率 | 产权比率=负债合计/归属母公司所有者权益合计 | [查看](#factor-052) |
| 53 | `debt_to_tangible_equity_ratio` | 有形净值债务率 | 负债合计/有形净值 其中有形净值=股东权益-无形资产净值，无形资产净值= 商誉+无形资产 | [查看](#factor-053) |
| 54 | `DEGM` | 毛利率增长 | 毛利率增长=(今年毛利率（TTM）/去年毛利率（TTM）)-1 | [查看](#factor-054) |
| 55 | `DEGM_8y` | 长期毛利率增长 | 过去8年(1+DEGM)的累成 ^ (1/8) - 1 | [查看](#factor-055) |
| 56 | `DSRI` | 应收账款指数 | 本期(年报)应收账款占营业收入比例/上期(年报)应收账款占营业收入比例 | [查看](#factor-056) |
| 57 | `equity_to_asset_ratio` | 股东权益比率 | 股东权益比率=股东权益/总资产 | [查看](#factor-057) |
| 58 | `equity_to_fixed_asset_ratio` | 股东权益与固定资产比率 | 股东权益与固定资产比率=股东权益/(固定资产+工程物资+在建工程) | [查看](#factor-058) |
| 59 | `equity_turnover_rate` | 股东权益周转率 | 股东权益周转率=营业收入(ttm)/股东权益 | [查看](#factor-059) |
| 60 | `financial_expense_rate` | 财务费用与营业总收入之比 | = 财务费用（TTM） / 营业总收入（TTM） | [查看](#factor-060) |
| 61 | `fixed_asset_ratio` | 固定资产比率 | 固定资产比率=(固定资产+工程物资+在建工程)/总资产 | [查看](#factor-061) |
| 62 | `fixed_assets_turnover_rate` | 固定资产周转率 | 等于过去12个月的营业收入/过去12个月的平均（固定资产+工程物资+在建工程） | [查看](#factor-062) |
| 63 | `GMI` | 毛利率指数 | 上期(年报)毛利率/本期(年报)毛利率 | [查看](#factor-063) |
| 64 | `goods_service_cash_to_operating_revenue_ttm` | 销售商品提供劳务收到的现金与营业收入之比 | 销售商品提供劳务收到的现金与营业收入之比=销售商品和提供劳务收到的现金（TTM）/营业收入（TTM） | [查看](#factor-064) |
| 65 | `gross_income_ratio` | 销售毛利率 | 销售毛利率=(营业收入（TTM）-营业成本（TTM）)/营业收入（TTM） | [查看](#factor-065) |
| 66 | `intangible_asset_ratio` | 无形资产比率 | 无形资产比率=(无形资产+研发支出+商誉)/总资产 | [查看](#factor-066) |
| 67 | `inventory_turnover_days` | 存货周转天数 | 存货周转天数=360/存货周转率 | [查看](#factor-067) |
| 68 | `inventory_turnover_rate` | 存货周转率 | 存货周转率=营业成本（TTM）/存货 | [查看](#factor-068) |
| 69 | `invest_income_associates_to_total_profit` | 对联营和合营公司投资收益/利润总额 | 对联营和营公司投资收益/利润总额 | [查看](#factor-069) |
| 70 | `long_debt_to_asset_ratio` | 长期借款与资产总计之比 | 长期借款与资产总计之比=长期借款/总资产 | [查看](#factor-070) |
| 71 | `long_debt_to_working_capital_ratio` | 长期负债与营运资金比率 | 长期负债与营运资金比率=非流动负债合计/(流动资产合计-流动负债合计) | [查看](#factor-071) |
| 72 | `long_term_debt_to_asset_ratio` | 长期负债与资产总计之比 | 长期负债与资产总计之比=非流动负债合计/总资产 | [查看](#factor-072) |
| 73 | `LVGI` | 财务杠杆指数 | 本期(年报)资产负债率/上期(年报)资产负债率 | [查看](#factor-073) |
| 74 | `margin_stability` | 盈利能力稳定性 | mean(GM)/std(GM); GM 为过去8年毛利率ttm | [查看](#factor-074) |
| 75 | `maximum_margin` | 最大盈利水平 | max(margin_stability, DEGM_8y) | [查看](#factor-075) |
| 76 | `MLEV` | 市场杠杆 | 市场杠杆=非流动负债合计/(非流动负债合计+总市值) | [查看](#factor-076) |
| 77 | `net_non_operating_income_to_total_profit` | 营业外收支利润净额/利润总额 | 营业外收支利润净额/利润总额 | [查看](#factor-077) |
| 78 | `net_operate_cash_flow_to_asset` | 总资产现金回收率 | 经营活动产生的现金流量净额(ttm) / 总资产 | [查看](#factor-078) |
| 79 | `net_operate_cash_flow_to_net_debt` | 经营活动产生现金流量净额/净债务 | 经营活动产生现金流量净额/净债务 | [查看](#factor-079) |
| 80 | `net_operate_cash_flow_to_operate_income` | 经营活动产生的现金流量净额与经营活动净收益之比 | 经营活动产生的现金流量净额（TTM）/(营业总收入（TTM）-营业总成本（TTM） | [查看](#factor-080) |
| 81 | `net_operate_cash_flow_to_total_current_liability` | 现金流动负债比 | 现金流动负债比=经营活动产生的现金流量净额（TTM）/流动负债合计 | [查看](#factor-081) |
| 82 | `net_operate_cash_flow_to_total_liability` | 经营活动产生的现金流量净额/负债合计 | 经营活动产生的现金流量净额/负债合计 | [查看](#factor-082) |
| 83 | `net_operating_cash_flow_coverage` | 净利润现金含量 | 经营活动产生的现金流量净额/归属于母公司所有者的净利润 | [查看](#factor-083) |
| 84 | `net_profit_ratio` | 销售净利率 | 售净利率=净利润（TTM）/营业收入（TTM） | [查看](#factor-084) |
| 85 | `net_profit_to_total_operate_revenue_ttm` | 净利润与营业总收入之比 | 净利润与营业总收入之比=净利润（TTM）/营业总收入（TTM） | [查看](#factor-085) |
| 86 | `non_current_asset_ratio` | 非流动资产比率 | 非流动资产比率=非流动资产合计/总资产 | [查看](#factor-086) |
| 87 | `operating_cost_to_operating_revenue_ratio` | 销售成本率 | 销售成本率=营业成本（TTM）/营业收入（TTM） | [查看](#factor-087) |
| 88 | `operating_profit_growth_rate` | 营业利润增长率 | 营业利润增长率=(今年营业利润（TTM）/去年营业利润（TTM）)-1 | [查看](#factor-088) |
| 89 | `operating_profit_ratio` | 营业利润率 | 营业利润率=营业利润（TTM）/营业收入（TTM） | [查看](#factor-089) |
| 90 | `operating_profit_to_operating_revenue` | 营业利润与营业总收入之比 | 营业利润与营业总收入之比=营业利润（TTM）/营业总收入（TTM） | [查看](#factor-090) |
| 91 | `operating_profit_to_total_profit` | 经营活动净收益/利润总额 | 经营活动净收益/利润总额 | [查看](#factor-091) |
| 92 | `operating_tax_to_operating_revenue_ratio_ttm` | 销售税金率 | 销售税金率=营业税金及附加（TTM）/营业收入（TTM） | [查看](#factor-092) |
| 93 | `OperatingCycle` | 营业周期 | 应收账款周转天数+存货周转天数 | [查看](#factor-093) |
| 94 | `profit_margin_ttm` | 销售利润率TTM | 营业利润/营业收入 | [查看](#factor-094) |
| 95 | `quick_ratio` | 速动比率 | 速动比率=(流动资产合计-存货)/ 流动负债合计 | [查看](#factor-095) |
| 96 | `rnoa_ttm` | 经营资产回报率TTM | 销售利润率*经营资产周转率 | [查看](#factor-096) |
| 97 | `roa_ttm` | 资产回报率TTM | 资产回报率=净利润（TTM）/期末总资产 | [查看](#factor-097) |
| 98 | `roa_ttm_8y` | 长期资产回报率TTM | 8年(1+roa_ttm)的乘积 ^ (1/8) - 1 # 至少要有近4年的数据，否则为 nan | [查看](#factor-098) |
| 99 | `ROAEBITTTM` | 总资产报酬率 | （利润总额（TTM）+利息支出（TTM）） / 总资产在过去12个月的平均 | [查看](#factor-099) |
| 100 | `roe_ttm` | 权益回报率TTM | 权益回报率=净利润（TTM）/期末股东权益 | [查看](#factor-100) |
| 101 | `roe_ttm_8y` | 长期权益回报率TTM | 8年(1+roe_ttm)的累乘 ^ (1/8) - 1 # 至少要有近4年的数据，否则为 nan | [查看](#factor-101) |
| 102 | `roic_ttm` | 投资资本回报率TTM | 权益回报率=归属于母公司股东的净利润（TTM）/ 前四个季度投资资本均值; 投资资本=股东权益+负债合计-无息流动负债-无息非流动负债; 无息流动负债=应付账款+预收款项+应… | [查看](#factor-102) |
| 103 | `sale_expense_to_operating_revenue` | 营业费用与营业总收入之比 | 营业费用与营业总收入之比=销售费用（TTM）/营业总收入（TTM） | [查看](#factor-103) |
| 104 | `SGAI` | 销售管理费用指数 | 本期(年报)销售管理费用占营业收入的比例/上期(年报)销售管理费用占营业收入的比例 | [查看](#factor-104) |
| 105 | `SGI` | 营业收入指数 | 本期(年报)营业收入/上期(年报)营业收入 | [查看](#factor-105) |
| 106 | `super_quick_ratio` | 超速动比率 | （货币资金+交易性金融资产+应收票据+应收帐款+其他应收款）／流动负债合计 | [查看](#factor-106) |
| 107 | `total_asset_turnover_rate` | 总资产周转率 | 总资产周转率=营业收入(ttm)/总资产 | [查看](#factor-107) |
| 108 | `total_profit_to_cost_ratio` | 成本费用利润率 | 成本费用利润率=利润总额/(营业成本+财务费用+销售费用+管理费用)，以上科目使用的都是TTM的数值 | [查看](#factor-108) |

### 每股指标因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 109 | `capital_reserve_fund_per_share` | 每股资本公积金 | 每股资本公积金 | [查看](#factor-109) |
| 110 | `cash_and_equivalents_per_share` | 每股现金及现金等价物余额 | 每股现金及现金等价物余额 | [查看](#factor-110) |
| 111 | `cashflow_per_share_ttm` | 每股现金流量净额，根据当时日期来获取最近变更日的总股本 | 现金流量净额（TTM）除以总股本 | [查看](#factor-111) |
| 112 | `eps_ttm` | 每股收益TTM | 过去12个月归属母公司所有者的净利润（TTM）除以总股本 | [查看](#factor-112) |
| 113 | `net_asset_per_share` | 每股净资产 | 归属母公司所有者权益合计除以总股本 | [查看](#factor-113) |
| 114 | `net_operate_cash_flow_per_share` | 每股经营活动产生的现金流量净额 | 每股经营活动产生的现金流量净额 | [查看](#factor-114) |
| 115 | `operating_profit_per_share` | 每股营业利润 | 每股营业利润 | [查看](#factor-115) |
| 116 | `operating_profit_per_share_ttm` | 每股营业利润TTM | 营业利润（TTM）除以总股本 | [查看](#factor-116) |
| 117 | `operating_revenue_per_share` | 每股营业收入 | 每股营业收入 | [查看](#factor-117) |
| 118 | `operating_revenue_per_share_ttm` | 每股营业收入TTM | 营业收入（TTM）除以总股本 | [查看](#factor-118) |
| 119 | `retained_earnings_per_share` | 每股留存收益 | 每股留存收益 | [查看](#factor-119) |
| 120 | `retained_profit_per_share` | 每股未分配利润 | 每股未分配利润 | [查看](#factor-120) |
| 121 | `surplus_reserve_fund_per_share` | 每股盈余公积金 | 每股盈余公积金 | [查看](#factor-121) |
| 122 | `total_operating_revenue_per_share` | 每股营业总收入 | 每股营业总收入 | [查看](#factor-122) |
| 123 | `total_operating_revenue_per_share_ttm` | 每股营业总收入TTM | 营业总收入（TTM）除以总股本 | [查看](#factor-123) |

### 风险因子 - 风格因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 124 | `average_share_turnover_annual` | 年度平均月换手率 | ln(sum(turn_over_ratio)/12)，turn_over_ratio为过去十二个月（252个交易日）的平均换手率 | [查看](#factor-124) |
| 125 | `average_share_turnover_quarterly` | 季度平均平均月换手率 | ln(sum(turn_over_ratio)/3)，turn_over_ratio为过去三个月（63个交易日）的平均换手率 | [查看](#factor-125) |
| 126 | `beta` | BETA | 一元线性回归求解 beta=sum(w_t*(r_t-r_mean)*(R_t-R_mean))/sum(w_t*(R_t-R_mean)^2)，其中r_t、R_t分别使用前… | [查看](#factor-126) |
| 127 | `book_leverage` | 账面杠杆 | (普通股账面价值 + 优先股账面价值 + 长期负债账面价值) / 普通股账面价值 | [查看](#factor-127) |
| 128 | `book_to_price_ratio` | 市净率因子 | 每股净资产与每股股价的比率 | [查看](#factor-128) |
| 129 | `cash_earnings_to_price_ratio` | 现金流量市值比 | 过去一年的净经营现金流 除以 当前股票市值 | [查看](#factor-129) |
| 130 | `cube_of_size` | 市值立方因子 | 标准化市值因子的三次方，之后将结果和标准化市值因子回归取残差（去除和市值因子的共线性），然后残差值进行缩尾处理（将3倍标准差之外的点处理成3倍标准差）和标准化 | [查看](#factor-130) |
| 131 | `cumulative_range` | 收益离差 | ln(1+Z_max)-ln(1+Z_min)，其中Z_t=cumsum(ln(1+r_t))，t=1,2,...,12，r_t为向前推t个月的月收益 | [查看](#factor-131) |
| 132 | `daily_standard_deviation` | 日收益率标准差 | sqrt(sum(w_t*(r_t - r_mean)**2))，其中r_t为过去252个交易日的日收益率，w_t为半衰期为42个交易日的指数权重，满足w(t-42)=0.5… | [查看](#factor-132) |
| 133 | `debt_to_assets` | 资产负债率 | 总负债账面价值 / 总资产账面价值 | [查看](#factor-133) |
| 134 | `earnings_growth` | 5年盈利增长率 | 过去5个财年 年均EPS增长 除以 年均EPS | [查看](#factor-134) |
| 135 | `earnings_to_price_ratio` | 利润市值比 | 过去一年的净利润 除以 当前股票市值，等于 PE 的倒数 | [查看](#factor-135) |
| 136 | `earnings_yield` | 盈利预期因子 | 0.68 * 预期市盈率 + 0.21 * 营业收益市值比 + 0.11 * 利润市值比 | [查看](#factor-136) |
| 137 | `growth` | 成长因子 | 0.18 * 预期长期盈利增长率 + 0.11 * 预期短期盈利增长率 + 0.24 * 5年盈利增长率 + 0.47 * 5年营业收入增长率 | [查看](#factor-137) |
| 138 | `historical_sigma` | 残差历史波动率 | 计算 beta 收益之时的残差收益率的波动率 | [查看](#factor-138) |
| 139 | `leverage` | 杠杆因子 | 0.38 * 市场杠杆 + 0.35 * 资产负债率 + 0.27 * 账面杠杆 | [查看](#factor-139) |
| 140 | `liquidity` | 流动性因子 | 0.35 * 月换手率 + 0.35 * 季度平均平均月换手率 + 0.3 * 年度平均月换手率，之后将结果和市值因子做回归，取残差（去除和市值因子的共线性） | [查看](#factor-140) |
| 141 | `long_term_predicted_earnings_growth` | 预期长期盈利增长率 | 分析师预测未来3-5年盈利增长率 | [查看](#factor-141) |
| 142 | `market_leverage` | 市场杠杆 | (普通股市值 + 优先股账面价值(中国股票为0) + 长期负债账面价值) / 普通股市值，长期负债账面价值=长期借款+应付债券 | [查看](#factor-142) |
| 143 | `momentum` | 动量因子 | 动量因子=1.0*相对强弱因子=sum(w_t * ln(1 + r_t))，其中r_t取滞后21个交易日的前504个交易日的close数据，w_t为半衰期为126天的指数权… | [查看](#factor-143) |
| 144 | `natural_log_of_market_cap` | 对数总市值 | 对数总市值=总市值的对数 | [查看](#factor-144) |
| 145 | `non_linear_size` | 非线性市值因子 | 1.0*市值立方因子，标准化市值因子的三次方，之后将结果和标准化市值因子回归取残差（去除和市值因子的共线性），然后残差值进行缩尾处理（将3倍标准差之外的点处理成3倍标准差）和标准化 | [查看](#factor-145) |
| 146 | `predicted_earnings_to_price_ratio` | 预期市盈率 | 分析师对未来一年预期盈利加权平均值 除以 当前股票市值 | [查看](#factor-146) |
| 147 | `raw_beta` | RAW BETA | 一元线性回归求解 beta=sum(w_t*(r_t-r_mean)*(R_t-R_mean))/sum(w_t*(R_t-R_mean)^2)，其中r_t、R_t分别使用前… | [查看](#factor-147) |
| 148 | `relative_strength` | 相对强弱 | sum(w_t * ln(1 + r_t))，其中r_t取滞后21个交易日的前504个交易日的close数据，w_t为半衰期为126天的指数权重，满足w(t-126)=0.5… | [查看](#factor-148) |
| 149 | `residual_volatility` | 残差波动因子 | 0.74 * 日收益率标准差(DASTD) + 0.16 * 收益离差(CMRA) + 0.1 * 残差历史波动率(HSIGMA)，之后将结果和beta因子，市值因子做回归，取残差 | [查看](#factor-149) |
| 150 | `sales_growth` | 5年营业收入增长率 | 过去5个财年的 每股营业收入增长 除以 年均每股营业收入 | [查看](#factor-150) |
| 151 | `share_turnover_monthly` | 月换手率 | ln(sum(turn_over_ratio))，turn_over_ratio为过去21个交易日的换手率 | [查看](#factor-151) |
| 152 | `short_term_predicted_earnings_growth` | 预期短期盈利增长率 | 分析师预测未来1年盈利增长率 | [查看](#factor-152) |
| 153 | `size` | 市值因子 | 资产规模 = 1.0 * 对数总资产 = 总资产的对数 | [查看](#factor-153) |

### 情绪类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 154 | `AR` | 人气指标 | AR=N日内（当日最高价—当日开市价）之和 / N日内（当日开市价—当日最低价）之和 * 100，n设定为26 | [查看](#factor-154) |
| 155 | `ARBR` | ARBR | 因子 AR 与因子 BR 的差 | [查看](#factor-155) |
| 156 | `ATR14` | 14日均幅指标 | 真实振幅的14日移动平均 | [查看](#factor-156) |
| 157 | `ATR6` | 6日均幅指标 | 真实振幅的6日移动平均 | [查看](#factor-157) |
| 158 | `BR` | 意愿指标 | BR=N日内（当日最高价－昨日收盘价）之和 / N日内（昨日收盘价－当日最低价）之和×100 n设定为26 | [查看](#factor-158) |
| 159 | `DAVOL10` | 10日平均换手率与120日平均换手率之比 | 10日平均换手率 / 120日平均换手率 | [查看](#factor-159) |
| 160 | `DAVOL20` | 20日平均换手率与120日平均换手率之比 | 20日平均换手率 / 120日平均换手率 | [查看](#factor-160) |
| 161 | `DAVOL5` | 5日平均换手率与120日平均换手率 | 5日平均换手率 / 120日平均换手率 | [查看](#factor-161) |
| 162 | `MAWVAD` | 因子WVAD的6日均值 | - | [查看](#factor-162) |
| 163 | `money_flow_20` | 20日资金流量 | 用收盘价、最高价及最低价的均值乘以当日成交量即可得到该交易日的资金流量 | [查看](#factor-163) |
| 164 | `PSY` | 心理线指标 | n日内连续上涨的天数/n *100。 本因子的计算窗口为12日。 | [查看](#factor-164) |
| 165 | `turnover_volatility` | 换手率相对波动率 | 取20个交易日个股换手率的标准差 | [查看](#factor-165) |
| 166 | `TVMA20` | 20日成交金额的移动平均值 | 20日成交金额的移动平均值 | [查看](#factor-166) |
| 167 | `TVMA6` | 6日成交金额的移动平均值 | 6日成交金额的移动平均值 | [查看](#factor-167) |
| 168 | `TVSTD20` | 20日成交金额的标准差 | 20日成交额的标准差 | [查看](#factor-168) |
| 169 | `TVSTD6` | 6日成交金额的标准差 | 6日成交额的标准差 | [查看](#factor-169) |
| 170 | `VDEA` | 计算VMACD因子的中间变量 | EMA(VDIFF，M) short设置为12，long设置为26，M设置为9 | [查看](#factor-170) |
| 171 | `VDIFF` | 计算VMACD因子的中间变量 | EMA(VOLUME，SHORT)-EMA(VOLUME，LONG) short设置为12，long设置为26，M设置为9 | [查看](#factor-171) |
| 172 | `VEMA10` | 成交量的10日指数移动平均 | - | [查看](#factor-172) |
| 173 | `VEMA12` | 12日成交量的移动平均值 | - | [查看](#factor-173) |
| 174 | `VEMA26` | 成交量的26日指数移动平均 | - | [查看](#factor-174) |
| 175 | `VEMA5` | 成交量的5日指数移动平均 | - | [查看](#factor-175) |
| 176 | `VMACD` | 成交量指数平滑异同移动平均线 | 快的指数移动平均线（EMA12）减去慢的指数移动平均线（EMA26）得到快线DIFF, 由DIFF的M日移动平均得到DEA，由DIFF-DEA的值得到MACD | [查看](#factor-176) |
| 177 | `VOL10` | 10日平均换手率 | 10日换手率的均值,单位为% | [查看](#factor-177) |
| 178 | `VOL120` | 120日平均换手率 | 120日换手率的均值,单位为% | [查看](#factor-178) |
| 179 | `VOL20` | 20日平均换手率 | 20日换手率的均值,单位为% | [查看](#factor-179) |
| 180 | `VOL240` | 240日平均换手率 | 240日换手率的均值,单位为% | [查看](#factor-180) |
| 181 | `VOL5` | 5日平均换手率 | 5日换手率的均值,单位为% | [查看](#factor-181) |
| 182 | `VOL60` | 60日平均换手率 | 60日换手率的均值,单位为% | [查看](#factor-182) |
| 183 | `VOSC` | 成交量震荡 | 'VEMA12'和'VEMA26'两者的差值，再求差值与'VEMA12'的比，最后将比值放大100倍，得到VOSC值 | [查看](#factor-183) |
| 184 | `VR` | 成交量比率（Volume Ratio） | VR=（AVS+1/2CVS）/（BVS+1/2CVS） | [查看](#factor-184) |
| 185 | `VROC12` | 12日量变动速率指标 | 成交量减N日前的成交量，再除以N日前的成交量，放大100倍，得到VROC值 ，n=12 | [查看](#factor-185) |
| 186 | `VROC6` | 6日量变动速率指标 | 成交量减N日前的成交量，再除以N日前的成交量，放大100倍，得到VROC值 ，n=6 | [查看](#factor-186) |
| 187 | `VSTD10` | 10日成交量标准差 | 10日成交量去标准差 | [查看](#factor-187) |
| 188 | `VSTD20` | 20日成交量标准差 | 20日成交量去标准差 | [查看](#factor-188) |
| 189 | `WVAD` | 威廉变异离散量 | (收盘价－开盘价)/(最高价－最低价)×成交量，再做加和，使用过去6个交易日的数据 | [查看](#factor-189) |

### 成长类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 190 | `financing_cash_growth_rate` | 筹资活动产生的现金流量净额增长率 | 过去12个月的筹资现金流量净额 / 4季度前的12个月的筹资现金流量净额 - 1 | [查看](#factor-190) |
| 191 | `net_asset_growth_rate` | 净资产增长率 | （当季的股东权益/三季度前的股东权益）-1 | [查看](#factor-191) |
| 192 | `net_operate_cashflow_growth_rate` | 经营活动产生的现金流量净额增长率 | =(今年经营活动产生的现金流量净额（TTM）/去年经营活动产生的现金流量净额（TTM）)-1 | [查看](#factor-192) |
| 193 | `net_profit_growth_rate` | 净利润增长率 | 净利润增长率=(今年净利润（TTM）/去年净利润（TTM）)-1 | [查看](#factor-193) |
| 194 | `np_parent_company_owners_growth_rate` | 归属母公司股东的净利润增长率 | (今年归属于母公司所有者的净利润（TTM）/去年归属于母公司所有者的净利润（TTM）)-1 | [查看](#factor-194) |
| 195 | `operating_revenue_growth_rate` | 营业收入增长率 | 营业收入增长率=（今年营业收入（TTM）/去年营业收入（TTM））-1 | [查看](#factor-195) |
| 196 | `PEG` | PEG | PEG = PE / (归母公司净利润(TTM)增长率 * 100) # 如果 PE 或 增长率为负，则为 nan | [查看](#factor-196) |
| 197 | `total_asset_growth_rate` | 总资产增长率 | 总资产 / 总资产_4 -1 | [查看](#factor-197) |
| 198 | `total_profit_growth_rate` | 利润总额增长率 | 利润总额增长率=(今年利润总额（TTM）/去年利润总额（TTM）)-1 | [查看](#factor-198) |

### 风险类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 199 | `Kurtosis120` | 个股收益的120日峰度 | 取121个交易日的收盘价数据，计算日收益率，再计算其峰度值 | [查看](#factor-199) |
| 200 | `Kurtosis20` | 个股收益的20日峰度 | 取21个交易日的收盘价数据，计算日收益率，再计算其峰度值 | [查看](#factor-200) |
| 201 | `Kurtosis60` | 个股收益的60日峰度 | 取61个交易日的收盘价数据，计算日收益率，再计算其峰度值 | [查看](#factor-201) |
| 202 | `sharpe_ratio_120` | 120日夏普比率 | （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差） | [查看](#factor-202) |
| 203 | `sharpe_ratio_20` | 20日夏普比率 | （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差） | [查看](#factor-203) |
| 204 | `sharpe_ratio_60` | 60日夏普比率 | （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差） | [查看](#factor-204) |
| 205 | `Skewness120` | 个股收益的120日偏度 | 取121个交易日的收盘价数据，计算日收益率，再计算其偏度 | [查看](#factor-205) |
| 206 | `Skewness20` | 个股收益的20日偏度 | 取21个交易日的收盘价数据，计算日收益率，再计算其偏度 | [查看](#factor-206) |
| 207 | `Skewness60` | 个股收益的60日偏度 | 取61个交易日的收盘价数据，计算日收益率，再计算其偏度 | [查看](#factor-207) |
| 208 | `Variance120` | 120日收益方差 | 取121个交易日的收盘价，算出日收益率，再取方差 | [查看](#factor-208) |
| 209 | `Variance20` | 20日收益方差 | 取21个交易日的收盘价，算出日收益率，再取方差 | [查看](#factor-209) |
| 210 | `Variance60` | 60日收益方差 | 取61个交易日的收盘价，算出日收益率，再取方差 | [查看](#factor-210) |

### 技术指标因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 211 | `boll_down` | 下轨线（布林线）指标 | (MA(CLOSE,M)-2*STD(CLOSE,M)) / 今日收盘价; M=20 | [查看](#factor-211) |
| 212 | `boll_up` | 上轨线（布林线）指标 | (MA(CLOSE,M)+2*STD(CLOSE,M)) / 今日收盘价; M=20 | [查看](#factor-212) |
| 213 | `EMA5` | 5日指数移动均线 | 5日指数移动均线 / 今日收盘价 | [查看](#factor-213) |
| 214 | `EMAC10` | 10日指数移动均线 | 10日指数移动均线 / 今日收盘价 | [查看](#factor-214) |
| 215 | `EMAC12` | 12日指数移动均线 | 12日指数移动均线 / 今日收盘价 | [查看](#factor-215) |
| 216 | `EMAC120` | 120日指数移动均线 | 120日指数移动均线 / 今日收盘价 | [查看](#factor-216) |
| 217 | `EMAC20` | 20日指数移动均线 | 20日指数移动均线 / 今日收盘价 | [查看](#factor-217) |
| 218 | `EMAC26` | 26日指数移动均线 | 26日指数移动均线 / 今日收盘价 | [查看](#factor-218) |
| 219 | `MAC10` | 10日移动均线 | 10日移动均线 / 今日收盘价 | [查看](#factor-219) |
| 220 | `MAC120` | 120日移动均线 | 120日移动均线 / 今日收盘价 | [查看](#factor-220) |
| 221 | `MAC20` | 20日移动均线 | 20日移动均线 / 今日收盘价 | [查看](#factor-221) |
| 222 | `MAC5` | 5日移动均线 | 5日移动均线 / 今日收盘价 | [查看](#factor-222) |
| 223 | `MAC60` | 60日移动均线 | 60日移动均线 / 今日收盘价 | [查看](#factor-223) |
| 224 | `MACDC` | 平滑异同移动平均线 | MACD(SHORT=12, LONG=26, MID=9) / 今日收盘价 | [查看](#factor-224) |
| 225 | `MFI14` | 资金流量指标 | ①求得典型价格（当日最高价，最低价和收盘价的均值）②根据典型价格高低判定正负向资金流（资金流=典型价格*成交量）③计算MR= 正向/负向 ④MFI=100-100/（1+MR） | [查看](#factor-225) |
| 226 | `price_no_fq` | 不复权价格因子 | 不复权价格 | [查看](#factor-226) |

### 动量类因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 227 | `arron_down_25` | Aroon指标下轨 | Aroon(下降)=[(计算期天数-最低价后的天数)/计算期天数]*100 | [查看](#factor-227) |
| 228 | `arron_up_25` | Aroon指标上轨 | Aroon(上升)=[(计算期天数-最高价后的天数)/计算期天数]*100 | [查看](#factor-228) |
| 229 | `BBIC` | BBI 动量 | BBI(3, 6, 12, 24) / 收盘价 （BBI 为常用技术指标类因子“多空均线”） | [查看](#factor-229) |
| 230 | `bear_power` | 空头力道 | (最低价-EMA(close,13)) / close | [查看](#factor-230) |
| 231 | `BIAS10` | 10日乖离率 | （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取10 | [查看](#factor-231) |
| 232 | `BIAS20` | 20日乖离率 | （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取20 | [查看](#factor-232) |
| 233 | `BIAS5` | 5日乖离率 | （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取5 | [查看](#factor-233) |
| 234 | `BIAS60` | 60日乖离率 | （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取60 | [查看](#factor-234) |
| 235 | `bull_power` | 多头力道 | (最高价-EMA(close,13)) / close | [查看](#factor-235) |
| 236 | `CCI10` | 10日顺势指标 | CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=10 | [查看](#factor-236) |
| 237 | `CCI15` | 15日顺势指标 | CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=15 | [查看](#factor-237) |
| 238 | `CCI20` | 20日顺势指标 | CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=20 | [查看](#factor-238) |
| 239 | `CCI88` | 88日顺势指标 | CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=88 | [查看](#factor-239) |
| 240 | `CR20` | CR指标 | ①中间价=1日前的最高价+最低价/2 ②上升值=今天的最高价-前一日的中间价（负值记0） ③下跌值=前一日的中间价-今天的最低价（负值记0） ④多方强度=20天的上升值的和，… | [查看](#factor-240) |
| 241 | `fifty_two_week_close_rank` | 当前价格处于过去1年股价的位置 | 取过去的250个交易日各股的收盘价时间序列，每只股票按照从大到小排列，并找出当日所在的位置 | [查看](#factor-241) |
| 242 | `MASS` | 梅斯线 | MASS(N1=9, N2=25, M=6) | [查看](#factor-242) |
| 243 | `PLRC12` | 12日收盘价格与日期线性回归系数 | 计算 12 日收盘价格，与日期序号（1-12）的线性回归系数，(close / mean(close)) = beta * t + alpha | [查看](#factor-243) |
| 244 | `PLRC24` | 24日收盘价格与日期线性回归系数 | 计算 24 日收盘价格，与日期序号（1-24）的线性回归系数， (close / mean(close)) = beta * t + alpha | [查看](#factor-244) |
| 245 | `PLRC6` | 6日收盘价格与日期线性回归系数 | 计算 6 日收盘价格，与日期序号（1-6）的线性回归系数，(close / mean(close)) = beta * t + alpha | [查看](#factor-245) |
| 246 | `Price1M` | 当前股价除以过去一个月股价均值再减1 | 当日收盘价 / mean(过去一个月(21天)的收盘价) -1 | [查看](#factor-246) |
| 247 | `Price1Y` | 当前股价除以过去一年股价均值再减1 | 当日收盘价 / mean(过去一年(250天)的收盘价) -1 | [查看](#factor-247) |
| 248 | `Price3M` | 当前股价除以过去三个月股价均值再减1 | 当日收盘价 / mean(过去三个月(61天)的收盘价) -1 | [查看](#factor-248) |
| 249 | `Rank1M` | 1减去 过去一个月收益率排名与股票总数的比值 | 1-(Rank(个股20日收益) / 股票总数) | [查看](#factor-249) |
| 250 | `ROC12` | 12日变动速率（Price Rate of Change） | ①AX=今天的收盘价—12天前的收盘价 ②BX=12天前的收盘价 ③ROC=AX/BX*100 | [查看](#factor-250) |
| 251 | `ROC120` | 120日变动速率（Price Rate of Change） | ①AX=今天的收盘价—20天前的收盘价 ②BX=60天前的收盘价 ③ROC=AX/BX*100 | [查看](#factor-251) |
| 252 | `ROC20` | 20日变动速率（Price Rate of Change） | ①AX=今天的收盘价—20天前的收盘价 ②BX=20天前的收盘价 ③ROC=AX/BX*100 | [查看](#factor-252) |
| 253 | `ROC6` | 6日变动速率（Price Rate of Change） | ①AX=今天的收盘价—6天前的收盘价 ②BX=6天前的收盘价 ③ROC=AX/BX*100 | [查看](#factor-253) |
| 254 | `ROC60` | 60日变动速率（Price Rate of Change） | ①AX=今天的收盘价—20天前的收盘价 ②BX=60天前的收盘价 ③ROC=AX/BX*100 | [查看](#factor-254) |
| 255 | `single_day_VPT` | 单日价量趋势 | （今日收盘价 - 昨日收盘价）/ 昨日收盘价 * 当日成交量 # (复权方法为基于当日前复权) | [查看](#factor-255) |
| 256 | `single_day_VPT_12` | 单日价量趋势12均值 | MA(single_day_VPT, 12) | [查看](#factor-256) |
| 257 | `single_day_VPT_6` | 单日价量趋势6日均值 | MA(single_day_VPT, 6) | [查看](#factor-257) |
| 258 | `TRIX10` | 10日终极指标TRIX | MTR=收盘价的10日指数移动平均的10日指数移动平均的10日指数移动平均; TRIX=(MTR-1日前的MTR)/1日前的MTR*100 | [查看](#factor-258) |
| 259 | `TRIX5` | 5日终极指标TRIX | MTR=收盘价的5日指数移动平均的10日指数移动平均的5日指数移动平均; TRIX=(MTR-1日前的MTR)/1日前的MTR*100 | [查看](#factor-259) |
| 260 | `Volume1M` | 当前交易量相比过去1个月日均交易量 与过去过去20日日均收益率乘积 | 当日交易量 / 过去20日交易量MEAN * 过去20日收益率MEAN | [查看](#factor-260) |

### 风险因子 - 新风格因子

| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |
|---:|---|---|---|---|
| 261 | `btop` | 市净率因子 | 1.00 * book_to_price | [查看](#factor-261) |
| 262 | `dividend_yield_v2` | 分红收益率因子 | - | [查看](#factor-262) |
| 263 | `divyild` | 分红因子 | 0.50 * dividend_to_price + 0.50 * analyst_predicted_dividend_to_price | [查看](#factor-263) |
| 264 | `earnqlty` | 盈利质量因子 | 0.60 * accruals_balance_sheet_version + 0.40 * accruals_cashflow_statement_version | [查看](#factor-264) |
| 265 | `earnvar` | 盈利变动率因子 | 0.20 * variability_in_cashflows + 0.25 * variability_in_earnings + 0.20 * variability_i… | [查看](#factor-265) |
| 266 | `earnyild` | 收益因子 | 0.10 * cash_earnings_to_price + 0.20 * enterprise_multiple + 0.50 * analyst_predicted_e… | [查看](#factor-266) |
| 267 | `financial_leverage` | 财务杠杆因子 | 0.40 * debt_to_asset + 0.30 * book_lev + 0.30 * market_lev | [查看](#factor-267) |
| 268 | `growth_v2` | 成长因子 | - | [查看](#factor-268) |
| 269 | `invsqlty` | 投资能力因子 | 0.20 * capital_expenditure_growth + 0.40 * total_assets_growth_rate + 0.40 * issuance_g… | [查看](#factor-269) |
| 270 | `liquidity_v2` | 流动性因子 | - | [查看](#factor-270) |
| 271 | `liquidty` | 流动性因子 | 0.25 * monthly_share_turnover + 0.25 * quarterly_share_turnover + 0.25 * annual_share_t… | [查看](#factor-271) |
| 272 | `long_growth` | 长期成长因子 | 0.20 * sales_per_share_growth_rate + 0.70 * long_term_pred_earnings_growth + 0.10 * ear… | [查看](#factor-272) |
| 273 | `ltrevrsl` | 长期反转因子 | 0.46 * long_term_historical_alpha + 0.54 * long_term_relative_strength | [查看](#factor-273) |
| 274 | `market_beta` | 市场波动率因子 | 1.00 * historical_beta | [查看](#factor-274) |
| 275 | `market_size` | 市值规模因子 | 1.00 * log_of_market_capitalization | [查看](#factor-275) |
| 276 | `midcap` | 中等市值因子 | 1.00 * cube_of_size_exposure | [查看](#factor-276) |
| 277 | `momentum_v2` | 动量因子 | - | [查看](#factor-277) |
| 278 | `profit` | 盈利能力因子 | 0.25 * asset_turnover + 0.25 * return_on_assets + 0.25 * gross_profitability_margin + 0… | [查看](#factor-278) |
| 279 | `quality_v2` | 质量因子 | - | [查看](#factor-279) |
| 280 | `relative_momentum` | 相对动量因子 | 0.50 * historical_alpha + 0.50 * relative_strength_12_month | [查看](#factor-280) |
| 281 | `resvol` | 残余波动率因子 | 0.50 * daily_std + 0.42 * historical_resid_sigma + 0.08 * cum_range | [查看](#factor-281) |
| 282 | `sentiment_v2` | 情绪因子 | - | [查看](#factor-282) |
| 283 | `size_v2` | 规模因子 | - | [查看](#factor-283) |
| 284 | `value_v2` | 价值因子 | - | [查看](#factor-284) |
| 285 | `volatility_v2` | 波动率因子 | - | [查看](#factor-285) |

## 因子详情

### 基础科目及衍生类因子

<a id="factor-001"></a>
#### 1. `administration_expense_ttm` — 管理费用TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`8e83bab52085545284998b6fb014aa9c`
- 更新时间：2021-02-23 12:31:40
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.015034；IR=0.134454；多空年化=-0.014867；多空夏普=-0.488593；多空最大回撤=0.165164
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8e83bab52085545284998b6fb014aa9c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 管理费用 之和

<a id="factor-002"></a>
#### 2. `asset_impairment_loss_ttm` — 资产减值损失TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`e176481e80107592d3df57e739d15520`
- 更新时间：2021-02-23 12:31:38
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006330；IR=0.096616；多空年化=0.065529；多空夏普=0.346688；多空最大回撤=0.067223
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e176481e80107592d3df57e739d15520)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 资产减值损失 之和

<a id="factor-003"></a>
#### 3. `cash_flow_to_price_ratio` — 现金流市值比

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`0d02f4f8f36b6ae1ec3d7ffd7dd826b4`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004988；IR=0.104725；多空年化=0.113894；多空夏普=1.191100；多空最大回撤=0.046340
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0d02f4f8f36b6ae1ec3d7ffd7dd826b4)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1 / pcf_ratio (ttm)

<a id="factor-004"></a>
#### 4. `circulating_market_cap` — 流通市值

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`5c0f619d831807285ac5d0b62b8d71a5`
- 更新时间：2021-02-23 12:43:47
- 产出时间：15:00
- 数据处理：行业中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.001652；IR=0.011541；多空年化=-0.022650；多空夏普=-0.354550；多空最大回撤=0.328594
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5c0f619d831807285ac5d0b62b8d71a5)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 流通市值

<a id="factor-005"></a>
#### 5. `EBIT` — 息税前利润

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`df371f38a8b151601cb3dd72e016dd35`
- 更新时间：2021-02-23 13:19:51
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.026844；IR=0.183683；多空年化=0.122798；多空夏普=0.486117；多空最大回撤=0.203762
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/df371f38a8b151601cb3dd72e016dd35)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> =净利润+所得税+财务费用

<a id="factor-006"></a>
#### 6. `EBITDA` — 息税折旧摊销前利润

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`e4fcfc322942bbcf5ac531db37b138dd`
- 更新时间：2021-02-23 12:57:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.022584；IR=0.172204；多空年化=0.118174；多空夏普=0.496237；多空最大回撤=0.177260
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e4fcfc322942bbcf5ac531db37b138dd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> =营业收入－经营成本－营业税及附加

<a id="factor-007"></a>
#### 7. `financial_assets` — 金融资产

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`b3ecd500434786282097492aaf6ea4f8`
- 更新时间：2021-02-23 13:02:30
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.015389；IR=0.149510；多空年化=0.025783；多空夏普=-0.116220；多空最大回撤=0.193308
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b3ecd500434786282097492aaf6ea4f8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 货币资金 + 交易性金融资产 + 应收票据 + 应收利息 + 应收股利 + 可供出售金融资产 + 持有至到期投资

<a id="factor-008"></a>
#### 8. `financial_expense_ttm` — 财务费用TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`88d7c035effdf8d77050a13cad90ab0d`
- 更新时间：2021-02-23 13:34:43
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006955；IR=0.079712；多空年化=0.019879；多空夏普=-0.242509；多空最大回撤=0.137781
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/88d7c035effdf8d77050a13cad90ab0d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 财务费用 之和

<a id="factor-009"></a>
#### 9. `financial_liability` — 金融负债

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`59720bb49979b598e55ca8a2c1fcae30`
- 更新时间：2021-02-23 13:30:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.012850；IR=0.116172；多空年化=0.040501；多空夏普=0.004515；多空最大回撤=0.099082
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/59720bb49979b598e55ca8a2c1fcae30)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (流动负债合计-无息流动负债)+(有息非流动负债)=(流动负债合计-应付账款-预收款项-应付职工薪酬-应交税费-其他应付款-一年内的递延收益-其它流动负债)+(长期借款+应付债券)

<a id="factor-010"></a>
#### 10. `goods_sale_and_service_render_cash_ttm` — 销售商品提供劳务收到的现金

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`50aef35a081ea1717e0484112a408a69`
- 更新时间：2021-02-23 13:43:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.017733；IR=0.143074；多空年化=0.035952；多空夏普=-0.030924；多空最大回撤=0.152569
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/50aef35a081ea1717e0484112a408a69)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 销售商品提供劳务收到的现金 之和

<a id="factor-011"></a>
#### 11. `gross_profit_ttm` — 毛利TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`c63be18ab0820008a5e2197ac1442bf8`
- 更新时间：2021-02-23 12:31:41
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.017356；IR=0.140108；多空年化=0.046434；多空夏普=0.041254；多空最大回撤=0.148231
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c63be18ab0820008a5e2197ac1442bf8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月毛利之和

<a id="factor-012"></a>
#### 12. `interest_carry_current_liability` — 带息流动负债

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`33f641f153e0ccec29e85035f315d11e`
- 更新时间：2021-02-23 13:05:09
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.011858；IR=0.115831；多空年化=0.044857；多空夏普=0.047438；多空最大回撤=0.103708
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/33f641f153e0ccec29e85035f315d11e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 流动负债合计 - 无息流动负债

<a id="factor-013"></a>
#### 13. `interest_free_current_liability` — 无息流动负债

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`8d0c6b22637e516eff1c468673598add`
- 更新时间：2021-02-23 13:41:05
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.013002；IR=0.119780；多空年化=0.021146；多空夏普=-0.166776；多空最大回撤=0.123728
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8d0c6b22637e516eff1c468673598add)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 应付票据+应付账款+预收账款(用 预售款项 代替)+应交税费+应付利息+其他应付款+其他流动负债

<a id="factor-014"></a>
#### 14. `market_cap` — 市值

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`448af30960428a81e3f0ab2dfff0e019`
- 更新时间：2021-02-23 12:38:22
- 产出时间：15:00
- 数据处理：行业中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000784；IR=-0.005262；多空年化=0.014964；多空夏普=-0.135457；多空最大回撤=0.303291
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/448af30960428a81e3f0ab2dfff0e019)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 市值

<a id="factor-015"></a>
#### 15. `net_debt` — 净债务

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`b727684c13f5c810d22173d4dd2153b2`
- 更新时间：2021-02-23 12:48:56
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.013212；IR=0.115762；多空年化=0.005824；多空夏普=-0.329988；多空最大回撤=0.102451
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b727684c13f5c810d22173d4dd2153b2)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总债务-期末现金及现金等价物余额

<a id="factor-016"></a>
#### 16. `net_finance_cash_flow_ttm` — 筹资活动现金流量净额TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`d4695ad5aa874c4530069e96fa58e80f`
- 更新时间：2021-02-23 13:52:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.010830；IR=-0.124555；多空年化=0.004900；多空夏普=-0.317190；多空最大回撤=0.186521
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/d4695ad5aa874c4530069e96fa58e80f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 筹资活动现金流量净额 之和

<a id="factor-017"></a>
#### 17. `net_interest_expense` — 净利息费用

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`b41f855e586304d92cb06b75cc7c0951`
- 更新时间：2021-02-23 13:31:15
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.030471；IR=-0.105725；多空年化=-0.107643；多空夏普=-0.824427；多空最大回撤=0.347833
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b41f855e586304d92cb06b75cc7c0951)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 利息支出-利息收入

<a id="factor-018"></a>
#### 18. `net_invest_cash_flow_ttm` — 投资活动现金流量净额TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`3d720c086deb03452b200c9e072b8785`
- 更新时间：2021-02-23 12:31:26
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006130；IR=-0.084630；多空年化=-0.037716；多空夏普=-0.900355；多空最大回撤=0.166674
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3d720c086deb03452b200c9e072b8785)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 投资活动现金流量净额 之和

<a id="factor-019"></a>
#### 19. `net_operate_cash_flow_ttm` — 经营活动现金流量净额TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`dad5e596b8692987a015681c9e862cbe`
- 更新时间：2021-02-23 13:49:43
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.020416；IR=0.161437；多空年化=0.050793；多空夏普=0.069472；多空最大回撤=0.221720
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/dad5e596b8692987a015681c9e862cbe)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 经营活动产生的现金流量净值 之和

<a id="factor-020"></a>
#### 20. `net_profit_ttm` — 净利润TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`55cf3e061e83c60582d81443fe8e8453`
- 更新时间：2021-02-23 12:42:46
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.021830；IR=0.150070；多空年化=0.083151；多空夏普=0.241872；多空最大回撤=0.230634
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/55cf3e061e83c60582d81443fe8e8453)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 净利润 之和

<a id="factor-021"></a>
#### 21. `net_working_capital` — 净运营资本

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`837f133f287f970c38fcefd7795d1448`
- 更新时间：2021-02-23 13:04:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006117；IR=0.115746；多空年化=0.002090；多空夏普=-0.519247；多空最大回撤=0.166130
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/837f133f287f970c38fcefd7795d1448)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 流动资产 － 流动负债

<a id="factor-022"></a>
#### 22. `non_operating_net_profit_ttm` — 营业外收支净额TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`0516423257525b9da629f59258b31e9d`
- 更新时间：2021-02-23 13:55:41
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000195；IR=0.004322；多空年化=0.006925；多空夏普=-0.476767；多空最大回撤=0.081366
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0516423257525b9da629f59258b31e9d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业外收入（TTM） - 营业外支出（TTM）

<a id="factor-023"></a>
#### 23. `non_recurring_gain_loss` — 非经常性损益

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`2e74c388f7093213f42b32f923d69398`
- 更新时间：2021-02-23 13:05:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008985；IR=0.151169；多空年化=0.025335；多空夏普=-0.159511；多空最大回撤=0.180953
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2e74c388f7093213f42b32f923d69398)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 净利润-扣除非经常损益后的净利润(元)

<a id="factor-024"></a>
#### 24. `np_parent_company_owners_ttm` — 归属于母公司股东的净利润TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`a06390397fe25a72ac2288ed85b4b590`
- 更新时间：2021-02-23 13:49:49
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.022330；IR=0.150190；多空年化=0.103605；多空夏普=0.364495；多空最大回撤=0.206960
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a06390397fe25a72ac2288ed85b4b590)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 归属于母公司股东的净利润 之和

<a id="factor-025"></a>
#### 25. `OperateNetIncome` — 经营活动净收益

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`1e4e03c0db4a6535b1b95f5796f82e37`
- 更新时间：2021-02-23 12:23:05
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.021809；IR=0.163590；多空年化=0.132819；多空夏普=0.608987；多空最大回撤=0.204321
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1e4e03c0db4a6535b1b95f5796f82e37)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动净收益/利润总额(%) * 利润总额

<a id="factor-026"></a>
#### 26. `operating_assets` — 经营性资产

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`9d8a2593b5a147301d00b80ff92f67a0`
- 更新时间：2021-02-23 13:13:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.019858；IR=0.139148；多空年化=0.048255；多空夏普=0.062811；多空最大回撤=0.123753
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9d8a2593b5a147301d00b80ff92f67a0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总资产 - 金融资产

<a id="factor-027"></a>
#### 27. `operating_cost_ttm` — 营业成本TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`9198bb23d07015e1f8d27ef2f7fa5a85`
- 更新时间：2021-02-23 13:16:38
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.016153；IR=0.136378；多空年化=0.026933；多空夏普=-0.110208；多空最大回撤=0.149388
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9198bb23d07015e1f8d27ef2f7fa5a85)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月的 营业成本 之和

<a id="factor-028"></a>
#### 28. `operating_liability` — 经营性负债

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`3a88d0ba1382d324cae917ab71d88b60`
- 更新时间：2021-02-23 13:40:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.015352；IR=0.119320；多空年化=-0.012116；多空夏普=-0.404242；多空最大回撤=0.206206
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3a88d0ba1382d324cae917ab71d88b60)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总负债 - 金融负债

<a id="factor-029"></a>
#### 29. `operating_profit_ttm` — 营业利润TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`20e9594679be8a46f9c5e22bcc836503`
- 更新时间：2021-02-23 13:23:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.021835；IR=0.148771；多空年化=0.088512；多空夏普=0.265769；多空最大回撤=0.223247
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/20e9594679be8a46f9c5e22bcc836503)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 营业利润 之和

<a id="factor-030"></a>
#### 30. `operating_revenue_ttm` — 营业收入TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`a0a00a8dbf9e1bdbafde35c91c0da04a`
- 更新时间：2021-02-23 14:06:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.018227；IR=0.142900；多空年化=0.028716；多空夏普=-0.084202；多空最大回撤=0.171295
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a0a00a8dbf9e1bdbafde35c91c0da04a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月的 营业收入 之和

<a id="factor-031"></a>
#### 31. `retained_earnings` — 留存收益

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`861d74129a76a7898680dea8b62252f6`
- 更新时间：2021-02-23 13:28:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.024476；IR=0.162405；多空年化=0.067519；多空夏普=0.167079；多空最大回撤=0.229878
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/861d74129a76a7898680dea8b62252f6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> =盈余公积金+未分配利润

<a id="factor-032"></a>
#### 32. `sale_expense_ttm` — 销售费用TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`e0f2bfe88bd758b5a1ffa223016beb24`
- 更新时间：2021-02-23 14:11:23
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005432；IR=0.053339；多空年化=0.003766；多空夏普=-0.364746；多空最大回撤=0.095271
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e0f2bfe88bd758b5a1ffa223016beb24)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 销售费用 之和

<a id="factor-033"></a>
#### 33. `sales_to_price_ratio` — 营收市值比

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`7d0cb9f9b805742587bf9f698defcd58`
- 更新时间：2021-02-23 12:59:15
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.017559；IR=0.141383；多空年化=0.006091；多空夏普=-0.258750；多空最大回撤=0.241706
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7d0cb9f9b805742587bf9f698defcd58)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1 / ps_ratio (ttm)

<a id="factor-034"></a>
#### 34. `total_operating_cost_ttm` — 营业总成本TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`18d934a87c90f38b9899db660ffe8c00`
- 更新时间：2021-02-23 12:52:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.016483；IR=0.135348；多空年化=0.031588；多空夏普=-0.066319；多空最大回撤=0.186494
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/18d934a87c90f38b9899db660ffe8c00)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月的 营业总成本 之和

<a id="factor-035"></a>
#### 35. `total_operating_revenue_ttm` — 营业总收入TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`94614fcc2bc3ad7dce769ef34e437f82`
- 更新时间：2021-02-23 13:33:55
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.017574；IR=0.138760；多空年化=0.022954；多空夏普=-0.126449；多空最大回撤=0.164887
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/94614fcc2bc3ad7dce769ef34e437f82)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月的 营业总收入 之和

<a id="factor-036"></a>
#### 36. `total_profit_ttm` — 利润总额TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`2f6d633311b45cfbdfdd2d12dda87639`
- 更新时间：2021-02-23 13:51:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.021946；IR=0.149521；多空年化=0.086127；多空夏普=0.254406；多空最大回撤=0.227482
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2f6d633311b45cfbdfdd2d12dda87639)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 利润总额 之和

<a id="factor-037"></a>
#### 37. `value_change_profit_ttm` — 价值变动净收益TTM

- 聚宽分类：基础科目及衍生类因子
- 快照 factor_id：`76d39aeee94e67d81ab3884f7cc9c957`
- 更新时间：2021-02-23 12:43:54
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.015991；IR=0.175751；多空年化=0.024668；多空夏普=-0.161707；多空最大回撤=0.179732
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/76d39aeee94e67d81ab3884f7cc9c957)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算过去12个月 价值变动净收益 之和

### 质量类因子

<a id="factor-038"></a>
#### 38. `ACCA` — 现金流资产比和资产回报率之差

- 聚宽分类：质量类因子
- 快照 factor_id：`c77289869e2650e3beac0441da6705fa`
- 更新时间：2021-02-23 13:41:35
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.002940；IR=0.052331；多空年化=-0.033445；多空夏普=-0.878232；多空最大回撤=0.208407
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c77289869e2650e3beac0441da6705fa)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 现金流资产比-资产回报率,其中现金流资产比=经营活动产生的现金流量净额/总资产

<a id="factor-039"></a>
#### 39. `account_receivable_turnover_days` — 应收账款周转天数

- 聚宽分类：质量类因子
- 快照 factor_id：`fd7dace2f56d3280545a684f7cc7ff6a`
- 更新时间：2021-02-23 13:26:58
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.008170；IR=-0.076848；多空年化=-0.040232；多空夏普=-0.620662；多空最大回撤=0.206295
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/fd7dace2f56d3280545a684f7cc7ff6a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 应收账款周转天数=360/应收账款周转率

<a id="factor-040"></a>
#### 40. `account_receivable_turnover_rate` — 应收账款周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`003625182a092022ad9410199ef74abf`
- 更新时间：2021-02-23 14:11:55
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007261；IR=0.063984；多空年化=0.003545；多空夏普=-0.276052；多空最大回撤=0.177033
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/003625182a092022ad9410199ef74abf)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 即，TTM(营业收入,0)/（AvgQ(应收账款,4,0) + AvgQ(应收票据,4,0) + AvgQ(预收账款,4,0) ）

<a id="factor-041"></a>
#### 41. `accounts_payable_turnover_days` — 应付账款周转天数

- 聚宽分类：质量类因子
- 快照 factor_id：`45df332c94215180b407b7e467241bc2`
- 更新时间：2021-02-23 12:31:33
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.010068；IR=-0.119481；多空年化=-0.029467；多空夏普=-0.596071；多空最大回撤=0.163944
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/45df332c94215180b407b7e467241bc2)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 应付账款周转天数 = 360 / 应付账款周转率

<a id="factor-042"></a>
#### 42. `accounts_payable_turnover_rate` — 应付账款周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`80f29713b7d2a8fa0baccdfbc3d1ab80`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009443；IR=0.110296；多空年化=-0.017064；多空夏普=-0.471995；多空最大回撤=0.277232
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/80f29713b7d2a8fa0baccdfbc3d1ab80)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> TTM(营业成本,0)/（AvgQ(应付账款,4,0) + AvgQ(应付票据,4,0) + AvgQ(预付款项,4,0) ）

<a id="factor-043"></a>
#### 43. `adjusted_profit_to_total_profit` — 扣除非经常损益后的净利润/净利润

- 聚宽分类：质量类因子
- 快照 factor_id：`703573effca6c9db99921ecab802c344`
- 更新时间：2021-02-23 13:26:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003444；IR=-0.057938；多空年化=0.053836；多空夏普=0.167658；多空最大回撤=0.086085
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/703573effca6c9db99921ecab802c344)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 扣除非经常损益后的净利润/净利润

<a id="factor-044"></a>
#### 44. `admin_expense_rate` — 管理费用与营业总收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`6d8a465e4c02733fa8ad908dab68f108`
- 更新时间：2021-02-23 13:37:07
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.005400；IR=-0.079536；多空年化=-0.047023；多空夏普=-1.053001；多空最大回撤=0.172555
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/6d8a465e4c02733fa8ad908dab68f108)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 管理费用与营业总收入之比=管理费用（TTM）/营业总收入（TTM）

<a id="factor-045"></a>
#### 45. `asset_turnover_ttm` — 经营资产周转率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`107da11f7040d314a178db46aa080a5b`
- 更新时间：2021-02-23 13:42:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000785；IR=-0.012864；多空年化=-0.058327；多空夏普=-1.071219；多空最大回撤=0.327307
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/107da11f7040d314a178db46aa080a5b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业收入TTM/近4个季度期末净经营性资产均值; 净经营性资产=经营资产-经营负债

<a id="factor-046"></a>
#### 46. `cash_rate_of_sales` — 经营活动产生的现金流量净额与营业收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`4c46c76902318b3274788316c1c2134e`
- 更新时间：2021-02-23 13:48:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008616；IR=0.126231；多空年化=0.021766；多空夏普=-0.180329；多空最大回撤=0.235043
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4c46c76902318b3274788316c1c2134e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额（TTM） / 营业收入（TTM）

<a id="factor-047"></a>
#### 47. `cash_to_current_liability` — 现金比率

- 聚宽分类：质量类因子
- 快照 factor_id：`dbd6db0bc73ef6591f7593b568193bdd`
- 更新时间：2021-02-23 12:24:30
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003888；IR=-0.060824；多空年化=-0.033965；多空夏普=-0.864835；多空最大回撤=0.245559
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/dbd6db0bc73ef6591f7593b568193bdd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 期末现金及现金等价物余额/流动负债合计的12个月均值

<a id="factor-048"></a>
#### 48. `cfo_to_ev` — 经营活动产生的现金流量净额与企业价值之比TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`acc4d2121e6e6ce4815e859fc53ee055`
- 更新时间：2021-02-23 12:24:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.019551；IR=0.180754；多空年化=0.025418；多空夏普=-0.105849；多空最大回撤=0.278280
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/acc4d2121e6e6ce4815e859fc53ee055)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额TTM / 企业价值。其中，企业价值=司市值+负债合计-货币资金

<a id="factor-049"></a>
#### 49. `current_asset_turnover_rate` — 流动资产周转率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`a212b3719d69f2e9deb11a8d64f09bdc`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009361；IR=0.098759；多空年化=0.004353；多空夏普=-0.278352；多空最大回撤=0.217528
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a212b3719d69f2e9deb11a8d64f09bdc)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去12个月的营业收入/过去12个月的平均流动资产合计

<a id="factor-050"></a>
#### 50. `current_ratio` — 流动比率(单季度)

- 聚宽分类：质量类因子
- 快照 factor_id：`8b34108e9960df44ed58ed191b663291`
- 更新时间：2021-02-23 13:33:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.007864；IR=-0.090431；多空年化=-0.037643；多空夏普=-0.798470；多空最大回撤=0.195030
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8b34108e9960df44ed58ed191b663291)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 流动比率=流动资产合计/流动负债合计

<a id="factor-051"></a>
#### 51. `debt_to_asset_ratio` — 债务总资产比

- 聚宽分类：质量类因子
- 快照 factor_id：`ad8b72b68eb5bed79f78ee88a54c237a`
- 更新时间：2021-02-23 13:16:44
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003981；IR=0.054028；多空年化=-0.004768；多空夏普=-0.524349；多空最大回撤=0.112588
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ad8b72b68eb5bed79f78ee88a54c237a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 债务总资产比=负债合计/总资产

<a id="factor-052"></a>
#### 52. `debt_to_equity_ratio` — 产权比率

- 聚宽分类：质量类因子
- 快照 factor_id：`10fb4ed420668fb365219c3fce937f87`
- 更新时间：2021-02-23 13:57:04
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004311；IR=0.058707；多空年化=-0.002461；多空夏普=-0.524769；多空最大回撤=0.122856
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/10fb4ed420668fb365219c3fce937f87)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 产权比率=负债合计/归属母公司所有者权益合计

<a id="factor-053"></a>
#### 53. `debt_to_tangible_equity_ratio` — 有形净值债务率

- 聚宽分类：质量类因子
- 快照 factor_id：`249525de377db305da2234145ef59296`
- 更新时间：2021-02-23 14:02:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004895；IR=0.067077；多空年化=0.006248；多空夏普=-0.419060；多空最大回撤=0.115346
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/249525de377db305da2234145ef59296)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 负债合计/有形净值 其中有形净值=股东权益-无形资产净值，无形资产净值= 商誉+无形资产

<a id="factor-054"></a>
#### 54. `DEGM` — 毛利率增长

- 聚宽分类：质量类因子
- 快照 factor_id：`46411ccbe2abb5929850cde4af1070f6`
- 更新时间：2021-02-23 12:52:34
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005347；IR=0.088912；多空年化=0.037541；多空夏普=-0.029762；多空最大回撤=0.087411
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/46411ccbe2abb5929850cde4af1070f6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 毛利率增长=(今年毛利率（TTM）/去年毛利率（TTM）)-1

<a id="factor-055"></a>
#### 55. `DEGM_8y` — 长期毛利率增长

- 聚宽分类：质量类因子
- 快照 factor_id：`1ca1a827f8fc03c7d271ade54cf02604`
- 更新时间：2021-02-23 12:30:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.002046；IR=0.036000；多空年化=0.024516；多空夏普=-0.186027；多空最大回撤=0.090666
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1ca1a827f8fc03c7d271ade54cf02604)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去8年(1+DEGM)的累成 ^ (1/8) - 1

<a id="factor-056"></a>
#### 56. `DSRI` — 应收账款指数

- 聚宽分类：质量类因子
- 快照 factor_id：`c20a74a8c894fe42119ffd951874a99c`
- 更新时间：2021-02-23 12:42:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003377；IR=-0.056603；多空年化=-0.050073；多空夏普=-1.169749；多空最大回撤=0.161422
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c20a74a8c894fe42119ffd951874a99c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 本期(年报)应收账款占营业收入比例/上期(年报)应收账款占营业收入比例

<a id="factor-057"></a>
#### 57. `equity_to_asset_ratio` — 股东权益比率

- 聚宽分类：质量类因子
- 快照 factor_id：`ec70024d5649572d2d0d92e92e5c7ea6`
- 更新时间：2021-02-23 13:37:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003981；IR=-0.054028；多空年化=-0.005319；多空夏普=-0.530360；多空最大回撤=0.194252
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ec70024d5649572d2d0d92e92e5c7ea6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 股东权益比率=股东权益/总资产

<a id="factor-058"></a>
#### 58. `equity_to_fixed_asset_ratio` — 股东权益与固定资产比率

- 聚宽分类：质量类因子
- 快照 factor_id：`4347ceef5f6e2cfc254172df606884cf`
- 更新时间：2021-02-23 13:42:38
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000253；IR=-0.003475；多空年化=-0.090536；多空夏普=-1.192832；多空最大回撤=0.356748
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4347ceef5f6e2cfc254172df606884cf)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 股东权益与固定资产比率=股东权益/(固定资产+工程物资+在建工程)

<a id="factor-059"></a>
#### 59. `equity_turnover_rate` — 股东权益周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`66bd99d14607e41024d68a0589caa55a`
- 更新时间：2021-02-23 13:43:58
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.002312；IR=-0.033477；多空年化=0.017377；多空夏普=-0.261284；多空最大回撤=0.096382
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/66bd99d14607e41024d68a0589caa55a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 股东权益周转率=营业收入(ttm)/股东权益

<a id="factor-060"></a>
#### 60. `financial_expense_rate` — 财务费用与营业总收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`25eee4fbcccf516761875b15dca402e1`
- 更新时间：2021-02-23 13:19:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003769；IR=0.053239；多空年化=0.003681；多空夏普=-0.398769；多空最大回撤=0.170531
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/25eee4fbcccf516761875b15dca402e1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> = 财务费用（TTM） / 营业总收入（TTM）

<a id="factor-061"></a>
#### 61. `fixed_asset_ratio` — 固定资产比率

- 聚宽分类：质量类因子
- 快照 factor_id：`e87e41fcd5f7417fa562b8e4b60e5725`
- 更新时间：2021-02-23 13:56:04
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.002690；IR=0.036342；多空年化=0.036505；多空夏普=-0.037006；多空最大回撤=0.131829
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e87e41fcd5f7417fa562b8e4b60e5725)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 固定资产比率=(固定资产+工程物资+在建工程)/总资产

<a id="factor-062"></a>
#### 62. `fixed_assets_turnover_rate` — 固定资产周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`1c06eda229b1a3b12545fa77acea4440`
- 更新时间：2021-02-23 13:02:18
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000161；IR=0.002483；多空年化=-0.085790；多空夏普=-1.277085；多空最大回撤=0.320229
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1c06eda229b1a3b12545fa77acea4440)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 等于过去12个月的营业收入/过去12个月的平均（固定资产+工程物资+在建工程）

<a id="factor-063"></a>
#### 63. `GMI` — 毛利率指数

- 聚宽分类：质量类因子
- 快照 factor_id：`83291cad46fb6d35c20e10429ce85005`
- 更新时间：2021-02-23 13:27:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.001358；IR=-0.024220；多空年化=-0.070393；多空夏普=-1.451141；多空最大回撤=0.231629
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/83291cad46fb6d35c20e10429ce85005)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 上期(年报)毛利率/本期(年报)毛利率

<a id="factor-064"></a>
#### 64. `goods_service_cash_to_operating_revenue_ttm` — 销售商品提供劳务收到的现金与营业收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`a760cba4bbb09caf8d94654dc3a9d82d`
- 更新时间：2021-02-23 13:09:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004025；IR=0.057499；多空年化=-0.023449；多空夏普=-0.574097；多空最大回撤=0.293204
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a760cba4bbb09caf8d94654dc3a9d82d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 销售商品提供劳务收到的现金与营业收入之比=销售商品和提供劳务收到的现金（TTM）/营业收入（TTM）

<a id="factor-065"></a>
#### 65. `gross_income_ratio` — 销售毛利率

- 聚宽分类：质量类因子
- 快照 factor_id：`4d4543b3dbfe4e97a34cdbaeab180ab4`
- 更新时间：2021-02-23 13:37:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006606；IR=-0.074209；多空年化=0.012505；多空夏普=-0.295142；多空最大回撤=0.109415
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4d4543b3dbfe4e97a34cdbaeab180ab4)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 销售毛利率=(营业收入（TTM）-营业成本（TTM）)/营业收入（TTM）

<a id="factor-066"></a>
#### 66. `intangible_asset_ratio` — 无形资产比率

- 聚宽分类：质量类因子
- 快照 factor_id：`df97e92ae593a1459d180395000eb660`
- 更新时间：2021-02-23 13:11:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003272；IR=0.061664；多空年化=0.014006；多空夏普=-0.371162；多空最大回撤=0.116005
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/df97e92ae593a1459d180395000eb660)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 无形资产比率=(无形资产+研发支出+商誉)/总资产

<a id="factor-067"></a>
#### 67. `inventory_turnover_days` — 存货周转天数

- 聚宽分类：质量类因子
- 快照 factor_id：`be778c69f20cefb5f714684cdb6a030f`
- 更新时间：2021-02-23 13:52:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006784；IR=-0.075842；多空年化=0.044320；多空夏普=0.038636；多空最大回撤=0.135107
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/be778c69f20cefb5f714684cdb6a030f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 存货周转天数=360/存货周转率

<a id="factor-068"></a>
#### 68. `inventory_turnover_rate` — 存货周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`18aa69c4fd0672d18863099a16a3abfd`
- 更新时间：2021-02-23 12:59:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007477；IR=0.067818；多空年化=-0.049072；多空夏普=-0.799977；多空最大回撤=0.278453
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/18aa69c4fd0672d18863099a16a3abfd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 存货周转率=营业成本（TTM）/存货

<a id="factor-069"></a>
#### 69. `invest_income_associates_to_total_profit` — 对联营和合营公司投资收益/利润总额

- 聚宽分类：质量类因子
- 快照 factor_id：`910903623d1c475df819101207085e30`
- 更新时间：2021-02-23 12:59:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006235；IR=0.114228；多空年化=-0.028584；多空夏普=-0.943379；多空最大回撤=0.207064
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/910903623d1c475df819101207085e30)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 对联营和营公司投资收益/利润总额

<a id="factor-070"></a>
#### 70. `long_debt_to_asset_ratio` — 长期借款与资产总计之比

- 聚宽分类：质量类因子
- 快照 factor_id：`f8509db690bbe8e0a45e64781ec776f5`
- 更新时间：2021-02-23 12:55:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006028；IR=0.090863；多空年化=0.056640；多空夏普=0.201014；多空最大回撤=0.098920
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f8509db690bbe8e0a45e64781ec776f5)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 长期借款与资产总计之比=长期借款/总资产

<a id="factor-071"></a>
#### 71. `long_debt_to_working_capital_ratio` — 长期负债与营运资金比率

- 聚宽分类：质量类因子
- 快照 factor_id：`50203c1a9be88f47a59f471a41464c42`
- 更新时间：2021-02-23 12:50:03
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003398；IR=0.061689；多空年化=0.059131；多空夏普=0.260026；多空最大回撤=0.078440
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/50203c1a9be88f47a59f471a41464c42)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 长期负债与营运资金比率=非流动负债合计/(流动资产合计-流动负债合计)

<a id="factor-072"></a>
#### 72. `long_term_debt_to_asset_ratio` — 长期负债与资产总计之比

- 聚宽分类：质量类因子
- 快照 factor_id：`a1503a25c2469b2af626f95730a95c37`
- 更新时间：2021-02-23 13:13:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003384；IR=0.050618；多空年化=0.052162；多空夏普=0.140052；多空最大回撤=0.113870
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a1503a25c2469b2af626f95730a95c37)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 长期负债与资产总计之比=非流动负债合计/总资产

<a id="factor-073"></a>
#### 73. `LVGI` — 财务杠杆指数

- 聚宽分类：质量类因子
- 快照 factor_id：`5c1c479e8f82a83f108982ed753ca675`
- 更新时间：2021-02-23 14:12:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.002582；IR=-0.051681；多空年化=-0.023513；多空夏普=-0.817987；多空最大回撤=0.125077
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5c1c479e8f82a83f108982ed753ca675)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 本期(年报)资产负债率/上期(年报)资产负债率

<a id="factor-074"></a>
#### 74. `margin_stability` — 盈利能力稳定性

- 聚宽分类：质量类因子
- 快照 factor_id：`9b43f287c4d8c25647ae9b67288c9c4a`
- 更新时间：2021-02-23 13:55:26
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000359；IR=-0.005303；多空年化=0.020404；多空夏普=-0.237047；多空最大回撤=0.113592
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9b43f287c4d8c25647ae9b67288c9c4a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> mean(GM)/std(GM); GM 为过去8年毛利率ttm

<a id="factor-075"></a>
#### 75. `maximum_margin` — 最大盈利水平

- 聚宽分类：质量类因子
- 快照 factor_id：`1c4f7fd7cd5bcc3e85adf71aa3acd2c3`
- 更新时间：2021-02-23 14:04:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000337；IR=0.005904；多空年化=0.017021；多空夏普=-0.295052；多空最大回撤=0.077998
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1c4f7fd7cd5bcc3e85adf71aa3acd2c3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> max(margin_stability, DEGM_8y)

<a id="factor-076"></a>
#### 76. `MLEV` — 市场杠杆

- 聚宽分类：质量类因子
- 快照 factor_id：`29d767b990c1276f7938b6a29c13c3d2`
- 更新时间：2021-02-23 12:24:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.010448；IR=0.104103；多空年化=0.003935；多空夏普=-0.390466；多空最大回撤=0.146178
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/29d767b990c1276f7938b6a29c13c3d2)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 市场杠杆=非流动负债合计/(非流动负债合计+总市值)

<a id="factor-077"></a>
#### 77. `net_non_operating_income_to_total_profit` — 营业外收支利润净额/利润总额

- 聚宽分类：质量类因子
- 快照 factor_id：`4684b94909f91b9ef23c990a448f170a`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：过滤值为0的因子 -> 中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006532；IR=0.130249；多空年化=0.107896；多空夏普=0.832818；多空最大回撤=0.094617
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4684b94909f91b9ef23c990a448f170a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业外收支利润净额/利润总额

<a id="factor-078"></a>
#### 78. `net_operate_cash_flow_to_asset` — 总资产现金回收率

- 聚宽分类：质量类因子
- 快照 factor_id：`c3c1502adf884fe791388714be56810b`
- 更新时间：2021-02-23 12:56:45
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009204；IR=0.108796；多空年化=0.019319；多空夏普=-0.177550；多空最大回撤=0.251335
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c3c1502adf884fe791388714be56810b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额(ttm) / 总资产

<a id="factor-079"></a>
#### 79. `net_operate_cash_flow_to_net_debt` — 经营活动产生现金流量净额/净债务

- 聚宽分类：质量类因子
- 快照 factor_id：`7b41133d085b57d718949ed3b7bd90b9`
- 更新时间：2021-02-23 12:58:05
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005847；IR=0.107295；多空年化=0.017394；多空夏普=-0.283337；多空最大回撤=0.134043
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7b41133d085b57d718949ed3b7bd90b9)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生现金流量净额/净债务

<a id="factor-080"></a>
#### 80. `net_operate_cash_flow_to_operate_income` — 经营活动产生的现金流量净额与经营活动净收益之比

- 聚宽分类：质量类因子
- 快照 factor_id：`3410d8a52adc831f63015aa43512c8c3`
- 更新时间：2021-02-23 12:24:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007497；IR=0.142224；多空年化=-0.001926；多空夏普=-0.489117；多空最大回撤=0.202612
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3410d8a52adc831f63015aa43512c8c3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额（TTM）/(营业总收入（TTM）-营业总成本（TTM）

<a id="factor-081"></a>
#### 81. `net_operate_cash_flow_to_total_current_liability` — 现金流动负债比

- 聚宽分类：质量类因子
- 快照 factor_id：`19ed0f82760bab7e14a168ca2a2e0963`
- 更新时间：2021-02-23 13:24:12
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008084；IR=0.102454；多空年化=0.046853；多空夏普=0.063045；多空最大回撤=0.192909
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/19ed0f82760bab7e14a168ca2a2e0963)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 现金流动负债比=经营活动产生的现金流量净额（TTM）/流动负债合计

<a id="factor-082"></a>
#### 82. `net_operate_cash_flow_to_total_liability` — 经营活动产生的现金流量净额/负债合计

- 聚宽分类：质量类因子
- 快照 factor_id：`3754eba2cf547259030ad9bc7d9e9d7a`
- 更新时间：2021-02-23 12:49:56
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008239；IR=0.117904；多空年化=0.034183；多空夏普=-0.062370；多空最大回撤=0.126756
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3754eba2cf547259030ad9bc7d9e9d7a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额/负债合计

<a id="factor-083"></a>
#### 83. `net_operating_cash_flow_coverage` — 净利润现金含量

- 聚宽分类：质量类因子
- 快照 factor_id：`2039b9828cf5665f5e92cb8e4fbee9cc`
- 更新时间：2021-02-23 12:44:06
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003730；IR=0.072594；多空年化=0.009548；多空夏普=-0.419097；多空最大回撤=0.122972
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2039b9828cf5665f5e92cb8e4fbee9cc)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动产生的现金流量净额/归属于母公司所有者的净利润

<a id="factor-084"></a>
#### 84. `net_profit_ratio` — 销售净利率

- 聚宽分类：质量类因子
- 快照 factor_id：`203c34d29310111ad02f6caa53ca4800`
- 更新时间：2021-02-23 13:11:11
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005873；IR=0.068413；多空年化=0.077152；多空夏普=0.398157；多空最大回撤=0.079589
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/203c34d29310111ad02f6caa53ca4800)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 售净利率=净利润（TTM）/营业收入（TTM）

<a id="factor-085"></a>
#### 85. `net_profit_to_total_operate_revenue_ttm` — 净利润与营业总收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`e5a11726afd71fade8af9804a8d73324`
- 更新时间：2021-02-23 13:44:35
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006646；IR=0.076095；多空年化=0.081882；多空夏普=0.444454；多空最大回撤=0.075547
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e5a11726afd71fade8af9804a8d73324)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 净利润与营业总收入之比=净利润（TTM）/营业总收入（TTM）

<a id="factor-086"></a>
#### 86. `non_current_asset_ratio` — 非流动资产比率

- 聚宽分类：质量类因子
- 快照 factor_id：`111a2f265e9a1563b25a61d70a1f8600`
- 更新时间：2021-02-23 13:37:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007439；IR=0.086939；多空年化=0.013949；多空夏普=-0.279613；多空最大回撤=0.112708
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/111a2f265e9a1563b25a61d70a1f8600)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 非流动资产比率=非流动资产合计/总资产

<a id="factor-087"></a>
#### 87. `operating_cost_to_operating_revenue_ratio` — 销售成本率

- 聚宽分类：质量类因子
- 快照 factor_id：`ee264b79af9b7ca327ec2fbc5ae08484`
- 更新时间：2021-02-23 12:24:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003984；IR=0.047762；多空年化=-0.014155；多空夏普=-0.579257；多空最大回撤=0.139794
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ee264b79af9b7ca327ec2fbc5ae08484)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 销售成本率=营业成本（TTM）/营业收入（TTM）

<a id="factor-088"></a>
#### 88. `operating_profit_growth_rate` — 营业利润增长率

- 聚宽分类：质量类因子
- 快照 factor_id：`8aaf3d0c8edf1dd807e4616d880cd0f9`
- 更新时间：2021-02-23 14:09:53
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007409；IR=0.110266；多空年化=0.023030；多空夏普=-0.198421；多空最大回撤=0.122799
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8aaf3d0c8edf1dd807e4616d880cd0f9)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业利润增长率=(今年营业利润（TTM）/去年营业利润（TTM）)-1

<a id="factor-089"></a>
#### 89. `operating_profit_ratio` — 营业利润率

- 聚宽分类：质量类因子
- 快照 factor_id：`7d64246395c5f60dc2caca8d4ad6f1fc`
- 更新时间：2021-02-23 12:24:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005995；IR=0.071059；多空年化=0.074791；多空夏普=0.366682；多空最大回撤=0.091093
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7d64246395c5f60dc2caca8d4ad6f1fc)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业利润率=营业利润（TTM）/营业收入（TTM）

<a id="factor-090"></a>
#### 90. `operating_profit_to_operating_revenue` — 营业利润与营业总收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`e92a64fd2b3138e0375f38cd9a3f4ab0`
- 更新时间：2021-02-23 12:50:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006581；IR=0.077024；多空年化=0.074514；多空夏普=0.361108；多空最大回撤=0.084370
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e92a64fd2b3138e0375f38cd9a3f4ab0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业利润与营业总收入之比=营业利润（TTM）/营业总收入（TTM）

<a id="factor-091"></a>
#### 91. `operating_profit_to_total_profit` — 经营活动净收益/利润总额

- 聚宽分类：质量类因子
- 快照 factor_id：`9b5a7d270bebf80e504100106852a6b5`
- 更新时间：2021-02-23 13:47:53
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000209；IR=-0.004300；多空年化=0.029375；多空夏普=-0.168848；多空最大回撤=0.110095
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9b5a7d270bebf80e504100106852a6b5)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 经营活动净收益/利润总额

<a id="factor-092"></a>
#### 92. `operating_tax_to_operating_revenue_ratio_ttm` — 销售税金率

- 聚宽分类：质量类因子
- 快照 factor_id：`25bdc190dbe065716770415e82e78cb7`
- 更新时间：2021-02-23 13:58:23
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000418；IR=0.008245；多空年化=-0.027881；多空夏普=-0.933761；多空最大回撤=0.187840
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/25bdc190dbe065716770415e82e78cb7)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 销售税金率=营业税金及附加（TTM）/营业收入（TTM）

<a id="factor-093"></a>
#### 93. `OperatingCycle` — 营业周期

- 聚宽分类：质量类因子
- 快照 factor_id：`4dba0c86c166025df985ec400cd8fa17`
- 更新时间：2021-02-23 12:30:43
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.010686；IR=-0.096577；多空年化=0.024711；多空夏普=-0.108135；多空最大回撤=0.223646
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4dba0c86c166025df985ec400cd8fa17)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 应收账款周转天数+存货周转天数

<a id="factor-094"></a>
#### 94. `profit_margin_ttm` — 销售利润率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`46b2d0951e2774a7dd65f261b5f84e7e`
- 更新时间：2021-02-23 13:26:25
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006002；IR=0.071249；多空年化=0.076639；多空夏普=0.386048；多空最大回撤=0.091093
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/46b2d0951e2774a7dd65f261b5f84e7e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业利润/营业收入

<a id="factor-095"></a>
#### 95. `quick_ratio` — 速动比率

- 聚宽分类：质量类因子
- 快照 factor_id：`2b87e9fafdaacd2b8d07dca799ab3f12`
- 更新时间：2021-02-23 13:12:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.008390；IR=-0.103717；多空年化=-0.052633；多空夏普=-1.037314；多空最大回撤=0.245806
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2b87e9fafdaacd2b8d07dca799ab3f12)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 速动比率=(流动资产合计-存货)/ 流动负债合计

<a id="factor-096"></a>
#### 96. `rnoa_ttm` — 经营资产回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`49cf35585c0847a7f3d22cda34991248`
- 更新时间：2021-02-23 12:24:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.002684；IR=0.029294；多空年化=-0.025713；多空夏普=-0.680060；多空最大回撤=0.219050
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/49cf35585c0847a7f3d22cda34991248)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 销售利润率*经营资产周转率

<a id="factor-097"></a>
#### 97. `roa_ttm` — 资产回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`604bcb6f6bc8eda53677fa5b3a1b0999`
- 更新时间：2021-02-23 13:06:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005263；IR=0.048933；多空年化=0.060241；多空夏普=0.176410；多空最大回撤=0.130056
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/604bcb6f6bc8eda53677fa5b3a1b0999)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 资产回报率=净利润（TTM）/期末总资产

<a id="factor-098"></a>
#### 98. `roa_ttm_8y` — 长期资产回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`6309e3a5351241a735bb9a00e718778b`
- 更新时间：2021-02-23 12:58:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000146；IR=-0.001485；多空年化=0.040513；多空夏普=0.004710；多空最大回撤=0.145420
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/6309e3a5351241a735bb9a00e718778b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 8年(1+roa_ttm)的乘积 ^ (1/8) - 1 # 至少要有近4年的数据，否则为 nan

<a id="factor-099"></a>
#### 99. `ROAEBITTTM` — 总资产报酬率

- 聚宽分类：质量类因子
- 快照 factor_id：`0b067d1949fac2e307b701465df9c887`
- 更新时间：2021-02-23 12:45:33
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005556；IR=0.051717；多空年化=0.071448；多空夏普=0.265650；多空最大回撤=0.133480
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0b067d1949fac2e307b701465df9c887)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （利润总额（TTM）+利息支出（TTM）） / 总资产在过去12个月的平均

<a id="factor-100"></a>
#### 100. `roe_ttm` — 权益回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`045528964683a045f9fb557460ecd9ee`
- 更新时间：2021-02-23 14:10:16
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005928；IR=0.051782；多空年化=0.090609；多空夏普=0.424441；多空最大回撤=0.171947
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/045528964683a045f9fb557460ecd9ee)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 权益回报率=净利润（TTM）/期末股东权益

<a id="factor-101"></a>
#### 101. `roe_ttm_8y` — 长期权益回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`05f3a7be18901f174fa572d2d0def3f8`
- 更新时间：2021-02-23 13:44:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000607；IR=0.005801；多空年化=0.051091；多空夏普=0.101971；多空最大回撤=0.168158
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/05f3a7be18901f174fa572d2d0def3f8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 8年(1+roe_ttm)的累乘 ^ (1/8) - 1 # 至少要有近4年的数据，否则为 nan

<a id="factor-102"></a>
#### 102. `roic_ttm` — 投资资本回报率TTM

- 聚宽分类：质量类因子
- 快照 factor_id：`73f14e1b153e0bb304488d3097ec45e1`
- 更新时间：2021-02-23 12:44:19
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004141；IR=0.036730；多空年化=0.069413；多空夏普=0.248667；多空最大回撤=0.137360
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/73f14e1b153e0bb304488d3097ec45e1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 权益回报率=归属于母公司股东的净利润（TTM）/ 前四个季度投资资本均值; 投资资本=股东权益+负债合计-无息流动负债-无息非流动负债; 无息流动负债=应付账款+预收款项+应付职工薪酬+应交税费+其他应付款+一年内的递延收益+其它流动负债; 无息非流动负债=非流动负债合计-长期借款-应付债券；

<a id="factor-103"></a>
#### 103. `sale_expense_to_operating_revenue` — 营业费用与营业总收入之比

- 聚宽分类：质量类因子
- 快照 factor_id：`23790dc364b90a2e4a5340c65128cb0a`
- 更新时间：2021-02-23 12:38:25
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.007781；IR=-0.095208；多空年化=-0.046761；多空夏普=-0.991165；多空最大回撤=0.255430
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/23790dc364b90a2e4a5340c65128cb0a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业费用与营业总收入之比=销售费用（TTM）/营业总收入（TTM）

<a id="factor-104"></a>
#### 104. `SGAI` — 销售管理费用指数

- 聚宽分类：质量类因子
- 快照 factor_id：`0a23a5dcc4f2ec77630a8a830079ed68`
- 更新时间：2021-02-23 13:56:50
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.001325；IR=-0.023902；多空年化=0.013619；多空夏普=-0.326320；多空最大回撤=0.114006
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0a23a5dcc4f2ec77630a8a830079ed68)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 本期(年报)销售管理费用占营业收入的比例/上期(年报)销售管理费用占营业收入的比例

<a id="factor-105"></a>
#### 105. `SGI` — 营业收入指数

- 聚宽分类：质量类因子
- 快照 factor_id：`ab310b1f24f4e45929339c6ba30dd1fa`
- 更新时间：2021-02-23 12:24:26
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.002115；IR=-0.027503；多空年化=0.045150；多空夏普=0.054000；多空最大回撤=0.122476
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ab310b1f24f4e45929339c6ba30dd1fa)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 本期(年报)营业收入/上期(年报)营业收入

<a id="factor-106"></a>
#### 106. `super_quick_ratio` — 超速动比率

- 聚宽分类：质量类因子
- 快照 factor_id：`7df4975387a6b4aa58bf4b3b685855dd`
- 更新时间：2021-02-23 13:49:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.007144；IR=-0.088013；多空年化=-0.012330；多空夏普=-0.572813；多空最大回撤=0.195536
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7df4975387a6b4aa58bf4b3b685855dd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （货币资金+交易性金融资产+应收票据+应收帐款+其他应收款）／流动负债合计

<a id="factor-107"></a>
#### 107. `total_asset_turnover_rate` — 总资产周转率

- 聚宽分类：质量类因子
- 快照 factor_id：`c7ec152da43955a47ce75e8e3d9c1372`
- 更新时间：2021-02-23 13:09:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004750；IR=0.064652；多空年化=-0.021067；多空夏普=-0.672672；多空最大回撤=0.181592
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c7ec152da43955a47ce75e8e3d9c1372)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总资产周转率=营业收入(ttm)/总资产

<a id="factor-108"></a>
#### 108. `total_profit_to_cost_ratio` — 成本费用利润率

- 聚宽分类：质量类因子
- 快照 factor_id：`380f5f7eaa19040232555de56677c0ea`
- 更新时间：2021-02-23 13:30:34
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003576；IR=0.039581；多空年化=0.068940；多空夏普=0.295746；多空最大回撤=0.081625
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/380f5f7eaa19040232555de56677c0ea)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 成本费用利润率=利润总额/(营业成本+财务费用+销售费用+管理费用)，以上科目使用的都是TTM的数值

### 每股指标因子

<a id="factor-109"></a>
#### 109. `capital_reserve_fund_per_share` — 每股资本公积金

- 聚宽分类：每股指标因子
- 快照 factor_id：`5e4174a2739e493b4b5cc58a6c0764dc`
- 更新时间：2021-02-23 13:13:53
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.009650；IR=-0.108909；多空年化=0.096442；多空夏普=0.523507；多空最大回撤=0.171965
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5e4174a2739e493b4b5cc58a6c0764dc)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股资本公积金

<a id="factor-110"></a>
#### 110. `cash_and_equivalents_per_share` — 每股现金及现金等价物余额

- 聚宽分类：每股指标因子
- 快照 factor_id：`fb7aca213bd07d7155022664b54c7f1a`
- 更新时间：2021-02-23 13:44:52
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.002138；IR=-0.032683；多空年化=0.047694；多空夏普=0.091773；多空最大回撤=0.085997
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/fb7aca213bd07d7155022664b54c7f1a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股现金及现金等价物余额

<a id="factor-111"></a>
#### 111. `cashflow_per_share_ttm` — 每股现金流量净额，根据当时日期来获取最近变更日的总股本

- 聚宽分类：每股指标因子
- 快照 factor_id：`460941d7d2be5308c9b36d1650ca6541`
- 更新时间：2021-02-23 12:50:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004386；IR=0.085763；多空年化=0.095222；多空夏普=0.647505；多空最大回撤=0.082874
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/460941d7d2be5308c9b36d1650ca6541)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 现金流量净额（TTM）除以总股本

<a id="factor-112"></a>
#### 112. `eps_ttm` — 每股收益TTM

- 聚宽分类：每股指标因子
- 快照 factor_id：`d91792b579d31178cbd3b16cc3bf5991`
- 更新时间：2021-02-23 12:52:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005295；IR=0.046609；多空年化=0.097458；多空夏普=0.488817；多空最大回撤=0.175656
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/d91792b579d31178cbd3b16cc3bf5991)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去12个月归属母公司所有者的净利润（TTM）除以总股本

<a id="factor-113"></a>
#### 113. `net_asset_per_share` — 每股净资产

- 聚宽分类：每股指标因子
- 快照 factor_id：`6f93cca00e42c6bd88c8372068ec1684`
- 更新时间：2021-02-23 12:51:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000924；IR=-0.010905；多空年化=0.073576；多空夏普=0.354991；多空最大回撤=0.155070
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/6f93cca00e42c6bd88c8372068ec1684)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 归属母公司所有者权益合计除以总股本

<a id="factor-114"></a>
#### 114. `net_operate_cash_flow_per_share` — 每股经营活动产生的现金流量净额

- 聚宽分类：每股指标因子
- 快照 factor_id：`4c2f553c2946603170605d3313116a76`
- 更新时间：2021-02-23 12:31:35
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007410；IR=0.099733；多空年化=0.031333；多空夏普=-0.093641；多空最大回撤=0.141457
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4c2f553c2946603170605d3313116a76)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股经营活动产生的现金流量净额

<a id="factor-115"></a>
#### 115. `operating_profit_per_share` — 每股营业利润

- 聚宽分类：每股指标因子
- 快照 factor_id：`f8d658d47253fd1f7274379e16ba28bd`
- 更新时间：2021-02-23 14:02:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.011132；IR=0.098419；多空年化=0.168730；多空夏普=1.048687；多空最大回撤=0.121940
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f8d658d47253fd1f7274379e16ba28bd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股营业利润

<a id="factor-116"></a>
#### 116. `operating_profit_per_share_ttm` — 每股营业利润TTM

- 聚宽分类：每股指标因子
- 快照 factor_id：`524505248d077c629352c214eea6a127`
- 更新时间：2021-02-23 13:48:16
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005267；IR=0.046386；多空年化=0.092130；多空夏普=0.435803；多空最大回撤=0.174531
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/524505248d077c629352c214eea6a127)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业利润（TTM）除以总股本

<a id="factor-117"></a>
#### 117. `operating_revenue_per_share` — 每股营业收入

- 聚宽分类：每股指标因子
- 快照 factor_id：`9f9b9c61540aec84707c6123d6c5ec6b`
- 更新时间：2021-02-23 14:09:12
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003929；IR=0.048897；多空年化=0.093932；多空夏普=0.613870；多空最大回撤=0.091409
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9f9b9c61540aec84707c6123d6c5ec6b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股营业收入

<a id="factor-118"></a>
#### 118. `operating_revenue_per_share_ttm` — 每股营业收入TTM

- 聚宽分类：每股指标因子
- 快照 factor_id：`4d6e5031750064765489a81566f1ceae`
- 更新时间：2021-02-23 13:55:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.001821；IR=0.023564；多空年化=0.053875；多空夏普=0.163333；多空最大回撤=0.098206
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4d6e5031750064765489a81566f1ceae)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业收入（TTM）除以总股本

<a id="factor-119"></a>
#### 119. `retained_earnings_per_share` — 每股留存收益

- 聚宽分类：每股指标因子
- 快照 factor_id：`516c7c64bead5f19fb0af5867fa6134c`
- 更新时间：2021-02-23 12:37:08
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005201；IR=0.053234；多空年化=0.035225；多空夏普=-0.044873；多空最大回撤=0.149671
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/516c7c64bead5f19fb0af5867fa6134c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股留存收益

<a id="factor-120"></a>
#### 120. `retained_profit_per_share` — 每股未分配利润

- 聚宽分类：每股指标因子
- 快照 factor_id：`c8a52b8e0f1c914c67d55e82478368d2`
- 更新时间：2021-02-23 12:36:46
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004786；IR=0.050521；多空年化=0.060505；多空夏普=0.201192；多空最大回撤=0.164627
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c8a52b8e0f1c914c67d55e82478368d2)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股未分配利润

<a id="factor-121"></a>
#### 121. `surplus_reserve_fund_per_share` — 每股盈余公积金

- 聚宽分类：每股指标因子
- 快照 factor_id：`7c5d2be88d4ec3af0985a1994047f447`
- 更新时间：2021-02-23 12:58:09
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.010692；IR=0.113421；多空年化=0.086487；多空夏普=0.435989；多空最大回撤=0.134248
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7c5d2be88d4ec3af0985a1994047f447)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股盈余公积金

<a id="factor-122"></a>
#### 122. `total_operating_revenue_per_share` — 每股营业总收入

- 聚宽分类：每股指标因子
- 快照 factor_id：`50d521a3c96b350275a7b14ab5043b95`
- 更新时间：2021-02-23 14:09:54
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003605；IR=0.045105；多空年化=0.096586；多空夏普=0.646458；多空最大回撤=0.086168
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/50d521a3c96b350275a7b14ab5043b95)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股营业总收入

<a id="factor-123"></a>
#### 123. `total_operating_revenue_per_share_ttm` — 每股营业总收入TTM

- 聚宽分类：每股指标因子
- 快照 factor_id：`cc54797f3a2b2c8b851631f25cffa5fa`
- 更新时间：2021-02-23 13:33:56
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.001473；IR=0.019195；多空年化=0.051537；多空夏普=0.136837；多空最大回撤=0.099488
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/cc54797f3a2b2c8b851631f25cffa5fa)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业总收入（TTM）除以总股本

### 风险因子 - 风格因子

<a id="factor-124"></a>
#### 124. `average_share_turnover_annual` — 年度平均月换手率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`27d5343c05b8e832145a5790f14e4e7e`
- 更新时间：2021-02-23 13:16:08
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.026131；IR=-0.102687；多空年化=0.066561；多空夏普=0.092116；多空最大回撤=0.424113
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/27d5343c05b8e832145a5790f14e4e7e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ln(sum(turn_over_ratio)/12)，turn_over_ratio为过去十二个月（252个交易日）的平均换手率

<a id="factor-125"></a>
#### 125. `average_share_turnover_quarterly` — 季度平均平均月换手率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`90c9645f21c8ed0d1a0976b05bbbe5c3`
- 更新时间：2021-02-23 13:37:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.032826；IR=-0.126122；多空年化=0.028087；多空夏普=-0.039386；多空最大回撤=0.460680
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/90c9645f21c8ed0d1a0976b05bbbe5c3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ln(sum(turn_over_ratio)/3)，turn_over_ratio为过去三个月（63个交易日）的平均换手率

<a id="factor-126"></a>
#### 126. `beta` — BETA

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`21ea849b38c902bde8d7e868a079cf62`
- 更新时间：2024-09-21 16:43:02
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.025410；IR=-0.076092；多空年化=0.154816；多空夏普=0.315765；多空最大回撤=0.452780
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/21ea849b38c902bde8d7e868a079cf62)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 一元线性回归求解 beta=sum(w_t*(r_t-r_mean)*(R_t-R_mean))/sum(w_t*(R_t-R_mean)^2)，其中r_t、R_t分别使用前252个交易日的股票和中证流通指数的close数据，w_t为半衰期为63个交易日的指数加权平均权重，w_t=0.5**(t/63)

<a id="factor-127"></a>
#### 127. `book_leverage` — 账面杠杆

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`f4ee70eaa2613bf4dbf32c4c55d82f66`
- 更新时间：2021-02-23 14:04:19
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005498；IR=0.046310；多空年化=-0.049316；多空夏普=-0.709572；多空最大回撤=0.256364
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f4ee70eaa2613bf4dbf32c4c55d82f66)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (普通股账面价值 + 优先股账面价值 + 长期负债账面价值) / 普通股账面价值

<a id="factor-128"></a>
#### 128. `book_to_price_ratio` — 市净率因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`1203de872e2a85157a2c3245362cbf75`
- 更新时间：2021-02-23 12:49:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.032453；IR=0.135195；多空年化=-0.053099；多空夏普=-0.332374；多空最大回撤=0.531417
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1203de872e2a85157a2c3245362cbf75)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 每股净资产与每股股价的比率

<a id="factor-129"></a>
#### 129. `cash_earnings_to_price_ratio` — 现金流量市值比

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`1e4bb3c2debf9033b7c755e0b3984339`
- 更新时间：2021-02-23 13:14:32
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.024440；IR=0.125066；多空年化=-0.056295；多空夏普=-0.398445；多空最大回撤=0.519825
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1e4bb3c2debf9033b7c755e0b3984339)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去一年的净经营现金流 除以 当前股票市值

<a id="factor-130"></a>
#### 130. `cube_of_size` — 市值立方因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`2c82b80977066b9aa0f49bbdef17ee17`
- 更新时间：2021-02-23 12:50:56
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004084；IR=0.021796；多空年化=-0.021509；多空夏普=-0.257674；多空最大回撤=0.436298
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2c82b80977066b9aa0f49bbdef17ee17)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 标准化市值因子的三次方，之后将结果和标准化市值因子回归取残差（去除和市值因子的共线性），然后残差值进行缩尾处理（将3倍标准差之外的点处理成3倍标准差）和标准化

<a id="factor-131"></a>
#### 131. `cumulative_range` — 收益离差

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`dc7423ae71e1ed5de5d160a7f5a7cc80`
- 更新时间：2021-02-23 13:21:58
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.036313；IR=-0.144145；多空年化=-0.033933；多空夏普=-0.262636；多空最大回撤=0.405645
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/dc7423ae71e1ed5de5d160a7f5a7cc80)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ln(1+Z_max)-ln(1+Z_min)，其中Z_t=cumsum(ln(1+r_t))，t=1,2,...,12，r_t为向前推t个月的月收益

<a id="factor-132"></a>
#### 132. `daily_standard_deviation` — 日收益率标准差

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`267366a74cc5e08d2c35e980c78e7d5c`
- 更新时间：2021-02-23 13:18:09
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.046023；IR=-0.150895；多空年化=-0.075672；多空夏普=-0.341293；多空最大回撤=0.467901
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/267366a74cc5e08d2c35e980c78e7d5c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> sqrt(sum(w_t*(r_t - r_mean)**2))，其中r_t为过去252个交易日的日收益率，w_t为半衰期为42个交易日的指数权重，满足w(t-42)=0.5*w(t)

<a id="factor-133"></a>
#### 133. `debt_to_assets` — 资产负债率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`db61fce9d3e7f9356f4ecf1a8a03fa87`
- 更新时间：2021-02-23 13:03:41
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008171；IR=0.066439；多空年化=-0.063002；多空夏普=-0.821598；多空最大回撤=0.202580
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/db61fce9d3e7f9356f4ecf1a8a03fa87)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总负债账面价值 / 总资产账面价值

<a id="factor-134"></a>
#### 134. `earnings_growth` — 5年盈利增长率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`f3c5177099b88a12c4cb05025667ce62`
- 更新时间：2021-02-23 12:45:22
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.004709；IR=-0.049343；多空年化=0.015683；多空夏普=-0.245595；多空最大回撤=0.129547
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f3c5177099b88a12c4cb05025667ce62)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去5个财年 年均EPS增长 除以 年均EPS

<a id="factor-135"></a>
#### 135. `earnings_to_price_ratio` — 利润市值比

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`a94243c8e40dc4bf142d958e81ff3b95`
- 更新时间：2021-02-23 12:44:18
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.027034；IR=0.122146；多空年化=0.012517；多空夏普=-0.106673；多空最大回撤=0.462382
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a94243c8e40dc4bf142d958e81ff3b95)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去一年的净利润 除以 当前股票市值，等于 PE 的倒数

<a id="factor-136"></a>
#### 136. `earnings_yield` — 盈利预期因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`844b47b08277525674f569c7ceec0dca`
- 更新时间：2021-02-23 12:38:33
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.028302；IR=0.121033；多空年化=0.010030；多空夏普=-0.106933；多空最大回撤=0.465365
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/844b47b08277525674f569c7ceec0dca)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.68 * 预期市盈率 + 0.21 * 营业收益市值比 + 0.11 * 利润市值比

<a id="factor-137"></a>
#### 137. `growth` — 成长因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`a5fb02f635cae6daa73a8d79116264f3`
- 更新时间：2021-02-23 13:25:34
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.005901；IR=-0.060459；多空年化=0.097465；多空夏普=0.507432；多空最大回撤=0.143529
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a5fb02f635cae6daa73a8d79116264f3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.18 * 预期长期盈利增长率 + 0.11 * 预期短期盈利增长率 + 0.24 * 5年盈利增长率 + 0.47 * 5年营业收入增长率

<a id="factor-138"></a>
#### 138. `historical_sigma` — 残差历史波动率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`1897d24f8e15a80233486e8e301e74f4`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.041548；IR=-0.166901；多空年化=-0.049209；多空夏普=-0.313597；多空最大回撤=0.434004
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1897d24f8e15a80233486e8e301e74f4)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算 beta 收益之时的残差收益率的波动率

<a id="factor-139"></a>
#### 139. `leverage` — 杠杆因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`f113ca936a3652b2052a157ff5746085`
- 更新时间：2021-02-23 13:20:37
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.012849；IR=0.085257；多空年化=-0.071878；多空夏普=-0.698650；多空最大回撤=0.335494
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f113ca936a3652b2052a157ff5746085)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.38 * 市场杠杆 + 0.35 * 资产负债率 + 0.27 * 账面杠杆

<a id="factor-140"></a>
#### 140. `liquidity` — 流动性因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`1ed4c076e73bc527c4f879de5ab92aa1`
- 更新时间：2021-02-23 14:12:10
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.033258；IR=-0.129828；多空年化=-0.003021；多空夏普=-0.144674；多空最大回撤=0.461778
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1ed4c076e73bc527c4f879de5ab92aa1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.35 * 月换手率 + 0.35 * 季度平均平均月换手率 + 0.3 * 年度平均月换手率，之后将结果和市值因子做回归，取残差（去除和市值因子的共线性）

<a id="factor-141"></a>
#### 141. `long_term_predicted_earnings_growth` — 预期长期盈利增长率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`148e2d3cf9e0016ac144cc54edf87760`
- 更新时间：2021-02-23 13:09:19
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.000924；IR=0.007862；多空年化=-0.042684；多空夏普=-0.728875；多空最大回撤=0.217814
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/148e2d3cf9e0016ac144cc54edf87760)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 分析师预测未来3-5年盈利增长率

<a id="factor-142"></a>
#### 142. `market_leverage` — 市场杠杆

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`0bf306f256850383633d7400803d47b6`
- 更新时间：2021-02-23 13:19:40
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.014021；IR=0.083149；多空年化=-0.034770；多空夏普=-0.365437；多空最大回撤=0.362339
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0bf306f256850383633d7400803d47b6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (普通股市值 + 优先股账面价值(中国股票为0) + 长期负债账面价值) / 普通股市值，长期负债账面价值=长期借款+应付债券

<a id="factor-143"></a>
#### 143. `momentum` — 动量因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`9ac578f0166b1f98fa4f9a929454ad98`
- 更新时间：2021-02-23 12:31:31
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009772；IR=0.040483；多空年化=0.061009；多空夏普=0.073558；多空最大回撤=0.435562
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9ac578f0166b1f98fa4f9a929454ad98)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 动量因子=1.0*相对强弱因子=sum(w_t * ln(1 + r_t))，其中r_t取滞后21个交易日的前504个交易日的close数据，w_t为半衰期为126天的指数权重，满足w(t-126)=0.5*w(t)

<a id="factor-144"></a>
#### 144. `natural_log_of_market_cap` — 对数总市值

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`2798d2e9c589077e1f87a76b79ca964d`
- 更新时间：2021-02-23 14:10:55
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.004084；IR=-0.021796；多空年化=-0.033026；多空夏普=-0.305868；多空最大回撤=0.406253
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2798d2e9c589077e1f87a76b79ca964d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 对数总市值=总市值的对数

<a id="factor-145"></a>
#### 145. `non_linear_size` — 非线性市值因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`378c871bae65756bf835413d80ea7f3e`
- 更新时间：2021-02-23 12:38:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004279；IR=0.023183；多空年化=-0.007288；多空夏普=-0.208305；多空最大回撤=0.409011
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/378c871bae65756bf835413d80ea7f3e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1.0*市值立方因子，标准化市值因子的三次方，之后将结果和标准化市值因子回归取残差（去除和市值因子的共线性），然后残差值进行缩尾处理（将3倍标准差之外的点处理成3倍标准差）和标准化

<a id="factor-146"></a>
#### 146. `predicted_earnings_to_price_ratio` — 预期市盈率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`070c8010c269052c0e97372d3dd8a974`
- 更新时间：2021-02-23 12:56:58
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.027683；IR=0.116584；多空年化=0.011425；多空夏普=-0.099959；多空最大回撤=0.458213
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/070c8010c269052c0e97372d3dd8a974)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 分析师对未来一年预期盈利加权平均值 除以 当前股票市值

<a id="factor-147"></a>
#### 147. `raw_beta` — RAW BETA

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`f0b022ab6e10f1f33ec681e3426a9588`
- 更新时间：2021-02-23 12:38:23
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.025423；IR=-0.076128；多空年化=0.154817；多空夏普=0.315764；多空最大回撤=0.452780
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f0b022ab6e10f1f33ec681e3426a9588)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 一元线性回归求解 beta=sum(w_t*(r_t-r_mean)*(R_t-R_mean))/sum(w_t*(R_t-R_mean)^2)，其中r_t、R_t分别使用前252个交易日的股票和中证流通指数的close数据，w_t为半衰期为63个交易日的指数加权平均权重，w_t=0.5**(t/63)

<a id="factor-148"></a>
#### 148. `relative_strength` — 相对强弱

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`1f1911c2f8c75a98a4ec0fd5a939fc2c`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009797；IR=0.040588；多空年化=0.059458；多空夏普=0.068123；多空最大回撤=0.435562
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1f1911c2f8c75a98a4ec0fd5a939fc2c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> sum(w_t * ln(1 + r_t))，其中r_t取滞后21个交易日的前504个交易日的close数据，w_t为半衰期为126天的指数权重，满足w(t-126)=0.5*w(t)

<a id="factor-149"></a>
#### 149. `residual_volatility` — 残差波动因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`36c1ba850faca3034c4a0c0a1e50a556`
- 更新时间：2021-02-23 14:02:44
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.041155；IR=-0.276593；多空年化=-0.106618；多空夏普=-0.737919；多空最大回撤=0.394280
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/36c1ba850faca3034c4a0c0a1e50a556)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.74 * 日收益率标准差(DASTD) + 0.16 * 收益离差(CMRA) + 0.1 * 残差历史波动率(HSIGMA)，之后将结果和beta因子，市值因子做回归，取残差

<a id="factor-150"></a>
#### 150. `sales_growth` — 5年营业收入增长率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`061b9834850a60e460db342d6f4a90e9`
- 更新时间：2021-02-23 13:18:26
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006634；IR=-0.067815；多空年化=0.080193；多空夏普=0.352731；多空最大回撤=0.152052
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/061b9834850a60e460db342d6f4a90e9)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去5个财年的 每股营业收入增长 除以 年均每股营业收入

<a id="factor-151"></a>
#### 151. `share_turnover_monthly` — 月换手率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`5d0c0c84a8a040305e5eb1b537fae5d8`
- 更新时间：2021-02-23 14:05:56
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.037968；IR=-0.150503；多空年化=-0.025547；多空夏普=-0.217984；多空最大回撤=0.472617
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5d0c0c84a8a040305e5eb1b537fae5d8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ln(sum(turn_over_ratio))，turn_over_ratio为过去21个交易日的换手率

<a id="factor-152"></a>
#### 152. `short_term_predicted_earnings_growth` — 预期短期盈利增长率

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`0739cc0d46ad164184a1c2621217087f`
- 更新时间：2021-02-23 13:31:13
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.007384；IR=-0.051329；多空年化=0.037759；多空夏普=-0.011863；多空最大回撤=0.333812
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0739cc0d46ad164184a1c2621217087f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 分析师预测未来1年盈利增长率

<a id="factor-153"></a>
#### 153. `size` — 市值因子

- 聚宽分类：风险因子 - 风格因子
- 快照 factor_id：`d6acab3eceab567711390d4bbfc955af`
- 更新时间：2021-02-23 13:20:30
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.004153；IR=-0.022415；多空年化=-0.041533；多空夏普=-0.358596；多空最大回撤=0.384461
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/d6acab3eceab567711390d4bbfc955af)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 资产规模 = 1.0 * 对数总资产 = 总资产的对数

### 情绪类因子

<a id="factor-154"></a>
#### 154. `AR` — 人气指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`195a9d6a394908fb09dd0ca2fb7d1f3b`
- 更新时间：2021-02-23 14:03:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.026131；IR=-0.221055；多空年化=-0.093651；多空夏普=-0.950949；多空最大回撤=0.276413
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/195a9d6a394908fb09dd0ca2fb7d1f3b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> AR=N日内（当日最高价—当日开市价）之和 / N日内（当日开市价—当日最低价）之和 * 100，n设定为26

<a id="factor-155"></a>
#### 155. `ARBR` — ARBR

- 聚宽分类：情绪类因子
- 快照 factor_id：`be8fc05d9fb152bb46cfe92539647c77`
- 更新时间：2021-02-23 13:06:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.008530；IR=-0.114450；多空年化=-0.074744；多空夏普=-1.133309；多空最大回撤=0.269557
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/be8fc05d9fb152bb46cfe92539647c77)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 因子 AR 与因子 BR 的差

<a id="factor-156"></a>
#### 156. `ATR14` — 14日均幅指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`e09c7b9b7d9e668e3fcf411715391e5e`
- 更新时间：2021-02-23 12:57:05
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.012773；IR=-0.133519；多空年化=-0.014448；多空夏普=-0.491585；多空最大回撤=0.164903
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e09c7b9b7d9e668e3fcf411715391e5e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 真实振幅的14日移动平均

<a id="factor-157"></a>
#### 157. `ATR6` — 6日均幅指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`170d12f92ae78f813b69c544e14c45aa`
- 更新时间：2021-02-23 13:26:30
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.015603；IR=-0.165726；多空年化=-0.005648；多空夏普=-0.421628；多空最大回撤=0.142879
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/170d12f92ae78f813b69c544e14c45aa)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 真实振幅的6日移动平均

<a id="factor-158"></a>
#### 158. `BR` — 意愿指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`255f5b557b410e9e5322a1f73bbad435`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022999；IR=-0.186852；多空年化=-0.099323；多空夏普=-0.943122；多空最大回撤=0.316738
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/255f5b557b410e9e5322a1f73bbad435)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> BR=N日内（当日最高价－昨日收盘价）之和 / N日内（昨日收盘价－当日最低价）之和×100 n设定为26

<a id="factor-159"></a>
#### 159. `DAVOL10` — 10日平均换手率与120日平均换手率之比

- 聚宽分类：情绪类因子
- 快照 factor_id：`80dcac015f0cb1cc19791e070a857417`
- 更新时间：2021-02-23 13:47:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.024976；IR=-0.261555；多空年化=-0.133595；多空夏普=-1.470691；多空最大回撤=0.384397
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/80dcac015f0cb1cc19791e070a857417)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 10日平均换手率 / 120日平均换手率

<a id="factor-160"></a>
#### 160. `DAVOL20` — 20日平均换手率与120日平均换手率之比

- 聚宽分类：情绪类因子
- 快照 factor_id：`1cb40863182627a388f2f6ef78aec240`
- 更新时间：2021-02-23 14:05:47
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021397；IR=-0.224065；多空年化=-0.127823；多空夏普=-1.452802；多空最大回撤=0.337050
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1cb40863182627a388f2f6ef78aec240)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日平均换手率 / 120日平均换手率

<a id="factor-161"></a>
#### 161. `DAVOL5` — 5日平均换手率与120日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`e189655883111ba6ded713a4016c2100`
- 更新时间：2021-02-23 12:45:36
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.028612；IR=-0.293540；多空年化=-0.148496；多空夏普=-1.541950；多空最大回撤=0.393267
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e189655883111ba6ded713a4016c2100)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 5日平均换手率 / 120日平均换手率

<a id="factor-162"></a>
#### 162. `MAWVAD` — 因子WVAD的6日均值

- 聚宽分类：情绪类因子
- 快照 factor_id：`1344f5e61e6f1c121b07382e198caf68`
- 更新时间：2021-02-23 12:31:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.012866；IR=-0.119748；多空年化=-0.096567；多空夏普=-1.016985；多空最大回撤=0.268611
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1344f5e61e6f1c121b07382e198caf68)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-163"></a>
#### 163. `money_flow_20` — 20日资金流量

- 聚宽分类：情绪类因子
- 快照 factor_id：`cbd64ccb9acb4f4f3d26ca8ee9870163`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.028065；IR=-0.175796；多空年化=-0.012544；多空夏普=-0.341415；多空最大回撤=0.196696
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/cbd64ccb9acb4f4f3d26ca8ee9870163)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 用收盘价、最高价及最低价的均值乘以当日成交量即可得到该交易日的资金流量

<a id="factor-164"></a>
#### 164. `PSY` — 心理线指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`8581f47e553ee6364334cecb8f6a49f2`
- 更新时间：2021-02-23 13:12:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.011175；IR=-0.109480；多空年化=-0.064038；多空夏普=-0.819232；多空最大回撤=0.282954
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8581f47e553ee6364334cecb8f6a49f2)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> n日内连续上涨的天数/n *100。 本因子的计算窗口为12日。

<a id="factor-165"></a>
#### 165. `turnover_volatility` — 换手率相对波动率

- 聚宽分类：情绪类因子
- 快照 factor_id：`c081b694b1d3f9f3062a9a866e94632a`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.036165；IR=-0.244593；多空年化=-0.126803；多空夏普=-1.028236；多空最大回撤=0.404689
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c081b694b1d3f9f3062a9a866e94632a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取20个交易日个股换手率的标准差

<a id="factor-166"></a>
#### 166. `TVMA20` — 20日成交金额的移动平均值

- 聚宽分类：情绪类因子
- 快照 factor_id：`44bbb14ef2e5d4fefc0beb555afb9225`
- 更新时间：2021-02-23 14:05:18
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.028054；IR=-0.175700；多空年化=-0.013525；多空夏普=-0.347803；多空最大回撤=0.196864
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/44bbb14ef2e5d4fefc0beb555afb9225)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日成交金额的移动平均值

<a id="factor-167"></a>
#### 167. `TVMA6` — 6日成交金额的移动平均值

- 聚宽分类：情绪类因子
- 快照 factor_id：`446c4ab58bfae013c2e3bdf336ead26f`
- 更新时间：2021-02-23 13:06:52
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.033414；IR=-0.218629；多空年化=-0.047989；多空夏普=-0.588235；多空最大回撤=0.206650
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/446c4ab58bfae013c2e3bdf336ead26f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 6日成交金额的移动平均值

<a id="factor-168"></a>
#### 168. `TVSTD20` — 20日成交金额的标准差

- 聚宽分类：情绪类因子
- 快照 factor_id：`c1e99bb3ba6ab92666c62f09a96b9b3b`
- 更新时间：2021-02-23 12:59:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.032682；IR=-0.218638；多空年化=-0.077912；多空夏普=-0.789622；多空最大回撤=0.294436
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c1e99bb3ba6ab92666c62f09a96b9b3b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日成交额的标准差

<a id="factor-169"></a>
#### 169. `TVSTD6` — 6日成交金额的标准差

- 聚宽分类：情绪类因子
- 快照 factor_id：`2b958b7b4d372e9e5fb8094eb909e00b`
- 更新时间：2021-02-23 13:59:12
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.035686；IR=-0.274157；多空年化=-0.042519；多空夏普=-0.609873；多空最大回撤=0.210197
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2b958b7b4d372e9e5fb8094eb909e00b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 6日成交额的标准差

<a id="factor-170"></a>
#### 170. `VDEA` — 计算VMACD因子的中间变量

- 聚宽分类：情绪类因子
- 快照 factor_id：`15825db2c25c9616068ada7f4d1b634c`
- 更新时间：2021-02-23 13:57:52
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.017952；IR=-0.191224；多空年化=-0.099469；多空夏普=-1.211735；多空最大回撤=0.299904
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/15825db2c25c9616068ada7f4d1b634c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> EMA(VDIFF，M) short设置为12，long设置为26，M设置为9

<a id="factor-171"></a>
#### 171. `VDIFF` — 计算VMACD因子的中间变量

- 聚宽分类：情绪类因子
- 快照 factor_id：`4c40a7e92146309f22fb030a92421931`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022062；IR=-0.231204；多空年化=-0.087843；多空夏普=-1.073972；多空最大回撤=0.268983
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4c40a7e92146309f22fb030a92421931)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> EMA(VOLUME，SHORT)-EMA(VOLUME，LONG) short设置为12，long设置为26，M设置为9

<a id="factor-172"></a>
#### 172. `VEMA10` — 成交量的10日指数移动平均

- 聚宽分类：情绪类因子
- 快照 factor_id：`26b6b6351c7fdfa1bd824942fc98daba`
- 更新时间：2021-02-23 13:59:19
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.015837；IR=-0.215480；多空年化=-0.112553；多空夏普=-1.574631；多空最大回撤=0.323020
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/26b6b6351c7fdfa1bd824942fc98daba)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-173"></a>
#### 173. `VEMA12` — 12日成交量的移动平均值

- 聚宽分类：情绪类因子
- 快照 factor_id：`c12715b05b1601c25af537ce557a1db3`
- 更新时间：2021-02-23 14:01:59
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.015314；IR=-0.207884；多空年化=-0.112170；多空夏普=-1.575844；多空最大回撤=0.323309
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c12715b05b1601c25af537ce557a1db3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-174"></a>
#### 174. `VEMA26` — 成交量的26日指数移动平均

- 聚宽分类：情绪类因子
- 快照 factor_id：`4305f0fda4feb4c3fc4108f08b2f9c01`
- 更新时间：2021-02-23 13:41:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.013084；IR=-0.176060；多空年化=-0.111416；多空夏普=-1.584216；多空最大回撤=0.325743
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4305f0fda4feb4c3fc4108f08b2f9c01)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-175"></a>
#### 175. `VEMA5` — 成交量的5日指数移动平均

- 聚宽分类：情绪类因子
- 快照 factor_id：`46d27688dc9c9712839e6fcb795f4979`
- 更新时间：2021-02-23 13:21:03
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.017733；IR=-0.242854；多空年化=-0.103641；多空夏普=-1.471179；多空最大回撤=0.306301
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/46d27688dc9c9712839e6fcb795f4979)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-176"></a>
#### 176. `VMACD` — 成交量指数平滑异同移动平均线

- 聚宽分类：情绪类因子
- 快照 factor_id：`0a793f74397d3c4c45bc947ac50bccf5`
- 更新时间：2021-02-23 12:51:46
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.014294；IR=-0.158693；多空年化=-0.073870；多空夏普=-1.059598；多空最大回撤=0.207487
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0a793f74397d3c4c45bc947ac50bccf5)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 快的指数移动平均线（EMA12）减去慢的指数移动平均线（EMA26）得到快线DIFF, 由DIFF的M日移动平均得到DEA，由DIFF-DEA的值得到MACD

<a id="factor-177"></a>
#### 177. `VOL10` — 10日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`8f8bf3add9d867a9f0b8ad13e7f73577`
- 更新时间：2021-02-23 13:06:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.035462；IR=-0.221869；多空年化=-0.040790；多空夏普=-0.448472；多空最大回撤=0.227490
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8f8bf3add9d867a9f0b8ad13e7f73577)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 10日换手率的均值,单位为%

<a id="factor-178"></a>
#### 178. `VOL120` — 120日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`713545f7fe94f9891fb3b63a7297748d`
- 更新时间：2021-02-23 14:02:49
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.024813；IR=-0.147251；多空年化=0.013206；多空夏普=-0.147156；多空最大回撤=0.240963
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/713545f7fe94f9891fb3b63a7297748d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 120日换手率的均值,单位为%

<a id="factor-179"></a>
#### 179. `VOL20` — 20日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`55c75e5c7aaec01f7a3078df89f5ff83`
- 更新时间：2021-02-23 14:09:43
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.032712；IR=-0.199949；多空年化=-0.054402；多空夏普=-0.521542；多空最大回撤=0.260943
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/55c75e5c7aaec01f7a3078df89f5ff83)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日换手率的均值,单位为%

<a id="factor-180"></a>
#### 180. `VOL240` — 240日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`4b7bdc3376097bb35dbcc66fb36025a3`
- 更新时间：2021-02-23 13:23:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022737；IR=-0.137038；多空年化=-0.008589；多空夏普=-0.272024；多空最大回撤=0.255543
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4b7bdc3376097bb35dbcc66fb36025a3)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 240日换手率的均值,单位为%

<a id="factor-181"></a>
#### 181. `VOL5` — 5日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`208e8203245c8daf7c474eedf4b6cf70`
- 更新时间：2021-02-23 13:55:06
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.038257；IR=-0.244520；多空年化=-0.064153；多空夏普=-0.582132；多空最大回撤=0.260710
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/208e8203245c8daf7c474eedf4b6cf70)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 5日换手率的均值,单位为%

<a id="factor-182"></a>
#### 182. `VOL60` — 60日平均换手率

- 聚宽分类：情绪类因子
- 快照 factor_id：`48620cb8ec5cec4cec1cb4d8d5182bb1`
- 更新时间：2021-02-23 13:05:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.027412；IR=-0.161363；多空年化=-0.008927；多空夏普=-0.265719；多空最大回撤=0.254001
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/48620cb8ec5cec4cec1cb4d8d5182bb1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 60日换手率的均值,单位为%

<a id="factor-183"></a>
#### 183. `VOSC` — 成交量震荡

- 聚宽分类：情绪类因子
- 快照 factor_id：`ef7bce5967f3efb7b098a59a49a9894e`
- 更新时间：2021-02-23 13:47:36
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.027489；IR=-0.262558；多空年化=-0.122747；多空夏普=-1.288785；多空最大回撤=0.374425
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ef7bce5967f3efb7b098a59a49a9894e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 'VEMA12'和'VEMA26'两者的差值，再求差值与'VEMA12'的比，最后将比值放大100倍，得到VOSC值

<a id="factor-184"></a>
#### 184. `VR` — 成交量比率（Volume Ratio）

- 聚宽分类：情绪类因子
- 快照 factor_id：`554baf02d54023d60ad96553ea9ecca0`
- 更新时间：2021-02-23 14:03:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.017538；IR=-0.160207；多空年化=-0.115074；多空夏普=-1.203906；多空最大回撤=0.347248
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/554baf02d54023d60ad96553ea9ecca0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> VR=（AVS+1/2CVS）/（BVS+1/2CVS）

<a id="factor-185"></a>
#### 185. `VROC12` — 12日量变动速率指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`ad153d27d5bc9c2a93b18ee909ec55f6`
- 更新时间：2021-02-23 14:04:11
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022912；IR=-0.261117；多空年化=-0.057969；多空夏普=-0.875714；多空最大回撤=0.203115
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ad153d27d5bc9c2a93b18ee909ec55f6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 成交量减N日前的成交量，再除以N日前的成交量，放大100倍，得到VROC值 ，n=12

<a id="factor-186"></a>
#### 186. `VROC6` — 6日量变动速率指标

- 聚宽分类：情绪类因子
- 快照 factor_id：`39b84ad506575548fbf4497d7876fa85`
- 更新时间：2021-02-23 12:31:25
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.020030；IR=-0.226136；多空年化=-0.107161；多空夏普=-1.342074；多空最大回撤=0.295550
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/39b84ad506575548fbf4497d7876fa85)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 成交量减N日前的成交量，再除以N日前的成交量，放大100倍，得到VROC值 ，n=6

<a id="factor-187"></a>
#### 187. `VSTD10` — 10日成交量标准差

- 聚宽分类：情绪类因子
- 快照 factor_id：`709b292617afafa11d774367b827c8d6`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019935；IR=-0.271749；多空年化=-0.126647；多空夏普=-1.757865；多空最大回撤=0.340490
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/709b292617afafa11d774367b827c8d6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 10日成交量去标准差

<a id="factor-188"></a>
#### 188. `VSTD20` — 20日成交量标准差

- 聚宽分类：情绪类因子
- 快照 factor_id：`e506682513bdc7baea0447ff31119e6d`
- 更新时间：2021-02-23 13:13:51
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.018960；IR=-0.248104；多空年化=-0.123692；多空夏普=-1.720841；多空最大回撤=0.357203
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e506682513bdc7baea0447ff31119e6d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日成交量去标准差

<a id="factor-189"></a>
#### 189. `WVAD` — 威廉变异离散量

- 聚宽分类：情绪类因子
- 快照 factor_id：`38eb78dfcf9d592c745ad73d0b4d9fe8`
- 更新时间：2021-02-23 12:45:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.018191；IR=-0.164125；多空年化=-0.130506；多空夏普=-1.285571；多空最大回撤=0.397168
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/38eb78dfcf9d592c745ad73d0b4d9fe8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (收盘价－开盘价)/(最高价－最低价)×成交量，再做加和，使用过去6个交易日的数据

### 成长类因子

<a id="factor-190"></a>
#### 190. `financing_cash_growth_rate` — 筹资活动产生的现金流量净额增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`15417b994b19fa263de675620be5bfb1`
- 更新时间：2021-02-23 13:20:53
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.001814；IR=0.038111；多空年化=0.057515；多空夏普=0.211905；多空最大回撤=0.096447
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/15417b994b19fa263de675620be5bfb1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 过去12个月的筹资现金流量净额 / 4季度前的12个月的筹资现金流量净额 - 1

<a id="factor-191"></a>
#### 191. `net_asset_growth_rate` — 净资产增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`f4c785b19f479b38d8bb957826a84272`
- 更新时间：2021-02-23 12:24:04
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.001309；IR=0.014831；多空年化=0.137588；多空夏普=1.004117；多空最大回撤=0.104819
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f4c785b19f479b38d8bb957826a84272)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （当季的股东权益/三季度前的股东权益）-1

<a id="factor-192"></a>
#### 192. `net_operate_cashflow_growth_rate` — 经营活动产生的现金流量净额增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`7c3b1b196df8fc0aea232a54d126f5fe`
- 更新时间：2021-02-23 13:42:40
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004629；IR=0.089638；多空年化=-0.011169；多空夏普=-0.692243；多空最大回撤=0.167480
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7c3b1b196df8fc0aea232a54d126f5fe)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> =(今年经营活动产生的现金流量净额（TTM）/去年经营活动产生的现金流量净额（TTM）)-1

<a id="factor-193"></a>
#### 193. `net_profit_growth_rate` — 净利润增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`36e5381b642d40f41cfbd9103c846065`
- 更新时间：2021-02-23 12:38:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.006746；IR=0.095982；多空年化=0.002654；多空夏普=-0.452086；多空最大回撤=0.120478
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/36e5381b642d40f41cfbd9103c846065)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 净利润增长率=(今年净利润（TTM）/去年净利润（TTM）)-1

<a id="factor-194"></a>
#### 194. `np_parent_company_owners_growth_rate` — 归属母公司股东的净利润增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`40cbb2744011cfd525f4a49f654b6a45`
- 更新时间：2021-02-23 13:55:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008114；IR=0.115016；多空年化=0.032750；多空夏普=-0.085303；多空最大回撤=0.132972
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/40cbb2744011cfd525f4a49f654b6a45)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (今年归属于母公司所有者的净利润（TTM）/去年归属于母公司所有者的净利润（TTM）)-1

<a id="factor-195"></a>
#### 195. `operating_revenue_growth_rate` — 营业收入增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`309ec6e99515f52cdfbb8ec71fc1de65`
- 更新时间：2021-02-23 13:09:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005709；IR=0.077460；多空年化=0.119347；多空夏普=0.868595；多空最大回撤=0.101070
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/309ec6e99515f52cdfbb8ec71fc1de65)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 营业收入增长率=（今年营业收入（TTM）/去年营业收入（TTM））-1

<a id="factor-196"></a>
#### 196. `PEG` — PEG

- 聚宽分类：成长类因子
- 快照 factor_id：`ea17761c3b8675621319b1045e1d555c`
- 更新时间：2021-02-23 13:19:56
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003321；IR=-0.040389；多空年化=-0.041221；多空夏普=-0.748672；多空最大回撤=0.194631
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ea17761c3b8675621319b1045e1d555c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> PEG = PE / (归母公司净利润(TTM)增长率 * 100) # 如果 PE 或 增长率为负，则为 nan

<a id="factor-197"></a>
#### 197. `total_asset_growth_rate` — 总资产增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`e4f4a2dd3cd78a2ebae31eb4dd8642c0`
- 更新时间：2021-02-23 14:06:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000198；IR=-0.002379；多空年化=0.126361；多空夏普=0.832669；多空最大回撤=0.111879
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e4f4a2dd3cd78a2ebae31eb4dd8642c0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 总资产 / 总资产_4 -1

<a id="factor-198"></a>
#### 198. `total_profit_growth_rate` — 利润总额增长率

- 聚宽分类：成长类因子
- 快照 factor_id：`8ef029b717d022d3167ed41ea201e3b9`
- 更新时间：2021-02-23 13:35:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007290；IR=0.107161；多空年化=-0.005410；多空夏普=-0.537637；多空最大回撤=0.135979
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/8ef029b717d022d3167ed41ea201e3b9)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 利润总额增长率=(今年利润总额（TTM）/去年利润总额（TTM）)-1

### 风险类因子

<a id="factor-199"></a>
#### 199. `Kurtosis120` — 个股收益的120日峰度

- 聚宽分类：风险类因子
- 快照 factor_id：`a9030beaa69b136ad5b8adfb816b3c6c`
- 更新时间：2021-02-23 12:24:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003215；IR=0.043838；多空年化=0.039561；多空夏普=-0.004089；多空最大回撤=0.157266
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a9030beaa69b136ad5b8adfb816b3c6c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取121个交易日的收盘价数据，计算日收益率，再计算其峰度值

<a id="factor-200"></a>
#### 200. `Kurtosis20` — 个股收益的20日峰度

- 聚宽分类：风险类因子
- 快照 factor_id：`ebb60fd5c24f95ea2ae25c043abf9e6b`
- 更新时间：2021-02-23 13:23:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006425；IR=-0.100501；多空年化=-0.034995；多空夏普=-0.743562；多空最大回撤=0.190628
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/ebb60fd5c24f95ea2ae25c043abf9e6b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取21个交易日的收盘价数据，计算日收益率，再计算其峰度值

<a id="factor-201"></a>
#### 201. `Kurtosis60` — 个股收益的60日峰度

- 聚宽分类：风险类因子
- 快照 factor_id：`3da571f2686b32cf700c460a348574ce`
- 更新时间：2021-02-23 12:52:23
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003191；IR=-0.045265；多空年化=0.011918；多空夏普=-0.258009；多空最大回撤=0.147285
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3da571f2686b32cf700c460a348574ce)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取61个交易日的收盘价数据，计算日收益率，再计算其峰度值

<a id="factor-202"></a>
#### 202. `sharpe_ratio_120` — 120日夏普比率

- 聚宽分类：风险类因子
- 快照 factor_id：`e5ae3157d0f6b1a932bf9a88050fb99c`
- 更新时间：2021-02-23 12:43:07
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.011153；IR=-0.083233；多空年化=0.008665；多空夏普=-0.209922；多空最大回撤=0.287372
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e5ae3157d0f6b1a932bf9a88050fb99c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差）

<a id="factor-203"></a>
#### 203. `sharpe_ratio_20` — 20日夏普比率

- 聚宽分类：风险类因子
- 快照 factor_id：`9f91a460c4668cabda174a078a091d0a`
- 更新时间：2021-02-23 12:36:04
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.026959；IR=-0.205888；多空年化=-0.074841；多空夏普=-0.810322；多空最大回撤=0.307212
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9f91a460c4668cabda174a078a091d0a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差）

<a id="factor-204"></a>
#### 204. `sharpe_ratio_60` — 60日夏普比率

- 聚宽分类：风险类因子
- 快照 factor_id：`b08d048b4d1ed296df54f1cf3ba040ef`
- 更新时间：2021-02-23 12:45:14
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019198；IR=-0.141845；多空年化=-0.099744；多空夏普=-0.935557；多空最大回撤=0.300278
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b08d048b4d1ed296df54f1cf3ba040ef)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （Rp - Rf） / Sigma p 其中，Rp是个股的年化收益率，Rf是无风险利率（在这里设置为0.04），Sigma p是个股的收益波动率（标准差）

<a id="factor-205"></a>
#### 205. `Skewness120` — 个股收益的120日偏度

- 聚宽分类：风险类因子
- 快照 factor_id：`137299b8f42b63a46a19bd998df688fc`
- 更新时间：2021-02-23 13:20:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.020230；IR=-0.217783；多空年化=-0.063613；多空夏普=-0.834941；多空最大回撤=0.195512
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/137299b8f42b63a46a19bd998df688fc)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取121个交易日的收盘价数据，计算日收益率，再计算其偏度

<a id="factor-206"></a>
#### 206. `Skewness20` — 个股收益的20日偏度

- 聚宽分类：风险类因子
- 快照 factor_id：`104b7e4cab91aacf9c6b5434608ff6fe`
- 更新时间：2021-02-23 13:48:52
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.015609；IR=-0.200692；多空年化=-0.052011；多空夏普=-0.840804；多空最大回撤=0.202368
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/104b7e4cab91aacf9c6b5434608ff6fe)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取21个交易日的收盘价数据，计算日收益率，再计算其偏度

<a id="factor-207"></a>
#### 207. `Skewness60` — 个股收益的60日偏度

- 聚宽分类：风险类因子
- 快照 factor_id：`9983303da12d8ceb79fe2279927a3b5a`
- 更新时间：2021-02-23 12:55:08
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021476；IR=-0.246197；多空年化=-0.058703；多空夏普=-0.862149；多空最大回撤=0.231922
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9983303da12d8ceb79fe2279927a3b5a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取61个交易日的收盘价数据，计算日收益率，再计算其偏度

<a id="factor-208"></a>
#### 208. `Variance120` — 120日收益方差

- 聚宽分类：风险类因子
- 快照 factor_id：`5d242c80497a51b6ed07199a66aa1274`
- 更新时间：2021-02-23 12:24:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.037013；IR=-0.189520；多空年化=-0.084918；多空夏普=-0.601679；多空最大回撤=0.350776
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5d242c80497a51b6ed07199a66aa1274)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取121个交易日的收盘价，算出日收益率，再取方差

<a id="factor-209"></a>
#### 209. `Variance20` — 20日收益方差

- 聚宽分类：风险类因子
- 快照 factor_id：`00830cf5837a8f018f07826c74888b33`
- 更新时间：2021-02-23 13:40:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.042298；IR=-0.252340；多空年化=-0.162956；多空夏普=-1.059208；多空最大回撤=0.499166
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/00830cf5837a8f018f07826c74888b33)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取21个交易日的收盘价，算出日收益率，再取方差

<a id="factor-210"></a>
#### 210. `Variance60` — 60日收益方差

- 聚宽分类：风险类因子
- 快照 factor_id：`cc7d48b42812c6bf6735c8130e0c7cac`
- 更新时间：2021-02-23 12:45:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.040174；IR=-0.212012；多空年化=-0.115154；多空夏普=-0.733140；多空最大回撤=0.416355
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/cc7d48b42812c6bf6735c8130e0c7cac)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取61个交易日的收盘价，算出日收益率，再取方差

### 技术指标因子

<a id="factor-211"></a>
#### 211. `boll_down` — 下轨线（布林线）指标

- 聚宽分类：技术指标因子
- 快照 factor_id：`4ba7d740fc17eaf6f78815009794bde1`
- 更新时间：2021-02-23 13:41:10
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.038007；IR=0.312225；多空年化=0.093481；多空夏普=0.352257；多空最大回撤=0.178445
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4ba7d740fc17eaf6f78815009794bde1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (MA(CLOSE,M)-2*STD(CLOSE,M)) / 今日收盘价; M=20

<a id="factor-212"></a>
#### 212. `boll_up` — 上轨线（布林线）指标

- 聚宽分类：技术指标因子
- 快照 factor_id：`5b2a18acf89bbbd5fbd390b45267227e`
- 更新时间：2021-02-23 12:38:26
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004965；IR=0.030965；多空年化=-0.025065；多空夏普=-0.362146；多空最大回撤=0.297989
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5b2a18acf89bbbd5fbd390b45267227e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (MA(CLOSE,M)+2*STD(CLOSE,M)) / 今日收盘价; M=20

<a id="factor-213"></a>
#### 213. `EMA5` — 5日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`b3ecc55971456684e5131a2d74121cef`
- 更新时间：2021-02-23 12:38:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.025412；IR=0.168998；多空年化=-0.016908；多空夏普=-0.312913；多空最大回撤=0.298811
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b3ecc55971456684e5131a2d74121cef)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 5日指数移动均线 / 今日收盘价

<a id="factor-214"></a>
#### 214. `EMAC10` — 10日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`23678e8db911aeeefe4d8eee9df6c7d8`
- 更新时间：2021-02-23 13:54:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.027218；IR=0.174355；多空年化=0.038379；多空夏普=-0.008748；多空最大回撤=0.260215
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/23678e8db911aeeefe4d8eee9df6c7d8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 10日指数移动均线 / 今日收盘价

<a id="factor-215"></a>
#### 215. `EMAC12` — 12日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`e7e5a97e83bc6c9a5313b66f33436880`
- 更新时间：2021-02-23 13:12:47
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.027948；IR=0.177895；多空年化=0.077179；多空夏普=0.199690；多空最大回撤=0.224038
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e7e5a97e83bc6c9a5313b66f33436880)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 12日指数移动均线 / 今日收盘价

<a id="factor-216"></a>
#### 216. `EMAC120` — 120日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`5261682eb947842ee0b20d7ff732c06c`
- 更新时间：2021-02-23 14:10:50
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.016678；IR=0.104502；多空年化=0.123852；多空夏普=0.491779；多空最大回撤=0.203743
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5261682eb947842ee0b20d7ff732c06c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 120日指数移动均线 / 今日收盘价

<a id="factor-217"></a>
#### 217. `EMAC20` — 20日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`0ef3a7533edfab1c3b302575c5d7f8fd`
- 更新时间：2021-02-23 13:35:24
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.028862；IR=0.181439；多空年化=0.088608；多空夏普=0.260921；多空最大回撤=0.254306
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0ef3a7533edfab1c3b302575c5d7f8fd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日指数移动均线 / 今日收盘价

<a id="factor-218"></a>
#### 218. `EMAC26` — 26日指数移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`16acc36bb4cf8f026404fbb2a72a1b37`
- 更新时间：2021-02-23 13:06:42
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.028411；IR=0.177647；多空年化=0.112910；多空夏普=0.392309；多空最大回撤=0.230032
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/16acc36bb4cf8f026404fbb2a72a1b37)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 26日指数移动均线 / 今日收盘价

<a id="factor-219"></a>
#### 219. `MAC10` — 10日移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`6bd64514c78eb921f252e433c411465b`
- 更新时间：2021-02-23 13:33:25
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.025497；IR=0.166323；多空年化=0.024513；多空夏普=-0.085741；多空最大回撤=0.232085
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/6bd64514c78eb921f252e433c411465b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 10日移动均线 / 今日收盘价

<a id="factor-220"></a>
#### 220. `MAC120` — 120日移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`d329ab673c0436d0e7d808c555748417`
- 更新时间：2021-02-23 13:04:11
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.016413；IR=0.105439；多空年化=0.051677；多空夏普=0.067350；多空最大回撤=0.277699
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/d329ab673c0436d0e7d808c555748417)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 120日移动均线 / 今日收盘价

<a id="factor-221"></a>
#### 221. `MAC20` — 20日移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`22cbe208da58e7c862b3cf395ad525b1`
- 更新时间：2021-02-23 13:19:23
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.026646；IR=0.171412；多空年化=0.085814；多空夏普=0.249370；多空最大回撤=0.228176
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/22cbe208da58e7c862b3cf395ad525b1)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 20日移动均线 / 今日收盘价

<a id="factor-222"></a>
#### 222. `MAC5` — 5日移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`617b10c58e811928c070d06ba6505f9a`
- 更新时间：2021-02-23 13:48:29
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.023187；IR=0.159195；多空年化=-0.038643；多空夏普=-0.436052；多空最大回撤=0.273498
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/617b10c58e811928c070d06ba6505f9a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 5日移动均线 / 今日收盘价

<a id="factor-223"></a>
#### 223. `MAC60` — 60日移动均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`665fa866a0c0bf430855eec2f98784cd`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.022919；IR=0.142694；多空年化=0.110040；多空夏普=0.384602；多空最大回撤=0.217671
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/665fa866a0c0bf430855eec2f98784cd)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 60日移动均线 / 今日收盘价

<a id="factor-224"></a>
#### 224. `MACDC` — 平滑异同移动平均线

- 聚宽分类：技术指标因子
- 快照 factor_id：`25905cb62a0c10f044151887caf35f40`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019187；IR=-0.134639；多空年化=-0.066846；多空夏普=-0.622041；多空最大回撤=0.271239
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/25905cb62a0c10f044151887caf35f40)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MACD(SHORT=12, LONG=26, MID=9) / 今日收盘价

<a id="factor-225"></a>
#### 225. `MFI14` — 资金流量指标

- 聚宽分类：技术指标因子
- 快照 factor_id：`a2c7404c7d0322b4ad82c8cba3a6b4f8`
- 更新时间：2021-02-23 12:52:27
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019603；IR=-0.167914；多空年化=-0.067690；多空夏普=-0.783099；多空最大回撤=0.230523
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a2c7404c7d0322b4ad82c8cba3a6b4f8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①求得典型价格（当日最高价，最低价和收盘价的均值）②根据典型价格高低判定正负向资金流（资金流=典型价格*成交量）③计算MR= 正向/负向 ④MFI=100-100/（1+MR）

<a id="factor-226"></a>
#### 226. `price_no_fq` — 不复权价格因子

- 聚宽分类：技术指标因子
- 快照 factor_id：`0cd5f6c82c113b33cd1e41776f166166`
- 更新时间：2021-04-08 15:32:05
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.027809；IR=-0.122539；多空年化=0.060866；多空夏普=0.078697；多空最大回撤=0.404090
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0cd5f6c82c113b33cd1e41776f166166)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 不复权价格

### 动量类因子

<a id="factor-227"></a>
#### 227. `arron_down_25` — Aroon指标下轨

- 聚宽分类：动量类因子
- 快照 factor_id：`7b394f6944612855871c51d31ec0e60a`
- 更新时间：2021-02-23 12:58:44
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008447；IR=0.076727；多空年化=0.023559；多空夏普=-0.135503；多空最大回撤=0.188600
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7b394f6944612855871c51d31ec0e60a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> Aroon(下降)=[(计算期天数-最低价后的天数)/计算期天数]*100

<a id="factor-228"></a>
#### 228. `arron_up_25` — Aroon指标上轨

- 聚宽分类：动量类因子
- 快照 factor_id：`4eb501f1129080732f14ebe652d95b4c`
- 更新时间：2021-02-23 13:05:49
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.014513；IR=-0.134416；多空年化=-0.026206；多空夏普=-0.583540；多空最大回撤=0.135004
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4eb501f1129080732f14ebe652d95b4c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> Aroon(上升)=[(计算期天数-最高价后的天数)/计算期天数]*100

<a id="factor-229"></a>
#### 229. `BBIC` — BBI 动量

- 聚宽分类：动量类因子
- 快照 factor_id：`593a8757ad200049365499e9befb5bbb`
- 更新时间：2021-02-23 13:30:18
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.028136；IR=0.179370；多空年化=0.060912；多空夏普=0.111637；多空最大回撤=0.249445
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/593a8757ad200049365499e9befb5bbb)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> BBI(3, 6, 12, 24) / 收盘价 （BBI 为常用技术指标类因子“多空均线”）

<a id="factor-230"></a>
#### 230. `bear_power` — 空头力道

- 聚宽分类：动量类因子
- 快照 factor_id：`2d02b21579888056b99f631c5cc449f6`
- 更新时间：2021-02-23 13:58:41
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.010589；IR=-0.066528；多空年化=-0.027095；多空夏普=-0.379132；多空最大回撤=0.280268
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2d02b21579888056b99f631c5cc449f6)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (最低价-EMA(close,13)) / close

<a id="factor-231"></a>
#### 231. `BIAS10` — 10日乖离率

- 聚宽分类：动量类因子
- 快照 factor_id：`718b25c70d7251e9e104d5cb821bf2d0`
- 更新时间：2021-02-23 14:08:52
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.025435；IR=-0.165824；多空年化=-0.057044；多空夏普=-0.537435；多空最大回撤=0.323019
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/718b25c70d7251e9e104d5cb821bf2d0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取10

<a id="factor-232"></a>
#### 232. `BIAS20` — 20日乖离率

- 聚宽分类：动量类因子
- 快照 factor_id：`69a1896ac88dd727673bc01afe262f27`
- 更新时间：2021-02-23 13:28:05
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.026627；IR=-0.171097；多空年化=-0.116069；多空夏普=-0.843653；多空最大回撤=0.405646
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/69a1896ac88dd727673bc01afe262f27)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取20

<a id="factor-233"></a>
#### 233. `BIAS5` — 5日乖离率

- 聚宽分类：动量类因子
- 快照 factor_id：`f1bf14c399c179eb5aa8886f56e965d5`
- 更新时间：2021-02-23 13:12:32
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.023184；IR=-0.159066；多空年化=0.004028；多空夏普=-0.199179；多空最大回撤=0.332795
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f1bf14c399c179eb5aa8886f56e965d5)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取5

<a id="factor-234"></a>
#### 234. `BIAS60` — 60日乖离率

- 聚宽分类：动量类因子
- 快照 factor_id：`de21d3a7c084f87a4094b0280b404fc7`
- 更新时间：2021-02-23 12:22:55
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022879；IR=-0.142158；多空年化=-0.152191；多空夏普=-1.059612；多空最大回撤=0.439714
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/de21d3a7c084f87a4094b0280b404fc7)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （收盘价-收盘价的N日简单平均）/ 收盘价的N日简单平均*100，在此n取60

<a id="factor-235"></a>
#### 235. `bull_power` — 多头力道

- 聚宽分类：动量类因子
- 快照 factor_id：`dd145d59eccfd517306cd62ff09a4adf`
- 更新时间：2021-02-23 12:31:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.028987；IR=-0.194089；多空年化=-0.076383；多空夏普=-0.632632；多空最大回撤=0.322749
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/dd145d59eccfd517306cd62ff09a4adf)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> (最高价-EMA(close,13)) / close

<a id="factor-236"></a>
#### 236. `CCI10` — 10日顺势指标

- 聚宽分类：动量类因子
- 快照 factor_id：`582f8571d294ee29690d20277a12050d`
- 更新时间：2021-02-23 12:31:37
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019167；IR=-0.139959；多空年化=-0.028217；多空夏普=-0.441339；多空最大回撤=0.256552
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/582f8571d294ee29690d20277a12050d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=10

<a id="factor-237"></a>
#### 237. `CCI15` — 15日顺势指标

- 聚宽分类：动量类因子
- 快照 factor_id：`4fe6adc8e6396ffd039e0e902c49c59e`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021873；IR=-0.157895；多空年化=-0.044868；多空夏普=-0.542055；多空最大回撤=0.204745
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4fe6adc8e6396ffd039e0e902c49c59e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=15

<a id="factor-238"></a>
#### 238. `CCI20` — 20日顺势指标

- 聚宽分类：动量类因子
- 快照 factor_id：`b44fd47296a68031bd7db037dbd8cc17`
- 更新时间：2021-02-23 12:38:22
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021844；IR=-0.157342；多空年化=-0.063339；多空夏普=-0.638154；多空最大回撤=0.246148
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b44fd47296a68031bd7db037dbd8cc17)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=20

<a id="factor-239"></a>
#### 239. `CCI88` — 88日顺势指标

- 聚宽分类：动量类因子
- 快照 factor_id：`e896774afb27e7fbc04b75d274437433`
- 更新时间：2021-02-23 12:59:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.018173；IR=-0.131925；多空年化=-0.081293；多空夏普=-0.812899；多空最大回撤=0.313153
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/e896774afb27e7fbc04b75d274437433)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> CCI:=(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N)); TYP:=(HIGH+LOW+CLOSE)/3; N:=88

<a id="factor-240"></a>
#### 240. `CR20` — CR指标

- 聚宽分类：动量类因子
- 快照 factor_id：`4571be88e92c90884180560a3c424b83`
- 更新时间：2021-02-23 12:29:39
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021916；IR=-0.157109；多空年化=-0.099406；多空夏普=-0.898706；多空最大回撤=0.356682
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/4571be88e92c90884180560a3c424b83)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①中间价=1日前的最高价+最低价/2 ②上升值=今天的最高价-前一日的中间价（负值记0） ③下跌值=前一日的中间价-今天的最低价（负值记0） ④多方强度=20天的上升值的和，空方强度=20天的下跌值的和 ⑤CR=（多方强度÷空方强度）×100

<a id="factor-241"></a>
#### 241. `fifty_two_week_close_rank` — 当前价格处于过去1年股价的位置

- 聚宽分类：动量类因子
- 快照 factor_id：`d25c977883fe2a9f1e060b7e551a1d03`
- 更新时间：2021-02-23 13:33:41
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.007418；IR=0.058313；多空年化=-0.054227；多空夏普=-0.858061；多空最大回撤=0.236767
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/d25c977883fe2a9f1e060b7e551a1d03)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 取过去的250个交易日各股的收盘价时间序列，每只股票按照从大到小排列，并找出当日所在的位置

<a id="factor-242"></a>
#### 242. `MASS` — 梅斯线

- 聚宽分类：动量类因子
- 快照 factor_id：`872f0953bae81758bcf6d1eee3141cb8`
- 更新时间：2021-02-23 13:26:46
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.017401；IR=-0.160365；多空年化=-0.090969；多空夏普=-1.019847；多空最大回撤=0.303077
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/872f0953bae81758bcf6d1eee3141cb8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MASS(N1=9, N2=25, M=6)

<a id="factor-243"></a>
#### 243. `PLRC12` — 12日收盘价格与日期线性回归系数

- 聚宽分类：动量类因子
- 快照 factor_id：`3324bbe6ae17717bcebc37693f603fde`
- 更新时间：2021-02-23 12:35:57
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.019474；IR=-0.133756；多空年化=-0.105931；多空夏普=-0.864710；多空最大回撤=0.321665
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3324bbe6ae17717bcebc37693f603fde)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算 12 日收盘价格，与日期序号（1-12）的线性回归系数，(close / mean(close)) = beta * t + alpha

<a id="factor-244"></a>
#### 244. `PLRC24` — 24日收盘价格与日期线性回归系数

- 聚宽分类：动量类因子
- 快照 factor_id：`752fdd348302e4ba4d54fe81667eed77`
- 更新时间：2021-02-23 12:51:09
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.016061；IR=-0.108558；多空年化=-0.077366；多空夏普=-0.705784；多空最大回撤=0.301636
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/752fdd348302e4ba4d54fe81667eed77)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算 24 日收盘价格，与日期序号（1-24）的线性回归系数， (close / mean(close)) = beta * t + alpha

<a id="factor-245"></a>
#### 245. `PLRC6` — 6日收盘价格与日期线性回归系数

- 聚宽分类：动量类因子
- 快照 factor_id：`aa6faa216467f90386fd4d8b7a3af582`
- 更新时间：2021-02-23 13:02:21
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.020477；IR=-0.137635；多空年化=-0.062448；多空夏普=-0.584105；多空最大回撤=0.345884
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/aa6faa216467f90386fd4d8b7a3af582)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 计算 6 日收盘价格，与日期序号（1-6）的线性回归系数，(close / mean(close)) = beta * t + alpha

<a id="factor-246"></a>
#### 246. `Price1M` — 当前股价除以过去一个月股价均值再减1

- 聚宽分类：动量类因子
- 快照 factor_id：`9f142c8aa040b856a82c7a8347ae029a`
- 更新时间：2021-02-23 13:57:06
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.027110；IR=-0.173830；多空年化=-0.116472；多空夏普=-0.844789；多空最大回撤=0.405000
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9f142c8aa040b856a82c7a8347ae029a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 当日收盘价 / mean(过去一个月(21天)的收盘价) -1

<a id="factor-247"></a>
#### 247. `Price1Y` — 当前股价除以过去一年股价均值再减1

- 聚宽分类：动量类因子
- 快照 factor_id：`c4ee591d533630ceb8d42ae0d55a986f`
- 更新时间：2021-02-23 13:50:35
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006722；IR=-0.043590；多空年化=-0.015511；多空夏普=-0.352540；多空最大回撤=0.321916
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c4ee591d533630ceb8d42ae0d55a986f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 当日收盘价 / mean(过去一年(250天)的收盘价) -1

<a id="factor-248"></a>
#### 248. `Price3M` — 当前股价除以过去三个月股价均值再减1

- 聚宽分类：动量类因子
- 快照 factor_id：`55d32f8c80db52c3261c5a28e6d7e931`
- 更新时间：2021-02-23 12:29:48
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022847；IR=-0.142000；多空年化=-0.146097；多空夏普=-1.023680；多空最大回撤=0.426770
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/55d32f8c80db52c3261c5a28e6d7e931)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 当日收盘价 / mean(过去三个月(61天)的收盘价) -1

<a id="factor-249"></a>
#### 249. `Rank1M` — 1减去 过去一个月收益率排名与股票总数的比值

- 聚宽分类：动量类因子
- 快照 factor_id：`1d84304d2160302fcb890591833ec272`
- 更新时间：2021-02-23 12:24:28
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.023273；IR=0.151890；多空年化=0.093311；多空夏普=0.320720；多空最大回撤=0.172414
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1d84304d2160302fcb890591833ec272)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1-(Rank(个股20日收益) / 股票总数)

<a id="factor-250"></a>
#### 250. `ROC12` — 12日变动速率（Price Rate of Change）

- 聚宽分类：动量类因子
- 快照 factor_id：`98f670bc26aad1fbc4b7d22b72699d12`
- 更新时间：2021-02-23 13:51:47
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.023082；IR=-0.155180；多空年化=-0.103900；多空夏普=-0.792721；多空最大回撤=0.349341
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/98f670bc26aad1fbc4b7d22b72699d12)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①AX=今天的收盘价—12天前的收盘价 ②BX=12天前的收盘价 ③ROC=AX/BX*100

<a id="factor-251"></a>
#### 251. `ROC120` — 120日变动速率（Price Rate of Change）

- 聚宽分类：动量类因子
- 快照 factor_id：`11eeb752425e536d0566fbc9681aa765`
- 更新时间：2021-02-23 12:37:13
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.007120；IR=-0.047989；多空年化=0.015733；多空夏普=-0.152097；多空最大回撤=0.280509
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/11eeb752425e536d0566fbc9681aa765)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①AX=今天的收盘价—20天前的收盘价 ②BX=60天前的收盘价 ③ROC=AX/BX*100

<a id="factor-252"></a>
#### 252. `ROC20` — 20日变动速率（Price Rate of Change）

- 聚宽分类：动量类因子
- 快照 factor_id：`9ed42e7d820fb002d0b733725ddc5d56`
- 更新时间：2021-02-23 13:40:53
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.024311；IR=-0.160410；多空年化=-0.130485；多空夏普=-0.977245；多空最大回撤=0.402601
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/9ed42e7d820fb002d0b733725ddc5d56)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①AX=今天的收盘价—20天前的收盘价 ②BX=20天前的收盘价 ③ROC=AX/BX*100

<a id="factor-253"></a>
#### 253. `ROC6` — 6日变动速率（Price Rate of Change）

- 聚宽分类：动量类因子
- 快照 factor_id：`a558a8fbfa3d3366714bbae5046587d0`
- 更新时间：2021-02-23 13:34:11
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.025821；IR=-0.174135；多空年化=-0.122581；多空夏普=-0.926619；多空最大回撤=0.426075
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a558a8fbfa3d3366714bbae5046587d0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①AX=今天的收盘价—6天前的收盘价 ②BX=6天前的收盘价 ③ROC=AX/BX*100

<a id="factor-254"></a>
#### 254. `ROC60` — 60日变动速率（Price Rate of Change）

- 聚宽分类：动量类因子
- 快照 factor_id：`3ec9ddbdf7214e75f9fa2fe5cb096488`
- 更新时间：2021-02-23 13:26:47
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.015134；IR=-0.101432；多空年化=-0.123082；多空夏普=-0.980205；多空最大回撤=0.357565
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/3ec9ddbdf7214e75f9fa2fe5cb096488)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> ①AX=今天的收盘价—20天前的收盘价 ②BX=60天前的收盘价 ③ROC=AX/BX*100

<a id="factor-255"></a>
#### 255. `single_day_VPT` — 单日价量趋势

- 聚宽分类：动量类因子
- 快照 factor_id：`58a3a73e98b45561b5b268a6eb8e171f`
- 更新时间：2021-02-23 13:28:12
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021397；IR=-0.181252；多空年化=-0.058114；多空夏普=-0.728644；多空最大回撤=0.271875
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/58a3a73e98b45561b5b268a6eb8e171f)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> （今日收盘价 - 昨日收盘价）/ 昨日收盘价 * 当日成交量 # (复权方法为基于当日前复权)

<a id="factor-256"></a>
#### 256. `single_day_VPT_12` — 单日价量趋势12均值

- 聚宽分类：动量类因子
- 快照 factor_id：`f134c9392d138c4de57466ad30cc349e`
- 更新时间：2021-02-23 12:38:30
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.023248；IR=-0.208235；多空年化=-0.034045；多空夏普=-0.533717；多空最大回撤=0.136632
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f134c9392d138c4de57466ad30cc349e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MA(single_day_VPT, 12)

<a id="factor-257"></a>
#### 257. `single_day_VPT_6` — 单日价量趋势6日均值

- 聚宽分类：动量类因子
- 快照 factor_id：`a20fc97c26c125c86a6385a957ad3005`
- 更新时间：2021-02-23 12:45:23
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.024386；IR=-0.204623；多空年化=-0.123501；多空夏普=-1.186234；多空最大回撤=0.322270
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a20fc97c26c125c86a6385a957ad3005)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MA(single_day_VPT, 6)

<a id="factor-258"></a>
#### 258. `TRIX10` — 10日终极指标TRIX

- 聚宽分类：动量类因子
- 快照 factor_id：`f1991a0c2695c23f2229fc228a034e5c`
- 更新时间：2021-02-23 12:24:25
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.016774；IR=-0.109617；多空年化=-0.093320；多空夏普=-0.780916；多空最大回撤=0.333732
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f1991a0c2695c23f2229fc228a034e5c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MTR=收盘价的10日指数移动平均的10日指数移动平均的10日指数移动平均; TRIX=(MTR-1日前的MTR)/1日前的MTR*100

<a id="factor-259"></a>
#### 259. `TRIX5` — 5日终极指标TRIX

- 聚宽分类：动量类因子
- 快照 factor_id：`c9e0674171667d5a47ed72cc63976d61`
- 更新时间：2021-02-23 12:17:31
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.021594；IR=-0.142322；多空年化=-0.107828；多空夏普=-0.841399；多空最大回撤=0.345818
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c9e0674171667d5a47ed72cc63976d61)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> MTR=收盘价的5日指数移动平均的10日指数移动平均的5日指数移动平均; TRIX=(MTR-1日前的MTR)/1日前的MTR*100

<a id="factor-260"></a>
#### 260. `Volume1M` — 当前交易量相比过去1个月日均交易量 与过去过去20日日均收益率乘积

- 聚宽分类：动量类因子
- 快照 factor_id：`1ff0cc03bb74556bbe4f1c48a6155369`
- 更新时间：2021-02-23 13:35:26
- 产出时间：15:00
- 数据处理：中位数去极值 -> 行业市值对数中性化 -> zscore标准化
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.026397；IR=-0.181333；多空年化=-0.119876；多空夏普=-0.907106；多空最大回撤=0.405060
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1ff0cc03bb74556bbe4f1c48a6155369)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 当日交易量 / 过去20日交易量MEAN * 过去20日收益率MEAN

### 风险因子 - 新风格因子

<a id="factor-261"></a>
#### 261. `btop` — 市净率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`2c132ea3f9ea572d432fee557151798d`
- 更新时间：2024-03-28 11:37:47
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.032165；IR=0.133956；多空年化=-0.057023；多空夏普=-0.346236；多空最大回撤=0.531417
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2c132ea3f9ea572d432fee557151798d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1.00 * book_to_price

<a id="factor-262"></a>
#### 262. `dividend_yield_v2` — 分红收益率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`01e78600cbd3ea91acd377245ce8557a`
- 更新时间：2023-03-29 18:02:06
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.025883；IR=0.100470；多空年化=-0.119947；多空夏普=-0.526586；多空最大回撤=0.620477
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/01e78600cbd3ea91acd377245ce8557a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-263"></a>
#### 263. `divyild` — 分红因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`5746c92d9107303b6b46e21979da443c`
- 更新时间：2024-03-28 11:37:47
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.024396；IR=0.108850；多空年化=-0.038319；多空夏普=-0.296486；多空最大回撤=0.481151
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5746c92d9107303b6b46e21979da443c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.50 * dividend_to_price + 0.50 * analyst_predicted_dividend_to_price

<a id="factor-264"></a>
#### 264. `earnqlty` — 盈利质量因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`7720d76c933c9276db3caca393fa67ab`
- 更新时间：2024-03-28 11:37:47
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.011709；IR=0.111577；多空年化=-0.056241；多空夏普=-0.734567；多空最大回撤=0.372471
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7720d76c933c9276db3caca393fa67ab)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.60 * accruals_balance_sheet_version + 0.40 * accruals_cashflow_statement_version

<a id="factor-265"></a>
#### 265. `earnvar` — 盈利变动率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`bafc9a2370c530e0d20e64489cb92cef`
- 更新时间：2024-03-29 16:20:06
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.014574；IR=-0.115147；多空年化=0.072500；多空夏普=0.244584；多空最大回撤=0.247355
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/bafc9a2370c530e0d20e64489cb92cef)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.20 * variability_in_cashflows + 0.25 * variability_in_earnings + 0.20 * variability_in_sales + 0.35 * std_of_analyst_forecast_earning_to_price

<a id="factor-266"></a>
#### 266. `earnyild` — 收益因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`7470a1c70b741d0afeffdf18386681c7`
- 更新时间：2024-03-29 16:20:06
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.026753；IR=0.114168；多空年化=0.009190；多空夏普=-0.110378；多空最大回撤=0.462646
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/7470a1c70b741d0afeffdf18386681c7)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.10 * cash_earnings_to_price + 0.20 * enterprise_multiple + 0.50 * analyst_predicted_earnings_to_price + 0.20 * earnings_to_price

<a id="factor-267"></a>
#### 267. `financial_leverage` — 财务杠杆因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`52a3a44dc6254a366bdfd3ed75d99f1e`
- 更新时间：2024-03-29 16:20:06
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.009786；IR=0.072687；多空年化=-0.011172；多空夏普=-0.375181；多空最大回撤=0.211101
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/52a3a44dc6254a366bdfd3ed75d99f1e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.40 * debt_to_asset + 0.30 * book_lev + 0.30 * market_lev

<a id="factor-268"></a>
#### 268. `growth_v2` — 成长因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`72b5b15e3015ef23263d6b0377d82280`
- 更新时间：2023-03-29 18:03:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.006853；IR=-0.064283；多空年化=0.096582；多空夏普=0.414332；多空最大回撤=0.205960
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/72b5b15e3015ef23263d6b0377d82280)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-269"></a>
#### 269. `invsqlty` — 投资能力因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`505569b6cfc3726823750e281dd9662e`
- 更新时间：2024-03-29 16:20:14
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.015047；IR=0.105728；多空年化=-0.119720；多空夏普=-0.902470；多空最大回撤=0.518906
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/505569b6cfc3726823750e281dd9662e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.20 * capital_expenditure_growth + 0.40 * total_assets_growth_rate + 0.40 * issuance_growth

<a id="factor-270"></a>
#### 270. `liquidity_v2` — 流动性因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`0731c94b2a25859f9a6842fc2454f3b4`
- 更新时间：2023-03-29 18:12:42
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.033103；IR=-0.126227；多空年化=0.036254；多空夏普=-0.012406；多空最大回撤=0.459346
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0731c94b2a25859f9a6842fc2454f3b4)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-271"></a>
#### 271. `liquidty` — 流动性因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`c3dd1c0c372bd8af972f79872dc31a2a`
- 更新时间：2024-03-29 16:20:14
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.028418；IR=-0.115434；多空年化=0.019759；多空夏普=-0.070308；多空最大回撤=0.449655
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/c3dd1c0c372bd8af972f79872dc31a2a)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.25 * monthly_share_turnover + 0.25 * quarterly_share_turnover + 0.25 * annual_share_turnover + 0.25 * annualized_traded_value_ratio

<a id="factor-272"></a>
#### 272. `long_growth` — 长期成长因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`a200f71fd7aa4a74695d42d0c4e06758`
- 更新时间：2024-03-29 16:20:14
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.012279；IR=-0.085546；多空年化=0.156048；多空夏普=0.664474；多空最大回撤=0.284666
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a200f71fd7aa4a74695d42d0c4e06758)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.20 * sales_per_share_growth_rate + 0.70 * long_term_pred_earnings_growth + 0.10 * earnings_per_share_growth_rate

<a id="factor-273"></a>
#### 273. `ltrevrsl` — 长期反转因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`81003c9c7c9f16268da6547ab76285ab`
- 更新时间：2024-03-29 16:20:21
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.003523；IR=-0.030641；多空年化=-0.105034；多空夏普=-1.053688；多空最大回撤=0.375198
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/81003c9c7c9f16268da6547ab76285ab)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.46 * long_term_historical_alpha + 0.54 * long_term_relative_strength

<a id="factor-274"></a>
#### 274. `market_beta` — 市场波动率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`a7d8635103d30ff7d685569a7bd1ca91`
- 更新时间：2024-03-29 16:20:21
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.022987；IR=-0.074303；多空年化=0.129229；多空夏普=0.268237；多空最大回撤=0.415195
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a7d8635103d30ff7d685569a7bd1ca91)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1.00 * historical_beta

<a id="factor-275"></a>
#### 275. `market_size` — 市值规模因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`faf01cca46ce04626f3fbb8d8e51d0e8`
- 更新时间：2024-03-29 16:20:21
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.004234；IR=-0.022610；多空年化=-0.035090；多空夏普=-0.314481；多空最大回撤=0.406253
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/faf01cca46ce04626f3fbb8d8e51d0e8)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1.00 * log_of_market_capitalization

<a id="factor-276"></a>
#### 276. `midcap` — 中等市值因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`1eb6293baef6056144c91f36a520f3f0`
- 更新时间：2024-03-29 16:20:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.004234；IR=0.022610；多空年化=-0.019428；多空夏普=-0.248928；多空最大回撤=0.436298
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1eb6293baef6056144c91f36a520f3f0)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 1.00 * cube_of_size_exposure

<a id="factor-277"></a>
#### 277. `momentum_v2` — 动量因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`1ec042b4f0b8d777e8f82b66d109e93b`
- 更新时间：2023-03-29 18:04:57
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.010184；IR=-0.052066；多空年化=-0.045464；多空夏普=-0.383856；多空最大回撤=0.432136
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/1ec042b4f0b8d777e8f82b66d109e93b)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-278"></a>
#### 278. `profit` — 盈利能力因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`60121635155b5542cf1f312ced50ae3e`
- 更新时间：2024-03-29 16:20:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.009619；IR=-0.070327；多空年化=-0.005235；多空夏普=-0.358357；多空最大回撤=0.181477
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/60121635155b5542cf1f312ced50ae3e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.25 * asset_turnover + 0.25 * return_on_assets + 0.25 * gross_profitability_margin + 0.25 * gross_profitability

<a id="factor-279"></a>
#### 279. `quality_v2` — 质量因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`0a236909729e1bb4908a80ea5b067b4d`
- 更新时间：2023-03-29 18:09:01
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.000046；IR=-0.000400；多空年化=-0.087335；多空夏普=-1.021851；多空最大回撤=0.291900
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0a236909729e1bb4908a80ea5b067b4d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-280"></a>
#### 280. `relative_momentum` — 相对动量因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`5157c55dad733481578b2d1593396d6c`
- 更新时间：2024-03-29 16:20:29
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.008525；IR=0.036465；多空年化=0.057175；多空夏普=0.061930；多空最大回撤=0.448747
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/5157c55dad733481578b2d1593396d6c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.50 * historical_alpha + 0.50 * relative_strength_12_month

<a id="factor-281"></a>
#### 281. `resvol` — 残余波动率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`b4b5f584f15b2033513e216f76a9b3f7`
- 更新时间：2024-03-29 16:20:36
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.045038；IR=-0.152706；多空年化=-0.052007；多空夏普=-0.280826；多空最大回撤=0.458057
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/b4b5f584f15b2033513e216f76a9b3f7)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 0.50 * daily_std + 0.42 * historical_resid_sigma + 0.08 * cum_range

<a id="factor-282"></a>
#### 282. `sentiment_v2` — 情绪因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`0627b696fcda08ca677aeca3a5deff7e`
- 更新时间：2023-03-29 18:07:57
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.005712；IR=0.061784；多空年化=0.092008；多空夏普=0.458959；多空最大回撤=0.100250
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/0627b696fcda08ca677aeca3a5deff7e)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-283"></a>
#### 283. `size_v2` — 规模因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`2bf8be16c09803978f5f1b42b232e03c`
- 更新时间：2023-03-29 18:10:56
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.003865；IR=0.020629；多空年化=-0.022932；多空夏普=-0.263649；多空最大回撤=0.436298
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/2bf8be16c09803978f5f1b42b232e03c)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-284"></a>
#### 284. `value_v2` — 价值因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`f7f3e86142deabd4db76c02507ff7800`
- 更新时间：2023-03-29 18:06:25
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=0.029282；IR=0.138532；多空年化=-0.075196；多空夏普=-0.481159；多空最大回撤=0.528346
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/f7f3e86142deabd4db76c02507ff7800)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑

<a id="factor-285"></a>
#### 285. `volatility_v2` — 波动率因子

- 聚宽分类：风险因子 - 新风格因子
- 快照 factor_id：`a837a568a1393e97193a6357d5c9d97d`
- 更新时间：2023-03-29 18:02:48
- 产出时间：15:00
- 数据处理：无
- 默认参数/加权：加权方式为按市值加权
- 看板绩效：IC 均值=-0.035647；IR=-0.110949；多空年化=0.052761；多空夏普=0.036257；多空最大回撤=0.449484
- 聚宽详情页：[打开快照详情](https://www.joinquant.com/view/factorlib/detail/a837a568a1393e97193a6357d5c9d97d)
- AlphaFactor 实施状态：`待转换评估`
- 依赖字段：`待梳理`
- 目标表达式：`待编写`

计算逻辑：

> 聚宽公开详情未提供计算逻辑
