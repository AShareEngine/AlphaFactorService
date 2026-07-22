INSERT INTO ab_factor.factor_definitions
(
    factor_id,
    version,
    label,
    description,
    entity_type,
    category,
    group_name,
    output_type,
    frequency,
    required_fields,
    params_json,
    expression,
    enabled,
    created_at,
    updated_at
)
SELECT
    'mean_volume',
    toUInt32(1),
    'N日平均成交量',
    '计算指定窗口内的平均成交量。',
    'stock',
    '量价因子',
    'price_volume',
    'number',
    'daily',
    ['volume'],
    '{"window":20}',
    'Mean($volume, $window)',
    toUInt8(1),
    now(),
    now()
UNION ALL
SELECT 'mean_amount', toUInt32(1), 'N日平均成交额', '计算指定窗口内的平均成交额。', 'stock', '量价因子', 'price_volume', 'number', 'daily', ['amount'], '{"window":20}', 'Mean($amount, $window)', toUInt8(1), now(), now()
UNION ALL
SELECT 'mean_turnover_rate', toUInt32(1), 'N日平均换手率', '计算指定窗口内的平均换手率。', 'stock', '量价因子', 'price_volume', 'number', 'daily', ['turnover_rate'], '{"window":20}', 'Mean($turnover_rate, $window)', toUInt8(1), now(), now()
UNION ALL
SELECT 'period_return', toUInt32(1), 'N日涨跌幅', '计算指定窗口内的阶段涨跌幅。', 'stock', '量价因子', 'price_volume', 'number', 'daily', ['close'], '{"window":20}', 'PeriodReturn($close, $window)', toUInt8(1), now(), now()
UNION ALL
SELECT 'limit_up_count', toUInt32(1), 'N日涨停次数', '统计指定窗口内的涨停次数。', 'stock', '涨停因子', 'limit_up', 'number', 'daily', ['close', 'high_limited'], '{"window":20}', 'Sum(And(Gt($high_limited, 0), Ge($close, $high_limited)), $window)', toUInt8(1), now(), now()
UNION ALL
SELECT 'first_limit_up_window', toUInt32(1), 'N日首次涨停', '判断指定窗口内是否首次出现涨停。', 'stock', '涨停因子', 'limit_up', 'boolean', 'daily', ['close', 'high_limited'], '{"window":60}', 'FirstTrue(And(Gt($high_limited, 0), Ge($close, $high_limited)), $window)', toUInt8(1), now(), now()
UNION ALL
SELECT
    'stock_fear_proxy',
    toUInt32(1),
    '个股恐慌度',
    '基于20日收益波动、20日下行波动、5日跌幅和成交量放大构造的个股恐慌代理分数；数值越高表示恐慌越强，不代表期权隐含波动率。',
    'stock',
    '风险情绪因子',
    'risk_sentiment',
    'number',
    'daily',
    ['close', 'pct_chg', 'volume'],
    '{"vol_window":20,"return_window":5,"volume_window":20,"rv_weight":0.35,"downside_weight":0.3,"loss_weight":0.2,"volume_weight":0.15,"volume_scale":10}',
    '$rv_weight * Std($pct_chg, $vol_window) + $downside_weight * Power(Mean(Power(Less($pct_chg, 0), 2), $vol_window), 0.5) + $loss_weight * Greater(-100 * PeriodReturn($close, $return_window), 0) + $volume_weight * $volume_scale * Greater($volume / NullIf(Mean($volume, $volume_window), 0) - 1, 0)',
    toUInt8(1),
    now(),
    now();
