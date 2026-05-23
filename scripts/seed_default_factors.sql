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
SELECT 'first_limit_up_window', toUInt32(1), 'N日首次涨停', '判断指定窗口内是否首次出现涨停。', 'stock', '涨停因子', 'limit_up', 'boolean', 'daily', ['close', 'high_limited'], '{"window":60}', 'FirstTrue(And(Gt($high_limited, 0), Ge($close, $high_limited)), $window)', toUInt8(1), now(), now();
