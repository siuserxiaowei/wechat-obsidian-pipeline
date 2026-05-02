#!/usr/bin/env bash
# daily_report.sh — 每日自动跑微信群聊日报
#
# 流程:
#   1. wx-cli sync 拉昨天的数据
#   2. wf_report.py 生成 stats.json + simplified_chat.txt
#   3. claude CLI 读 prompt + 数据 → 写 ai_content.json
#   4. generate_report.py → report.png
#   5. 拷到 vault/聊天记录导出/群聊/<群>/reports/
#
# 用法:
#   daily_report.sh                        # 默认跑昨天
#   daily_report.sh 2026-04-30             # 指定日期
#   daily_report.sh 2026-05-01 49192810916@chatroom  # 单群
#
# 由 launchd 每天 08:00 触发
# Logs: .claudian/wechat/logs/daily-report-YYYYMMDD.log

set -uo pipefail

# === 路径 ===
# VAULT 路径: 默认从脚本位置推算 (脚本应放在 <vault>/.claudian/wechat/bin/)
VAULT="${VAULT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
WECHAT_ROOT="$VAULT/.claudian/wechat"
SKILL_DIR="$HOME/.claude/skills/wechat-daily-report"
SECRETS="$WECHAT_ROOT/secrets.json"
LOGS="$WECHAT_ROOT/logs"
mkdir -p "$LOGS"

# === 日志 ===
DATE_TODAY=$(date +%Y%m%d)
LOG="$LOGS/daily-report-$DATE_TODAY.log"
exec >> "$LOG" 2>&1

# === 参数 ===
TARGET_DATE="${1:-$(date -v-1d +%Y-%m-%d)}"
SINGLE_TALKER="${2:-}"

echo ""
echo "════════════════════════════════════════════"
echo "$(date '+%Y-%m-%d %H:%M:%S') · daily_report.sh"
echo "目标日期: $TARGET_DATE"
[ -n "$SINGLE_TALKER" ] && echo "单群模式: $SINGLE_TALKER"
echo "════════════════════════════════════════════"

# === 配置: 监控的群列表 ===
# 格式: "chatroom_id|显示名"
# - chatroom_id: 跑 `wf sessions` 找到, 形如 1234567890@chatroom 或 wxid_xxx
# - 显示名: 决定 vault 里的目录名(聊天记录导出/群聊/<显示名>/)
#
# 也可以用环境变量 TARGETS_FILE 指向一个外部配置文件
# (每行 "id|name", 注释行以 # 开头)
if [ -n "${TARGETS_FILE:-}" ] && [ -f "$TARGETS_FILE" ]; then
  declare -a TARGETS=()
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" ]] && continue
    TARGETS+=("$line")
  done < "$TARGETS_FILE"
else
  declare -a TARGETS=(
    # 改成你自己的群. 跑 `wf sessions` 找 ID.
    "1234567890@chatroom|示例群A"
    "9876543210@chatroom|示例群B"
  )
fi

cd "$VAULT" || exit 1

# === PATH 注入 (launchd 没有 user shell PATH) ===
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# === 工具检查 ===
for tool in wx python3 jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "❌ $tool 不在 PATH"; exit 2
  fi
done

if [ ! -x "$SKILL_DIR/.venv/bin/python" ]; then
  echo "❌ skill venv 不存在: $SKILL_DIR/.venv"; exit 2
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "❌ claude CLI 不在 PATH"; exit 2
fi

