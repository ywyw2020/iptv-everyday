# iptv-everyday

每天凌晨 02:00（北京时间）自动狩猎全网新出现的 IPTV 直播源，去重、真实可播放性验证、
生成标准 M3U / TXT，并自动提交回本仓库。播放器订阅同一个 raw 直链，次日即自动拿到最新内容。

> **先说明一句实话**：本项目的可用性保障来自代码里的三层验证链（HTTP → ffprobe → ffmpeg 真实试播），
> 而不是任何"记忆"或预先写死的源清单。仓库里**没有一条手工填写的直播源地址**，
> 所有源都是 `scripts/discover_sources.py` 每天在流水线里现场搜出来的。

---

## 一、订阅地址

| 格式 | 地址 | 适用播放器 |
| --- | --- | --- |
| M3U | `https://gitee.com/ywyw2020/iptv-everyday/raw/master/output/iptv.m3u` | VLC / TiviMate / APTV / DIYP / Kodi |
| TXT | `https://gitee.com/ywyw2020/iptv-everyday/raw/master/output/iptv.txt` | DIYP、电视家类壳子 |

用法：播放器里"添加远程订阅 / 网络播放列表"，粘贴上面任意一条即可。
仓库每天更新后，播放器刷新订阅就会拉到新内容（raw 直链永远不变）。

---

## 二、它是怎么工作的

```
                    ┌──────────────────────────────────────────┐
02:00 定时触发 ───▶ │ discover_sources.py   发现层（4 条渠道）  │
                    │  ① GitHub Search API  ② 递归裂变          │
                    │  ③ Awesome 元列表     ④ DDG 搜索引擎兜底  │
                    └───────────────┬──────────────────────────┘
                                    ▼ sources/registry.json
                    ┌──────────────────────────────────────────┐
                    │ retire_sources.py   淘汰 + 墓地回访复活   │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │ fetch_sources.py    下载并解析所有播放列表 │
                    └───────────────┬──────────────────────────┘
                                    ▼ output/work/raw_streams.json
                    ┌──────────────────────────────────────────┐
                    │ deduplicate.py      URL + 频道名双重去重   │
                    └───────────────┬──────────────────────────┘
                                    ▼ output/work/dedup_streams.json
                    ┌──────────────────────────────────────────┐
                    │ validate_streams.py 三层真实验证（20 线程）│
                    └───────────────┬──────────────────────────┘
                                    ▼ output/work/validated_streams.json
                    ┌──────────────────────────────────────────┐
                    │ generate_m3u.py    生成 m3u / txt         │
                    │ push_to_gitee.sh   自动提交并推送          │
                    └──────────────────────────────────────────┘
```

关键设计：

- **零白名单**：`sources/seeds.json` 里只有 `iptv-org/awesome-iptv` 这类**元列表**和 GitHub topic 聚合页，
  它们只是"引路"，真正的源全部由运行时请求解析出来。
- **数量不设上限，质量设硬门槛**：仓库要过 star/活跃度/文件树三重准入，流要过三层验证。
- **动态注册表**：源的发现、验证、淘汰、复活全部由程序维护，`registry.json` 不需要人工编辑。
- **单点失败不中断**：每个模块、每个渠道、每条源都有独立 try/except，坏一条不影响整体。

---

## 三、目录结构

```
iptv-everyday/
├── sources/
│   ├── seeds.json           # 冷启动种子（只放 meta 元列表，不放具体源）
│   ├── registry.json        # 动态源注册表（程序自动维护）
│   ├── graveyard.json       # 淘汰源墓地（30 天后自动回访）
│   └── local/               # 人工补充的本地 m3u / txt（可选）
├── scripts/
│   ├── common.py            # 公共层：配置/日志/HTTP/播放列表解析
│   ├── discover_sources.py  # A 发现层（核心）
│   ├── fetch_sources.py     # B 抓取层
│   ├── deduplicate.py       # C 去重引擎
│   ├── validate_streams.py  # D 三层验证
│   ├── retire_sources.py    # E 淘汰与回访
│   ├── generate_m3u.py      # F M3U 生成
│   └── push_to_gitee.sh     # G 推送
├── output/
│   ├── iptv.m3u / iptv.txt  # 最终产物
│   ├── work/                # 中间产物（原始流、去重结果、验证结果、发现报告）
│   └── log/                 # update_YYYYMMDD.log / verify_YYYYMMDD.log
├── .gitee/workflows/daily-update.yml
├── config.ini
└── requirements.txt
```

