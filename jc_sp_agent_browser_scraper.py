#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩足球SP Agent-Browser 抓取器（Playwright版）

用途：
- 用真实 Chromium 打开公开网页，把页面渲染后的 DOM 中的比赛、SP、让球、赛果等字段取出。
- 适合 requests/BeautifulSoup 抓不到、页面由 JS 渲染、或需要等页面加载完成的情况。

边界：
- 不登录、不绕过验证码、不破解风控、不突破权限。
- 如果页面出现验证码/登录/访问限制，本脚本会保存截图和HTML，然后停止该页，方便人工判断。

安装：
    pip install -r jc_sp_agent_requirements.txt
    python -m playwright install chromium

示例：
    python jc_sp_agent_browser_scraper.py --start 2026-07-01 --end 2026-07-05 --source 500 --out jc_sp_browser_raw.csv
    python jc_sp_agent_browser_scraper.py --start 2026-07-01 --end 2026-07-05 --source 500 --out jc_sp_browser_raw.csv --headed

输出：
- CSV：结构化数据
- debug_dir：失败或页面异常时保存 html/png，方便调试选择器
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

OUT_COLUMNS = [
    "match_date",
    "weekday_num",
    "jc_num",
    "league",
    "kickoff_time",
    "home_team",
    "away_team",
    "home_away_text",
    "half_score",
    "full_score",
    "market_type",
    "handicap",
    "win_sp",
    "draw_sp",
    "loss_sp",
    "result_text",
    "data_type",
    "source",
    "source_url",
    "fetched_at",
    "raw_text",
]


def date_range(start: str, end: str) -> Iterable[dt.date]:
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    if end_d < start_d:
        raise ValueError("end date must be >= start date")
    cur = start_d
    while cur <= end_d:
        yield cur
        cur += dt.timedelta(days=1)


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def split_teams(text: str) -> tuple[str, str]:
    text = clean_text(text)
    for sep in [" VS ", " vs ", "VS", "vs", "—", "-", "－"]:
        if sep in text:
            a, b = text.split(sep, 1)
            return clean_text(a), clean_text(b)
    return text, ""


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    out = {c: "" for c in OUT_COLUMNS}
    out.update({k: clean_text(str(v)) for k, v in row.items() if v is not None})
    home, away = split_teams(out.get("home_away_text", ""))
    if not out.get("home_team"):
        out["home_team"] = home
    if not out.get("away_team"):
        out["away_team"] = away
    if not out.get("weekday_num") and out.get("jc_num"):
        out["weekday_num"] = out["jc_num"][:2]
    return out


def looks_blocked(text: str) -> bool:
    bad_words = ["验证码", "安全验证", "访问过于频繁", "登录", "人机验证", "captcha", "verify"]
    low = text.lower()
    return any(w.lower() in low for w in bad_words)


