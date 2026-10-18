#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块 A：发现层（本项目灵魂）。

四条发现渠道，全部在运行时通过网络请求获得源，代码内不含任何具体 IPTV 源地址：

  渠道 1  GitHub Search API（主渠道，带仓库级准入三重门槛）
  渠道 2  递归裂变（解析已准入仓库 README / config，深度 ≤ 2）
  渠道 3  Awesome 元列表挖掘（sources/seeds.json 中的 meta 仓库 / topic 聚合页）
  渠道 4  搜索引擎兜底（DuckDuckGo HTML 端点，任何失败静默跳过）

用法：
  python scripts/discover_sources.py                # 正式运行，写入 registry.json
  python scripts/discover_sources.py --dry-run      # 只看发现了什么，不落盘
  python scripts/discover_sources.py --dry-run --only search --max-repos 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DISCOVER_REPORT_PATH,
    SEEDS_PATH,
    Config,
    days_between,
    extract_urls,
    fetch_text,
    get_logger,
    http_request,
    iso_now,
    load_registry,
    new_session,
    now,
    read_json,
    save_registry,
    truncate,
    write_json,
)

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

REPO_LINK_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
RAW_LINK_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/[^\s\"'<>)]+", re.I
)
M3U_LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+\.m3u8?(?:\?[^\s\"'<>)\]]*)?", re.I)

NOT_REPO_OWNERS = {
    "topics",
    "search",
    "features",
    "sponsors",
    "marketplace",
    "login",
    "settings",
    "orgs",
    "about",
    "pricing",
    "collections",
    "trending",
    "explore",
    "site",
    "blog",
    "security",
    "readme",
    "apps",
    "enterprise",
    "contact",
    "join",
    "new",
    "resources",
    "solutions",
    "open-source",
    "customer-stories",
    "codespaces",
    "team",
}

SKIP_BASENAMES = {
    "readme.txt",
    "license.txt",
    "licence.txt",
    "requirements.txt",
    "robots.txt",
    "changelog.txt",
    "notice.txt",
    "contributing.txt",
    "package.txt",
    "cmakelists.txt",
    "gemfile.txt",
    "authors.txt",
}

PLAYLIST_EXT = (".m3u", ".m3u8", ".txt")


class RateLimitStop(Exception):
    """GitHub 配额耗尽且等待时间超过上限，提前收工。"""


# ---------------------------------------------------------------- GitHub 客户端


class GitHubClient:
    def __init__(self, cfg: Config, log):
        self.cfg = cfg
        self.log = log
        self.session = new_session()
        self.token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"
        self.min_rate_limit = cfg.get_int("discover", "min_rate_limit", 100)
        if not self.token:
            # 匿名核心配额只有 60/h，阈值必须跟着降，否则每次调用都会误触发休眠
            self.min_rate_limit = min(self.min_rate_limit, 5)
        self.max_wait = cfg.get_int("discover", "max_wait_seconds", 300)
        self.max_rate_retries = cfg.get_int("discover", "max_rate_limit_retries", 2)
        self.rate_sleeps = 0
        self.max_tree_calls = cfg.get_int("discover", "max_tree_calls", 120)
        self.timeout = cfg.get_float("general", "request_timeout", 15)
        self.remaining: int | None = None
        self.reset_at = 0
        self.calls = 0
        self.tree_calls = 0
        self.errors = 0
        self._tree_cache: dict[str, dict] = {}
        self._meta_cache: dict[str, dict] = {}
        self.log.info("GitHub 模式：%s（配额上限 %s/h）", "token" if self.token else "匿名", "5000" if self.token else "60")

    # -- 限流 --------------------------------------------------------
    def _update_rate(self, resp) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self.remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self.reset_at = int(reset)
            except ValueError:
                pass

    def _sleep_for_reset(self) -> None:
        self.rate_sleeps += 1
        if self.rate_sleeps > self.max_rate_retries:
            raise RateLimitStop(
                f"连续 {self.rate_sleeps} 次触发限流，已放弃本轮（建议配置 GITHUB_TOKEN 提升到 5000 次/小时）"
            )
        wait = max(0, self.reset_at - int(time.time())) + 5 if self.reset_at else 60
        if wait > self.max_wait:
            raise RateLimitStop(f"配额恢复还需 {wait}s > max_wait_seconds={self.max_wait}")
        self.log.warning("触发 GitHub 限流，休眠 %ss 等待配额重置（第 %s 次）", wait, self.rate_sleeps)
        time.sleep(wait)

    def _pre_call(self) -> None:
        if self.remaining is not None and self.remaining < self.min_rate_limit:
            self._sleep_for_reset()

    # -- 基础请求 ----------------------------------------------------
    def api(self, url: str, params: dict | None = None, precheck: bool = True):
        # Search API 与核心 API 是两套独立配额（10/min vs 60/h），
        # 所以 search 调用不做 min_rate_limit 预检，只靠 403 兜底 + 主动节流。
        if precheck:
            self._pre_call()
        self.calls += 1
        resp = http_request(
            "GET",
            url,
            timeout=self.timeout,
            retries=self.cfg.get_int("general", "request_retries", 2),
            headers=self.headers,
            session=self.session,
            params=params,
        )
        if resp is None:
            self.errors += 1
            return None
        self._update_rate(resp)
        if resp.status_code == 200:
            self.rate_sleeps = 0
            try:
                return resp.json()
            except ValueError:
                return None
        if resp.status_code in (403, 429):
            self.errors += 1
            body = ""
            try:
                body = (resp.text or "")[:512].lower()
            except Exception:
                body = ""
            if self.remaining == 0 or "rate limit" in body or "abuse" in body:
                self._sleep_for_reset()
            else:
                self.log.debug("403 非限流原因，跳过：%s", url)
            return None
        if resp.status_code >= 500:
            self.errors += 1
        return None

    def repo_meta(self, full_name: str) -> dict | None:
        key = full_name.lower()
        if key in self._meta_cache:
            return self._meta_cache[key]
        data = self.api(f"{GITHUB_API}/repos/{full_name}")
        if not isinstance(data, dict) or not data.get("full_name"):
            return None
        self._meta_cache[key] = data
        return data

    def tree(self, full_name: str, branch: str | None = None) -> dict | None:
        key = full_name.lower()
        if key in self._tree_cache:
            return self._tree_cache[key]
        if self.tree_calls >= self.max_tree_calls:
            self.log.warning("git trees 调用已达上限 %s，跳过 %s", self.max_tree_calls, full_name)
            return None
        refs = [branch] if branch else []
        refs.append("HEAD")
        for ref in dict.fromkeys(refs):
            self.tree_calls += 1
            data = self.api(f"{GITHUB_API}/repos/{full_name}/git/trees/{ref}", params={"recursive": "1"})
            if isinstance(data, dict) and data.get("tree"):
                self._tree_cache[key] = data
                return data
        return None

    def raw_text(self, full_name: str, paths: list[str], branches: list[str] | None = None) -> str | None:
        for branch in branches or ["main", "master"]:
            for path in paths:
                url = f"{RAW_BASE}/{full_name}/{branch}/{quote(path, safe='/')}"
                text = fetch_text(url, timeout=self.timeout, retries=1, verify=False)
                if text and len(text.strip()) > 0:
                    return text
        return None

    def search_repos(self, query: str, sort: str, order: str, pages: int, per_page: int) -> list[dict]:
        results: list[dict] = []
        # Search API 有独立配额（匿名 10 次/分钟，token 30 次/分钟），主动节流
        delay = 2.0 if self.token else 6.5
        for page in range(1, pages + 1):
            data = self.api(
                f"{GITHUB_API}/search/repositories",
                params={"q": query, "sort": sort, "order": order, "per_page": per_page, "page": page},
                precheck=False,
            )
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            if not items:
                break
            results.extend(items)
            if len(items) < per_page:
                break
            if page < pages:
                time.sleep(delay)
        return results


# ---------------------------------------------------------------- 发现器


def _valid_repo_pair(owner: str, name: str) -> bool:
    if not owner or not name:
        return False
    if owner.lower() in NOT_REPO_OWNERS:
        return False
    if name.lower() in ("", ".", ".."):
        return False
    if name.endswith(".git"):
        name = name[:-4]
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", owner)) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name))


class Discoverer:
    def __init__(self, cfg: Config, log, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.registry = load_registry()
        self.gh = GitHubClient(cfg, log)

        self.max_new_repos = cfg.get_int("discover", "max_new_repos_per_run", 50)
        self.max_new_playlists = cfg.get_int("discover", "max_new_playlists_per_run", 300)
        self.max_per_repo = cfg.get_int("discover", "max_playlist_per_repo", 12)
        self.min_stars = cfg.get_int("discover", "min_stars", 20)
        self.min_stars_fresh = cfg.get_int("discover", "min_stars_fresh", 5)
        self.fresh_days = cfg.get_int("discover", "fresh_days", 7)
        self.max_repo_age = cfg.get_int("discover", "max_repo_age_days", 90)
        self.max_depth = cfg.get_int("discover", "recursive_depth", 2)
        self.max_recursive_repos = cfg.get_int("discover", "recursive_max_repos", 30)
        self.docs_per_repo = cfg.get_int("discover", "recursive_docs_per_repo", 4)

        self.visited_repos: set[str] = set()
        self.visited_urls: set[str] = set()
        self.new_repos = 0
        self.new_playlists = 0
        self.recursive_admitted = 0
        self.admitted: list[dict] = []
        self.stop_reason = ""
        self.started = time.time()
        self.report = {
            "started_at": iso_now(),
            "dry_run": dry_run,
            "channels": {},
            "admitted_repos": [],
            "new_playlists": [],
            "errors": [],
        }

    # -- 记录 --------------------------------------------------------
    def _chan_stat(self, channel: str, key: str, delta: int = 1) -> None:
        stat = self.report["channels"].setdefault(channel, {"repos": 0, "playlists": 0, "skipped": 0})
        stat[key] = stat.get(key, 0) + delta

    def add_playlist(self, url: str, source_repo: str, via: str, stars: int = 0, last_push: str | None = None) -> bool:
        url = (url or "").strip().rstrip(".,;")
        if not url.lower().startswith(("http://", "https://")):
            return False
        if url in self.visited_urls:
            return False
        self.visited_urls.add(url)

        existing = self.registry["sources"].get(url)
        if existing:
            existing["last_seen_at"] = iso_now()
            if stars:
                existing["stars"] = max(int(existing.get("stars") or 0), int(stars))
            return False

        if self.new_playlists >= self.max_new_playlists:
            return False

        record = {
            "source_repo": source_repo or "",
            "playlist_url": url,
            "discovered_via": via,
            "discovered_at": iso_now(),
            "last_seen_at": iso_now(),
            "stars": int(stars or 0),
            "last_push": (last_push or "")[:10] or None,
            "last_verified": None,
            "consecutive_failures": 0,
            "status": "discovered",
            "kind": "txt" if url.lower().split("?")[0].endswith(".txt") else "m3u",
            "last_stream_count": None,
            "last_valid_count": None,
            "repo_stale": False,
        }
        if not self.dry_run:
            self.registry["sources"][url] = record
        self.new_playlists += 1
        self.report["new_playlists"].append(
            {"playlist_url": url, "source_repo": source_repo, "via": via, "stars": record["stars"]}
        )
        self.log.info("  + [%s] %s  (%s, ★%s)", via, truncate(url, 96), source_repo or "-", record["stars"])
        return True

    # -- 仓库准入 ----------------------------------------------------
    def qualifies(self, meta: dict) -> tuple[bool, str]:
        if meta.get("archived"):
            return False, "archived"
        if meta.get("disabled"):
            return False, "disabled"
        stars = int(meta.get("stargazers_count") or 0)
        pushed = meta.get("pushed_at") or meta.get("updated_at")
        age = days_between(pushed)
        if age > self.max_repo_age:
            return False, f"stale({age:.0f}d)"
        if stars >= self.min_stars:
            return True, "ok"
        if stars >= self.min_stars_fresh and age <= self.fresh_days:
            return True, "fresh"
        return False, f"stars({stars})"

    def _playlist_paths(self, tree: dict) -> list[str]:
        paths = []
        for node in tree.get("tree") or []:
            if node.get("type") != "blob":
                continue
            path = node.get("path") or ""
            low = path.lower()
            if not low.endswith(PLAYLIST_EXT):
                continue
            base = low.rsplit("/", 1)[-1]
            if base in SKIP_BASENAMES:
                continue
            if base.startswith(("license", "readme", "changelog", "contributing")):
                continue
            if any(seg in low for seg in ("node_modules/", "/.git/", ".github/", "__pycache__/", "test/", "tests/")):
                continue
            paths.append(path)
        # 播放列表类命名优先，其余按字母序稳定输出
        def rank(p: str) -> tuple:
            low = p.lower()
            hints = ("iptv", "live", "playlist", "result", "output", "tv", "直播", "源", "channel")
            return (0 if any(h in low for h in hints) else 1, low.count("/"), low)

        return sorted(paths, key=rank)[: self.max_per_repo]

    def consider_repo(self, meta: dict, via: str, depth: int = 0) -> bool:
        full_name = meta.get("full_name") or ""
        if not full_name or "/" not in full_name:
            return False
        key = full_name.lower()
        if key in self.visited_repos:
            return False
        self.visited_repos.add(key)

        if self.new_repos >= self.max_new_repos:
            return False

        ok, reason = self.qualifies(meta)
        if not ok:
            self._chan_stat(via, "skipped")
            self.log.debug("  - 拒绝 %s：%s", full_name, reason)
            return False

        tree = self.gh.tree(full_name, meta.get("default_branch"))
        if not tree:
            self._chan_stat(via, "skipped")
            return False
        paths = self._playlist_paths(tree)
        if not paths:
            self._chan_stat(via, "skipped")
            self.log.debug("  - 拒绝 %s：文件树无播放列表", full_name)
            return False

        branch = meta.get("default_branch") or "main"
        stars = int(meta.get("stargazers_count") or 0)
        pushed = (meta.get("pushed_at") or "")[:10] or None
        added = 0
        for path in paths:
            url = f"{RAW_BASE}/{full_name}/{branch}/{quote(path, safe='/')}"
            if self.add_playlist(url, full_name, via, stars, pushed):
                added += 1

        self.new_repos += 1
        self._chan_stat(via, "repos")
        self._chan_stat(via, "playlists", added)
        self.admitted.append({"repo": full_name, "meta": meta, "depth": depth})
        self.report["admitted_repos"].append(
            {"repo": full_name, "via": via, "depth": depth, "stars": stars, "last_push": pushed, "files": len(paths)}
        )
        self.log.info("[%s] 准入 %s ★%s push=%s -> %d 个播放列表", via, full_name, stars, pushed, added)
        return True

    def consider_repo_by_name(self, full_name: str, via: str, depth: int = 0) -> bool:
        if not full_name or "/" not in full_name:
            return False
        if self.new_repos >= self.max_new_repos:
            return False
        if full_name.lower() in self.visited_repos:
            return False
        meta = self.gh.repo_meta(full_name)
        if not meta:
            return False
        return self.consider_repo(meta, via, depth)

    # -- 渠道 1：GitHub Search --------------------------------------
    def build_queries(self) -> list[dict]:
        since = (now() - timedelta(days=30)).strftime("%Y-%m-%d")
        queries = [
            {"label": "name_desc", "q": "iptv in:name,description", "sort": "updated", "order": "desc"},
            {"label": "m3u_stars", "q": f"iptv m3u stars:>50 pushed:>{since}", "sort": "stars", "order": "desc"},
            {"label": "readme_cn", "q": "直播源 in:readme", "sort": "updated", "order": "desc"},
            {"label": "playlist_py", "q": "iptv playlist language:python", "sort": "updated", "order": "desc"},
        ]
        mode = self.cfg.get("discover", "query_mode", "all").lower()
        day_index = int(now().strftime("%j"))
        if mode == "rotate":
            take = max(1, self.cfg.get_int("discover", "queries_per_run", 2))
            offset = day_index % len(queries)
            ordered = queries[offset:] + queries[:offset]
            picked = ordered[:take]
            self.log.info("查询模式 rotate：今日执行 %s", [item["label"] for item in picked])
            return picked
        offset = day_index % len(queries)
        ordered = queries[offset:] + queries[:offset]
        self.log.info("查询模式 all：执行顺序 %s", [item["label"] for item in ordered])
        return ordered

    def channel_github_search(self) -> None:
        self.log.info("=== 渠道 1：GitHub Search API ===")
        pages = self.cfg.get_int("discover", "max_pages_per_query", 5)
        per_page = self.cfg.get_int("discover", "per_page", 100)
        for query in self.build_queries():
            if self.new_repos >= self.max_new_repos:
                self.log.info("新仓库已达上限 %s，停止检索", self.max_new_repos)
                break
            try:
                items = self.gh.search_repos(query["q"], query["sort"], query["order"], pages, per_page)
            except RateLimitStop:
                raise
            except Exception as exc:  # 单查询失败不影响其他查询
                self.log.warning("查询 %s 失败：%s", query["label"], exc)
                self.report["errors"].append(f"search:{query['label']}:{exc}")
                continue
            self.log.info("查询 [%s] q=%s -> 命中 %d 个仓库", query["label"], query["q"], len(items))
            for item in items:
                if self.new_repos >= self.max_new_repos:
                    break
                try:
                    self.consider_repo(item, "github_search", 0)
                except RateLimitStop:
                    raise
                except Exception as exc:
                    self.log.debug("准入异常 %s：%s", item.get("full_name"), exc)

    # -- 渠道 3：Awesome 元列表 -------------------------------------
    def _links_in_text(self, text: str) -> tuple[set[str], set[str]]:
        repos, playlists = set(), set()
        for owner, name in REPO_LINK_RE.findall(text or ""):
            if _valid_repo_pair(owner, name):
                repos.add(f"{owner}/{name}")
        for owner, name in RAW_LINK_RE.findall(text or ""):
            if _valid_repo_pair(owner, name):
                repos.add(f"{owner}/{name}")
        for url in M3U_LINK_RE.findall(text or ""):
            playlists.add(url)
        return repos, playlists

    def _html_repo_links(self, html: str) -> set[str]:
        """解析 HTML 里的相对/绝对仓库链接（topic 聚合页、搜索结果页）。"""
        repos: set[str] = set()
        for owner, name in re.findall(r'href="(?:https?://github\.com)?/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"', html or ""):
            if _valid_repo_pair(owner, name):
                repos.add(f"{owner}/{name}")
        for owner, name in REPO_LINK_RE.findall(html or ""):
            if _valid_repo_pair(owner, name):
                repos.add(f"{owner}/{name}")
        return repos

    def channel_awesome(self) -> None:
        self.log.info("=== 渠道 3：Awesome 元列表挖掘 ===")
        seeds = (read_json(SEEDS_PATH, {}) or {}).get("seeds") or []
        if not seeds:
            self.log.warning("seeds.json 为空，跳过该渠道")
            return
        for seed in seeds:
            if self.new_repos >= self.max_new_repos:
                break
            kind = str(seed.get("type", "")).lower()
            try:
                if kind == "awesome_readme":
                    self._seed_readme(seed)
                elif kind == "github_topic":
                    self._seed_topic(seed)
                elif kind == "github_repo":
                    self.consider_repo_by_name(seed.get("repo", ""), "awesome", 1)
                else:
                    self.log.warning("未知种子类型：%s", kind)
            except RateLimitStop:
                raise
            except Exception as exc:
                self.log.warning("种子处理失败 %s：%s", seed, exc)
                self.report["errors"].append(f"seed:{seed.get('repo') or seed.get('topic')}:{exc}")

    def _seed_readme(self, seed: dict) -> None:
        repo = seed.get("repo", "")
        if not repo:
            return
        paths = [seed.get("path")] if seed.get("path") else ["README.md", "readme.md", "README-zh.md", "README_zh.md"]
        branches = [seed["branch"]] if seed.get("branch") else ["main", "master"]
        text = self.gh.raw_text(repo, [p for p in paths if p], branches)
        if not text:
            self.log.warning("种子 %s 的 README 抓取失败（可能改名/删库），已跳过", repo)
            self._chan_stat("awesome", "skipped")
            return
        repos, playlists = self._links_in_text(text)
        self.log.info("种子 %s 解析出 %d 个仓库链接、%d 个 m3u 直链", repo, len(repos), len(playlists))
        for url in playlists:
            self.add_playlist(url, repo, "awesome")
        for full in sorted(repos):
            if self.new_repos >= self.max_new_repos:
                break
            self.consider_repo_by_name(full, "awesome", 1)

    def _seed_topic(self, seed: dict) -> None:
        topic = seed.get("topic", "")
        if not topic:
            return
        url = f"https://github.com/topics/{quote(topic)}"
        resp = http_request("GET", url, timeout=self.cfg.get_float("general", "request_timeout", 15), retries=2)
        if resp is None or resp.status_code != 200:
            self.log.warning("topic %s 页面抓取失败（status=%s），已跳过", topic, getattr(resp, "status_code", "none"))
            self._chan_stat("awesome", "skipped")
            return
        html = resp.text
        repos, playlists = self._links_in_text(html)
        repos |= self._html_repo_links(html)
        self.log.info("topic %s 解析出 %d 个仓库链接", topic, len(repos))
        for full in sorted(repos):
            if self.new_repos >= self.max_new_repos:
                break
            self.consider_repo_by_name(full, "awesome", 1)

    # -- 渠道 2：递归裂变 -------------------------------------------
    def channel_recursive(self) -> None:
        self.log.info("=== 渠道 2：递归裂变（深度 ≤ %s）===", self.max_depth)
        queue = [(item["repo"], item["depth"]) for item in self.admitted]
        while queue:
            if self.recursive_admitted >= self.max_recursive_repos or self.new_repos >= self.max_new_repos:
                self.log.info("递归已达上限，停止裂变")
                break
            repo, depth = queue.pop(0)
            if depth >= self.max_depth:
                continue
            try:
                docs = self._collect_docs(repo)
            except RateLimitStop:
                raise
            except Exception as exc:
                self.log.debug("读取 %s 文档失败：%s", repo, exc)
                continue
            if not docs:
                continue
            text = "\n".join(docs)
            repos, playlists = self._links_in_text(text)
            repos.discard(repo)
            self.log.info("递归 %s（depth=%s）-> %d 个仓库链接、%d 个 m3u 直链", repo, depth, len(repos), len(playlists))
            for url in playlists:
                self.add_playlist(url, repo, "recursive")
            for full in sorted(repos):
                if self.recursive_admitted >= self.max_recursive_repos or self.new_repos >= self.max_new_repos:
                    break
                if full.lower() in self.visited_repos:
                    continue
                if self.consider_repo_by_name(full, "recursive", depth + 1):
                    self.recursive_admitted += 1
                    queue.append((full, depth + 1))

    def _collect_docs(self, repo: str) -> list[str]:
        """抓取 README + 若干 config/subscribe 类文件（用于提取互链源）。"""
        docs: list[str] = []
        readme = self.gh.raw_text(repo, ["README.md", "readme.md", "README-zh.md"])
        if readme:
            docs.append(readme)
        meta = self.gh.repo_meta(repo)
        branch = (meta or {}).get("default_branch") or "main"
        tree = self.gh.tree(repo, branch)
        if tree:
            candidates = []
            for node in tree.get("tree") or []:
                if node.get("type") != "blob":
                    continue
                path = node.get("path") or ""
                low = path.lower()
                base = low.rsplit("/", 1)[-1]
                if len(candidates) >= self.docs_per_repo:
                    break
                if base in ("config.ini", "config.yaml", "config.yml", "config.json", "config.py", "settings.py"):
                    candidates.append(path)
                elif base.startswith(("subscribe", "sources", "channels")) and low.endswith((".txt", ".json", ".yaml", ".yml", ".ini")):
                    candidates.append(path)
            for path in candidates[: self.docs_per_repo]:
                text = fetch_text(
                    f"{RAW_BASE}/{repo}/{branch}/{quote(path, safe='/')}",
                    timeout=self.cfg.get_float("general", "request_timeout", 15),
                    retries=1,
                    verify=False,
                    max_bytes=256 * 1024,
                )
                if text:
                    docs.append(text)
        return docs

    # -- 渠道 4：搜索引擎兜底 ---------------------------------------
    def channel_search_engine(self) -> None:
        if not self.cfg.get_bool("discover", "enable_search_engine", True):
            return
        self.log.info("=== 渠道 4：搜索引擎兜底（DuckDuckGo HTML）===")
        queries = self.cfg.get_list(
            "discover",
            "ddg_queries",
            ["iptv m3u github 自动更新", "直播源 每日更新 site:github.com", "iptv playlist auto update github"],
        )
        delay = self.cfg.get_float("discover", "search_delay_seconds", 3)
        for query in queries:
            if self.new_repos >= self.max_new_repos:
                break
            try:
                repos, playlists = self._ddg_search(query)
            except Exception as exc:  # 任何失败静默跳过
                self.log.info("搜索引擎查询 [%s] 跳过：%s", query, exc)
                self.report["errors"].append(f"search_engine:{query}:{exc}")
                continue
            self.log.info("搜索引擎 [%s] -> %d 个仓库、%d 个 m3u 直链", query, len(repos), len(playlists))
            for url in playlists:
                self.add_playlist(url, "", "search_engine")
            for full in sorted(repos):
                if self.new_repos >= self.max_new_repos:
                    break
                self.consider_repo_by_name(full, "search_engine", 1)
            time.sleep(delay)

    def _ddg_search(self, query: str) -> tuple[set[str], set[str]]:
        url = "https://html.duckduckgo.com/html/?q=" + quote(query)
        resp = http_request(
            "GET",
            url,
            timeout=self.cfg.get_float("general", "request_timeout", 15),
            retries=1,
            headers={"Referer": "https://duckduckgo.com/"},
        )
        if resp is None:
            raise RuntimeError("请求失败")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        html = resp.text or ""
        if "anomaly" in html.lower() or "captcha" in html.lower():
            raise RuntimeError("疑似验证码/风控页")

        repos: set[str] = set()
        playlists: set[str] = set()
        for encoded in re.findall(r"uddg=([^&\"'>]+)", html):
            target = unquote(encoded)
            for owner, name in REPO_LINK_RE.findall(target):
                if _valid_repo_pair(owner, name):
                    repos.add(f"{owner}/{name}")
            for found in M3U_LINK_RE.findall(target):
                playlists.add(found)
        extra_repos, extra_playlists = self._links_in_text(html)
        repos |= extra_repos
        playlists |= extra_playlists
        playlists = {url for url in playlists if "duckduckgo" not in url}
        return repos, playlists

    # -- 汇总 --------------------------------------------------------
    def summary(self) -> dict:
        elapsed = round(time.time() - self.started, 1)
        total = len(self.registry["sources"])
        self.report.update(
            {
                "finished_at": iso_now(),
                "elapsed_seconds": elapsed,
                "api_calls": self.gh.calls,
                "tree_calls": self.gh.tree_calls,
                "api_errors": self.gh.errors,
                "rate_remaining": self.gh.remaining,
                "new_repos": self.new_repos,
                "new_playlists": self.new_playlists,
                "registry_total": total,
                "stop_reason": self.stop_reason,
            }
        )
        return self.report

    def run(self, only: str = "all") -> dict:
        steps = [
            ("search", "渠道 1 GitHub Search", self.channel_github_search),
            ("awesome", "渠道 3 Awesome 元列表", self.channel_awesome),
            ("recursive", "渠道 2 递归裂变", self.channel_recursive),
            ("engine", "渠道 4 搜索引擎兜底", self.channel_search_engine),
        ]
        for key, title, fn in steps:
            if only not in ("all", key):
                continue
            try:
                fn()
            except RateLimitStop as exc:
                self.stop_reason = str(exc)
                self.log.warning("%s 因限流提前结束：%s", title, exc)
                break
            except Exception as exc:  # 单渠道异常不得中断整体
                self.log.exception("%s 异常：%s", title, exc)
                self.report["errors"].append(f"channel:{key}:{exc}")
        self.summary()
        if self.dry_run:
            self.log.info("dry-run 模式：不写入 registry.json")
        else:
            save_registry(self.registry)
            self.log.info("registry.json 已更新，总源数 %s", len(self.registry["sources"]))
        return self.report


def print_report(report: dict, log) -> None:
    log.info("========== 发现层汇总 ==========")
    log.info(
        "耗时 %.1fs | API 调用 %s 次 | trees %s 次 | 剩余配额 %s",
        report.get("elapsed_seconds", 0),
        report.get("api_calls"),
        report.get("tree_calls"),
        report.get("rate_remaining", "?"),
    )
    log.info(
        "准入仓库 %s 个 | 新增播放列表 %s 条 | 注册表总量 %s",
        report.get("new_repos"),
        report.get("new_playlists"),
        report.get("registry_total"),
    )
    for channel, stat in (report.get("channels") or {}).items():
        log.info("  - %s：准入仓库 %s，新增播放列表 %s", channel, stat.get("repos", 0), stat.get("playlists", 0))
    if report.get("errors"):
        log.info("非致命错误 %s 条（详见 discover_report.json）", len(report["errors"]))
    if report.get("stop_reason"):
        log.info("提前结束原因：%s", report["stop_reason"])


def main() -> int:
    parser = argparse.ArgumentParser(description="IPTV 源发现层")
    parser.add_argument("--dry-run", action="store_true", help="只观察发现结果，不写注册表")
    parser.add_argument("--only", default="all", choices=["all", "search", "awesome", "recursive", "engine"])
    parser.add_argument("--max-repos", type=int, default=0, help="本次最多准入多少仓库（覆盖配置）")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    log = get_logger("discover")
    cfg = Config(args.config) if args.config else Config()
    if args.max_repos > 0:
        cfg.parser.set("discover", "max_new_repos_per_run", str(args.max_repos))

    log.info("========== 发现层启动 %s ==========", iso_now())
    try:
        discoverer = Discoverer(cfg, log, dry_run=args.dry_run)
        report = discoverer.run(only=args.only)
    except Exception as exc:
        log.exception("发现层致命错误：%s", exc)
        return 1
    print_report(report, log)
    if not args.dry_run:
        write_json(DISCOVER_REPORT_PATH, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
