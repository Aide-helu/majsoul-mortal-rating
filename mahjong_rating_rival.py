"""雀魂对手分析: 分别汇总上家 / 对家 / 下家的跑谱与逐手统计.

用法:
    python mahjong_rating_rival.py --name "白马#" --games 20 --mode 玉

公共逻辑见 mahjong_core.py; 本脚本只负责对手分析的参数和概览排版,
不 import mahjong_rating.py, 两份入口可以各自独立运行.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from typing import Any

from mahjong_core import Game, MODES, dealer_lines, pct, run, yaochu_lines


def format_rival_overview(games: list[Game], nickname: str, uid: int,
                          position: str, mode_names: str) -> list[str]:
    n = len(games)
    rated = [g.rating for g in games if g.rating is not None]
    accs = [g.accordance for g in games if g.accordance is not None]
    deltas = [g.pt_delta for g in games if g.pt_delta is not None]
    rates = "/".join(f"{sum(g.rank == r for g in games) / n * 100:.1f}" for r in range(1, 5))
    lines = [
        f"{nickname} (UID {uid}) 的{position}",
        f"{mode_names}   最近 {n} 场对手汇总",
        "每场按相对座位选择一位对手",
        "",
        f"1~4位 %    {rates}",
        f"平均顺位    {statistics.fmean(g.rank for g in games):.3f}",
        f"平均点数    {statistics.fmean(g.score for g in games):.0f}",
        f"被飞率      {sum(g.score < 0 for g in games) / n * 100:.1f}%",
        "最近战绩 [" + "".join(str(g.rank) for g in sorted(games, key=lambda g: g.start_time)[-10:]) + "]",
        "",
        f"跑谱成功 {len(rated)}/{n} 局，失败 {n - len(rated)} 局",
        f"Rating 均 {statistics.fmean(rated):.1f}" if rated else "Rating 均 -",
        f"AI 一致率 均 {statistics.fmean(accs) * 100:.1f}%" if accs else "AI 一致率 均 -",
    ]
    if deltas:
        lines.append(f"pt 合计 {sum(deltas):+d} (均 {statistics.fmean(deltas):+.1f}/局)")

    hands = [g.hand_stats for g in games if g.hand_stats]
    if hands:
        def total(key: str) -> int:
            return sum(int(h.get(key) or 0) for h in hands)

        hora, ippatsu = total("hora"), total("ippatsu")
        riichi, riichi_win = total("riichi"), total("riichi_win")
        turns = total("riichi_turn_sum")
        oya_n, oya_sum = total("oya_n"), total("oya_shanten_sum")
        ko_n, ko_sum = total("ko_n"), total("ko_shanten_sum")
        tsumo_lost, tsumo_mangan = total("oya_tsumo_lost"), total("oya_tsumo_lost_mangan")
        lines += [
            "",
            f"一发率      {pct(ippatsu / hora) if hora else '-'} ({ippatsu}/{hora})",
            f"立直和了率  {pct(riichi_win / riichi) if riichi else '-'} ({riichi_win}/{riichi})",
            f"立直巡目    {turns / riichi:.2f}" if riichi else "立直巡目    -",
            f"起手向听    {(oya_sum + ko_sum) / (oya_n + ko_n):.2f} "
            f"(亲 {oya_sum / oya_n:.2f} / 子 {ko_sum / ko_n:.2f})"
            if oya_n and ko_n else "起手向听      -",
            f"被炸率      {pct(tsumo_mangan / tsumo_lost) if tsumo_lost else '-'} "
            f"({tsumo_mangan}/{tsumo_lost})",
        ]
    lines += dealer_lines(hands) + yaochu_lines(hands)
    lines += [f"pt 有效数据 {len(deltas)}/{n} 场", "", "统计范围：仅输入玩家这批牌谱", "各局对手可能不同，pt 从 0 累计", "段位及牌谱屋面板统计不适用"]
    return lines


def build_overview(games: list[Game], ov: dict[str, Any], position: str,
                   mode_names: str, uid: int, nickname: str) -> list[str]:
    """供 mahjong_core.run 回调; 对手分析不使用牌谱屋的 ov."""
    return format_rival_overview(games, nickname, uid, position, mode_names)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # 别让 Windows 控制台编码把程序搞崩
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="雀魂对手批量跑谱：上家/对家/下家")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--id", help="雀魂 UID")
    who.add_argument("--name", help="雀魂昵称")
    ap.add_argument("--games", type=int, default=20, help="跑最近多少局 (默认 20)")
    ap.add_argument("--mode", action="append", default=None,
                    help=f"场次类型, 可重复使用: {' / '.join(MODES)} (默认 玉)")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="同时开几个页面提交 (默认 3; 实测调到 8 以上人机验证会直接全挂, 别贪多)")
    ap.add_argument("--out", default="rating_out", help="输出根目录，按玩家昵称分类 (默认 rating_out)")
    ap.add_argument("--no-show", action="store_true", help="跑完不自动弹出图片窗口")
    ap.add_argument("--fresh", action="store_true",
                    help="连跑谱缓存一起清掉, 全部重新跑 (默认保留缓存以便断点续跑)")
    args = ap.parse_args()
    args.mode = args.mode or ["玉"]
    if args.games < 1 or args.concurrency < 1:
        ap.error("--games 和 --concurrency 必须为正整数")
    try:
        return asyncio.run(run(args, rival=True, overview_fn=build_overview))
    except KeyboardInterrupt:
        print("\n已中断 (进度保存在 results.json, 重跑会自动续上)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
