#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 D：流级有效性验证（严格三层）。

  第 1 层  HTTP 探测：状态码 200/206/301/302，超时 5s
  第 2 层  码流探测：ffprobe 必须返回 codec 信息（排除 HTML 错误页伪流）
  第 3 层  真实播放：ffmpeg -i <url> -t 5 -f null -，要求正常退出

同时输出每条流的响应时间、分辨率、编解码、是否疑似地理封锁，
并将结果回写 registry.json 的 last_verified / consecutive_failures。

用法：
  python scripts/validate_streams.py
  python scripts/validate_streams.py --dry-run --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DEDUP_STREAMS_PATH,
    LOG_DIR,
    RAW_STREAMS_PATH,
    STREAM_SCORES_PATH,
    VALIDATED_STREAMS_PATH,
    Config,
    get_logger,
    http_request,
    iso_now,
    load_registry,
    read_json,
    save_registry,
    today_tag,
    truncate,
    url_key,
    write_json,
)

FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or ""
FFPROBE = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe") or ""

GEO_HINTS = ("geo", "not available in your country", "地区", "地域限制", "地区限制", "blocked in your")


# ---------------------------------------------------------------- 三层验证


def layer1_http(url: str, timeout: float, verify: bool) -> dict:
    started = time.perf_counter()
    result = {"ok": False, "status": None, "latency_ms": None, "geo_blocked": False, "reason": ""}
    resp = http_request("HEAD", url, timeout=timeout, retries=0, verify=verify)
    status = None
    if resp is not None:
        status = resp.status_code
        try:
            resp.close()
        except Exception:
            pass
    if status is None or status >= 400 or status == 405:
        # 大量 HLS 服务器拒绝 HEAD，降级为带 Range 的 GET 探测
        resp = http_request(
            "GET",
            url,
            timeout=timeout,
            retries=0,
            verify=verify,
            headers={"Range": "bytes=0-2047"},
            stream=True,
        )
        if resp is not None:
            status = resp.status_code
            snippet = ""
            try:
                snippet = next(resp.iter_content(512), b"").decode("utf-8", errors="ignore").lower()
            except Exception:
                snippet = ""
            try:
                resp.close()
            except Exception:
                pass
            if status in (403, 451) or any(hint in snippet for hint in GEO_HINTS if hint.isascii()):
                result["geo_blocked"] = True

    result["status"] = status
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    if status in (200, 206, 301, 302, 303, 307, 308):
        result["ok"] = True
    else:
        result["reason"] = f"HTTP {status}" if status else "连接失败/超时"
    return result


def layer2_probe(url: str, timeout: float) -> dict:
    result = {"ok": False, "codec": "", "width": None, "height": None, "reason": ""}
    if not FFPROBE:
        result["reason"] = "ffprobe 未安装"
        return result
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-rw_timeout",
        str(int(timeout * 1000000)),
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, errors="ignore")
    except subprocess.TimeoutExpired:
        result["reason"] = "ffprobe 超时"
        return result
    except Exception as exc:
        result["reason"] = f"ffprobe 异常：{exc}"
        return result

    if proc.returncode != 0 and not (proc.stdout or "").strip():
        result["reason"] = "ffprobe 无法解析流"
        return result
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result["reason"] = "ffprobe 输出非 JSON"
        return result

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("codec_name")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio" and s.get("codec_name")), None)
    picked = video or audio
    if not picked:
        result["reason"] = "无有效 codec 信息（疑似 HTML 错误页）"
        return result
    result.update(
        {
            "ok": True,
            "codec": picked.get("codec_name") or "",
            "width": picked.get("width"),
            "height": picked.get("height"),
            "has_video": bool(video),
        }
    )
    return result


