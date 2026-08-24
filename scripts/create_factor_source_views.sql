CREATE DATABASE IF NOT EXISTS ab_factor;

CREATE OR REPLACE VIEW ab_factor.stock_basic_factor_source AS
SELECT
    code,
    type
FROM baostock.bs_stock_basic;

CREATE OR REPLACE VIEW ab_factor.stock_daily_factor_source AS
SELECT
    toDate(k.trade_time) AS trade_time,
    k.code AS code,
    k.open AS open,
    k.high AS high,
    k.low AS low,
    k.close AS close,
    k.open * ifNull(a.backward_adj_factor, 1.0) AS open_adj,
    k.high * ifNull(a.backward_adj_factor, 1.0) AS high_adj,
    k.low * ifNull(a.backward_adj_factor, 1.0) AS low_adj,
    k.close * ifNull(a.backward_adj_factor, 1.0) AS close_adj,
    ifNull(a.backward_adj_factor, 1.0) AS backward_adj_factor,
    k.volume AS volume,
    k.amount AS amount,
    coalesce(s.preclose, toFloat64OrNull(b.preclose)) AS pre_close,
    coalesce(s.preclose, toFloat64OrNull(b.preclose)) AS preclose,
    coalesce(s.preclose, toFloat64OrNull(b.preclose))
        * ifNull(a.backward_adj_factor, 1.0) AS pre_close_adj,
    toFloat64OrNull(b.turn) AS turnover_rate,
    toFloat64OrNull(b.pct_chg) AS pct_chg,
    toFloat64OrNull(b.pe_ttm) AS pe,
    toFloat64OrNull(b.pb_mrq) AS pb,
    toUInt8(ifNull(b.is_st, '') IN ('1', 'true', 'True')) AS is_st,
    toUInt8(ifNull(b.tradestatus, '') = '0' OR ifNull(s.is_susp_sec, '') IN ('1', 'true', 'True')) AS is_suspended,
    toUInt8(ifNull(s.is_wd_sec, '') IN ('1', 'true', 'True')) AS is_wd_sec,
    toUInt8(ifNull(s.is_xr_sec, '') IN ('1', 'true', 'True')) AS is_xr_sec,
    toUInt8(endsWith(k.code, '.SH') AND startsWith(k.code, '688')) AS is_kcb,
    toUInt8(endsWith(k.code, '.SZ') AND startsWith(k.code, '300')) AS is_cyb,
    toUInt8(endsWith(k.code, '.BJ')) AS is_bjs,
    s.high_limited AS high_limited,
    s.low_limited AS low_limited,
    s.high_limited * ifNull(a.backward_adj_factor, 1.0) AS high_limited_adj,
    s.low_limited * ifNull(a.backward_adj_factor, 1.0) AS low_limited_adj
FROM starlight.ad_market_kline_daily AS k
ASOF LEFT JOIN (
    SELECT
        code AS adjustment_code,
        toDate(divid_operate_date) AS factor_date,
        toFloat64OrNull(nullIf(back_adjust_factor, '')) AS backward_adj_factor
    FROM baostock.bs_adjust_factor
    ORDER BY adjustment_code, factor_date
) AS a
    ON k.code = a.adjustment_code
   AND toDate(k.trade_time) >= a.factor_date
ANY LEFT JOIN starlight.ad_history_stock_status AS s
    ON k.code = s.market_code
   AND toDate(k.trade_time) = s.trade_date
ANY LEFT JOIN baostock.bs_daily_kline AS b
    ON k.code = b.code
   AND toDate(k.trade_time) = toDate(b.date);
