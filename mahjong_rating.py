"""雀魂 rating 批量跑谱 + 折线图工具.

流程:
  1. 从雀魂牌谱屋 (amae-koromo) 取该玩家最近的对局列表 (可按场次类型筛选) + 一份战绩概览
  2. 逐局提交到 mjai.ekyu.moe, 用 Mortal 4.1b 跑谱, 取回分数(rating) 与 AI 一致率
  3. 输出 CSV 明细 + PNG 图 (上图 rating/AI一致率, 下图 每局 pt 与段位分走势)

用法:
    python mahjong_rating.py --id 12345678 --games 20 --mode 玉
    python mahjong_rating.py --name 示例玩家 --games 30 --mode 玉 --mode 玉东

依赖: requirements.txt (playwright + matplotlib), 以及本机安装的 Edge / Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import pathlib
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

AMA_CHART = "https://amae-koromo.sapk.ch"
AMA_API = "https://5-data.amae-koromo.com/api/v2/pl4"
MJAI_FORM = "https://mjai.ekyu.moe/zh-cn.html"
MJAI_REPORT = "https://mjai.ekyu.moe/report/{task}.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

EARLIEST_MS = 1262304000000  # 2010-01-01, 牌谱屋数据起点
FUTURE_MS = 4102444800000    # 2100-01-01

# GameMode 常量, 取自 amae-koromo: src/data/types/gameMode.ts
MODES: dict[str, int] = {
    "王座": 16, "玉": 12, "金": 9,
    "王东": 15, "玉东": 11, "金东": 8,
    "三王座": 22, "三玉": 24, "三金": 26,
    "三王东": 21, "三玉东": 23, "三金东": 25,
}
MODE_BY_ID = {v: k for k, v in MODES.items()}
# 概览文字里用的完整场次名
MODE_FULL = {16: "王座之间", 12: "玉之间", 9: "金之间", 15: "王东", 11: "玉东", 8: "金东",
             22: "三王座", 24: "三玉之间", 26: "三金之间", 21: "三王东", 23: "三玉东",
             25: "三金东"}

# 段位: 取自 amae-koromo: src/data/types/level.ts 的 LEVEL_MAX_POINTS
# 索引 = (大段位 - 1) * 3 + (小段位 - 1); 雀豪1 -> 9 -> 2800, 雀豪3 -> 11 -> 3600
LEVEL_MAX_POINTS = [20, 80, 200, 600, 800, 1000, 1200, 1400, 2000, 2800, 3200, 3600, 4000, 6000, 9000]
KONTEN_MAX_POINTS = 2000
RANK_SHORT = {1: "初心", 2: "雀士", 3: "雀杰", 4: "豪", 5: "圣", 6: "魂天", 7: "魂天"}

MODEL_TAG = "4.1b"
MAX_ATTEMPTS = 3  # 一局最多提交 3 次(每次失败后等 25/50s 再试); 整轮跑完还会统一重试一轮, 仍失败就转人工

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def level_info(level_id: int | None) -> tuple[str, int]:
    """段位 id -> (名称, 该段位分数上限), 例如 10401 -> ("豪1", 2800)."""
    if not level_id:
        return "-", 0
    real = level_id % 10000
    major, minor = real // 100, real % 100
    name = RANK_SHORT.get(major, f"?{major}")
    if major >= 6:
        return name, KONTEN_MAX_POINTS
    idx = (major - 1) * 3 + (minor - 1)
    cap = LEVEL_MAX_POINTS[idx] if 0 <= idx < len(LEVEL_MAX_POINTS) else KONTEN_MAX_POINTS
    return f"{name}{minor}", cap


# --------------------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------------------


@dataclass
class Game:
    uuid: str
    mode_id: int
    start_time: int
    seat: int
    rank: int
    score: int
    record_url: str
    pt_delta: int | None = None       # 该局段位分变化 (±pt)
    rating: float | None = None       # 0-100, 页面上显示的"分数"
    accordance: float | None = None   # 0-1, AI 一致率
    accordance_detail: str = ""       # 形如 "34/45"
    task_id: str = ""                 # mjai 任务号, 存下来以便中断后直接取结果
    error: str = ""
    player_id: int | None = None
    nickname: str = ""
    position: str = ""                 # 相对于输入玩家的上家/对家/下家
    hand_stats: dict[str, int] | None = None  # 逐手统计, 见 parse_hand_stats()

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.start_time))


# --------------------------------------------------------------------------------------
# 启动一个"用户态"浏览器, 再用 CDP 接管
#
# 说明: Playwright 自己 launch 的浏览器会被 Cloudflare Turnstile 判定为自动化环境,
# 提交按钮会一直 disabled (拿不到 token); 由我们 Popen 启动、再用 CDP 接进去的浏览器
# 没有自动化启动标志, 验证码可以静默通过.
# --------------------------------------------------------------------------------------


def find_browser() -> str:
    for path in BROWSERS:
        if pathlib.Path(path).exists():
            return path
    raise SystemExit("找不到 Edge/Chrome, 请安装其中之一或修改脚本里的 BROWSERS 列表")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_browser(exe: str, port: int, profile: pathlib.Path,
                   wait_s: float) -> subprocess.Popen | None:
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",              # 别让 Edge 往新 profile 里塞推荐扩展(比如 AdGuard)
        # 关键: 不让浏览器节流后台标签页 —— 否则后台标签里的 Cloudflare 验证根本不跑,
        # 表现就是"只有当前看着的那个页面在转, 其它都在空等"
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,CalculateNativeWinOcclusion",
        "--hide-crash-restore-bubble",       # 不要弹"Edge 意外关闭, 是否还原页面"
        "about:blank",
    ])
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return proc
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:  # 进程已经退了
                return None
            time.sleep(0.4)
    return proc  # 进程还在, 但调试端口没起来


def launch_browser(exe: str, port: int, profile: pathlib.Path) -> subprocess.Popen:
    """
    启动浏览器.

    注意: profile 被占用时**绝不**去杀别人的进程 —— 那很可能是另一个正在跑的实例
    (或被占用的窗口), 杀了会让对方跑到一半全失败. 这里改成换一个备用 profile 重试.
    """
    proc = _spawn_browser(exe, port, profile, 35)
    if proc is None:
        alt = profile.with_name(f"{profile.name}_{int(time.time()) % 100000}")
        print(f"      (!) {profile.name} 正被占用 (上次没退干净或有另一个窗口在跑)")
        print(f"          不动别人的进程, 改用备用目录 {alt.name} 启动")
        proc = _spawn_browser(exe, port, alt, 35)
    if proc is None:
        raise SystemExit("浏览器启动失败。请手动关掉本程序遗留的 Edge/Chrome 窗口后再跑一次")
    return proc


def close_browser(proc: subprocess.Popen | None) -> None:
    """关掉自己启动的浏览器(连子进程一起), 免得下次启动被占着 profile."""
    if proc is None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=30)
    else:
        proc.terminate()


# --------------------------------------------------------------------------------------
# 阶段一: 牌谱屋
# --------------------------------------------------------------------------------------


async def resolve_player(page, ident: str, mode_ids: list[int]) -> tuple[int, str]:
    """UID 或昵称 -> (account_id, nickname)."""
    if "amae-koromo" not in page.url:  # 在 about:blank 上发不出跨域 fetch
        await page.goto(AMA_CHART, wait_until="domcontentloaded", timeout=60_000)
    if ident.isdigit():
        uid = int(ident)
        for mid in list(dict.fromkeys(mode_ids + [12, 9])):  # player_stats 必须带 mode
            name = await page.evaluate(
                """async ([api, uid, mid]) => {
                    const r = await fetch(
                        `${api}/player_stats/${uid}/1262304000000/4102444800000?mode=${mid}`,
                        {cache: 'no-store'});
                    if (!r.ok) return '';
                    const j = await r.json();
                    return (j && j.nickname) || '';
                }""",
                [AMA_API, uid, mid],
            )
            if name:
                return uid, name
        return uid, str(uid)

    players = await page.evaluate(
        """async ([api, term]) => {
            const r = await fetch(`${api}/search_player/${encodeURIComponent(term)}`);
            return await r.json();
        }""",
        [AMA_API, ident],
    )
    if not players:
        raise SystemExit(f"没有找到昵称 {ident!r} 的玩家")
    best = max(players, key=lambda p: p.get("latest_timestamp", 0))
    return int(best["id"]), best["nickname"]


async def fetch_records(page, uid: int, mode_ids: list[int], want: int) -> list[dict[str, Any]]:
    """打开牌谱屋玩家页, 拦下页面自己发出的 player_records 响应."""
    collected: dict[str, dict[str, Any]] = {}

    async def on_response(resp):
        if "player_records" not in resp.url:
            return
        try:
            data = await resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("uuid"):
                    collected[item["uuid"]] = item

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    for mid in mode_ids:
        def have() -> int:
            return len([r for r in collected.values() if r.get("modeId") == mid])

        if have() >= want:
            continue
        try:
            await page.goto(f"{AMA_CHART}/player/{uid}/{mid}",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! 打开场次 {MODE_BY_ID.get(mid, mid)} 页失败: {exc}", file=sys.stderr)
            continue
        last_n, idle = -1, 0
        for _ in range(25):
            await page.wait_for_timeout(1_000)
            n = have()
            if n >= want:
                break
            idle = idle + 1 if n == last_n else 0
            last_n = n
            if n and idle >= 3:  # 数据到手且连着 3 秒没动静, 就是加载完了
                break

        # 牌谱屋一次只给 150 条, 想要更多就往下滚, 让页面自己加载更早的对局
        stagnant = 0
        while have() < want and stagnant < 6:
            before = have()
            try:
                await page.evaluate(
                    "() => { window.scrollTo(0, document.body.scrollHeight);"
                    " const el = document.scrollingElement; if (el) el.scrollTop = el.scrollHeight; }")
            except Exception:  # noqa: BLE001
                break
            await page.wait_for_timeout(1_800)
            if have() == before:
                stagnant += 1
            else:
                stagnant = 0
        if have() < want:
            print(f"      (牌谱屋只给到 {have()} 局, 要 {want} 局的话拿不到更多了)")

    return sorted(collected.values(), key=lambda r: r["startTime"], reverse=True)[:want]


async def fetch_overview(page, uid: int, mode_ids: list[int],
                         since_ms: int) -> dict[str, Any]:
    """拉玩家概览: 总体统计 + 最近这批对局(从 since_ms 起)的统计."""
    modes = ".".join(str(m) for m in mode_ids)
    return await page.evaluate(
        """async ([api, uid, modes, t0, t1, since]) => {
            const get = async (url) => {
                try {
                    const r = await fetch(url, {cache: 'no-store'});
                    return r.ok ? await r.json() : null;
                } catch (e) { return null; }
            };
            const now = Date.now();
            const day = 86400000;
            const stats = (s, e) => `${api}/player_stats/${uid}/${s}/${e}?mode=${modes}`;
            const ext = (s, e) => `${api}/player_extended_stats/${uid}/${s}/${e}?mode=${modes}`;
            const [all, allExt, recent, recentExt, d1, d7, d30] = await Promise.all([
                get(stats(t0, t1)),
                get(ext(t0, t1)),
                get(stats(since, t1)),
                get(ext(since, t1)),
                get(stats(now - day, now)),
                get(stats(now - 7 * day, now)),
                get(stats(now - 30 * day, now)),
            ]);
            return {all, allExt, recent, recentExt, d1, d7, d30, now};
        }""",
        [AMA_API, uid, modes, EARLIEST_MS, FUTURE_MS, since_ms],
    )


RIVAL_POSITIONS = {"上家": 3, "对家": 2, "下家": 1}


def to_game(raw: dict[str, Any], uid: int) -> Game | None:
    players = raw.get("players") or []
    seat = next((i for i, p in enumerate(players) if p.get("accountId") == uid), None)
    return game_at_seat(raw, seat, uid) if seat is not None else None


def game_at_seat(raw: dict[str, Any], seat: int, record_uid: int,
                 position: str = "") -> Game:
    """players 按起家东南西北排列；牌谱链接保留输入玩家的访问视角，跑谱按 seat 选人。"""
    players = raw["players"]
    player = players[seat]
    score = player.get("score", 0)
    rank = 1 + sum(1 for i, p in enumerate(players)
                   if p.get("score", 0) > score or (p.get("score", 0) == score and i < seat))
    return Game(
        uuid=raw["uuid"], mode_id=raw.get("modeId", 0),
        start_time=raw.get("startTime", 0), seat=seat, rank=rank, score=score,
        record_url=f"https://game.maj-soul.com/1/?paipu={raw['uuid']}_a{record_uid}",
        pt_delta=player.get("gradingScore"), player_id=player.get("accountId"),
        nickname=player.get("nickname", ""), position=position,
    )


def rival_games(raws: list[dict[str, Any]], uid: int) -> dict[str, list[Game]]:
    """同一批四人牌谱按相对于输入玩家的位置汇总，位置不随庄家轮转改变。"""
    groups: dict[str, list[Game]] = {position: [] for position in RIVAL_POSITIONS}
    for raw in raws:
        players = raw.get("players") or []
        own_seat = next((i for i, p in enumerate(players) if p.get("accountId") == uid), None)
        if own_seat is None:
            continue
        if len(players) != 4:
            raise ValueError("对手分析只支持四人对局，无法生成上家/对家/下家三份结果")
        for position, offset in RIVAL_POSITIONS.items():
            groups[position].append(game_at_seat(raw, (own_seat + offset) % 4, uid, position))
    return groups


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def ago(now_s: int, then_s: int) -> str:
    d = max(0, now_s - then_s)
    if d < 3600:
        return f"{d // 60}分前"
    if d < 86400:
        return f"{d // 3600}时前"
    return f"{d // 86400}日前"


def dealer_lines(hands: list[dict[str, int]]) -> list[str]:
    """做庄时的立直率/和了率, 以及同一局闲家 9 巡内的立直率/自摸率."""
    if not hands:
        return []

    def total(key: str) -> int:
        return sum(int(h.get(key) or 0) for h in hands)

    def rate(num: int, den: int) -> str:
        return pct(num / den) if den else "-"

    kyoku = total("oya_kyoku")
    rival_n = total("oya_rival_n")
    if not kyoku:
        return ["庄家统计    -", "闲家9巡内    -"]
    oya_riichi, oya_hora = total("oya_riichi"), total("oya_hora")
    rival_riichi9, rival_tsumo9 = total("oya_rival_riichi9"), total("oya_rival_tsumo9")
    return [
        f"庄家        亲局 {kyoku}   立直率 {rate(oya_riichi, kyoku)} ({oya_riichi}/{kyoku})"
        f"   和了率 {rate(oya_hora, kyoku)} ({oya_hora}/{kyoku})",
        f"闲家9巡内   立直率 {rate(rival_riichi9, rival_n)} ({rival_riichi9}/{rival_n})"
        f"   自摸率 {rate(rival_tsumo9, rival_n)} ({rival_tsumo9}/{rival_n})",
    ]


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
    dlines = dealer_lines(hands)
    if dlines:
        lines.append("")
        lines += dlines
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


def disp_len(s: str) -> int:
    """中文按 2 列宽计算, 让边框对得齐."""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def print_overview(lines: list[str]) -> None:
    width = max(disp_len(x) for x in lines) + 2
    bar = "+" + "-" * (width + 2) + "+"
    print("    " + bar)
    for ln in lines:
        print("    | " + ln + " " * (width - disp_len(ln)) + " |")
    print("    " + bar)


# --------------------------------------------------------------------------------------
# 阶段二: mjai.ekyu.moe
# --------------------------------------------------------------------------------------


async def click_turnstile(page) -> bool:
    """
    Cloudflare 偶尔会把隐形验证升级成可见的「验证您是真人」复选框.
    这时脚本自己点一下(等同手动点), 点到了返回 True.
    """
    try:
        frames = [f for f in page.frames if "challenges.cloudflare.com" in (f.url or "")]
    except Exception:  # noqa: BLE001
        return False
    for fr in frames:
        try:
            box = await (await fr.frame_element()).bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if not box or box.get("width", 0) < 60 or box.get("height", 0) < 30:
            continue
        # Turnstile 复选框在 widget 左侧、垂直居中
        await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
        return True
    return False


async def submit_game(ctx, game: Game, form_timeout: float = 100.0) -> str:
    """提交一局, 返回 mjai 任务号."""
    page = await ctx.new_page()
    try:
        await page.goto(MJAI_FORM, wait_until="domcontentloaded", timeout=60_000)
        await page.fill("input[name=log-url]", game.record_url)
        await page.select_option("select[name=engine]", "mortal")
        await page.select_option("select[name=mortal-model-tag]", MODEL_TAG)
        await page.select_option("select[name=player-id]", str(game.seat))

        btn = page.locator("form[name=reviewForm] button[type=submit]")
        deadline = time.time() + form_timeout
        tick = 0
        while await btn.is_disabled():
            if time.time() > deadline:
                raise TimeoutError(f"人机验证未通过 ({int(form_timeout)}s)")
            tick += 1
            # Cloudflare 有时会弹可见的「验证您是真人」复选框 —— 帮它点一下
            if tick % 5 == 0:
                await click_turnstile(page)
            await page.wait_for_timeout(1_000)

        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
            await btn.click()
        url = page.url
        if "task=" not in url:
            # 提交被服务端拒了 (通常是并发太快被限流), 把页面上的提示捞出来
            try:
                body = await page.inner_text("body")
                msg = " ".join(x.strip() for x in body.splitlines() if x.strip())[:200]
            except Exception:  # noqa: BLE001
                msg = ""
            raise RuntimeError(f"提交未成功 (返回 {url}) {msg}")
    finally:
        await page.close()

    return url.split("task=")[-1].split("&")[0]


def http_get_json(url: str, timeout: float = 30.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001
        return 0, None


async def wait_report(task_id: str, timeout: float = 200.0) -> dict[str, Any]:
    """轮询 report JSON, 直到报告生成完成 (403/404 表示还在跑)."""
    url = MJAI_REPORT.format(task=task_id)
    deadline = time.time() + timeout
    delay = 3.0
    while time.time() < deadline:
        code, data = await asyncio.to_thread(http_get_json, url)
        if code == 200 and isinstance(data, dict):
            return data
        await asyncio.sleep(delay)
        delay = min(delay * 1.4, 10.0)
    raise TimeoutError(f"等报告超时 ({int(timeout)}s)")


def apply_report(game: Game, report: dict[str, Any]) -> None:
    review = report.get("review") or {}
    rating = review.get("rating")
    if rating is None:
        raise RuntimeError("报告里没有 rating 字段")
    matches, total = review.get("total_matches"), review.get("total_reviewed")
    game.rating = round(float(rating) * 100, 1)
    if isinstance(matches, int) and isinstance(total, int) and total:
        game.accordance = round(matches / total, 4)
        game.accordance_detail = f"{matches}/{total}"
    game.hand_stats = parse_hand_stats(report.get("mjai_log") or [], game.seat)


async def review_all(ctx, games: list[Game], cache: pathlib.Path, concurrency: int,
                     state: dict[str, Any], merge: bool = True) -> None:
    """
    两阶段跑谱, 避免"一局一局串着等":
      1) 并发提交, 先把所有局的任务号拿到 (这一步要开浏览器过验证, 受 concurrency 限制)
      2) 所有任务并行等结果 (纯 HTTP 轮询, 不占浏览器, 可以全开)
    总耗时 ≈ 提交用时 + 最慢那一局的跑谱时间.
    """
    todo = [g for g in games if g.rating is None]
    if not todo:
        print("      全部已在缓存里, 无需跑谱")
        return
    lock = asyncio.Lock()
    stop_early = False
    captcha_fail = 0
    # 上次已经提交过、但还没取到结果的局: 直接等结果, 不用重新过验证
    task_ids: dict[str, str] = {g.uuid: g.task_id for g in todo if g.task_id}
    to_submit = [g for g in todo if not g.task_id]

    def save() -> None:
        state["games"] = [asdict(g) for g in games]
        write_cache(cache, state, merge=merge)

    # ---------------- 阶段 1: 并发提交 ----------------
    if task_ids:
        print(f"      ({len(task_ids)} 局上次已经提交过了, 直接取结果)")
    print(f"      [1/2] 提交 {len(to_submit)} 局 (同时开 {concurrency} 个页面) ...")
    sem = asyncio.Semaphore(concurrency)

    async def submit_one(idx: int, game: Game) -> None:
        nonlocal stop_early, captcha_fail
        async with sem:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if stop_early:
                    game.error = "人机验证连续失败, 已提前中止"
                    break
                try:
                    task_ids[game.uuid] = await submit_game(ctx, game)
                    game.task_id = task_ids[game.uuid]
                    game.error = ""
                    captcha_fail = 0
                    print(f"    [{len(task_ids)}/{len(todo)}] 已提交 {game.when} "
                          f"{MODE_BY_ID.get(game.mode_id, game.mode_id)} {game.rank}位", flush=True)
                    break
                except Exception as exc:  # noqa: BLE001
                    game.error = f"{type(exc).__name__}: {exc}"
                    captcha = "人机验证" in str(exc)
                    captcha_fail = captcha_fail + 1 if captcha else 0
                    # 失败就直接跳过(重跑命令会自动补这些局), 不在这里耗时间
                    if attempt >= MAX_ATTEMPTS:
                        print(f"    提交失败 {game.when}: {game.error}", flush=True)
                        if captcha_fail >= 3 and not stop_early:
                            stop_early = True
                            print("\n      ! 连续几局都卡在人机验证, 剩下的先不跑了.\n"
                                  "        等一会儿再跑同一条命令, 跑好的会自动跳过.", flush=True)
                        break
                    wait = 25 * attempt
                    print(f"    {game.when} 第 {attempt} 次失败 ({game.error}), {wait}s 后重试 ...",
                          flush=True)
                    await asyncio.sleep(wait)
            async with lock:
                save()

    async def report_concurrency() -> None:
        await asyncio.sleep(8)
        print(f"      (浏览器里现在同时开着 {len(ctx.pages)} 个标签页在过验证)", flush=True)

    await asyncio.gather(*(submit_one(i, g) for i, g in enumerate(to_submit, 1)),
                         report_concurrency())

    # ---------------- 阶段 2: 全部并行等结果 ----------------
    waiting = [g for g in todo if task_ids.get(g.uuid)]
    if not waiting:
        print("      没有成功提交的对局")
        return
    print(f"      [2/2] {len(waiting)} 局正在跑谱, 并行等结果 ...", flush=True)
    sem2 = asyncio.Semaphore(max(8, concurrency * 4))  # 等结果是纯网络轮询, 可以开很多

    async def wait_one(game: Game) -> None:
        async with sem2:
            try:
                report = await wait_report(task_ids[game.uuid])
                apply_report(game, report)
                game.error = ""
                game.task_id = ""
                print(f"    完成 {game.when}  分数 {game.rating}  AI 一致率 "
                      f"{game.accordance_detail} = {game.accordance * 100:.1f}%", flush=True)
            except Exception as exc:  # noqa: BLE001
                game.error = f"{type(exc).__name__}: {exc}"
                game.task_id = ""  # 这个任务号取不到了, 下次重新提交
                print(f"    取结果失败 {game.when}: {game.error}", flush=True)
            async with lock:
                save()

    await asyncio.gather(*(wait_one(g) for g in waiting))


# --------------------------------------------------------------------------------------
# 人工兜底: 自动提交实在过不去验证时, 打开页面让用户自己点 (一次只处理一局)
# --------------------------------------------------------------------------------------

MANUAL_TIP_JS = """([i, n, when, rank]) => {
    const box = document.createElement('dialog');
    box.style.cssText = 'position:fixed;z-index:2147483647;top:16px;left:50%;'
        + 'transform:translateX(-50%);background:#c0392b;color:#fff;border:0;border-radius:8px;'
        + 'padding:12px 22px;font:16px/1.6 "Microsoft YaHei",sans-serif;text-align:center;'
        + 'box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:92vw';
    box.innerHTML = `<b>需要人工介入 (${i}/${n})</b><br>这局自动提交失败，请手动过验证并点「提交」`
        + `<br><small>${when} ${rank}位</small>`;
    document.body.appendChild(box);
    if (box.show) box.show(); else box.setAttribute('open', '');
    document.title = `需人工介入 (${i}/${n}) ` + document.title;
}"""


async def manual_submit(ctx, game: Game, idx: int, total: int, timeout: float) -> str:
    """打开提交页并填好表单, 弹窗提示人工介入, 等用户自己提交后返回任务号."""
    page = await ctx.new_page()
    try:
        await page.goto(MJAI_FORM, wait_until="domcontentloaded", timeout=60_000)
        await page.fill("input[name=log-url]", game.record_url)
        await page.select_option("select[name=engine]", "mortal")
        await page.select_option("select[name=mortal-model-tag]", MODEL_TAG)
        await page.select_option("select[name=player-id]", str(game.seat))
        await page.bring_to_front()
        await page.evaluate(MANUAL_TIP_JS, [idx, total, game.when, game.rank])
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "task=" in page.url:
                return page.url.split("task=")[-1].split("&")[0]
            await page.wait_for_timeout(1_000)
        raise TimeoutError(f"人工介入超时, 一直没提交 ({int(timeout)}s)")
    finally:
        await page.close()


async def manual_review(ctx, games: list[Game], cache: pathlib.Path, state: dict[str, Any],
                        timeout: float = 900.0, merge: bool = True) -> None:
    """兜底: 逐局(不并发)打开页面等人工提交, 提交完再等报告取结果."""
    bad = [g for g in games if g.rating is None]
    if not bad:
        return

    def save() -> None:
        state["games"] = [asdict(g) for g in games]
        write_cache(cache, state, merge=merge)

    for idx, game in enumerate(bad, 1):
        print(f"      [{idx}/{len(bad)}] 需要人工介入: {game.when} {game.rank}位 {game.record_url}",
              flush=True)
        try:
            task_id = await manual_submit(ctx, game, idx, len(bad), timeout)
        except Exception as exc:  # noqa: BLE001
            game.error = f"{type(exc).__name__}: {exc}"
            print(f"    人工介入没完成 {game.when}: {game.error}", flush=True)
            save()
            continue
        game.task_id = task_id
        game.error = ""
        save()
        try:
            report = await wait_report(task_id)
            apply_report(game, report)
            game.task_id = ""
            print(f"    人工介入完成 {game.when}  分数 {game.rating}  AI 一致率 "
                  f"{game.accordance_detail} = {game.accordance * 100:.1f}%", flush=True)
        except Exception as exc:  # noqa: BLE001
            game.error = f"{type(exc).__name__}: {exc}"
            game.task_id = ""
            print(f"    人工介入已提交但取结果失败 {game.when}: {game.error}", flush=True)
        save()


# --------------------------------------------------------------------------------------
# 阶段三: 输出
# --------------------------------------------------------------------------------------


def cumulative_pt(done: list[Game], score_now: int | None, cap: int) -> list[float | None]:
    """从当前段位分往前反推每局结束时的段位分; 跨段(越界)处断开为 None."""
    if score_now is None:
        return [None] * len(done)
    series: list[float | None] = [None] * len(done)
    cur: float | None = float(score_now)
    for i in range(len(done) - 1, -1, -1):
        if cur is None:
            break
        if cur < 0 or (cap and cur > cap):  # 说明这几局里升/降过段, 分数被重置了
            break
        series[i] = cur
        d = done[i].pt_delta
        if d is None:
            cur = None
        else:
            cur -= d
    return series


def draw_chart(games: list[Game], uid: int, nickname: str, overview: list[str],
               level_score: int | None, level_cap: int, out_png: pathlib.Path,
               aggregate_pt: bool = False) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    done = sorted(games, key=lambda g: g.start_time)  # 全部对局(失败的也留个位置)
    ok_ratings = [g.rating for g in done if g.rating is not None]
    if not ok_ratings:
        print("  ! 没有成功跑出分数的对局, 跳过绘图")
        return

    xs = list(range(1, len(done) + 1))
    ratings = [g.rating for g in done]
    failed = [x for x, r in zip(xs, ratings) if r is None]
    plot_r = [r if r is not None else float("nan") for r in ratings]
    accs = [g.accordance * 100 if g.accordance is not None else None for g in done]
    deltas = [g.pt_delta for g in done]
    avg = statistics.fmean(ok_ratings)
    lab_size = 7 if len(xs) <= 24 else 6

    # 左: 两张图; 右: 概览文字
    fig = plt.figure(figsize=(15, 9.5), dpi=130)
    ax = fig.add_axes([0.058, 0.545, 0.585, 0.365])
    ax_pt = fig.add_axes([0.058, 0.085, 0.585, 0.33], sharex=ax)

    # ---------------- 上图: rating + AI 一致率 ----------------
    ax.plot(xs, plot_r, marker="o", lw=2, color="#1f77b4", label="Rating (分数)")
    ax.axhline(avg, ls="--", lw=1.8, color="#d62728", label=f"平均 Rating = {avg:.1f}")
    for x, y in zip(xs, ratings):
        if y is None:  # 这一局没跑出来, 跳过标注
            continue
        ax.annotate(f"{y:.1f}", (x, y), textcoords="offset points", xytext=(0, 9),
                    ha="center", fontsize=lab_size, color="#1f77b4",
                    bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.75))
    ax.set_ylabel("Rating", color="#1f77b4")
    ax.tick_params(axis="y", labelcolor="#1f77b4")
    lo, hi = min(ok_ratings), max(ok_ratings)
    pad = max(2.0, (hi - lo) * 0.2)
    ax.set_ylim(lo - pad, hi + pad)
    if failed:  # 跑谱失败的局: 在底部打灰叉, 并写进图例
        ax.plot(failed, [lo - pad * 0.55] * len(failed), marker="x", ms=9, mew=2.2,
                color="#7f7f7f", ls="none", label=f"跑谱失败 {len(failed)} 局(已跳过)")
    ax.grid(alpha=0.25)

    ax_acc = ax.twinx()
    pts = [(x, a) for x, a in zip(xs, accs) if a is not None]
    if pts:
        ax_acc.plot([p[0] for p in pts], [p[1] for p in pts], marker="s", ms=4, lw=1.5,
                    color="#2ca02c", alpha=0.85, label="AI 一致率 (%)")
        ax_acc.set_ylabel("AI 一致率 (%)", color="#2ca02c")
        ax_acc.tick_params(axis="y", labelcolor="#2ca02c")
        ax_acc.set_ylim(0, 100)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax_acc.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower right", framealpha=0.92)

    # ---------------- 下图: 每局 pt 变化 + 段位分走势 ----------------
    deltas_f = [d if d is not None else 0 for d in deltas]
    ax_pt.bar(xs, deltas_f, color=["#2ca02c" if d >= 0 else "#d62728" for d in deltas_f],
              alpha=0.75, width=0.62, label="每局段位分变化 (±pt)")
    ax_pt.axhline(0, color="#888", lw=1)
    for x, d in zip(xs, deltas):
        if not d:  # None 或 0 就不用标了
            continue
        up = d > 0
        ax_pt.annotate(f"{d:+d}", (x, d), textcoords="offset points",
                       xytext=(0, 4 if up else -4), ha="center",
                       va="bottom" if up else "top", fontsize=lab_size,
                       color="#2ca02c" if up else "#d62728")
    ax_pt.set_ylabel("段位分变化 (pt)")
    if any(d is not None for d in deltas):
        span = max(abs(d) for d in deltas_f) or 1
        ax_pt.set_ylim(-span * 1.6, span * 1.6)
    ax_pt.grid(alpha=0.25)

    if aggregate_pt:
        # 多位对手的 pt 收支只从 0 累加，不反推某一位玩家的段位分。
        total = 0
        cum = []
        for delta in deltas:
            total = total + delta if total is not None and delta is not None else None
            cum.append(total)
    else:
        cum = cumulative_pt(done, level_score, level_cap)
    cum_pts = [(x, c) for x, c in zip(xs, cum) if c is not None]
    if len(cum_pts) >= 2:
        ax_cum = ax_pt.twinx()
        ax_cum.plot([p[0] for p in cum_pts], [p[1] for p in cum_pts], marker="D", ms=4,
                    lw=1.8, color="#9467bd", label="累计 pt 收支" if aggregate_pt else "段位分走势")
        ax_cum.set_ylabel("累计 pt 收支" if aggregate_pt else "段位分", color="#9467bd")
        ax_cum.tick_params(axis="y", labelcolor="#9467bd")
        ax_cum.margins(y=0.12)
        if not aggregate_pt and len(cum_pts) < len(xs):
            ax_cum.set_title("（段位分走势在升/降段处断开）", fontsize=8, color="#888")

        # 标出最高 / 最低 / 最近那一场
        hi_x, hi_y = max(cum_pts, key=lambda p: p[1])
        lo_x, lo_y = min(cum_pts, key=lambda p: p[1])
        last_x, last_y = cum_pts[-1]
        marks = [
            (hi_x, hi_y, f"最高 {hi_y:.0f}", "#d62728", (0, 13), "^"),
            (lo_x, lo_y, f"最低 {lo_y:.0f}", "#2ca02c", (0, -19), "v"),
            (last_x, last_y, f"最近 {last_y:.0f}", "#ff7f0e",
             (0, 27) if last_x == hi_x else (0, 13), "o"),
        ]
        for mx, my, text, color, off, mk in marks:
            ax_cum.plot([mx], [my], marker=mk, ms=7, color=color, ls="none", zorder=5)
            ax_cum.annotate(text, (mx, my), textcoords="offset points", xytext=off,
                            ha="center", fontsize=7.5, color=color, weight="bold", zorder=6,
                            bbox=dict(boxstyle="round,pad=0.18", fc="white", ec=color,
                                      lw=0.8, alpha=0.9))

        h3, l3 = ax_pt.get_legend_handles_labels()
        h4, l4 = ax_cum.get_legend_handles_labels()
        ax_pt.legend(h3 + h4, l3 + l4, loc="lower left", framealpha=0.92)
    else:
        ax_pt.legend(loc="lower left", framealpha=0.92)

    ax_pt.set_xlabel("最近对局 (由旧到新)")
    step = max(1, (len(xs) + 24) // 25)  # 刻度最多 25 个, 局数多了隔几个标一个
    ax_pt.set_xticks(xs[::step])
    ax_pt.set_xlim(0.5, len(xs) + 0.5)

    modes = ", ".join(sorted({MODE_FULL.get(g.mode_id, str(g.mode_id)) for g in done}))
    ax.set_title(f"{nickname} ({uid})   Mortal {MODEL_TAG}   最近 {len(done)} 场 [{modes}]",
                 fontsize=12)

    # ---------------- 右侧: 概览文字 ----------------
    fig.text(0.708, 0.955, "\n".join(overview), va="top", ha="left",
             fontsize=9, linespacing=1.58, family="Microsoft YaHei")

    # 右下角的空白处放个吉祥物 (有 turtle.png 才画, 没有就当无事发生)
    icon = pathlib.Path(__file__).with_name("turtle.png")
    if icon.exists():
        try:
            from matplotlib.offsetbox import AnnotationBbox, OffsetImage
            ab = AnnotationBbox(OffsetImage(plt.imread(str(icon)), zoom=0.34),
                                (0.94, 0.06), xycoords="figure fraction",
                                frameon=False, box_alignment=(0.5, 0.5))
            fig.add_artist(ab)
        except Exception:  # noqa: BLE001
            pass

    fig.savefig(out_png)
    plt.close(fig)
    print(f"  [OK] 图 (rating + pt): {out_png}")


def open_file(path: pathlib.Path) -> None:
    """用系统默认程序打开文件 (跑完直接把图弹出来看)."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 打不开图片查看器 ({exc}), 图片在: {path}")


