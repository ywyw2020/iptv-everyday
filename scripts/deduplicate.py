#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 C：去重引擎（URL + 频道名双重归一化）。

1) URL 归一化：忽略 http/https、主机小写、剔除时间戳类 query 参数；
2) 频道名归一化：全半角、繁简、去噪（高清/HD/备用/线路N）、别名映射，
   且 CCTV-5 与 CCTV-5+ 严格区分；
3) 双重去重：先按 URL 去重，再按归一化频道名分组；
4) 优先级保留：同频道按上一轮实测响应时间排序，保留最快的前 N(+冗余) 条。

用法：
  python scripts/deduplicate.py
  python scripts/deduplicate.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DEDUP_STREAMS_PATH,
    RAW_STREAMS_PATH,
    STREAM_SCORES_PATH,
    Config,
    get_logger,
    iso_now,
    nfkc,
    read_json,
    url_key,
    write_json,
)

# ---------------------------------------------------------------- 频道名归一化

# 常用繁体 -> 简体（只覆盖频道名高频字，避免引入重型依赖）
# 格式：两个字符一组 "繁体简体"，空格分隔；长度不一致的条目会被自动忽略
TRAD_PAIRS = (
    "臺台 視视 衛卫 東东 華华 鳳凤 資资 訊讯 綜综 藝艺 體体 紀纪 財财 經经 劇剧 場场 兒儿 動动 "
    "樂乐 戲戏 農农 業业 軍军 製制 聞闻 際际 語语 廣广 蘇苏 陝陕 遼辽 龍龙 贛赣 貴贵 渝渝 濟济 "
    "濱滨 寧宁 瓊琼 頻频 導导 購购 車车 聽听 畫画 質质 標标 灣湾 縣县 區区 鄉乡 鎮镇 環环 娛娱 "
    "歡欢 貨货 網网 絡络 電电 話话 節节 時时 點点 鐘钟 線线 綫线 選选 擇择 開开 關关 傳传 統统 "
    "陽阳 陰阴 雲云 馬马 鳥鸟 魚鱼 鴻鸿 鷹鹰 麗丽 豐丰 齡龄 歲岁 萬万 億亿 號号 檔档 專专 學学 "
    "習习 師师 範范 圖图 書书 報报 紙纸 郵邮 編编 輯辑 譯译 賽赛 隊队 員员 團团 會会 議议 廳厅 "
    "館馆 廚厨 廠厂 幣币 價价 賣卖 買买 貸贷 賬账 單单 據据 稅税 銀银 鐵铁 鋼钢 礦矿 輪轮 艦舰 "
    "戰战 鬥斗 擊击 傷伤 醫医 藥药 療疗 護护 險险 證证 遞递 運运 輸输 邊边 遠远 進进 過过 這这 "
    "裡里 麼么 誰谁 們们 個个 兩两 幾几 髮发 發发 醜丑 彎弯 潛潜 濕湿 溫温 燈灯 燦灿 爛烂 爺爷 "
    "媽妈 豬猪 雞鸡 蝦虾 麵面 鹹咸 醬酱 滷卤 磚砖 籤签 讚赞 釀酿 準准 儘尽 傑杰 啟启 遊游 曆历"
)
TRAD2SIMP = {}
for _pair in TRAD_PAIRS.split():
    if len(_pair) == 2 and _pair[0] != _pair[1]:
        TRAD2SIMP[ord(_pair[0])] = _pair[1]

BRACKET_RE = re.compile(r"[【\[（(][^】\]）)]{0,24}[】\]）)]")
QUALITY_RE = re.compile(
    r"(?:[-_ .·]*(?:1080p?|720p?|2160p?|4k|8k|uhd|fhd|hd|sd|ipv6|ipv4|hevc|h265|h264|高清|超清|蓝光|标清|原画|流畅))+$",
    re.I,
)
QUALITY_TAIL_RE = re.compile(r"(?:高清|超清|蓝光|标清|原画|流畅|测试|备用源?|直播源?|线路?\d+|第?\d+线|\d+号源)+$")
CCTV_DIGITS_RE = re.compile(
    r"^(?:cctv|央视|中央|中央电视台|中国中央电视台)[-_ ]*([0-9]{1,2}|[一二三四五六七八九十]{1,3})(\+|plus|佳)?"
)
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

