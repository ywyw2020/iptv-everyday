#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 B：抓取层。

遍历 registry.json 中所有在役源的 playlist_url 下载并解析出「频道名 + 流地址」，
同时合入 sources/local/ 下的人工补充列表。任一单源失败只记日志、不中断流程。

用法：
  python scripts/fetch_sources.py
  python scripts/fetch_sources.py --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    LOCAL_DIR,
    RAW_STREAMS_PATH,
    Config,
    active_sources,
    fetch_text,
    get_logger,
    iso_now,
    load_registry,
    new_session,
    parse_playlist,
    save_registry,
    truncate,
    write_json,
)

MAX_STREAMS_PER_PLAYLIST = 5000

_thread_local = threading.local()


def _session():
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = new_session()
        _thread_local.session = session
    return session


def _local_files() -> list[Path]:
    if not LOCAL_DIR.exists():
        return []
    files = []
    for suffix in ("*.m3u", "*.m3u8", "*.txt"):
        files.extend(sorted(LOCAL_DIR.glob(suffix)))
    return files


def _normalize_streams(items: list[dict], source_repo: str, playlist_url: str, origin: str) -> list[dict]:
    streams = []
    for item in items[:MAX_STREAMS_PER_PLAYLIST]:
        url = (item.get("url") or "").strip()
        name = (item.get("name") or item.get("tvg_name") or "").strip()
        if not url:
            continue
        streams.append(
            {
                "channel": name,
                "url": url,
                "logo": (item.get("logo") or "").strip(),
                "group_hint": (item.get("group") or "").strip(),
                "tvg_id_hint": (item.get("tvg_id") or "").strip(),
                "source_repo": source_repo,
                "playlist_url": playlist_url,
                "origin": origin,
            }
        )
    return streams


def fetch_one(rec: dict, cfg: Config, log) -> dict:
    """抓取单个播放列表，返回 {ok, streams, reason, url}。"""
    url = rec.get("playlist_url", "")
    repo = rec.get("source_repo", "")
    result = {"ok": False, "streams": [], "reason": "", "url": url, "repo": repo}
    try:
        text = fetch_text(
            url,
            timeout=cfg.get_float("fetch", "timeout", 10),
            retries=cfg.get_int("fetch", "retries", 2),
            session=_session(),
            verify=False,
            max_bytes=6 * 1024 * 1024,
        )
        if not text:
            result["reason"] = "下载失败或空响应"
            return result
        items = parse_playlist(text)
        if not items:
            result["reason"] = "解析出 0 条流（可能是 HTML 错误页或格式不支持）"
            return result
        result["streams"] = _normalize_streams(items, repo, url, "remote")
        result["ok"] = True
        return result
    except Exception as exc:  # 单源异常绝不外溢
        result["reason"] = f"异常：{exc}"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取注册表中所有源的播放列表")
    parser.add_argument("--dry-run", action="store_true", help="只抓不写文件/不更新注册表")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（调试用）")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("fetch")
    cfg = Config(args.config) if args.config else Config()
    min_streams = cfg.get_int("fetch", "min_streams", 1)
    workers = max(1, cfg.get_int("fetch", "workers", 8))

    registry = load_registry()
    sources = active_sources(registry)
    targets = list(sources.values())
    if args.limit > 0:
        targets = targets[: args.limit]

    log.info("========== 抓取层启动 %s ==========", iso_now())
    log.info("注册表在役源 %s 个，本次处理 %s 个，并发 %s", len(sources), len(targets), workers)

    all_streams: list[dict] = []
    ok_count = fail_count = 0
    started = time.time()

    if targets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, rec, cfg, log): rec for rec in targets}
            for future in as_completed(futures):
                rec = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "streams": [], "reason": f"线程异常：{exc}", "url": rec.get("playlist_url", "")}

                url = result.get("url", "")
                if result["ok"] and len(result["streams"]) >= min_streams:
                    ok_count += 1
                    all_streams.extend(result["streams"])
                    if not args.dry_run:
                        rec["last_fetch_at"] = iso_now()
                        rec["last_stream_count"] = len(result["streams"])
                        rec["fetch_failures"] = 0
                    log.info("OK   %-45s %4d 条  %s", truncate(rec.get("source_repo") or "-", 45), len(result["streams"]), truncate(url, 60))
                else:
                    fail_count += 1
                    if not args.dry_run:
                        rec["last_fetch_at"] = iso_now()
                        rec["last_stream_count"] = 0
                        rec["fetch_failures"] = int(rec.get("fetch_failures") or 0) + 1
                    log.warning("FAIL %-45s %s", truncate(rec.get("source_repo") or "-", 45), result["reason"])

    # 本地静态补充
    local_streams = 0
    for path in _local_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            items = parse_playlist(text)
            streams = _normalize_streams(items, f"local/{path.name}", f"local/{path.name}", "local")
            all_streams.extend(streams)
            local_streams += len(streams)
            log.info("LOCAL %-40s %4d 条", path.name, len(streams))
        except Exception as exc:
            log.warning("本地文件 %s 解析失败：%s", path, exc)

    # 跨源同 URL 先粗去重，减少下游压力（精细去重交给 deduplicate.py）
    seen, unique = set(), []
    for item in all_streams:
        key = item["url"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    payload = {
        "generated_at": iso_now(),
        "registry_sources": len(sources),
        "sources_ok": ok_count,
        "sources_fail": fail_count,
        "local_streams": local_streams,
        "raw_count": len(all_streams),
        "count": len(unique),
        "streams": unique,
    }

    if args.dry_run:
        log.info("dry-run：解析出 %d 条流（去重后），前 5 条样例：", len(unique))
        for item in unique[:5]:
            log.info("   %s | %s", truncate(item["channel"], 24), truncate(item["url"], 80))
    else:
        write_json(RAW_STREAMS_PATH, payload)
        save_registry(registry)
        log.info("已写入 %s", RAW_STREAMS_PATH)

    log.info(
        "========== 抓取层完成：成功 %s / 失败 %s / 本地 %s 条 / 合计 %s 条（耗时 %.1fs）==========",
        ok_count,
        fail_count,
        local_streams,
        len(unique),
        time.time() - started,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