def unique_run_dir(parent: pathlib.Path, stamp: str) -> pathlib.Path:
    """每次输出都放进独立的时间戳子目录, 不覆盖以前跑出来的图/CSV/概览."""
    path = parent / stamp
    n = 1
    while path.exists():
        n += 1
        path = parent / f"{stamp}_{n}"
    path.mkdir(parents=True)
    return path


def write_cache(cache: pathlib.Path, state: dict[str, Any], merge: bool = True) -> None:
    """写跑谱缓存; merge 时保留文件里已有的其它对局, 免得跑少量局就把旧记录冲掉."""
    games = [g for g in (state.get("games") or []) if isinstance(g, dict) and g.get("uuid")]
    if merge:
        merged: dict[tuple[str, Any], dict[str, Any]] = {}
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            old = {}
        if isinstance(old, dict):
            for g in old.get("games") or []:
                if isinstance(g, dict) and g.get("uuid"):
                    merged[(g["uuid"], g.get("seat"))] = g
        for g in games:
            key = (g["uuid"], g.get("seat"))
            prev = merged.get(key)
            if prev and g.get("rating") is None and prev.get("rating") is not None:
                continue  # 这次没跑出来, 别把以前跑好的分数冲掉
            merged[key] = g
        games = sorted(merged.values(), key=lambda g: g.get("start_time", 0))
    state = {**state, "games": games}
    cache.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def write_csv(games: list[Game], path: pathlib.Path, include_players: bool = False) -> None:
    rows = sorted(games, key=lambda g: g.start_time)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        player_headers = ["相对位置", "玩家UID", "玩家昵称", "座位(0=东)"] if include_players else []
        w.writerow(player_headers + ["时间", "场次", "顺位", "点数", "段位分变化", "分数(rating)", "AI一致率",
                                    "一致率明细", "牌谱", "备注"])
        for g in rows:
            player_values = [g.position, g.player_id, g.nickname, g.seat] if include_players else []
            w.writerow(player_values + [
                g.when, MODE_BY_ID.get(g.mode_id, g.mode_id), g.rank, g.score,
                "" if g.pt_delta is None else g.pt_delta,
                "" if g.rating is None else g.rating,
                "" if g.accordance is None else f"{g.accordance * 100:.1f}%",
                g.accordance_detail, g.record_url, g.error,
            ])
    print(f"  [OK] 明细 CSV: {path}")


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------


