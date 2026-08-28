# 本地数据目录

运行时 SQLite 库路径：`data/kline_match.db`（已 gitignore）。

## 表结构

```sql
CREATE TABLE bars (
  asset TEXT NOT NULL,          -- 规范名: BTC, ETH, XAU, NVDA, NAS100
  venue TEXT NOT NULL,          -- binance | okx | gate
  tf TEXT NOT NULL,             -- 1H | 4H | 12H | 1D
  ts INTEGER NOT NULL,          -- K 线开盘时间 UTC epoch 毫秒
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (asset, venue, tf, ts)
);
CREATE INDEX idx_bars_primary_scan ON bars (tf, asset, ts);

CREATE TABLE series (
  asset TEXT NOT NULL,
  venue TEXT NOT NULL,
  tf TEXT NOT NULL,
  native_symbol TEXT NOT NULL,  -- BTCUSDT / BTC-USDT / BTC_USDT
  is_primary INTEGER NOT NULL,  -- 1 = 用于匹配
  last_ts INTEGER,
  last_sync_at INTEGER,
  PRIMARY KEY (asset, venue, tf)
);
```

匹配只读取 `series.is_primary = 1` 且 `bars.is_closed = 1` 的序列。同一资产在 Binance / OKX / Gate 上视为同一规范资产，主序列只有一条。