def layer3_play(url: str, duration: int, timeout: float) -> dict:
    result = {"ok": False, "reason": ""}
    if not FFMPEG:
        result["reason"] = "ffmpeg 未安装"
        return result
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-rw_timeout", str(int(timeout * 1000000))]
    cmd += ["-i", url, "-t", str(duration), "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, errors="ignore")
    except subprocess.TimeoutExpired:
        result["reason"] = "ffmpeg 试播超时"
        return result
    except Exception as exc:
        result["reason"] = f"ffmpeg 异常：{exc}"
        return result

    stderr = (proc.stderr or "").strip()
    if proc.returncode == 0:
        result["ok"] = True
        return result
    low = stderr.lower()
    if "invalid data found" in low or "could not find codec" in low or "protocol not found" in low:
        result["reason"] = "码流无效/协议不支持"
    else:
        result["reason"] = f"ffmpeg 退出码 {proc.returncode}"
    result["stderr"] = truncate(stderr.splitlines()[-1] if stderr else "", 160)
    return result


def validate_one(stream: dict, cfg: Config) -> dict:
    url = stream.get("url", "")
    result = {
        "channel": stream.get("channel", ""),
        "channel_key": stream.get("channel_key", ""),
        "group": stream.get("group", ""),
        "url": url,
        "url_key": stream.get("url_key") or url_key(url),
        "logo": stream.get("logo", ""),
        "source_repo": stream.get("source_repo", ""),
        "playlist_url": stream.get("playlist_url", ""),
        "origin": stream.get("origin", "remote"),
        "ok": False,
        "failed_layer": 0,
        "http_status": None,
        "latency_ms": None,
        "codec": "",
        "width": None,
        "height": None,
        "resolution": "",
        "geo_blocked": False,
        "reason": "",
        "checked_at": iso_now(),
    }
    try:
        http_timeout = cfg.get_float("validate", "http_timeout", 5)
        probe_timeout = cfg.get_float("validate", "ffprobe_timeout", 20)
        play_timeout = cfg.get_float("validate", "ffmpeg_timeout", 25)
        duration = cfg.get_int("validate", "ffmpeg_duration", 5)
        verify = cfg.get_bool("general", "verify_tls", False)

        first = layer1_http(url, http_timeout, verify)
        result.update({"http_status": first["status"], "latency_ms": first["latency_ms"], "geo_blocked": first["geo_blocked"]})
        if not first["ok"]:
            result["failed_layer"] = 1
            result["reason"] = first["reason"]
            return result

        second = layer2_probe(url, probe_timeout)
        if not second["ok"]:
            # ffprobe 缺失时降级：不算失败层，仅标注
            if "未安装" in (second.get("reason") or ""):
                result["reason"] = "ffprobe 未安装，已降级为仅 HTTP 验证"
                result["ok"] = True
                return result
            result["failed_layer"] = 2
            result["reason"] = second["reason"]
            return result
        result.update(
            {
                "codec": second.get("codec", ""),
                "width": second.get("width"),
                "height": second.get("height"),
                "resolution": f"{second.get('width')}x{second.get('height')}" if second.get("width") else "",
            }
        )

        third = layer3_play(url, duration, play_timeout)
        if not third["ok"]:
            if "未安装" in (third.get("reason") or ""):
                result["ok"] = True
                result["reason"] = "ffmpeg 未安装，已降级为 HTTP+ffprobe 验证"
                return result
            result["failed_layer"] = 3
            result["reason"] = third["reason"]
            return result

        result["ok"] = True
        return result
    except Exception as exc:  # 单条流异常不得中断整体
        result["failed_layer"] = 9
        result["reason"] = f"验证异常：{exc}"
        return result


# ---------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser(description="流级三层验证")
    parser.add_argument("--dry-run", action="store_true", help="只验证不落盘、不回写注册表")
    parser.add_argument("--limit", type=int, default=0, help="只验证前 N 条（dry-run 默认 20 条）")
    parser.add_argument("--workers", type=int, default=0, help="并发线程数（覆盖配置）")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("validate", extra_files=[LOG_DIR / f"verify_{today_tag()}.log"])
    cfg = Config(args.config) if args.config else Config()

    payload = read_json(DEDUP_STREAMS_PATH, None)
    if not payload or not payload.get("streams"):
        payload = read_json(RAW_STREAMS_PATH, None)
        if payload and payload.get("streams"):
            log.warning("未找到去重结果，降级使用 raw_streams.json（未去重）")
    if not payload or not payload.get("streams"):
        log.warning("没有任何待验证流（注册表可能为空或全部抓取失败），本轮跳过验证")
        return 0

    streams = payload["streams"]
    limit = args.limit or (20 if args.dry_run else 0)
    if limit > 0:
        streams = streams[:limit]
    workers = args.workers or max(1, cfg.get_int("validate", "workers", 20))

    log.info("========== 验证层启动 %s ==========", iso_now())
    log.info(
        "待验证 %s 条 | 并发 %s | ffmpeg=%s | ffprobe=%s",
        len(streams),
        workers,
        FFMPEG or "缺失",
        FFPROBE or "缺失",
    )
    if not FFMPEG or not FFPROBE:
        log.warning("⚠ ffmpeg/ffprobe 缺失：本次将降级为弱验证，强烈建议安装 ffmpeg 后重跑")

    started = time.time()
    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(validate_one, stream, cfg): stream for stream in streams}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                stream = futures[future]
                result = {
                    "channel": stream.get("channel", ""),
                    "url": stream.get("url", ""),
                    "ok": False,
                    "failed_layer": 9,
                    "reason": f"线程异常：{exc}",
                }
            results.append(result)
            done += 1
            if done % 50 == 0 or done == len(streams):
                elapsed = time.time() - started
                log.info("进度 %s/%s | 已通过 %s 条 | 用时 %.0fs", done, len(streams), sum(1 for r in results if r["ok"]), elapsed)

    valid = [r for r in results if r.get("ok")]
    valid.sort(key=lambda r: (r.get("latency_ms") or 10**9))

    stats = {
        "generated_at": iso_now(),
        "workers": workers,
        "total": len(results),
        "http_ok": sum(1 for r in results if (r.get("failed_layer") or 0) > 1 or r.get("ok")),
        "probe_ok": sum(1 for r in results if (r.get("failed_layer") or 0) > 2 or r.get("ok")),
        "play_ok": len(valid),
        "valid": len(valid),
        "geo_blocked": sum(1 for r in results if r.get("geo_blocked")),
        "failed": len(results) - len(valid),
        "elapsed_seconds": round(time.time() - started, 1),
        "degraded": not (FFMPEG and FFPROBE),
        "streams": valid,
        "failures": [
            {"url": r.get("url"), "layer": r.get("failed_layer"), "reason": r.get("reason")}
            for r in results
            if not r.get("ok")
        ][:200],
    }

    log.info("========== 验证结果 ==========")
    log.info("总计 %s | 通过 %s | 失败 %s | 疑似地理封锁 %s | 用时 %.1fs", stats["total"], stats["valid"], stats["failed"], stats["geo_blocked"], stats["elapsed_seconds"])
    by_layer: dict[int, int] = {}
    for item in stats["failures"]:
        by_layer[item["layer"]] = by_layer.get(item["layer"], 0) + 1
    if by_layer:
        log.info("失败层分布：%s", "，".join(f"第{k}层 {v} 条" for k, v in sorted(by_layer.items())))
    for item in valid[:5]:
        log.info("   有效 %s | %s | %s | %sms", truncate(item.get("channel") or "-", 20), item.get("codec") or "-", item.get("resolution") or "-", item.get("latency_ms"))

    # 注册表回写：按源聚合
    registry = load_registry()
    per_source: dict[str, dict] = {}
    for result in results:
        source = result.get("playlist_url") or ""
        if not source:
            continue
        bucket = per_source.setdefault(source, {"total": 0, "valid": 0})
        bucket["total"] += 1
        if result.get("ok"):
            bucket["valid"] += 1

    for source, bucket in per_source.items():
        rec = registry["sources"].get(source)
        if not rec:
            continue
        rec["last_verified"] = iso_now()
        rec["last_valid_count"] = bucket["valid"]
        rec["last_total_checked"] = bucket["total"]
        # 连续失败计数由 retire_sources.py 统一结算（每日一次），此处只更新事实字段
        if bucket["valid"] > 0 and rec.get("status") in ("discovered", "revived"):
            rec["status"] = "verified"

    if args.dry_run:
        log.info("dry-run：不写入验证结果与注册表")
    else:
        write_json(VALIDATED_STREAMS_PATH, stats)
        strip_keys = {k.lower() for k in cfg.get_list("dedupe", "strip_query_keys", ["t", "ts", "timestamp"])}
        scores = {url_key(r["url"], strip_keys): {"latency_ms": r.get("latency_ms"), "ok": True, "validated_at": r.get("checked_at")} for r in valid if r.get("url")}
        write_json(STREAM_SCORES_PATH, scores)
        save_registry(registry)
        log.info("已写入 %s 与 %s", VALIDATED_STREAMS_PATH, STREAM_SCORES_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