# === 跑一个群的全流程 ===
process_group() {
  local talker="$1"
  local name="$2"

  echo ""
  echo "──────────────────────────────────────────"
  echo "📂 $name ($talker)"
  echo "──────────────────────────────────────────"

  # 1) wx-cli sync (--no-render, 只要 cache JSON)
  echo "[1/5] wx-cli sync ..."
  if ! python3 "$WECHAT_ROOT/bin/wx_sync.py" "$talker" \
       --since "$TARGET_DATE" --until "$TARGET_DATE" \
       --no-render \
       --secrets "$SECRETS"; then
    echo "  ⚠️  wx-cli sync 失败, 跳过该群"
    return 1
  fi

  # 2) wf_report.py 生成 stats + simplified
  echo "[2/5] wf_report.py 出统计..."
  local workdir
  workdir="$(mktemp -d -t wf-report-XXXXXX)"
  if ! "$SKILL_DIR/.venv/bin/python" "$WECHAT_ROOT/bin/wf_report.py" "$talker" \
       --date "$TARGET_DATE" \
       --workdir "$workdir" \
       --secrets "$SECRETS"; then
    echo "  ⚠️  wf_report 失败 (当天可能没消息), 跳过"
    return 1
  fi

  # 3) claude CLI 写 ai_content.json
  echo "[3/5] claude CLI 写 ai_content.json..."
  local prompt_md="$SKILL_DIR/references/ai_prompt.md"
  local stats="$workdir/stats.json"
  local chat="$workdir/simplified_chat.txt"

  if [ ! -s "$chat" ]; then
    echo "  ⚠️  simplified_chat 为空, 跳过"
    return 1
  fi

  local prompt_file
  prompt_file="$(mktemp -t claude-prompt-XXXXXX)"
  cat > "$prompt_file" <<PROMPT
按 ai_prompt.md 的 JSON schema 输出一个微信群日报的 ai_content. 必须是合法 JSON, 不能包 markdown 代码块或任何文字解释, 只输出 JSON 本身.

talker_profiles 的 key 必须严格匹配 stats.json 中 top_talkers 数组里每条的 name 字段.

================ ai_prompt.md ================
$(cat "$prompt_md")

================ stats.json ================
$(cat "$stats")

================ simplified_chat.txt ================
$(cat "$chat")

记住: 直接输出 JSON 对象, 第一个字符必须是 {, 最后一个字符必须是 }.
PROMPT

  if ! claude -p < "$prompt_file" > "$workdir/ai_content.json" 2>"$workdir/claude.err"; then
    echo "  ⚠️  claude CLI 失败:"; cat "$workdir/claude.err" | head -10
    return 1
  fi
  rm -f "$prompt_file"

  # 兜底: 去掉可能的 ```json 包裹
  python3 -c "
import json, sys, re
p = '$workdir/ai_content.json'
text = open(p).read().strip()
m = re.search(r'\{.*\}', text, re.S)
if m: text = m.group(0)
try:
    json.loads(text)
    open(p,'w').write(text)
    print('  ✓ ai_content.json valid')
except Exception as e:
    print(f'  ⚠️  ai_content 不合法 JSON: {e}')
    sys.exit(1)
"
  if [ $? -ne 0 ]; then return 1; fi

  # 4a) 先抓 message_count (后面 --clean-temp 会删 stats.json)
  local msg_count
  msg_count=$(jq -r '.meta.total_count // 0' "$stats")

  # 4b) 先归档 stats + simplified_chat (PNG 渲染会清掉它们)
  local target="$VAULT/聊天记录导出/群聊/$name/reports"
  mkdir -p "$target"
  cp "$stats"                       "$target/$TARGET_DATE-日报.stats.json"
  cp "$workdir/ai_content.json"     "$target/$TARGET_DATE-日报.ai-content.json"

  # 4c) generate_report.py 出 PNG (不要 --clean-temp, 我们自己控制)
  echo "[4/5] generate_report.py 出 PNG..."
  (
    cd "$SKILL_DIR" && \
    source .venv/bin/activate && \
    python scripts/generate_report.py \
      --stats "$stats" \
      --ai-content "$workdir/ai_content.json" \
      --output "$workdir/report.png" 2>&1 | tail -3
  )
  if [ ! -s "$workdir/report.png" ]; then
    echo "  ⚠️  PNG 没生成"; return 1
  fi

  # 5) 拷 PNG + HTML
  echo "[5/5] 归档 PNG..."
  cp "$workdir/report.png"        "$target/$TARGET_DATE-日报.png"
  [ -f "$workdir/report.html" ] && cp "$workdir/report.html" "$target/$TARGET_DATE-日报.html" || true
  cat > "$target/$TARGET_DATE-日报.md" <<MD
---
type: chat-report
chat: $name
chat_id: "$talker"
date: $TARGET_DATE
message_count: $msg_count
data_source: wx-cli
generated_by: daily_report.sh (launchd 自动)
generated_at: $(date '+%Y-%m-%dT%H:%M:%S%z')
tags: [chat-report, daily, wechat, auto]
---

# 📊 $name · $TARGET_DATE 日报

> [!note] 自动生成于 $(date '+%Y-%m-%d %H:%M')
> 数据源: wx-cli  ·  消息数: $msg_count

![[$TARGET_DATE-日报.png]]

## 关联

- [[../$name/$TARGET_DATE.md|当日完整时间线]]
- [[../$name/_index.md|会话索引]]
MD

  rm -rf "$workdir"
  echo "✅ Done: $target/$TARGET_DATE-日报.png"
  return 0
}

# === 主循环 ===
SUCCESS=0
FAIL=0
SKIP=0

for entry in "${TARGETS[@]}"; do
  IFS='|' read -r talker name <<< "$entry"
  [ -n "$SINGLE_TALKER" ] && [ "$talker" != "$SINGLE_TALKER" ] && { ((SKIP++)); continue; }
  if process_group "$talker" "$name"; then
    ((SUCCESS++))
  else
    ((FAIL++))
  fi
done

echo ""
echo "════════════════════════════════════════════"
echo "🏁 $(date '+%H:%M:%S')  成功 $SUCCESS / 失败 $FAIL / 跳过 $SKIP"
echo "════════════════════════════════════════════"
exit 0