---

## 四、`registry.json` 字段说明

`sources` 是一个以 `playlist_url` 为键的对象（天然防止同一个播放列表重复登记）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `source_repo` | string | 来源仓库，如 `owner/repo`；本地补源为 `local/xxx.m3u` |
| `playlist_url` | string | 播放列表 raw 直链 |
| `discovered_via` | string | 发现渠道：`github_search` / `recursive` / `awesome` / `search_engine` / `revived` |
| `discovered_at` | string | 首次发现时间（+08:00） |
| `last_seen_at` | string | 最近一次被再次发现的时间（用于观察源是否仍被上游引用） |
| `stars` | int | 仓库 star（本地源为 0） |
| `last_push` | string | 仓库最近 push 日期 `YYYY-MM-DD` |
| `last_verified` | string/null | 最近一次跑完验证的时间 |
| `consecutive_failures` | int | **连续**失败计数（由 retire 每日结算一次，成功即清零） |
| `last_total_checked` | int/null | 上一轮该源被验证的流数量 |
| `last_counted_date` | string/null | 上一次结算日期，防止一天多跑导致计数虚增 |
| `status` | string | `discovered` → `verified`；复活源为 `revived` |
| `kind` | string | `m3u` 或 `txt`（决定用哪种解析器） |
| `last_stream_count` | int/null | 最近一次抓取解析出的流数量 |
| `last_valid_count` | int/null | 最近一次验证通过的流数量 |
| `repo_stale` | bool | 仓库超过 90 天无 push，发现层不再接纳该仓库 |
| `revived_at` / `revive_count` | string/int | 仅复活源有：复活时间与第几次复活 |

---

## 五、准入、淘汰与回访规则

### 5.1 仓库级准入（三者同时满足，任一不满足整仓丢弃）

1. `stars ≥ 20`；若 `pushed` 在 7 天内则放宽到 `stars ≥ 5`；
2. 最近 push 在 90 天以内；
3. 文件树里存在 `.m3u / .m3u8 / .txt` 播放列表文件（排除 README/LICENSE/requirements 等噪声）。

单日新增仓库上限 50（`max_new_repos_per_run`），播放列表上限 300。

### 5.2 流级验证（三层，缺一不可）

| 层 | 手段 | 通过标准 |
| --- | --- | --- |
| 1 | HTTP 探测（HEAD，失败降级 Range GET） | 200/206/301/302，超时 5s |
| 2 | `ffprobe -show_entries stream=codec_name,width,height` | 必须返回 codec 信息（挡掉 HTML 错误页伪流） |
| 3 | `ffmpeg -i <url> -t 5 -f null -` | 无报错退出 |

系统未安装 ffmpeg/ffprobe 时会**降级验证**并在日志中明确告警，此时输出不可信，
所以流水线里会先尝试安装 ffmpeg。

### 5.3 淘汰

- 某个源 `consecutive_failures ≥ 7` → 移入 `graveyard.json`，
  记录 `retire_reason`、`retired_at`、`review_history`；
- **计数由 `retire_sources.py` 统一结算，每个自然日只结算一次**（`last_counted_date` 去重），
  依据上一轮的事实字段：
  - `last_stream_count = 0`（抓都抓不到）→ 失败 +1；
  - `last_valid_count = 0`（抓到了但一条都验不过）→ 失败 +1；
  - `last_valid_count > 0` → 失败计数直接清零；
  - 刚发现、还没抓过的源（无 `last_fetch_at`）跳过，不计失败；
- 不立即删除，是为了防止"换域名/迁移服务器"的源被误杀。

> 因为流水线顺序是 `discover → retire → fetch → …`，淘汰判断用的是"昨夜"的数据，
> 而复活发生在 fetch 之前，所以复活的源当晚就会被重新抓取——这是刻意设计的。

### 5.4 回访复活

- 墓地中的源每 `30` 天自动回访一次：重新请求 `playlist_url` 并解析，
  能解析出 ≥1 条流即判定复活，重新写回 `registry.json`（`status=revived`），当晚即参与抓取；
- 回访仍失败则 `next_review_at = now + 30 天`，`review_count + 1`，继续躺在墓地。