CCTV_DISPLAY = {
    "cctv1": "CCTV-1 综合",
    "cctv2": "CCTV-2 财经",
    "cctv3": "CCTV-3 综艺",
    "cctv4": "CCTV-4 中文国际",
    "cctv5": "CCTV-5 体育",
    "cctv5+": "CCTV-5+ 体育赛事",
    "cctv6": "CCTV-6 电影",
    "cctv7": "CCTV-7 国防军事",
    "cctv8": "CCTV-8 电视剧",
    "cctv9": "CCTV-9 纪录",
    "cctv10": "CCTV-10 科教",
    "cctv11": "CCTV-11 戏曲",
    "cctv12": "CCTV-12 社会与法",
    "cctv13": "CCTV-13 新闻",
    "cctv14": "CCTV-14 少儿",
    "cctv15": "CCTV-15 音乐",
    "cctv16": "CCTV-16 奥林匹克",
    "cctv17": "CCTV-17 农业农村",
    "cctv4k": "CCTV-4K 超高清",
}

# 别名表：只做“频道级”精确映射，绝不做模糊包含匹配，避免 CCTV-5 与 CCTV-5+ 误合并
ALIASES = {
    "央视一套": "cctv1",
    "央视二套": "cctv2",
    "央视三套": "cctv3",
    "央视四套": "cctv4",
    "央视五套": "cctv5",
    "央视六套": "cctv6",
    "央视七套": "cctv7",
    "央视八套": "cctv8",
    "央视九套": "cctv9",
    "央视十套": "cctv10",
    "央视十一套": "cctv11",
    "央视十二套": "cctv12",
    "央视十三套": "cctv13",
    "央视十四套": "cctv14",
    "央视十五套": "cctv15",
    "cctv5plus": "cctv5+",
    "cctv5佳": "cctv5+",
    "中央五套": "cctv5",
    "中国教育1台": "cetv1",
    "中国教育一台": "cetv1",
    "中国教育2台": "cetv2",
    "中国教育4台": "cetv4",
}

PROVINCE_ALIAS = {
    "湖南": "湖南卫视",
    "浙江": "浙江卫视",
    "江苏": "江苏卫视",
    "东方": "东方卫视",
    "北京": "北京卫视",
    "深圳": "深圳卫视",
    "广东": "广东卫视",
    "安徽": "安徽卫视",
    "山东": "山东卫视",
    "湖北": "湖北卫视",
    "天津": "天津卫视",
    "四川": "四川卫视",
    "重庆": "重庆卫视",
    "辽宁": "辽宁卫视",
    "黑龙江": "黑龙江卫视",
    "河南": "河南卫视",
    "河北": "河北卫视",
    "江西": "江西卫视",
    "福建": "福建卫视",
    "东南": "东南卫视",
    "贵州": "贵州卫视",
    "云南": "云南卫视",
    "广西": "广西卫视",
    "甘肃": "甘肃卫视",
    "宁夏": "宁夏卫视",
    "青海": "青海卫视",
    "新疆": "新疆卫视",
    "西藏": "西藏卫视",
    "内蒙古": "内蒙古卫视",
    "吉林": "吉林卫视",
    "山西": "山西卫视",
    "陕西": "陕西卫视",
    "海南": "海南卫视",
    "三沙": "三沙卫视",
    "厦门": "厦门卫视",
    "海峡": "海峡卫视",
    "康巴": "康巴卫视",
    "延边": "延边卫视",
    "兵团": "兵团卫视",
    "农林": "农林卫视",
}

