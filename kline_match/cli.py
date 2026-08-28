"""命令行：ingest / sync / match / status。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kline_match.config import db_path, load_universe
from kline_match.db import connect, status_rows
from kline_match.ingest import ingest_cli
from kline_match.match import format_table, run_match
from kline_match.timeframes import ms_to_utc, normalize_tf


def _split_csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kline_match",
        description="K 线形态匹配：本地主序列池检索最相似的历史窗口",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="初始 + 增量填充宇宙（含备份 venue）")
    p_ing.add_argument("--assets", help="逗号分隔资产，默认全部；BTC/ETH 在配置中优先")
    p_ing.add_argument("--tf", dest="tfs", help="逗号分隔周期，默认全部")

    p_sync = sub.add_parser("sync", help="只拉取 last_ts 之后的新 K 线")
    p_sync.add_argument("--assets", help="逗号分隔资产，默认全部")
    p_sync.add_argument("--tf", dest="tfs", help="逗号分隔周期，默认全部")

    p_m = sub.add_parser("match", help="用选定资产最近 N 根已收盘 K 线做 TOP10 匹配")
    p_m.add_argument("asset", help="规范资产名，如 BTC")
    p_m.add_argument("tf_pos", metavar="tf", help="周期 1H/4H/12H/1D")
    p_m.add_argument("-n", "--n", type=int, default=None, help="查询窗口长度，默认 30")
    p_m.add_argument("--tf", dest="tf_opt", default=None, help="覆盖位置参数中的周期")

    p_serve = sub.add_parser("serve", help="启动交易终端 HTTP 服务")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=18765)
    p_serve.add_argument("--no-boot-sync", action="store_true", help="启动时不自动 ingest/sync")

    sub.add_parser("status", help="各资产/周期行数与最后一根时间")
    return p


def _print_status(db_file: Path) -> int:
    conn = connect(db_file)
    try:
        rows = status_rows(conn)
    finally:
        conn.close()
    if not rows:
        print("数据库为空。请先运行: python -m kline_match ingest")
        return 0
    headers = (
        "asset",
        "venue",
        "tf",
        "pri",
        "symbol",
        "bars",
        "first_utc",
        "last_utc",
        "last_sync_utc",
    )
    table: list[tuple[str, ...]] = []
    for r in rows:
        table.append(
            (
                r["asset"],
                r["venue"],
                r["tf"],
                "1" if r["is_primary"] else "0",
                r["native_symbol"] or "",
                str(r["n_closed"] or 0),
                ms_to_utc(int(r["first_ts"])) if r["first_ts"] else "-",
                ms_to_utc(int(r["last_bar_ts"])) if r["last_bar_ts"] else "-",
                ms_to_utc(int(r["last_sync_at"])) if r["last_sync_at"] else "-",
            )
        )
    cols = list(zip(*([headers] + table)))
    widths = [max(len(x) for x in col) for col in cols]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(s.ljust(w) for s, w in zip(row, widths))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in table:
        print(fmt(row))
    total = sum(int(r[5]) for r in table)
    print(f"\nclosed bars total: {total}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_file = db_path()
    if args.cmd in {"ingest", "sync"}:
        mode = "full" if args.cmd == "ingest" else "incremental"
        tfs = [normalize_tf(x) for x in _split_csv(args.tfs)] if args.tfs else None
        return ingest_cli(db_file, mode=mode, assets=_split_csv(args.assets), tfs=tfs)
    if args.cmd == "status":
        return _print_status(db_file)
    if args.cmd == "match":
        tf = normalize_tf(args.tf_opt or args.tf_pos)
        conn = connect(db_file)
        try:
            bundle = run_match(conn, args.asset, tf, n=args.n)
        finally:
            conn.close()
        meta = bundle.query
        print(
            f"query {meta['asset']} {meta['tf']} N={meta['n']} "
            f"{meta['start_utc']} -> {meta['end_utc']} venue={meta['venue']}"
        )
        print("\n当前共振")
        print(format_table(bundle.resonance))
        print("\n历史类比")
        print(format_table(bundle.history))
        if meta.get("json_path"):
            print(f"\nwrote {meta['json_path']}")
        if meta.get("png_path"):
            print(f"wrote {meta['png_path']}")
        return 0
    if args.cmd == "serve":
        from kline_match.server import run_server

        run_server(host=args.host, port=args.port, boot_sync=not args.no_boot_sync)
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