def save_debug(page, debug_dir: Path, stem: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        (debug_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True)
    except Exception:
        pass


def extract_500_from_dom(page, day: dt.date, url: str) -> List[Dict[str, str]]:
    """在浏览器环境执行 JS，直接从渲染后的 DOM 提取表格。"""
    data = page.evaluate(
        """
        () => {
          function txt(el){ return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim(); }
          const trs = Array.from(document.querySelectorAll('tr.bet-tb-tr, tr[class*="bet-tb-tr"]'));
          return trs.map((tr) => {
            const tds = Array.from(tr.querySelectorAll('td'));
            const spans = Array.from(tr.querySelectorAll('td span')).map(txt).filter(Boolean);
            const ps = Array.from(tr.querySelectorAll('td p')).map(txt).filter(Boolean);
            const raw = txt(tr);
            return {
              td_texts: tds.map(txt),
              spans,
              ps,
              raw_text: raw
            };
          });
        }
        """
    )
    rows: List[Dict[str, str]] = []
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    for item in data:
        tds = item.get("td_texts") or []
        spans = item.get("spans") or []
        ps = item.get("ps") or []
        raw = clean_text(item.get("raw_text", ""))
        if len(tds) < 4:
            continue

        # 500 页面结构可能变动，所以这里做宽松解析：优先使用前几列，否则从 raw_text 保留证据。
        jc_num = clean_text(tds[0]) if len(tds) > 0 else ""
        league = clean_text(tds[1]) if len(tds) > 1 else ""
        kickoff = clean_text(tds[2]) if len(tds) > 2 else ""
        teams_text = clean_text(tds[3]) if len(tds) > 3 else ""
        home, away = split_teams(teams_text)

        # 常见结构：前3个span是胜平负，后3个span是让球胜平负。
        odds_sets = []
        if len(spans) >= 6:
            odds_sets.append(("SPF", ps[0] if len(ps) > 0 else "0", spans[0], spans[1], spans[2]))
            odds_sets.append(("RQSPF", ps[1] if len(ps) > 1 else "", spans[3], spans[4], spans[5]))
        elif len(spans) >= 3:
            odds_sets.append(("UNKNOWN_3WAY", ps[-1] if ps else "", spans[0], spans[1], spans[2]))
        else:
            # 没有抓到赔率时也保留一条 evidence，方便后续调选择器。
            odds_sets.append(("RAW_ONLY", "", "", "", ""))

        for market_type, handicap, win_sp, draw_sp, loss_sp in odds_sets:
            rows.append(normalize_row({
                "match_date": day.isoformat(),
                "weekday_num": jc_num[:2] if jc_num else "",
                "jc_num": jc_num,
                "league": league,
                "kickoff_time": kickoff,
                "home_team": home,
                "away_team": away,
                "home_away_text": teams_text,
                "half_score": "",
                "full_score": "",
                "market_type": market_type,
                "handicap": handicap,
                "win_sp": win_sp,
                "draw_sp": draw_sp,
                "loss_sp": loss_sp,
                "result_text": "",
                "data_type": "real_jc_from_rendered_dom" if win_sp and draw_sp and loss_sp else "dom_evidence_only",
                "source": "500.com/browser_dom",
                "source_url": url,
                "fetched_at": fetched_at,
                "raw_text": raw,
            }))
    return rows


def fetch_500_day(page, day: dt.date, debug_dir: Path, wait_ms: int = 1500) -> List[Dict[str, str]]:
    url = f"https://trade.500.com/jczq/?date={day.isoformat()}"
    print(f"OPEN {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # networkidle 对部分站点会一直等不到，所以只作为补充尝试。
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(wait_ms)
    except Exception as e:
        save_debug(page, debug_dir, f"500_{day.isoformat()}_goto_error")
        raise RuntimeError(f"goto failed: {e}")

    body_text = clean_text(page.locator("body").inner_text(timeout=10000))
    if looks_blocked(body_text):
        save_debug(page, debug_dir, f"500_{day.isoformat()}_blocked")
        print(f"WARN {day}: 页面疑似验证码/登录/限制，已保存debug文件", file=sys.stderr)
        return []

    rows = extract_500_from_dom(page, day, url)
    if not rows:
        save_debug(page, debug_dir, f"500_{day.isoformat()}_empty")
    return rows


def write_csv(path: str, rows: List[Dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in OUT_COLUMNS})


def main() -> int:
    ap = argparse.ArgumentParser(description="竞彩足球SP Agent-Browser抓取器")
    ap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--source", default="500", choices=["500"], help="数据源")
    ap.add_argument("--out", default="jc_sp_browser_raw.csv", help="输出CSV路径")
    ap.add_argument("--debug-dir", default="debug_pages", help="异常页面保存目录")
    ap.add_argument("--sleep-min", type=float, default=1.5, help="每日抓取最小暂停秒数")
    ap.add_argument("--sleep-max", type=float, default=3.5, help="每日抓取最大暂停秒数")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于调试")
    ap.add_argument("--slow-mo", type=int, default=0, help="操作慢放毫秒，调试用")
    args = ap.parse_args()

    debug_dir = Path(args.debug_dir)
    all_rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for day in date_range(args.start, args.end):
            try:
                if args.source == "500":
                    rows = fetch_500_day(page, day, debug_dir)
                else:
                    rows = []
                print(f"{day}: {len(rows)} rows")
                all_rows.extend(rows)
            except Exception as e:
                print(f"{day}: ERROR {e}", file=sys.stderr)
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))

        context.close()
        browser.close()

    write_csv(args.out, all_rows)
    print(f"Saved {len(all_rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
