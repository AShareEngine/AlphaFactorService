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
    k.volume AS volume,
    k.amount AS amount,
    coalesce(s.preclose, b.preclose) AS pre_close,
    coalesce(s.preclose, b.preclose) AS preclose,
    b.turn AS turnover_rate,
    b.pct_chg AS pct_chg,
    b.pe_ttm AS pe,
    b.pb_mrq AS pb,
    toUInt8(ifNull(b.is_st, '') IN ('1', 'true', 'True')) AS is_st,
    toUInt8(ifNull(b.tradestatus, '') = '0' OR ifNull(s.is_susp_sec, '') IN ('1', 'true', 'True')) AS is_suspended,
    toUInt8(ifNull(s.is_wd_sec, '') IN ('1', 'true', 'True')) AS is_wd_sec,
    toUInt8(ifNull(s.is_xr_sec, '') IN ('1', 'true', 'True')) AS is_xr_sec,
    toUInt8(endsWith(k.code, '.SH') AND startsWith(k.code, '688')) AS is_kcb,
    toUInt8(endsWith(k.code, '.SZ') AND startsWith(k.code, '300')) AS is_cyb,
    toUInt8(endsWith(k.code, '.BJ')) AS is_bjs,
    s.high_limited AS high_limited,
    s.low_limited AS low_limited
FROM starlight.ad_market_kline_daily AS k
ANY LEFT JOIN starlight.ad_history_stock_status AS s
    ON k.code = s.market_code
   AND toDate(k.trade_time) = s.trade_date
ANY LEFT JOIN baostock.bs_daily_kline AS b
    ON k.code = b.code
   AND toDate(k.trade_time) = toDate(b.date);