def player_output_dir(base: pathlib.Path, nickname: str, rival: bool = False) -> pathlib.Path:
    """保留昵称中的 #，替换 Windows 文件名禁用字符。"""
    folder = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nickname).rstrip(" .") or "player"
    if folder.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                                         *[f"LPT{i}" for i in range(1, 10)]}:
        folder = "_" + folder
    return base / (folder + "_rival" if rival else folder)


# 旧版缓存没有这些键, 缺任何一个就说明这局要重跑, 否则庄家统计会显示成 "-"
DEALER_STAT_KEYS = ("oya_kyoku", "oya_riichi", "oya_hora",
                    "oya_rival_n", "oya_rival_riichi9", "oya_rival_tsumo9")


def restore_cache(cache: pathlib.Path, games: list[Game], uid: int, position: str = "",
                  require_stats: bool = False) -> None:
    if not cache.exists():
        return
    try:
        old = json.loads(cache.read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            return
        if old.get("uid") != uid or old.get("position", "") != position:
            return
        if old.get("model", MODEL_TAG) != MODEL_TAG:
            return
        prev = {(g["uuid"], g["seat"]): g for g in old.get("games", [])}
        for game in games:
            cached = prev.get((game.uuid, game.seat))
            if not cached or cached.get("player_id") not in (None, game.player_id):
                continue
            stats = cached.get("hand_stats")
            if require_stats and not (isinstance(stats, dict)
                                      and all(k in stats for k in DEALER_STAT_KEYS)):
                continue  # 旧版缓存没有逐手/庄家统计, 这局重跑一次
            if cached.get("rating") is not None:
                game.rating = cached["rating"]
                game.accordance = cached.get("accordance")
                game.accordance_detail = cached.get("accordance_detail", "")
                game.hand_stats = cached.get("hand_stats") or None
            elif cached.get("task_id"):
                game.task_id = cached["task_id"]
        count = sum(g.rating is not None for g in games)
        if count:
            print(f"      复用上次已跑完的 {count} 局 (断点续跑)")
    except (OSError, ValueError, KeyError, TypeError):
        pass


# --------------------------------------------------------------------------------------
# 逐手统计 (起手向听 / 一发 / 立直) —— 数据来自 mjai 报告里的 mjai_log
# --------------------------------------------------------------------------------------

HONOR_INDEX = {"E": 27, "S": 28, "W": 29, "N": 30, "P": 31, "F": 32, "C": 33}
SUIT_BASE = {"m": 0, "p": 9, "s": 18}
KOKUSHI = (0, 8, 9, 17, 18, 26, 27, 28, 29, 30, 31, 32, 33)


def tile_index(pai: Any) -> int | None:
    """mjai 的牌字符串 ("3m" / "C" / "5pr") -> 34 种牌的下标."""
    if not isinstance(pai, str) or not pai:
        return None
    if pai in HONOR_INDEX:  # 字牌是单字符 (E/S/W/N/P/F/C)
        return HONOR_INDEX[pai]
    if len(pai) == 3 and pai[2] == "r":  # "5pr" 这种赤五写法
        pai = pai[:2]
    if len(pai) != 2:
        return None
    base = SUIT_BASE.get(pai[-1])
    num = pai[0]
    if base is None or not num.isdigit():
        return None
    n = 5 if num == "0" else int(num)  # "0m" 是赤五
    return base + n - 1 if 1 <= n <= 9 else None


def standard_shanten(counts: list[int]) -> int:
    """标准型(4 面子 + 1 雀头)向听数, counts 是 34 种牌的张数."""
    best = 8

    def dfs(i: int, melds: int, taatsu: int, pair: int) -> None:
        nonlocal best
        if i == 34:
            m, t = min(melds, 4), min(taatsu, 4 - min(melds, 4))
            best = min(best, 8 - 2 * m - t - pair)
            return
        n = counts[i]
        if n >= 3:  # 刻子
            counts[i] -= 3
            dfs(i, melds + 1, taatsu, pair)
            counts[i] += 3
        if n >= 2 and not pair:  # 雀头
            counts[i] -= 2
            dfs(i, melds, taatsu, 1)
            counts[i] += 2
        if n >= 2 and taatsu < 4:  # 对子当搭子
            counts[i] -= 2
            dfs(i, melds, taatsu + 1, pair)
            counts[i] += 2
        if i < 27:
            pos = i % 9
            if pos <= 6 and counts[i] and counts[i + 1] and counts[i + 2]:  # 顺子
                counts[i] -= 1
                counts[i + 1] -= 1
                counts[i + 2] -= 1
                dfs(i, melds + 1, taatsu, pair)
                counts[i] += 1
                counts[i + 1] += 1
                counts[i + 2] += 1
            if pos <= 7 and counts[i] and counts[i + 1] and taatsu < 4:  # 两面/边张
                counts[i] -= 1
                counts[i + 1] -= 1
                dfs(i, melds, taatsu + 1, pair)
                counts[i] += 1
                counts[i + 1] += 1
            if pos <= 6 and counts[i] and counts[i + 2] and taatsu < 4:  # 嵌张
                counts[i] -= 1
                counts[i + 2] -= 1
                dfs(i, melds, taatsu + 1, pair)
                counts[i] += 1
                counts[i + 2] += 1
        dfs(i + 1, melds, taatsu, pair)

    dfs(0, 0, 0, 0)
    return best


def hand_shanten(tehai: list[Any]) -> int:
    """13 张配牌的向听数 (标准型 / 七对子 / 国士无双向听, 取最小)."""
    counts = [0] * 34
    for pai in tehai:
        idx = tile_index(pai)
        if idx is not None:
            counts[idx] += 1
    pairs = sum(1 for n in counts if n >= 2)
    kinds = sum(1 for n in counts if n)
    chiitoi = 6 - pairs + max(0, 7 - kinds)
    honor_pairs = any(counts[i] >= 2 for i in KOKUSHI)
    kokushi = 13 - sum(1 for i in KOKUSHI if counts[i]) - (1 if honor_pairs else 0)
    return min(standard_shanten(counts), chiitoi, kokushi)


def parse_hand_stats(log: list[dict[str, Any]], seat: int) -> dict[str, int]:
    """从 mjai 逐手日志统计某个座位: 和了/一发/立直(巡目, 和了)/起手向听(亲/子)/做庄统计.

    mjai 的 hora 事件里没有役种列表, 所以「一发」按规则推断:
    立直宣言后, 在自己下一次打牌(或任何人鸣牌/杠)之前和了, 就算一发.

    庄家口径: 只统计 seat 坐庄的局. 「闲家」是同局里除庄家外的其他家;
    「9 巡内」按该家自己第 9 次打牌之前(含)计算.
    """
    st = {"hora": 0, "ippatsu": 0, "riichi": 0, "riichi_win": 0, "riichi_turn_sum": 0,
          "oya_n": 0, "oya_shanten_sum": 0, "ko_n": 0, "ko_shanten_sum": 0,
          "oya_kyoku": 0, "oya_riichi": 0, "oya_hora": 0,
          "oya_rival_n": 0, "oya_rival_riichi9": 0, "oya_rival_tsumo9": 0}
    discards = [0, 0, 0, 0]
    reach_turn: dict[int, int] = {}   # 本局宣言立直的座位 -> 巡目
    riichi = [False] * 4              # 本局已成立的立直
    ippatsu = [False] * 4             # 一发窗口开着
    dealer_tehai: list[Any] | None = None  # 自家是亲时, 等第一张摸牌再算向听(14 张)
    cur_oya: int | None = None        # 本局庄家座位

    def close_windows() -> None:
        for k in range(4):
            ippatsu[k] = False

    for ev in log:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "start_kyoku":
            discards = [0, 0, 0, 0]
            reach_turn, riichi, ippatsu = {}, [False] * 4, [False] * 4
            dealer_tehai = None
            oya = ev.get("oya")
            cur_oya = oya if isinstance(oya, int) else None
            tehais = ev.get("tehais") or []
            if cur_oya == seat:
                st["oya_kyoku"] += 1
                st["oya_rival_n"] += max(0, len(tehais) - 1)  # 三麻只有 2 个闲家
            if 0 <= seat < len(tehais) and len(tehais[seat]) == 13:
                if ev.get("oya") == seat:
                    dealer_tehai = list(tehais[seat])  # 亲家多摸一张, 见下面 tsumo 分支
                else:
                    st["ko_n"] += 1
                    st["ko_shanten_sum"] += hand_shanten(tehais[seat])
        elif kind == "tsumo":
            if dealer_tehai is not None and ev.get("actor") == seat:
                pai = ev.get("pai")
                if tile_index(pai) is not None:
                    st["oya_n"] += 1
                    st["oya_shanten_sum"] += hand_shanten(dealer_tehai + [pai])
                dealer_tehai = None
        elif kind == "reach":
            actor = ev.get("actor")
            if isinstance(actor, int) and 0 <= actor < 4:
                reach_turn[actor] = discards[actor] + 1  # 记所有人, 闲家 9 巡内立直要用
        elif kind == "reach_accepted":
            actor = ev.get("actor")
            turn = reach_turn.pop(actor, None)
            if actor == seat and turn is not None:
                st["riichi"] += 1
                st["riichi_turn_sum"] += turn
                riichi[seat] = True
                ippatsu[seat] = True
                if cur_oya == seat:
                    st["oya_riichi"] += 1
            elif (cur_oya == seat and isinstance(actor, int) and actor != seat
                  and turn is not None and turn <= 9):
                st["oya_rival_riichi9"] += 1  # 自己坐庄时, 闲家 9 巡内立直
        elif kind == "dahai":
            actor = ev.get("actor")
            if isinstance(actor, int) and 0 <= actor < 4:
                ippatsu[actor] = False  # 自己打牌, 一发窗口关闭
                discards[actor] += 1
        elif kind in ("chi", "pon", "daiminkan", "ankan", "kakan"):
            close_windows()           # 鸣牌/杠, 所有人的一发窗口都关闭
        elif kind == "hora":
            actor = ev.get("actor")
            if actor == seat:
                st["hora"] += 1
                if cur_oya == seat:
                    st["oya_hora"] += 1
                if ippatsu[seat]:
                    st["ippatsu"] += 1
                if riichi[seat]:
                    st["riichi_win"] += 1
                ippatsu[seat] = False
            elif (cur_oya == seat and isinstance(actor, int) and 0 <= actor < 4
                  and ev.get("target") == actor  # target == actor 表示自摸
                  and discards[actor] + 1 <= 9):
                st["oya_rival_tsumo9"] += 1  # 自己坐庄时, 闲家 9 巡内自摸
        elif kind in ("end_kyoku", "ryukyoku"):
            close_windows()
    return st


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
        lines += [
            "",
            f"一发率      {pct(ippatsu / hora) if hora else '-'} ({ippatsu}/{hora})",
            f"立直和了率  {pct(riichi_win / riichi) if riichi else '-'} ({riichi_win}/{riichi})",
            f"立直巡目    {turns / riichi:.2f}" if riichi else "立直巡目    -",
            f"起手向听    {(oya_sum + ko_sum) / (oya_n + ko_n):.2f} "
            f"(亲 {oya_sum / oya_n:.2f} / 子 {ko_sum / ko_n:.2f})"
            if oya_n and ko_n else "起手向听      -",
        ]
    lines += dealer_lines(hands)
    lines += [f"pt 有效数据 {len(deltas)}/{n} 场", "", "统计范围：仅输入玩家这批牌谱", "各局对手可能不同，pt 从 0 累计", "段位及牌谱屋面板统计不适用"]
    return lines


async def run(args: argparse.Namespace, rival: bool = False) -> int:
    from playwright.async_api import async_playwright

    base_dir = pathlib.Path(args.out).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    mode_ids: list[int] = []
    for mode in args.mode:
        mid = int(mode) if mode.isdigit() else MODES.get(mode)
        if mid is None:
            raise SystemExit(f"未知场次 {mode!r}, 可用: {', '.join(MODES)}")
        if rival and mid not in (8, 9, 11, 12, 15, 16):
            raise SystemExit("对手分析只支持四人场次：王座 玉 金 王东 玉东 金东")
        mode_ids.append(mid)
    mode_names = "/".join(MODE_FULL.get(m, str(m)) for m in mode_ids)
    exe = find_browser()
    port = free_port()
    print(f"启动浏览器: {exe}")
    proc = launch_browser(exe, port, base_dir / ".browser_profile")
    results = []
    jobs = []  # (position, games, cache, state), 供末尾重试/人工介入复用
    ov: dict[str, Any] = {}

    def group_overview(gs: list[Game], pos: str) -> list[str]:
        if rival:
            return format_rival_overview(gs, nickname, uid, pos, mode_names)
        return format_overview(ov, gs, mode_names, uid)

    def save_group(cache_p: pathlib.Path, st: dict[str, Any], gs: list[Game], pos: str) -> None:
        st["overview"] = group_overview(gs, pos)
        st["games"] = [asdict(x) for x in gs]
        write_cache(cache_p, st, merge=not args.fresh)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            print("[1/3] 读取对局列表 ...")
            for extra in list(ctx.pages)[1:]:
                try:
                    await extra.close()
                except Exception:  # noqa: BLE001
                    pass
            page = await ctx.new_page()
            uid, nickname = await resolve_player(page, args.id or args.name, mode_ids)
            out_dir = player_output_dir(base_dir, nickname, rival)
            raws = await fetch_records(page, uid, mode_ids, args.games)
            if rival:
                groups = rival_games(raws, uid)
                ov = {}
            else:
                groups = {"": [g for g in (to_game(r, uid) for r in raws) if g]}
                games = groups[""]
                since_ms = min((g.start_time for g in games), default=0) * 1000 or EARLIEST_MS
                ov = await fetch_overview(page, uid, mode_ids, since_ms)
            await page.close()
            if not any(groups.values()):
                print("      没有取到任何对局, 换个场次类型试试")
                return 1

            lv = (ov.get("all") or {}).get("level") or {}
            level_score = lv.get("score")
            level_cap = level_info(lv.get("id"))[1]
            for position, games in groups.items():
                target_dir = out_dir / position if position else out_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                cache = target_dir / "results.json"
                if args.fresh and cache.exists():
                    cache.unlink()
                    print("      --fresh: 已清掉跑谱缓存")
                restore_cache(cache, games, uid, position, require_stats=True)
                # 兼容旧版放在 rating_out 根目录的自身缓存；不改动旧文件。
                if not rival and not args.fresh and not cache.exists():
                    restore_cache(base_dir / "results.json", games, uid)
                print(f"      {position or nickname}: 取到 {len(games)} 局，缓存 {cache}")

                overview = group_overview(games, position)
                print_overview(overview)
                state = {"uid": uid, "nickname": nickname, "position": position, "model": MODEL_TAG,
                         "overview": overview, "games": [asdict(g) for g in games]}
                print(f"[2/3] {position or nickname} Mortal {MODEL_TAG} 跑谱 (并发 {args.concurrency}) ...")
                await review_all(ctx, games, cache, args.concurrency, state, merge=not args.fresh)
                save_group(cache, state, games, position)
                results.append((position, games, target_dir))
                jobs.append((position, games, cache, state))

            # 正常跑完后还有失败的局: 先整体重试一轮, 剩下的再逐局交给人
            if any(g.rating is None for _, gs, _, _ in jobs for g in gs):
                print("\n[!] 有局没跑出来, 开始重试 ...")
                for position, games, cache, state in jobs:
                    left = sum(g.rating is None for g in games)
                    if not left:
                        continue
                    print(f"      {position or nickname}: 重试 {left} 局 (并发 {args.concurrency}) ...")
                    await review_all(ctx, games, cache, args.concurrency, state, merge=not args.fresh)
                    save_group(cache, state, games, position)
            for position, games, cache, state in jobs:
                left = sum(g.rating is None for g in games)
                if not left:
                    continue
                print(f"\n[!] {position or nickname} 还有 {left} 局自动提交不了, 转人工介入 "
                      f"(一次一局, 请在浏览器里手动过验证并提交)")
                await manual_review(ctx, games, cache, state, merge=not args.fresh)
                save_group(cache, state, games, position)
            await ctx.close()
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        close_browser(proc)

    print("[3/3] 输出 ...")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for position, games, target_dir in results:
        run_dir = unique_run_dir(target_dir, stamp)  # 每次跑单独一个目录, 旧结果不动
        overview = group_overview(games, position)
        write_csv(games, run_dir / f"rating_{uid}.csv", include_players=rival)
        png = run_dir / f"rating_{uid}.png"
        title = f"{nickname} 的{position}" if rival else nickname
        draw_chart(games, uid, title, overview, level_score, level_cap, png, aggregate_pt=rival)
        summary_txt = run_dir / f"summary_{uid}.txt"
        summary_txt.write_text("\n".join(overview) + "\n", encoding="utf-8")
        print(f"  [OK] 概览文本: {summary_txt}")
        if not args.no_show and png.exists():
            open_file(png)
        ok = [g for g in games if g.rating is not None]
        if ok:
            print(f"\n{position or nickname} 平均 Rating: {statistics.fmean(g.rating for g in ok):.1f} "
                  f"(成功 {len(ok)}/{len(games)} 局)")
        bad = [g for g in games if g.rating is None]
        if bad:
            print(f"\n{position or nickname} 有 {len(bad)} 局没跑出来 (重跑同一条命令会自动只补这些):")
            for game in bad[:8]:
                print(f"  {game.when} {game.rank}位  {game.error[:88]}")
            if len(bad) > 8:
                print(f"  ... 其余 {len(bad) - 8} 局见 CSV 的「备注」列")
    return 0


def main(rival: bool = False) -> int:
    for stream in (sys.stdout, sys.stderr):  # 别让 Windows 控制台编码把程序搞崩
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    description = "雀魂对手批量跑谱：上家/对家/下家" if rival else "雀魂批量跑谱 + rating/pt 折线图 (Mortal 4.1b)"
    ap = argparse.ArgumentParser(description=description)
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
        return asyncio.run(run(args, rival=rival))
    except KeyboardInterrupt:
        print("\n已中断 (进度保存在 results.json, 重跑会自动续上)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
