#!/usr/bin/env bash
# ============================================================
# 模块 G：推送脚本（Gitee）
#   - 固定提交身份 iptv-bot
#   - 只提交产物与注册表，不提交日志
#   - 无实际变更时跳过提交（避免空提交污染历史）
#   - 通过 GITEE_PUSH_TOKEN + GITEE_REPO_URL 推送，失败打印退出码
#
# 环境变量：
#   GITEE_PUSH_TOKEN   私人令牌（projects 权限）
#   GITEE_REPO_URL     https://gitee.com/<user>/<repo>.git 或含 oauth2 的完整地址
#   PUSH_BRANCH        默认 master
#   GIT_USER_NAME / GIT_USER_EMAIL  默认 iptv-bot / bot@local
# ============================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

GIT_USER_NAME="${GIT_USER_NAME:-iptv-bot}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-bot@local}"
PUSH_BRANCH="${PUSH_BRANCH:-master}"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M')"

log() { printf '%s\n' "$*"; }
fail() {
  log "[ERROR] $*"
  exit 1
}

command -v git >/dev/null 2>&1 || fail "git 未安装"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "[WARN] 当前目录不是 git 仓库，执行 git init"
  git init >/dev/null 2>&1 || fail "git init 失败"
fi

git config user.name "$GIT_USER_NAME"
git config user.email "$GIT_USER_EMAIL"
git config core.quotepath false

# ---- 只提交这些产物
CANDIDATES=(
  "output/iptv.m3u"
  "output/iptv.txt"
  "sources/registry.json"
  "sources/graveyard.json"
)
FILES=()
for f in "${CANDIDATES[@]}"; do
  if [ -f "$f" ]; then
    FILES+=("$f")
  else
    log "[WARN] 缺失文件，跳过：$f"
  fi
done
[ ${#FILES[@]} -gt 0 ] || fail "没有任何可提交的产物文件（请先运行 generate_m3u.py）"

git add -- "${FILES[@]}" || fail "git add 失败"

# ---- 空变更检测
if git diff --cached --quiet; then
  log "[INFO] 无变更，跳过提交与推送"
  exit 0
fi

git commit -m "auto: update iptv sources ${TIMESTAMP}" >/dev/null || fail "git commit 失败（退出码 $?）"
log "[INFO] 已提交：auto: update iptv sources ${TIMESTAMP}"

# ---- 解析推送地址
REMOTE_URL="${GITEE_REPO_URL:-}"
if [ -z "$REMOTE_URL" ]; then
  REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
fi

if [ -n "${GITEE_PUSH_TOKEN:-}" ] && [ -n "$REMOTE_URL" ] && [[ "$REMOTE_URL" != *"@"* ]]; then
  # https://gitee.com/u/r.git -> https://oauth2:<token>@gitee.com/u/r.git
  REMOTE_URL="$(printf '%s' "$REMOTE_URL" | sed -E "s#^https?://#https://oauth2:${GITEE_PUSH_TOKEN}@#")"
fi

MASKED_URL="$(printf '%s' "$REMOTE_URL" | sed -E 's#(https://)[^@/]*@#\1***@#')"
log "[INFO] 推送目标：${MASKED_URL:-<未配置>}  分支：${PUSH_BRANCH}"

if [ -z "$REMOTE_URL" ]; then
  fail "未配置 GITEE_REPO_URL，且仓库没有 origin 远端"
fi

# 尝试把本地历史对齐远端（失败不阻塞，仅在冲突时由 push 报错暴露）
git pull --rebase --autostash "$REMOTE_URL" "$PUSH_BRANCH" >/dev/null 2>&1 || true

set +e
PUSH_OUTPUT="$(git push "$REMOTE_URL" "HEAD:${PUSH_BRANCH}" 2>&1)"
PUSH_CODE=$?
set -e

if [ $PUSH_CODE -ne 0 ]; then
  log "[ERROR] git push 失败，退出码 ${PUSH_CODE}"
  printf '%s\n' "$PUSH_OUTPUT" | sed -E 's#(https://)[^@/]*@#\1***@#g'
  log "[HINT] 常见原因：令牌无 projects 权限 / 令牌过期 / 分支保护 / 远端非快进（需先 pull）"
  exit $PUSH_CODE
fi

printf '%s\n' "$PUSH_OUTPUT" | sed -E 's#(https://)[^@/]*@#\1***@#g'
log "[OK] 推送完成：${TIMESTAMP}"
exit 0
