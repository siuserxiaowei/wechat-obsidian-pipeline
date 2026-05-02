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
