# wechat-obsidian-pipeline

> 把微信聊天记录自动接进 Obsidian vault, 每天早上 8 点出**社群日报长图**(Simon Lin 风格).

一套连接微信本地数据 ↔ Obsidian vault 的流水线工具. 支持两条独立的取数路径 ([wx-cli](https://github.com/jackwener/wx-cli) 和 [WeFlow](https://github.com/hicccc77/WeFlow)), 自动渲染成 Obsidian 友好的 Markdown 时间线, 并出每日 PNG 长图报告. 通过 macOS launchd 定时调度, 出来的报告 + AI 摘要直接归档到 vault, 可在 Obsidian 内全文搜索 / Dataview 查询 / 反链.

## 🎨 长图风格

参考 [SimonLin 社群日报](https://simonlin000.github.io/qun-riba-20260430/) 的设计:

- 🐉 头部品牌 + 日期 + 群名
- 🔥 **快捷事件** — 当日金句速览
- 🏆 **龙王榜** — TOP 3 话痨(带 🥇🥈🥉 奖牌 + 高频词)
- 🔗 **资源分享** — 工具/教程/链接卡片
- 多个 **故事章节** — 每段一个 emoji + 情境化标题(如 `💼 OPC创业大辩论` / `🐷 养殖业吐槽`)+ 对话气泡 + ✏️ **毒舌点评**
- 💪 **群体复读** — 当天被复读的金句
- ☁️ **关键词云** — 当日高频词 chips
- 📊 **今日总结** — 颁奖区(最卷/最清醒/最扎心/最佳黑马/今日金句)

## ✨ 它做的事

```
WeChat (本机)
   │
   ├─ wx-cli (npm) ──────┐    ★ 推荐, 直读本地 SQLite, 不依赖 GUI
   │                      │
   ├─ WeFlow API ─────────┤    HTTP API, 备选, 大群易超时
   │                      │
   └─ ChatLab JSON ───────┤    一次性手动导出
                          ▼
                    .cache/ ChatLab 标准格式 JSON
                          │
                ┌─────────┼──────────┐
                │         │          │
                ▼         ▼          ▼
           render.py   wf_report  daily_report.sh
                │       (stats)        │
                ▼          │      (Claude CLI)
        Obsidian Markdown   │            │
        (扁平 YYYY-MM-DD.md)│            ▼
        + frontmatter       │      ai_content.json
        + 群成员显示名      │            │
                           ▼            ▼
                  generate_report.py (Playwright)
                           │
                           ▼
                       report.png (1290×长)
                           │
                           ▼
              vault/聊天记录导出/群聊/<群>/reports/
```

## 🚀 快速开始

### 前置依赖

- macOS (Apple Silicon 或 Intel)
- Python 3.10+
- Node.js 18+ (装 wx-cli 用)
- `jq`, `curl`, `git`
- [Claude Code CLI](https://docs.claude.com/claude-code) (`claude -p` 跑 AI 步骤)
- [wechat-daily-report-skill](https://github.com/ADVISORYDZ/wechat-daily-report-skill) (出 PNG, 装到 `~/.claude/skills/`)
- 一个 Obsidian vault

### 装 wx-cli (推荐路径)

```bash
npm install -g @jackwener/wx-cli
sudo codesign --force --deep --sign - /Applications/WeChat.app
killall WeChat; open /Applications/WeChat.app
sleep 5
sudo wx init
# 修个权限 bug (老版本 sudo 留 root)
sudo chown -R "$(whoami)" ~/.wx-cli
wx sessions -n 5  # 验证
```

### 装 WeFlow (备选路径)

去 [WeFlow Releases](https://github.com/hicccc77/WeFlow/releases) 下 `.dmg`. 打开后:
1. 设置 → API 服务 → 启用
2. 默认端口 5031
3. 复制 Access Token

### 装 wechat-daily-report skill (PNG 出图)

```bash
git clone https://github.com/ADVISORYDZ/wechat-daily-report-skill ~/.claude/skills/wechat-daily-report
cd ~/.claude/skills/wechat-daily-report
python3 -m venv .venv
source .venv/bin/activate
pip install jieba jinja2 playwright
playwright install chromium
```

### 装本仓库

```bash
git clone https://github.com/<你>/wechat-obsidian-pipeline ~/Documents/<vault>/.claudian/wechat
cd ~/Documents/<vault>/.claudian/wechat
cp config.example.json secrets.json
# 编辑 secrets.json: 填 endpoint(默认 5031) + token(从 WeFlow GUI 复制)
chmod +x bin/*
```

## 📖 使用

`wf` 是主 CLI. 命令列表:

```bash
.claudian/wechat/bin/wf help
```

```
wf — WeFlow API CLI

用法:
  wf health                       健康检查
  wf sessions [keyword]           列出所有会话 (可选关键词过滤)
  wf messages <talker> [opts]     拉某会话消息
  wf members <chatroomId>         群成员列表(含发言数)
  wf moments [limit]              朋友圈时间线
  wf sync <talker> [opts]         WeFlow 路径: 拉取 → 渲染 → 写入 vault
  wf wxsync <chat_id> [opts]      ★推荐★ wx-cli 路径
                                  --since YYYY-MM-DD --until YYYY-MM-DD
  wf report <talker> --date D     生成日报 stats(供 Claude 接手做 ai_content + PNG)
  wf raw <path> [params...]       直接调用任意 API 端点 (调试用)
```

### 常见任务

```bash
# 列出 wx-cli 看到的全部会话
wf sessions

# 拉天策成长团 4/25-5/2 的全量数据并写进 vault
wf wxsync 49192810916@chatroom --since 2026-04-25 --until 2026-05-02

# 给某天某群出日报(交互式: Claude Code 会接手做 AI 步骤)
wf report 49192810916@chatroom --date 2026-05-01

# 端到端非交互(用 claude CLI 做 AI 步骤): 跑昨天的全部 3 个群
.claudian/wechat/bin/daily_report.sh

# 端到端非交互, 单群单日
.claudian/wechat/bin/daily_report.sh 2026-05-01 49192810916@chatroom
```

## 📅 每日自动化 (launchd)

### 安装

```bash
# 1. 编辑 launchd/com.claudian.wechat-daily-report.plist
#    把里面的路径改成你的 vault 实际路径
#    把 daily_report.sh 里的 GROUPS 数组改成你想监控的群

# 2. 拷到 LaunchAgents 目录
cp launchd/com.claudian.wechat-daily-report.plist \
   ~/Library/LaunchAgents/

# 3. 加载
launchctl bootstrap "gui/$(id -u)" \
   ~/Library/LaunchAgents/com.claudian.wechat-daily-report.plist

# 4. 验证
launchctl list | grep claudian
```

每天 08:00(本地时间) 自动跑. 日志在 `<vault>/.claudian/wechat/logs/`.

### 卸载

```bash
launchctl bootout "gui/$(id -u)/com.claudian.wechat-daily-report"
rm ~/Library/LaunchAgents/com.claudian.wechat-daily-report.plist
```

## 📁 项目结构

```
wechat-obsidian-pipeline/
├── bin/
│   ├── wf                       # 主 CLI (bash)
│   ├── wx_sync.py               # wx-cli → ChatLab JSON 适配
│   ├── sync_paginated.py        # WeFlow API 分页拉取
│   ├── wf_report.py             # 出 stats.json + simplified_chat.txt
│   ├── render.py                # ChatLab JSON → Obsidian Markdown
│   └── daily_report.sh          # 端到端日报 (launchd 触发)
├── launchd/
│   └── com.claudian.wechat-daily-report.plist
├── config.example.json          # 配置模板
└── README.md
```

### 写到 vault 的产出

```
<vault>/聊天记录导出/
├── 00.索引/
│   ├── 全部会话.md              (Dataview 看板)
│   ├── 推荐归档清单.md          (评分 TOP 100)
│   ├── 高价值文档.md
│   └── 待清理候选.md
├── 群聊/
│   └── <群名>/
│       ├── _index.md            (该群的所有日期索引)
│       ├── 2026-05-01.md        (扁平: 一天一个文件)
│       ├── 2026-05-02.md
│       └── reports/             (日报 PNG + ai_content.json)
│           ├── 2026-05-01-日报.md
│           ├── 2026-05-01-日报.png
│           ├── 2026-05-01-日报.html
│           ├── 2026-05-01-日报.ai-content.json
│           └── 2026-05-01-日报.stats.json
└── 私聊/...
```

## 🧠 设计决策

### 为什么 wx-cli 是首选, 不是 WeFlow

我们一开始用 WeFlow API. 实测后发现:
- ❌ WeFlow 大查询会卡死整个 GUI 进程, 反复需要重启
- ❌ WeFlow API limit 上限不明, `limit=10000` 经常超时
- ❌ WeFlow 的 ChatLab Pull 端点 (`/api/v1/sessions/:id/messages`) 在某些版本返回 404
- ✅ wx-cli 直读本地 SQLite, 速度快(几秒)
- ✅ wx-cli 跑 `wx members` 后, 后续 `wx history` 自动把 sender 从 wxid 转成昵称

### 为什么用扁平目录结构 (`2026-05-01.md`) 而不是嵌套 (`2026-05/2026-05-01.md`)

- Obsidian 反链 / Dataview / 全文搜索都更友好
- 跨日跳转可以用日期模板 `[[2026-05-01]]` 直接命中
- 单层目录更适合移动端浏览

### 为什么本地 launchd 不是远程 schedule

我们的管线必须读本机微信数据(wx-cli 直读 SQLite, 或 WeFlow GUI 跑在本机). Anthropic 的 schedule 跑在云上, 拿不到本机数据. 所以选 launchd.

## 🛟 故障排查

### `wx sessions` 报 "无法写入 ~/.wx-cli (权限不足)"

老版本 `sudo wx init` 留 root 拥有. 修复:
```bash
sudo chown -R "$(whoami)" ~/.wx-cli
```

### WeFlow API 反复超时

WeFlow 在大群 / 大查询时 GUI 进程会卡死. 重启 WeFlow 即可恢复(API + Token 自动持久化).

### `daily_report.sh` 报 `成功 0 / 失败 0 / 跳过 16` 这种诡异数字

不是 bug, 是 Bash 内置变量陷阱. 别用 `GROUPS` 当变量名(它是 user 所属 unix groups), 用 `TARGETS` 之类的.

### launchd 跑不起来

```bash
# 看错误日志
cat ~/Library/Logs/launchd.err.log
cat <vault>/.claudian/wechat/logs/launchd.err.log

# 手动触发一次
launchctl kickstart "gui/$(id -u)/com.claudian.wechat-daily-report"
```

## 📜 致谢与上游

| 项目 | 角色 |
|---|---|
| [jackwener/wx-cli](https://github.com/jackwener/wx-cli) | 本地微信 SQLite 直读 (推荐路径) |
| [hicccc77/WeFlow](https://github.com/hicccc77/WeFlow) | WeChat HTTP API 服务 (备选路径) |
| [ILoveBingLu/CipherTalk](https://github.com/ILoveBingLu/CipherTalk) | WeFlow 上游基础 |
| [ADVISORYDZ/wechat-daily-report-skill](https://github.com/ADVISORYDZ/wechat-daily-report-skill) | 长图 PNG 渲染 + AI prompt 模板 |
| [Jane-xiaoer/wechat-to-obsidian](https://github.com/Jane-xiaoer/wechat-to-obsidian) | 同方向先行项目, 灵感来源 |

## 📄 许可

MIT
