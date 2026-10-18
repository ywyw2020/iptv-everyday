# -*- coding: utf-8 -*-
"""iptv-everyday 公共基础设施。

包含：路径与时间、配置读取、日志、HTTP 请求（浏览器 UA 伪装 + 超时 + 重试）、
注册表/墓地读写、播放列表（M3U / TXT）解析。

设计约束：本模块不包含任何具体 IPTV 源地址，也不做任何"记忆式"资源列举。
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

# ---------------------------------------------------------------- 路径常量

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.ini"

SOURCES_DIR = ROOT / "sources"
LOCAL_DIR = SOURCES_DIR / "local"
SEEDS_PATH = SOURCES_DIR / "seeds.json"
REGISTRY_PATH = SOURCES_DIR / "registry.json"
GRAVEYARD_PATH = SOURCES_DIR / "graveyard.json"

OUTPUT_DIR = ROOT / "output"
WORK_DIR = OUTPUT_DIR / "work"
LOG_DIR = OUTPUT_DIR / "log"

RAW_STREAMS_PATH = WORK_DIR / "raw_streams.json"
DEDUP_STREAMS_PATH = WORK_DIR / "dedup_streams.json"
VALIDATED_STREAMS_PATH = WORK_DIR / "validated_streams.json"
STREAM_SCORES_PATH = WORK_DIR / "stream_scores.json"
DISCOVER_REPORT_PATH = WORK_DIR / "discover_report.json"

# ---------------------------------------------------------------- 时间工具

try:
    from zoneinfo import ZoneInfo

    BJT = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 无 tzdata 时退回固定 UTC+8（中国无夏令时）
    BJT = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(BJT)


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def human_now() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def today_tag() -> str:
    return now().strftime("%Y%m%d")


def parse_dt(value) -> datetime | None:
    """把注册表里的各种时间表示解析为带时区的 datetime。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=BJT)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=BJT)


def days_between(earlier, later=None) -> float:
    a = parse_dt(earlier)
    if a is None:
        return 10**6  # 无法解析视为"久远"
    b = parse_dt(later) or now()
    return (b - a).total_seconds() / 86400.0


def stamp() -> str:
    return now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- 配置