### 5.5 去重与多源容灾

- URL 归一化：忽略 `http/https` 差异、主机小写、剔除 `t/ts/timestamp/time` 等时间戳参数；
- 频道名归一化：全半角 → 去括号噪声 → 去 `高清/HD/4K/备用/线路N` → 繁转简 → 去符号 → 别名映射；
- **`CCTV-5` 与 `CCTV-5+` 是不同频道**，别名表按频道级精确映射，绝不做模糊包含匹配；
- 每个频道保留最快的前 `keep_per_channel=3` 条（去重阶段额外留 2 条冗余候选，抵消验证淘汰）。

---

## 六、如何在 Gitee 上部署

### 6.1 准备仓库

```bash
git remote add gitee https://gitee.com/ywyw2020/iptv-everyday.git
git add -A && git commit -m "init: iptv-everyday"
git push gitee master
```

### 6.2 配置流水线密钥变量

Gitee 仓库 → **流水线（Gitee Go）** → **设置** → 环境变量 / 密钥变量，新增：

| 变量名 | 值 | 说明 |
| --- | --- | --- |
| `GITEE_PUSH_TOKEN` | 私人令牌 | 头像 → 设置 → 私人令牌，勾选 **projects** 权限 |
| `GITEE_REPO_URL` | `https://oauth2:<TOKEN>@gitee.com/ywyw2020/iptv-everyday.git` | 推送地址；不配则回退 `origin` |
| `GITHUB_TOKEN` | GitHub PAT（可选但**强烈建议**） | 匿名仅 60 次/小时，认证后 5000 次/小时，直接决定发现量 |

> 只在"密钥变量"里填 token，**不要**写进 `config.ini` 或任何代码文件。

### 6.3 手动触发一次验证全流程

流水线 → 选择 `daily-iptv-hunt` → **运行**（立即执行），观察日志：

- `discover_sources.py` 应打印每个查询词命中仓库数、准入仓库、新增播放列表；
- `validate_streams.py` 应打印进度与三层通过率；
- 最后 `[OK] 推送完成` 表示产物已回到仓库。

首次运行 `registry.json` 是空的，源数量会从 0 开始爬升，属正常现象。

### 6.4 若 Gitee Go 对你的账号不可用

Gitee Go 目前并非所有账号/仓库都能开通。备选方案（效果等价）：

```bash
# 安装 ffmpeg + 依赖
sudo apt-get install -y ffmpeg && pip install -r requirements.txt

# crontab -e 加入（服务器时区为 Asia/Shanghai 时）
0 2 * * * cd /path/to/iptv-everyday && \
  GITHUB_TOKEN=xxx GITEE_PUSH_TOKEN=xxx \
  GITEE_REPO_URL=https://oauth2:xxx@gitee.com/ywyw2020/iptv-everyday.git \
  bash -lc 'python3 scripts/discover_sources.py && python3 scripts/retire_sources.py && \
            python3 scripts/fetch_sources.py && python3 scripts/deduplicate.py && \
            python3 scripts/validate_streams.py && python3 scripts/generate_m3u.py && \
            ./scripts/push_to_gitee.sh' >> output/log/cron.log 2>&1
```

---

## 七、本地运行与单独调试

```bash
pip install -r requirements.txt
# ffmpeg 必须自己装：Windows 用 winget install Gyan.FFmpeg，Ubuntu 用 apt install ffmpeg

python scripts/discover_sources.py --dry-run              # 只看发现层搜到了什么
python scripts/discover_sources.py --dry-run --only search --max-repos 10
python scripts/retire_sources.py --dry-run                # 看本轮会淘汰/复活谁
python scripts/fetch_sources.py --dry-run --limit 5       # 抓 5 个源看看
python scripts/deduplicate.py --dry-run
python scripts/validate_streams.py --dry-run --limit 20   # 只验 20 条
python scripts/generate_m3u.py --dry-run                  # 打印 m3u 预览，不落盘
```

所有 `--dry-run` 都**只读不写**：不动 `registry.json`、不写 `output/`、不推送。

### 手动添加种子

编辑 `sources/seeds.json`，只允许加**元列表**（awesome 类仓库 / GitHub topic），例如：

```json
{
  "type": "awesome_readme",
  "repo": "some-user/awesome-iptv-list",
  "note": "元列表：只解析其外链"
}
```

