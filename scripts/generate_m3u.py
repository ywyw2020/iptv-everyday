#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 F：M3U / TXT 生成器。

输入：output/work/validated_streams.json（缺失时降级用 dedup_streams.json）
输出：output/iptv.m3u（UTF-8 无 BOM）与 output/iptv.txt

用法：
  python scripts/generate_m3u.py
  python scripts/generate_m3u.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DEDUP_STREAMS_PATH,
    ROOT,
    VALIDATED_STREAMS_PATH,
    Config,
    atomic_write_text,
    get_logger,
    human_now,
    iso_now,
    read_json,
    safe_slug,
)

EXTRA_GROUP = "其他"


def _tvg_id(stream: dict) -> str:
    key = (stream.get("channel_key") or "").strip()
    if key and re.fullmatch(r"[a-z0-9+_-]+", key):
        return key.replace("+", "plus")
    return safe_slug(stream.get("channel") or stream.get("tvg_id_hint") or "ch")


def _logo(stream: dict, logo_base: str, tvg_id: str) -> str:
    logo = (stream.get("logo") or "").strip()
    if logo.lower().startswith(("http://", "https://")):
        return logo
    if logo_base:
        return logo_base.rstrip("/") + "/" + tvg_id + ".png"
    return ""


def build_files(cfg: Config, log, dry_run: bool = False) -> dict:
    payload = read_json(VALIDATED_STREAMS_PATH, None)
    source_name = "validated"
    if not payload or not payload.get("streams"):
        payload = read_json(DEDUP_STREAMS_PATH, None)
        source_name = "dedup"
        if payload and payload.get("streams"):
            log.warning("未找到验证结果，降级使用去重结果（未经 ffmpeg 真实播放验证）")

    streams = (payload or {}).get("streams") or []
    keep = max(1, cfg.get_int("dedupe", "keep_per_channel", 3))
    max_channels = cfg.get_int("generate", "max_channels", 0)
    groups = cfg.get_list("general", "groups", ["央视", "卫视", "国际", "体育", "纪录", "少儿"]) + [EXTRA_GROUP]
    group_index = {name: idx for idx, name in enumerate(groups)}
    epg = cfg.get("generate", "epg_url", "https://epg.pw/api/epg.xml")
    logo_base = cfg.get("generate", "logo_base", "")

    # 同频道只保留最快的前 N 条
    counter: dict[str, int] = {}
    picked: list[dict] = []
    for stream in streams:
        key = stream.get("channel_key") or _tvg_id(stream)
        if counter.get(key, 0) >= keep:
            continue
        counter[key] = counter.get(key, 0) + 1
        picked.append(stream)

    picked.sort(key=lambda s: (group_index.get(s.get("group") or EXTRA_GROUP, len(groups)), s.get("channel_key") or ""))
    if max_channels > 0:
        picked = picked[:max_channels]

    # ---- M3U
    lines = [f'#EXTM3U url-tvg="{epg}" x-tvg-url="{epg}"']
    lines.append(f"# 更新时间：{human_now()}")
    if not picked:
        lines.append("# 本批次无有效源")
        log.warning("本批次无任何有效源，m3u 头部已标注")
    else:
        stats = (payload or {}).get("valid", len(picked))
        lines.append(f"# 有效频道：{len(picked)} 条 / {len(counter)} 个频道（候选池 {stats} 条，数据源：{source_name}）")
    lines.append("# 由 iptv-everyday 自动生成，请勿手动编辑")

    for stream in picked:
        name = (stream.get("channel") or "").strip() or "未命名频道"
        tvg_id = _tvg_id(stream)
        logo = _logo(stream, logo_base, tvg_id)
        group = stream.get("group") or EXTRA_GROUP
        attrs = [f'tvg-id="{tvg_id}"', f'tvg-name="{name}"']
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'group-title="{group}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{name}")
        lines.append(stream.get("url", ""))

    m3u_text = "\n".join(lines) + "\n"

    # ---- TXT（DIYP 风格：分组,#genre# + 频道名,地址）
    txt_lines: list[str] = []
    if not picked:
        txt_lines.append("# 本批次无有效源")
    else:
        current_group = None
        for stream in picked:
            group = stream.get("group") or EXTRA_GROUP
            if group != current_group:
                txt_lines.append(f"{group},#genre#")
                current_group = group
            name = (stream.get("channel") or "").strip() or "未命名频道"
            txt_lines.append(f"{name},{stream.get('url', '')}")
    txt_text = "\n".join(txt_lines) + "\n"

    m3u_path = ROOT / cfg.get("generate", "output_m3u", "output/iptv.m3u")
    txt_path = ROOT / cfg.get("generate", "output_txt", "output/iptv.txt")

    log.info("========== 生成结果 ==========")
    log.info("候选 %s 条 -> 输出 %s 条 / %s 个频道（每频道上限 %s）", len(streams), len(picked), len({s.get('channel_key') for s in picked}), keep)
    group_count: dict[str, int] = {}
    for stream in picked:
        group = stream.get("group") or EXTRA_GROUP
        group_count[group] = group_count.get(group, 0) + 1
    log.info("分组分布：%s", "，".join(f"{k} {v}" for k, v in group_count.items()) or "无")

    if dry_run:
        log.info("dry-run：不写入文件，m3u 预览（前 20 行）：")
        for line in m3u_text.splitlines()[:20]:
            log.info("   %s", line)
        return {"count": len(picked), "channels": len(counter)}

    atomic_write_text(m3u_path, m3u_text, encoding="utf-8")
    atomic_write_text(txt_path, txt_text, encoding="utf-8")
    log.info("已写入 %s 与 %s（UTF-8 无 BOM）", m3u_path, txt_path)
    return {
        "generated_at": iso_now(),
        "count": len(picked),
        "channels": len(counter),
        "m3u": str(m3u_path),
        "txt": str(txt_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 iptv.m3u / iptv.txt")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("generate")
    cfg = Config(args.config) if args.config else Config()
    try:
        build_files(cfg, log, args.dry_run)
    except Exception as exc:
        log.exception("生成失败：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
