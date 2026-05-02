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
# VAULT 默认从脚本位置推算 (脚本应放在 <vault>/.claudian/wechat/bin/)
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
declare -a TARGETS=(
    # 改成你自己的群. 跑 `wf sessions` 找 ID.
    "1234567890@chatroom|示例群A"
    "9876543210@chatroom|示例群B"
  )

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

  # 3) claude CLI 写 ai_content.json (用 --json-schema 强制结构化)
  echo "[3/5] claude CLI 写 ai_content.json (Simon 风格)..."
  local prompt_md="$WECHAT_ROOT/references/simonlin_prompt.md"
  local schema_file="$WECHAT_ROOT/references/simonlin_schema.json"
  local stats="$workdir/stats.json"
  local chat="$workdir/simplified_chat.txt"

  if [ ! -s "$chat" ]; then
    echo "  ⚠️  simplified_chat 为空, 跳过"
    return 1
  fi
  if [ ! -f "$prompt_md" ] || [ ! -f "$schema_file" ]; then
    echo "  ❌ 缺 prompt/schema: $prompt_md / $schema_file"
    return 1
  fi

  local prompt_file
  prompt_file="$(mktemp -t claude-prompt-XXXXXX)"
  cat > "$prompt_file" <<PROMPT
按下面的 simonlin_prompt.md 风格生成微信群日报 ai_content. 严格遵循 JSON schema. 用中文引号「」代替英文引号防止转义问题.

================ 风格指南 ================
$(cat "$prompt_md")

================ stats.json ================
$(cat "$stats")

================ simplified_chat ================
$(cat "$chat")
PROMPT

  local schema
  schema="$(cat "$schema_file")"

  if ! claude -p --output-format json --json-schema "$schema" < "$prompt_file" > "$workdir/raw.json" 2>"$workdir/claude.err"; then
    echo "  ⚠️  claude CLI 失败:"; cat "$workdir/claude.err" | head -10
    rm -f "$prompt_file"
    return 1
  fi
  rm -f "$prompt_file"

  # 提取 structured_output 字段
  python3 -c "
import json, sys
raw = json.load(open('$workdir/raw.json'))
so = raw.get('structured_output')
if not so:
    so = raw.get('result')
    if isinstance(so, str):
        try: so = json.loads(so)
        except: pass
if not isinstance(so, dict):
    print('  ⚠️  没拿到 structured ai_content', file=sys.stderr)
    sys.exit(1)
open('$workdir/ai_content.json','w').write(json.dumps(so, ensure_ascii=False, indent=2))
print(f'  ✓ {len(so.get(\"story_sections\", []))} 章节 / {len(so.get(\"daily_awards\", []))} 奖项')
"
  if [ $? -ne 0 ]; then return 1; fi

  # 4a) 先抓 message_count
  local msg_count
  msg_count=$(jq -r '.meta.total_count // 0' "$stats")

  # 4b) 先归档 stats + ai_content
  local target="$VAULT/聊天记录导出/群聊/$name/reports"
  mkdir -p "$target"
  cp "$stats"                       "$target/$TARGET_DATE-日报.stats.json"
  cp "$workdir/ai_content.json"     "$target/$TARGET_DATE-日报.ai-content.json"

  # 4c) render_simonlin.py 出 PNG
  echo "[4/5] render_simonlin.py 出 PNG..."
  "$SKILL_DIR/.venv/bin/python" "$WECHAT_ROOT/bin/render_simonlin.py" \
    --stats "$stats" \
    --ai-content "$workdir/ai_content.json" \
    --output "$workdir/report.png" \
    --source "wx-cli" 2>&1 | tail -3
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
