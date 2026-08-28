# WAVE 波形终端

专业 K 线形态匹配交易终端。浏览器里是工作台，不是落地页。后端用本地 SQLite 主序列池，公开 REST 拉 Binance / OKX / Gate 行情，**不需要 API Key**。

## 匹配定义（不要改）

同一周期 internally 检索（`1H` `4H` `12H` `1D`，永不混周期）。

- **查询窗口**：选定主序列上一段已收盘 K 线。默认最近 N 根（N=30），也可在图上框选。
- **表示**：收盘价路径的 z-score（价格水平与振幅无关）。
- **相似度**：Pearson `r`（MASS / FFT）。`score = max(0, r) * 100`。
- **当前共振**：其它主序列、与查询窗口**时间戳完全相同**的那一段。跳过查询资产本身。
- **历史类比**：全部主序列上与查询 `[start, end]` **无时间重叠** 的窗口；同资产 NMS（重叠 > 50% 留更高 `r`），取 TOP10。排除查询窗口自身。
- **不**抬高 BTC/ETH 分数。它们只是宇宙里的主序列。

主序列：加密货币 = Binance USDT 现货；黄金 = OKX `XAUT-USDT`；美股代币 = Gate `NVDAX_USDT` 等。宇宙里不包含 NAS100 / SPX500 / US30。

未收盘 K 线不会写成 `is_closed=1`。匹配只读已收盘。

## 本地运行

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
npm run build          # 构建前端
python -m kline_match ingest   # 若 data/kline_match.db 为空
python -m kline_match serve --host 127.0.0.1 --port 18765
```

浏览器打开 `http://127.0.0.1:18765`。

开发热更新（API 18765 + Vite 41789）：

```bash
pip install -e ".[dev]"
npm --prefix web install
npm run dev
```

打开 `http://127.0.0.1:41789`。Vite 把 `/api` 代理到 18765。

Docker：

```bash
docker compose up --build
```

数据卷挂在 `kline-data`，SQLite 可持久化。进程启动时：库空则全量 ingest，否则增量 sync 已收盘 K 线。

## CLI（后端内部）

```bash
python -m kline_match ingest
python -m kline_match sync
python -m kline_match match BTC 1H --n 30
python -m kline_match status
python -m kline_match serve
```

## HTTP API

- `GET /api/universe?tf=1H`
- `GET /api/klines?asset=BTC&tf=1H&start_ts=&end_ts=&pad_after=0`  (`pad_after` = bars after `end_ts`)
- `POST /api/match`  `{ "asset":"BTC", "tf":"1H", "n":30, "start_ts":?, "end_ts":? }`
- `GET /api/status`

Binance 若 `api.binance.com` 返回 451，自动改走 `https://data-api.binance.vision`。接口失败会记入报告/HTTP 错误，**不会编造 OHLC**。

## 测试

```bash
pytest
```

## 上线

本仓库是「FastAPI + SQLite + 静态前端」，需要**常驻进程和磁盘**。Vercel 无服务器函数不适合 ingest 与 SQLite。

推荐：

1. **Fly.io / Railway / Render**：挂 volume，跑 `docker compose` 同一镜像，开放 18765。
2. Cursor Origin 若已连接 Vercel：只能托管静态前端，匹配 API 仍需独立常驻服务。请到 [Cursor Get started](https://cursor.com/codebase/get-started) 按文档把 Origin 仓库连上部署平台，并给该服务一块持久盘。

没有平台 token 时，用上面的 Docker / `npm run dev` 在本机或自有 VPS 运行即可。