SPORT_WORDS = ("体育", "足球", "篮球", "网球", "高尔夫", "赛事", "竞技", "电竞", "乒乓", "羽毛球", "台球", "赛车", "搏击", "武术")
DOC_WORDS = ("纪录", "纪实", "探索", "发现", "地理", "人文", "历史", "discovery", "documentary", "国家地理")
KIDS_WORDS = ("少儿", "卡通", "动画", "动漫", "亲子", "幼教", "kids", "cartoon", "少儿频道")
INTL_WORDS = (
    "国际",
    "环球",
    "世界",
    "海外",
    "cgtn",
    "凤凰",
    "tvb",
    "翡翠",
    "明珠",
    "亚视",
    "澳亚",
    "莲花",
    "香港",
    "澳门",
    "台湾",
    "中天",
    "东森",
    "三立",
    "民视",
    "nhk",
    "bbc",
    "cnn",
    "fox",
    "cnbc",
    "bloomberg",
    "aljazeera",
    "euronews",
    "dw",
    "france24",
    "arirang",
    "kbs",
    "mbc",
    "sbs",
)


def _cn_to_int(text: str) -> int:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + CN_NUM.get(text[1:], 0)
    if text.endswith("十"):
        return CN_NUM.get(text[:-1], 0) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return CN_NUM.get(left, 0) * 10 + CN_NUM.get(right, 0)
    return CN_NUM.get(text, 0)


def base_norm(name: str) -> str:
    """全半角统一 + 去括号噪声 + 去尾缀 + 繁转简 + 去符号。"""
    text = nfkc(name or "").lower()
    text = BRACKET_RE.sub("", text)
    text = QUALITY_RE.sub("", text)
    text = text.translate(TRAD2SIMP)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff+]+", "", text)
    text = QUALITY_TAIL_RE.sub("", text)
    return text


def canon_key(name: str) -> str:
    """返回频道级归一化键，如 cctv1 / cctv5+ / 湖南ws。"""
    compact = base_norm(name)
    if not compact:
        return ""
    if compact in ALIASES:
        return ALIASES[compact]
    if compact in PROVINCE_ALIAS:
        province = re.sub(r"卫视$", "", base_norm(PROVINCE_ALIAS[compact]))
        return f"{province}ws"
    if compact.startswith("cctv4k") or compact.startswith("cctv8k"):
        return "cctv4k"
    match = CCTV_DIGITS_RE.match(compact)
    if match:
        digits, plus = match.group(1), match.group(2)
        number = int(digits) if digits.isdigit() else _cn_to_int(digits)
        if 1 <= number <= 20:
            return f"cctv{number}+" if plus else f"cctv{number}"
    ws = re.match(r"^(.+?)卫视", compact)
    if ws:
        return f"{ws.group(1)}ws"
    return compact


def classify(name: str, key: str) -> str:
    compact = base_norm(name) + key
    if "cgtn" in compact:
        return "国际"
    if key.startswith("cctv") or key.startswith("cetv"):
        return "央视"
    if key.endswith("ws") or "卫视" in compact:
        return "卫视"
    if any(word in compact for word in SPORT_WORDS):
        return "体育"
    if any(word in compact for word in DOC_WORDS):
        return "纪录"
    if any(word in compact for word in KIDS_WORDS):
        return "少儿"
    if any(word in compact for word in INTL_WORDS):
        return "国际"
    return "其他"


def display_name(name: str, key: str) -> str:
    if key in CCTV_DISPLAY:
        return CCTV_DISPLAY[key]
    cleaned = nfkc(name or "").strip()
    cleaned = BRACKET_RE.sub(" ", cleaned)
    cleaned = QUALITY_RE.sub("", cleaned)
    cleaned = cleaned.translate(TRAD2SIMP)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -,|")
    return cleaned or key


# ---------------------------------------------------------------- 主流程