class Config:
    """config.ini 的薄封装，带类型安全与默认值。"""

    def __init__(self, path: Path | str = CONFIG_PATH):
        self.path = Path(path)
        self.parser = configparser.ConfigParser(inline_comment_prefixes=(";",))
        if self.path.exists():
            self.parser.read(self.path, encoding="utf-8")

    def get(self, section: str, key: str, default: str = "") -> str:
        try:
            return self.parser.get(section, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def get_int(self, section: str, key: str, default: int = 0) -> int:
        raw = self.get(section, key, "")
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def get_float(self, section: str, key: str, default: float = 0.0) -> float:
        raw = self.get(section, key, "")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        raw = self.get(section, key, "").lower()
        if raw in ("1", "true", "yes", "on", "y"):
            return True
        if raw in ("0", "false", "no", "off", "n"):
            return False
        return default

    def get_list(self, section: str, key: str, default=None) -> list[str]:
        raw = self.get(section, key, "")
        if not raw:
            return list(default or [])
        return [item.strip() for item in re.split(r"[,|\n]", raw) if item.strip()]


# ---------------------------------------------------------------- 日志


def add_file_handler(logger: logging.Logger, path: Path, level=logging.INFO) -> None:
    path = Path(path)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(path, mode="a", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)


def get_logger(name: str, extra_files: list[Path] | None = None) -> logging.Logger:
    """统一日志：控制台 + output/log/update_YYYYMMDD.log（+ 模块专属日志）。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(stream)
        logger.propagate = False
    add_file_handler(logger, LOG_DIR / f"update_{today_tag()}.log")
    for extra in extra_files or []:
        add_file_handler(logger, Path(extra))
    return logger


# ---------------------------------------------------------------- 文件 IO


def ensure_dirs() -> None:
    for path in (SOURCES_DIR, LOCAL_DIR, OUTPUT_DIR, WORK_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path: Path | str, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path | str, data, dry_run: bool = False) -> None:
    if dry_run:
        return
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def decode_body(content: bytes, declared: str | None = None) -> str:
    candidates = [declared, "utf-8-sig", "utf-8", "gb18030", "big5"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- 注册表


def load_registry(path: Path | str = REGISTRY_PATH) -> dict:
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    sources = data.get("sources")
    if isinstance(sources, list):  # 兼容早期列表结构
        converted = {}
        for item in sources:
            if isinstance(item, dict) and item.get("playlist_url"):
                converted[item["playlist_url"]] = item
        data["sources"] = converted
    elif not isinstance(sources, dict):
        data["sources"] = {}
    data.setdefault("version", 1)
    data.setdefault("updated_at", None)
    return data


def save_registry(data: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    data["updated_at"] = iso_now()
    data["sources"] = {key: data["sources"][key] for key in sorted(data["sources"])}
    write_json(REGISTRY_PATH, data)


def load_graveyard(path: Path | str = GRAVEYARD_PATH) -> dict:
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("entries", [])
    if not isinstance(data["entries"], list):
        data["entries"] = []
    return data


def save_graveyard(data: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    data["updated_at"] = iso_now()
    write_json(GRAVEYARD_PATH, data)


def active_sources(registry: dict) -> dict:
    """状态不是 retired / stale_retired 的源才算在役。"""
    result = {}
    for key, rec in registry.get("sources", {}).items():
        if rec.get("status") in ("retired", "dead"):
            continue
        result[key] = rec
    return result


# ---------------------------------------------------------------- HTTP

BROWSER_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
]

try:  # 流媒体站自签证书很常见，统一静音告警
    requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
        requests.packages.urllib3.exceptions.InsecureRequestWarning  # type: ignore[attr-defined]
    )
except Exception:  # pragma: no cover
    pass


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
    )
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def default_headers(extra: dict | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(BROWSER_UAS),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if extra:
        headers.update(extra)
    return headers


def http_request(
    method: str,
    url: str,
    timeout: float = 15,
    retries: int = 2,
    headers: dict | None = None,
    session: requests.Session | None = None,
    backoff: float = 1.5,
    **kwargs,
) -> requests.Response | None:
    """带浏览器 UA、超时与重试的请求。网络层彻底失败返回 None，绝不抛异常。"""
    sess = session or new_session()
    hdrs = default_headers(headers)
    kwargs.setdefault("allow_redirects", True)
    last_error = None
    for attempt in range(max(0, retries) + 1):
        try:
            resp = sess.request(method, url, timeout=timeout, headers=hdrs, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return resp
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    if last_error is not None:
        logging.getLogger("http").debug("request failed %s -> %s", url, last_error)
    return None


def fetch_text(
    url: str,
    timeout: float = 15,
    retries: int = 2,
    headers: dict | None = None,
    session: requests.Session | None = None,
    verify: bool | None = None,
    max_bytes: int = 8 * 1024 * 1024,
) -> str | None:
    """下载文本内容；非 200 或异常一律返回 None。"""
    resp = http_request(
        "GET",
        url,
        timeout=timeout,
        retries=retries,
        headers=headers,
        session=session,
        verify=verify,
        stream=True,
    )
    if resp is None:
        return None
    try:
        if resp.status_code >= 400:
            return None
        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        return decode_body(b"".join(chunks), resp.encoding)
    except requests.RequestException:
        return None
    finally:
        resp.close()


# ---------------------------------------------------------------- 播放列表解析

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _clean_url(url: str) -> str:
    return url.strip().strip("\"'").rstrip(",;)")


def parse_extm3u(lines: list[str]) -> list[dict]:
    streams: list[dict] = []
    pending: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            title = line.split(",", 1)[1].strip() if "," in line else ""
            pending = {
                "name": title or attrs.get("tvg-name", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", ""),
            }
            continue
        if upper.startswith("#"):
            continue
        if not line.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")):
            continue
        record = dict(pending or {"name": "", "tvg_id": "", "tvg_name": "", "logo": "", "group": ""})
        record["url"] = _clean_url(line)
        streams.append(record)
        pending = None
    return streams


def parse_plain(lines: list[str]) -> list[dict]:
    """解析 DIYP 风格的纯文本列表：`分组,#genre#` + `频道名,http://...`。"""
    streams: list[dict] = []
    group = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if "#genre#" in line.lower():
            group = line.split(",")[0].strip()
            continue
        if line.startswith("#") and not line.lower().startswith(("http://", "https://")):
            continue
        if "," in line:
            name, url = line.split(",", 1)
            name, url = name.strip(), _clean_url(url)
        else:
            name, url = "", _clean_url(line)
        if not url.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")):
            continue
        streams.append({"name": name, "tvg_id": "", "tvg_name": name, "logo": "", "group": group, "url": url})
    return streams


def parse_playlist(text: str) -> list[dict]:
    """自动识别 M3U / 纯文本，返回 [{name,url,logo,group,...}]。"""
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    head = "\n".join(lines[:5]).upper()
    streams = parse_extm3u(lines) if "#EXTM3U" in head or "#EXTINF" in text.upper() else parse_plain(lines)
    seen, unique = set(), []
    for item in streams:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(item)
    return unique


def extract_urls(text: str, suffix: str | None = None) -> list[str]:
    """从任意文本中提取 http(s) 链接，可按后缀过滤（如 .m3u）。"""
    found = []
    for match in URL_RE.findall(text or ""):
        url = _clean_url(match)
        if suffix and not url.lower().split("?")[0].endswith(suffix):
            continue
        found.append(url)
    return found


# ---------------------------------------------------------------- URL / 文本归一化


def url_key(url: str, strip_query_keys: set[str] | None = None) -> str:
    """URL 归一化键：忽略 scheme（http/https 等价）、主机小写、剔除时间戳参数。"""
    strip_keys = {k.lower() for k in (strip_query_keys or set())}
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if not host:
            return raw.lower()
        try:
            port = parts.port
        except ValueError:
            port = None
        if port and port not in (80, 443):
            host = f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in strip_keys:
                continue
            query.append(f"{key}={value}")
        key = host + path
        if query:
            key += "?" + "&".join(query)
        return key.lower()
    except Exception:
        return raw.lower()


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def safe_slug(text: str, default: str = "ch") -> str:
    """生成 tvg-id 用的 slug（保留中文，去掉标点）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", nfkc(text).strip().lower()).strip("-")
    return cleaned or default


def truncate(text: str, limit: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