或

```json
{ "type": "github_topic", "topic": "iptv-org" }
```

**不要**把 `http://xxx/live.m3u8` 这类具体源写进种子——那会破坏"零白名单"原则；
你手头的私有源请放进 `sources/local/`。

---

## 八、常见故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 发现层 0 新增 | `GITHUB_TOKEN` 未配，匿名配额 60/h 被秒用光 | 配 PAT；或 `query_mode=rotate` 降低消耗 |
| 日志出现 `rate limit` 后提前结束 | 配额耗尽且等待超过 `max_wait_seconds` | 调大该值，或配 token |
| 准入大量 `stale(...)` | 查询词命中一堆僵尸仓库 | 正常，说明门槛在起作用；可调 `max_repo_age_days` |
| `ffprobe 未安装` / 降级验证 | 运行环境缺 ffmpeg | 装 ffmpeg；流水线里已尝试自动安装 |
| 验证通过但播放器打不开 | 源绑定了 IP/地区，或需要 Referer/UA | 该类源属正常损耗，靠"每频道多条"容灾 |
| 输出 0 条 | 上游源整批失效，或验证全灭 | 看 `output/log/verify_YYYYMMDD.log` 的层分布；`sources/local/` 放兜底源 |
| `git push` 失败，退出码 128 | token 无 projects 权限 / 过期 / 分支保护 | 重新生成令牌；或改用 SSH 部署密钥 |
| 流水线成功但仓库没更新 | `git diff --cached --quiet` 为真（无变更） | 正常行为，脚本会打印"无变更，跳过" |
| `registry.json` 很大 | 长期运行累积了上千个源 | 正常；废弃源会自动进墓地并在 30 天后清退 |

---

## 九、三条自查命令

```bash
# ① 发现层每天是否真的搜到新仓库（不落盘，纯观察）
python scripts/discover_sources.py --dry-run --only search --max-repos 20

# ② 注册表规模与状态分布是否随时间稳步增长
python - <<'EOF'
import json, collections
d = json.load(open("sources/registry.json", encoding="utf-8"))
s = d["sources"]
print("总源数:", len(s))
print("状态分布:", dict(collections.Counter(v.get("status") for v in s.values())))
print("渠道分布:", dict(collections.Counter(v.get("discovered_via") for v in s.values())))
print("连续失败>0:", sum(1 for v in s.values() if (v.get("consecutive_failures") or 0) > 0))
EOF

# ③ 墓地里的源是否在 30 天后被回访复检（--review-all 强制立即回访，用于验证逻辑）
python scripts/retire_sources.py --review-all --dry-run
python - <<'EOF'
import json
for e in json.load(open("sources/graveyard.json", encoding="utf-8"))["entries"]:
    print(e["playlist_url"][:70], "| 回访次数", e.get("review_count"), "| 下次", (e.get("next_review_at") or "")[:10],
          "| 历史", [h["result"] for h in e.get("review_history", [])])
EOF
```

---

## 十、可调参数速查（`config.ini`）

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `[discover] query_mode` | `all` | `all` 每次跑全部查询词；`rotate` 每天轮换 `queries_per_run` 个 |
| `[discover] max_new_repos_per_run` | 50 | 单日新增仓库上限 |
| `[discover] min_stars` | 20 | 仓库准入 star 门槛 |
| `[discover] recursive_depth` | 2 | 递归裂变深度上限 |
| `[dedupe] keep_per_channel` | 3 | 每频道最终保留条数 |
| `[dedupe] extra_candidates` | 2 | 去重阶段额外冗余候选（抵消验证淘汰） |
| `[validate] workers` | 20 | 验证并发线程数 |
| `[retire] failure_threshold` | 7 | 连续失败多少天淘汰 |
| `[retire] graveyard_review_days` | 30 | 墓地回访间隔 |
| `[generate] epg_url` | `https://epg.pw/api/epg.xml` | EPG 节目单 |
| `[generate] logo_base` | 空 | 台标前缀；为空则只用源自带 `tvg-logo` |

---

## 十一、许可与免责

代码仅供学习与技术研究使用。所有流地址均来自互联网公开仓库，本项目不存储、不分发任何视频内容，
不对第三方源的合法性、稳定性、地域可用性作任何担保。请遵守当地法律法规与版权要求。
