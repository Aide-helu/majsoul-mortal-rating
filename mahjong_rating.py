"""雀魂自家分析: 批量跑谱 + rating/pt 折线图 + 概览.

用法:
    python mahjong_rating.py --id 12345678 --games 20 --mode 玉
    python mahjong_rating.py --name 示例玩家 --games 30 --mode 玉 --mode 玉东

公共逻辑见 mahjong_core.py; 本脚本只负责自家分析的参数和概览排版.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from typing import Any

from mahjong_core import (Game, MODES, ago, dealer_lines, disp_len, level_info, pct,
                          run, yaochu_lines)


def format_overview(ov: dict[str, Any], games: list[Game], mode_names: str, uid: int) -> list[str]:
    """拼出概览文本: 玩家信息 + 「最近 N 场 / 总体」两列完整统计 + 安定段."""
    stats = ov.get("all") or {}
    recent = ov.get("recent") or {}
    all_ext = ov.get("allExt") or {}
    recent_ext = ov.get("recentExt") or {}
    now_s = int((ov.get("now") or time.time() * 1000) / 1000)

    nick = stats.get("nickname") or str(uid)
    lv_name, lv_cap = level_info((stats.get("level") or {}).get("id"))
    lv_score = (stats.get("level") or {}).get("score")
    mx_name, mx_cap = level_info((stats.get("max_level") or {}).get("id"))
    mx_score = (stats.get("max_level") or {}).get("score")

    n_recent = recent.get("count") or len(games)
    col_a, col_b = f"最近 {n_recent} 场", "总体"
    w_label, w_a = 12, 26

    def pad(s: str, width: int) -> str:
        return s + " " * max(0, width - disp_len(s))

    def rates(d: dict[str, Any]) -> str:
        rr = d.get("rank_rates") or []
        return "/".join(f"{x * 100:.1f}" for x in rr[:4]) if rr else "-"

    def val(d: dict[str, Any], key: str, fmt: str = "{:.1f}") -> str:
        v = d.get(key)
        return fmt.format(v) if isinstance(v, (int, float)) else "-"

    def cnt(key: str) -> str:
        return str((ov.get(key) or {}).get("count", "-"))

    def row(label: str, a: str, b: str) -> str:
        return pad(label, w_label) + pad(a, w_a) + b

    rows: list[tuple[str, str, str]] = [
        ("场数", str(recent.get("count", "-")), str(stats.get("count", "-"))),
        ("1~4位 %", rates(recent), rates(stats)),
        ("平均顺位", val(recent, "avg_rank", "{:.3f}"), val(stats, "avg_rank", "{:.3f}")),
        ("和牌率", pct(recent_ext.get("和牌率")), pct(all_ext.get("和牌率"))),
        ("放铳率", pct(recent_ext.get("放铳率")), pct(all_ext.get("放铳率"))),
        ("自摸率", pct(recent_ext.get("自摸率")), pct(all_ext.get("自摸率"))),
        ("默听率", pct(recent_ext.get("默听率")), pct(all_ext.get("默听率"))),
        ("流局率", pct(recent_ext.get("流局率")), pct(all_ext.get("流局率"))),
        ("流听率", pct(recent_ext.get("流听率")), pct(all_ext.get("流听率"))),
        ("副露率", pct(recent_ext.get("副露率")), pct(all_ext.get("副露率"))),
        ("立直率", pct(recent_ext.get("立直率")), pct(all_ext.get("立直率"))),
        ("和了巡数", val(recent_ext, "和了巡数", "{:.2f}"), val(all_ext, "和了巡数", "{:.2f}")),
        ("平均打点", val(recent_ext, "平均打点", "{:.0f}"), val(all_ext, "平均打点", "{:.0f}")),
        ("平均铳点", val(recent_ext, "平均铳点", "{:.0f}"), val(all_ext, "平均铳点", "{:.0f}")),
        ("被飞率", pct(recent.get("negative_rate")), pct(stats.get("negative_rate"))),
    ]

    rated = [g.rating for g in games if g.rating is not None]
    accs = [g.accordance for g in games if g.accordance is not None]
    deltas = [g.pt_delta for g in games if g.pt_delta is not None]

    lines = [
        f"{nick} (UID {uid})",
        f"{lv_name} {lv_score}/{lv_cap}   最高段位 {mx_name} {mx_score}/{mx_cap}",
        f"{mode_names}   对战数 {stats.get('count', '-')}   "
        f"近1日/周/月 {cnt('d1')}/{cnt('d7')}/{cnt('d30')}",
        f"最近对战 {ago(now_s, games[0].start_time) if games else '-'}   "
        "最近战绩 ["
        + "".join(str(g.rank) for g in sorted(games, key=lambda g: g.start_time)[-10:]) + "]",
        "",
        pad("", w_label) + pad(col_a, w_a) + col_b,
        pad("-" * 10, w_label) + pad("-" * 18, w_a) + "-" * 12,
    ]
    lines += [row(label, a, b) for label, a, b in rows]
    lines.append("")
    if rated:
        n_fail = len(games) - len(rated)
        tail = f"，失败 {n_fail} 局(已跳过)" if n_fail else ""
        lines.append(f"跑谱成功 {len(rated)}/{len(games)} 局{tail}")
        lines.append(f"Rating 均 {statistics.fmean(rated):.1f}")
    if accs:
        lines.append(f"AI 一致率 均 {statistics.fmean(accs) * 100:.1f}%")
    if deltas:
        lines.append(f"pt 合计 {sum(deltas):+d} (均 {statistics.fmean(deltas):+.1f}/局)")
    hands = [g.hand_stats for g in games if g.hand_stats]
    hlines = dealer_lines(hands) + yaochu_lines(hands)
    if hlines:
        lines.append("")
        lines += hlines
    lines.append("")
    lines += stability_lines(deltas, lv_cap, lv_score, lv_name)
    return lines


def stability_lines(deltas: list[int], cap: int, score: int | None, lv_name: str) -> list[str]:
    """安定段 / 预计升段 (按最近 N 局实测 pt 的简易估算, 与牌谱屋统计口径不同)."""
    if not deltas:
        return ["安定段    -"]
    avg = statistics.fmean(deltas)
    if avg > 0.5:
        trend = "走势向上"
    elif avg < -0.5:
        trend = "走势向下, 注意掉段"
    else:
        trend = "基本持平"
    out = [f"安定段    {lv_name}", f"近{len(deltas)}局均 {avg:+.1f}pt/局, {trend}"]
    if avg > 0 and isinstance(score, int) and cap:
        need = max(0, cap - score)
        out.append(f"预计 {int(need / avg) + 1} 战后升段" if need else "已满段分")
    return out


def build_overview(games: list[Game], ov: dict[str, Any], position: str,
                   mode_names: str, uid: int, nickname: str) -> list[str]:
    """供 mahjong_core.run 回调; 自家分析不使用 position/nickname."""
    return format_overview(ov, games, mode_names, uid)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # 别让 Windows 控制台编码把程序搞崩
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="雀魂批量跑谱 + rating/pt 折线图 (Mortal 4.1b)")
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
        return asyncio.run(run(args, rival=False, overview_fn=build_overview))
    except KeyboardInterrupt:
        print("\n已中断 (进度保存在 results.json, 重跑会自动续上)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
