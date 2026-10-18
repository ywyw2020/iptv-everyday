#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 E：淘汰与回访。

规则：
  * 连续 N 天（默认 7）流级验证失败 -> 移入 sources/graveyard.json 并记录淘汰原因；
  * 墓地中的源每 30 天自动回访复检一次，能解析出流则复活并重新准入 registry；
  * 仓库超过 90 天无 push 的源打上 repo_stale 标记，发现层不再接纳其仓库。

本脚本在流水线中先于 fetch 执行：因此"复活"的源当晚即会被抓取，
而淘汰判断使用的是上一轮验证累积的 consecutive_failures。

用法：
  python scripts/retire_sources.py
  python scripts/retire_sources.py --dry-run
  python scripts/retire_sources.py --review-all          # 强制回访全部墓地源（自测用）
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    Config,
    days_between,
    fetch_text,
    get_logger,
    iso_now,
    load_graveyard,
    load_registry,
    now,
    parse_playlist,
    save_graveyard,
    save_registry,
    truncate,
)

GRAVE_FIELDS = (
    "source_repo",
    "playlist_url",
    "discovered_via",
    "discovered_at",
    "last_seen_at",
    "stars",
    "last_push",
    "kind",
)


def probe_playlist(rec: dict, cfg: Config, log) -> tuple[bool, int, str]:
    """回访探测：能否把 playlist_url 解析出至少 N 条流。"""
    url = rec.get("playlist_url", "")
    try:
        text = fetch_text(
            url,
            timeout=cfg.get_float("fetch", "timeout", 10),
            retries=cfg.get_int("fetch", "retries", 2),
            verify=False,
        )
        if not text:
            return False, 0, "下载失败或空响应"
        streams = parse_playlist(text)
        if len(streams) < cfg.get_int("retire", "revive_min_streams", 1):
            return False, len(streams), f"仅解析出 {len(streams)} 条流"
        return True, len(streams), "ok"
    except Exception as exc:
        return False, 0, f"探测异常：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="源淘汰与墓地回访")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review-all", action="store_true", help="忽略 next_review_at，强制回访全部墓地源")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("retire")
    cfg = Config(args.config) if args.config else Config()
    threshold = max(1, cfg.get_int("retire", "failure_threshold", 7))
    review_days = max(1, cfg.get_int("retire", "graveyard_review_days", 30))
    stale_days = cfg.get_int("retire", "repo_stale_days", 90)

    registry = load_registry()
    graveyard = load_graveyard()
    sources = registry.get("sources", {})

    log.info("========== 淘汰与回访启动 %s ==========", iso_now())
    log.info("注册表 %s 个源 | 墓地 %s 个源 | 淘汰阈值 %s 天 | 回访间隔 %s 天", len(sources), len(graveyard["entries"]), threshold, review_days)

    retired_now = []
    # ---- 1. 结算上一轮的成败，更新连续失败计数
    # 说明：retire 在 fetch/validate 之前运行，这里读的是"上一轮"的结果；
    # 每个自然日只结算一次，防止一天多跑导致计数虚增。
    today = now().strftime("%Y-%m-%d")
    counted = 0
    for key in list(sources.keys()):
        rec = sources[key]
        if not rec.get("last_fetch_at"):
            continue  # 新发现、还没被抓过，不参与结算
        if rec.get("last_counted_date") == today:
            continue
        failures = int(rec.get("consecutive_failures") or 0)
        valid = int(rec.get("last_valid_count") or 0)
        stream_count = int(rec.get("last_stream_count") or 0)
        if valid > 0:
            rec["consecutive_failures"] = 0
        else:
            rec["consecutive_failures"] = failures + 1
            reason = "抓取到 0 条流" if stream_count == 0 else "验证通过 0 条"
            log.info("失败 +1 %s（%s，累计 %s/%s）", truncate(key, 70), reason, rec["consecutive_failures"], threshold)
        if not args.dry_run:
            rec["last_counted_date"] = today
        counted += 1
    if counted:
        log.info("本轮结算 %s 个源的成败", counted)

    # ---- 2. 淘汰：连续失败达阈值
    for key in list(sources.keys()):
        rec = sources[key]
        failures = int(rec.get("consecutive_failures") or 0)
        if rec.get("status") in ("retired", "dead"):
            continue
        if failures >= threshold:
            entry = {field: rec.get(field) for field in GRAVE_FIELDS}
            entry.update(
                {
                    "playlist_url": rec.get("playlist_url") or key,
                    "retired_at": iso_now(),
                    "retire_reason": f"连续 {failures} 天流级验证失败（阈值 {threshold}）",
                    "consecutive_failures": failures,
                    "last_verified": rec.get("last_verified"),
                    "last_stream_count": rec.get("last_stream_count"),
                    "next_review_at": (now() + timedelta(days=review_days)).isoformat(timespec="seconds"),
                    "review_count": 0,
                    "last_review_at": None,
                    "review_history": [],
                }
            )
            graveyard["entries"].append(entry)
            retired_now.append(entry)
            if not args.dry_run:
                del sources[key]
            log.warning("淘汰 %s（失败 %s 次）", truncate(entry["playlist_url"], 80), failures)

    # ---- 3. 仓库陈旧标记
    stale_count = 0
    for rec in sources.values():
        age = days_between(rec.get("last_push")) if rec.get("last_push") else 0
        if rec.get("last_push") and age > stale_days:
            stale_count += 1
            if not args.dry_run:
                rec["repo_stale"] = True
        elif rec.get("repo_stale"):
            if not args.dry_run:
                rec["repo_stale"] = False
    if stale_count:
        log.info("仓库超过 %s 天无 push 的源：%s 个（发现层不再接纳其仓库）", stale_days, stale_count)

    # ---- 4. 墓地回访
    revived, still_dead = [], []
    current = now()
    for entry in list(graveyard["entries"]):
        next_review = entry.get("next_review_at")
        due = args.review_all or not next_review
        if not due:
            try:
                due = current >= datetime.fromisoformat(next_review)
            except ValueError:
                due = True
        if not due:
            continue
        try:
            ok, stream_count, reason = probe_playlist(entry, cfg, log)
        except Exception as exc:
            ok, stream_count, reason = False, 0, f"回访异常：{exc}"

        history = entry.setdefault("review_history", [])
        history.append({"at": iso_now(), "result": "alive" if ok else "dead", "streams": stream_count, "reason": reason})
        entry["review_history"] = history[-10:]
        entry["last_review_at"] = iso_now()
        entry["review_count"] = int(entry.get("review_count") or 0) + 1

        if ok:
            url = entry.get("playlist_url", "")
            record = {
                "source_repo": entry.get("source_repo", ""),
                "playlist_url": url,
                "discovered_via": entry.get("discovered_via") or "revived",
                "discovered_at": entry.get("discovered_at") or iso_now(),
                "last_seen_at": iso_now(),
                "stars": entry.get("stars", 0),
                "last_push": entry.get("last_push"),
                "last_verified": None,
                "consecutive_failures": 0,
                "status": "revived",
                "kind": entry.get("kind") or "m3u",
                "last_stream_count": stream_count,
                "last_valid_count": None,
                "revived_at": iso_now(),
                "revive_count": int(entry.get("review_count") or 0),
                "repo_stale": False,
            }
            if not args.dry_run:
                sources[url] = record
                graveyard["entries"].remove(entry)
            revived.append({"url": url, "streams": stream_count})
            log.info("复活 %s（解析出 %s 条流）", truncate(url, 80), stream_count)
        else:
            entry["next_review_at"] = (current + timedelta(days=review_days)).isoformat(timespec="seconds")
            still_dead.append({"url": entry.get("playlist_url", ""), "reason": reason})
            log.info("回访仍失效 %s：%s（下次回访 %s）", truncate(entry.get("playlist_url", ""), 70), reason, entry["next_review_at"][:10])

    log.info("========== 淘汰与回访完成 ==========")
    log.info("本轮淘汰 %s | 复活 %s | 回访仍失效 %s | 注册表 %s | 墓地 %s", len(retired_now), len(revived), len(still_dead), len(sources), len(graveyard["entries"]))

    if args.dry_run:
        log.info("dry-run：不写入 registry.json / graveyard.json")
    else:
        save_registry(registry)
        save_graveyard(graveyard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