def deduplicate(cfg: Config, log, dry_run: bool = False) -> dict:
    payload = read_json(RAW_STREAMS_PATH, None)
    if not payload or not payload.get("streams"):
        log.warning("没有可去重的原始数据（%s 为空），请先运行 fetch_sources.py", RAW_STREAMS_PATH)
        return {"count": 0, "channels": 0, "streams": []}

    strip_keys = {item.lower() for item in cfg.get_list("dedupe", "strip_query_keys", ["t", "ts", "timestamp"])}
    keep = max(1, cfg.get_int("dedupe", "keep_per_channel", 3))
    extra = max(0, cfg.get_int("dedupe", "extra_candidates", 2))
    min_len = cfg.get_int("dedupe", "min_url_length", 12)

    scores = read_json(STREAM_SCORES_PATH, {}) or {}
    raw_streams = payload.get("streams") or []

    # 第 1 步：URL 去重
    by_url: dict[str, dict] = {}
    dropped_invalid = 0
    for item in raw_streams:
        url = (item.get("url") or "").strip()
        if len(url) < min_len or url in (item.get("playlist_url"),):
            dropped_invalid += 1
            continue
        key = url_key(url, strip_keys)
        current = by_url.get(key)
        if current is None:
            by_url[key] = item
        else:
            # 优先保留"有频道名 / 本地补充"的那一条
            if not current.get("channel") and item.get("channel"):
                by_url[key] = item
    url_deduped = len(by_url)

    # 第 2 步：频道名归一化分组
    grouped: dict[str, list[dict]] = {}
    unnamed = 0
    for key, item in by_url.items():
        name = item.get("channel") or ""
        ckey = canon_key(name)
        if not ckey or len(ckey) < 1:
            unnamed += 1
            ckey = f"__noname__{key}"  # 无名流单独成组，仍可被保留
        score = scores.get(key) or {}
        latency = score.get("latency_ms")
        enriched = {
            "channel": display_name(name, ckey),
            "channel_key": ckey,
            "group": classify(name, ckey) if not ckey.startswith("__noname__") else "其他",
            "url": item.get("url", ""),
            "url_key": key,
            "logo": item.get("logo", ""),
            "source_repo": item.get("source_repo", ""),
            "playlist_url": item.get("playlist_url", ""),
            "origin": item.get("origin", "remote"),
            "prev_latency_ms": latency,
        }
        grouped.setdefault(ckey, []).append(enriched)

    # 第 3 步：组内按上一轮响应时间排序，保留 keep + extra
    selected: list[dict] = []
    trimmed = 0
    limit = keep + extra
    for ckey, items in grouped.items():
        items.sort(key=lambda x: (x["prev_latency_ms"] if isinstance(x["prev_latency_ms"], (int, float)) else 10**9, len(x["url"])))
        if len(items) > limit:
            trimmed += len(items) - limit
        for rank, item in enumerate(items[:limit], start=1):
            item["rank_in_channel"] = rank
            selected.append(item)

    # 组序：配置顺序 + 其他，再按频道名
    order = cfg.get_list("general", "groups", ["央视", "卫视", "国际", "体育", "纪录", "少儿"]) + ["其他"]
    order_index = {name: idx for idx, name in enumerate(order)}
    selected.sort(key=lambda x: (order_index.get(x["group"], len(order)), x["channel_key"]))

    stats = {
        "generated_at": iso_now(),
        "in_count": len(raw_streams),
        "url_deduped": url_deduped,
        "dropped_invalid": dropped_invalid,
        "unnamed": unnamed,
        "trimmed": trimmed,
        "channels": len(grouped),
        "count": len(selected),
        "keep_per_channel": keep,
        "extra_candidates": extra,
        "streams": selected,
    }

    log.info("========== 去重结果 ==========")
    log.info(
        "输入 %s 条 -> URL 去重后 %s 条 -> 频道组 %s 个 -> 保留候选 %s 条（%s 条被裁掉，%s 条无效 URL）",
        stats["in_count"],
        url_deduped,
        stats["channels"],
        stats["count"],
        trimmed,
        dropped_invalid,
    )
    group_count: dict[str, int] = {}
    for item in selected:
        group_count[item["group"]] = group_count.get(item["group"], 0) + 1
    log.info("分组分布：%s", "，".join(f"{k} {v} 条" for k, v in group_count.items()) or "无")
    for item in selected[:5]:
        log.info("   样例 %s | %s", item["channel"], item["url"][:80])

    if dry_run:
        log.info("dry-run：不写入 %s", DEDUP_STREAMS_PATH)
    else:
        write_json(DEDUP_STREAMS_PATH, stats)
        log.info("已写入 %s", DEDUP_STREAMS_PATH)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="IPTV 源去重")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("dedupe")
    cfg = Config(args.config) if args.config else Config()
    try:
        deduplicate(cfg, log, args.dry_run)
    except Exception as exc:
        log.exception("去重流程异常：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
