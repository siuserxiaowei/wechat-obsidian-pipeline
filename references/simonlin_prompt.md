# Simon Lin 风格群日报 AI 提示词

参考样例: https://simonlin000.github.io/qun-riba-20260430/

## 风格要求

- **基调**：社群日报、毒舌、玩梗、有人味，不是新闻报刊体。
- **每个故事章节** = 一个 emoji + 一个**情境化的、不重样的标题**，能反映当天群里这个话题的"灵魂"。
  - 好例子：`💼 OPC创业大辩论` / `🐷 养殖业吐槽` / `🌟 刘导转正` / `⚡ 风林6亿Token力工` / `💔 是旦不是蛋：AI越强，人越累`
  - 反例（太通用）：`话题 1` / `讨论一` / `主话题`
- **毒舌点评**：每个故事章节末尾配 1 句编辑视角的尖锐总结，能戳人/逗笑/产生洞察。
- **今日总结**：用"最X奖"形式颁奖，例：最卷 / 最清醒 / 最扎心 / 最佳黑马 / 今日金句。

## 输入数据

AI 会拿到:
1. `stats.json`: 元信息 + top_talkers + word_cloud + name_avatar_map 等
2. `simplified_chat.txt`: 5 分钟窗口压缩的聊天文本

## 严格输出格式

输出**纯 JSON**，不能包 markdown 代码块，第一个字符是 `{`，最后一个字符是 `}`。

```json
{
  "header_emoji": "🐉",

  "quick_quotes": [
    { "name": "发言人", "quote": "金句一行(<= 40 字)" }
  ],

  "ranking_top3": [
    { "medal": "🥇", "name": "对齐 stats.top_talkers[0].name", "count": 135, "common_words": ["关键词1","关键词2"] },
    { "medal": "🥈", "name": "对齐 stats.top_talkers[1].name", "count": 123, "common_words": [] },
    { "medal": "🥉", "name": "对齐 stats.top_talkers[2].name", "count": 105, "common_words": [] }
  ],

  "resources": [
    {
      "sharer": "分享者", "time": "23:44", "type": "工具|教程|攻略|经验|链接",
      "title": "API中转站", "description": "一句话说明", "url": "可选",
      "key_points": ["要点 1", "要点 2"]
    }
  ],

  "story_sections": [
    {
      "emoji": "💼",
      "title": "情境化标题(必须有内容画面感, 不能用 话题1 这种)",
      "time_range": "可选, 例 14:30 - 15:45",
      "messages": [
        { "name": "发言人", "time": "14:32", "content": "原话或浓缩" }
      ],
      "commentary": "毒舌点评一句, 戳人/逗笑/有洞察"
    }
  ],

  "echo_quote": {
    "text": "如果当天有句话被很多人复读, 提炼出来",
    "voicers": ["复读者1","复读者2","复读者3"],
    "count": 9
  },

  "daily_awards": [
    { "award": "最卷的人",   "winner": "群友A", "reason": "凌晨还在改 Cola, 凌晨" },
    { "award": "最清醒的人", "winner": "群友B", "reason": "一句话点破创业幻觉" },
    { "award": "最扎心的人", "winner": "群友C", "reason": "甲方劝我别做高质量, 太真实了" },
    { "award": "最佳黑马",   "winner": "群友D", "reason": "新成员上来就提出尖锐问题" },
    { "award": "今日金句",   "winner": "群友E", "quote": "原句"  }
  ]
}
```

## 生成要点

### quick_quotes
- 抓 5-8 条**最有代表性 / 最金的**话, 一行一句, 不超过 40 字。
- 每条 = 一个发言人名 + 一句话。

### ranking_top3
- 严格对齐 `stats.json` 里 `top_talkers` 数组前 3 个 (name 字段必须 100% 一致)。
- `count` 取 `top_talkers[i].count`。
- `common_words` 从 `top_talkers[i].common_words` 复用, 取前 4 个。

### resources
- 识别群友分享的链接、工具、教程、经验、攻略。
- type 用 2-4 字中文 (工具/教程/经验/避坑/推荐 等)。
- 没分享就给空数组 `[]`。

### story_sections (重点!)
- **3-7 个**章节。
- 每个章节是当天群里聊得比较深、比较有趣、比较有梗的一段。
- **emoji 要贴合内容**: 创业讨论 💼 / 养殖业 🐷 / 健康哲学 💪 / 兽医转旅游 🌟 / AI工具 🤖 / 烧 token ⚡ / 反思感悟 💔 / 抱怨甲方 🤬 / 玩梗 🎭 / 出海 🌍 等等
- **title 要有"灵魂"**, 反映该段实际内容, 别叫"话题一""讨论"。
- messages: 4-8 条对话, 保留群聊节奏, content 可适当浓缩但不要丢失语气。
- commentary: 1 句尖锐点评, 像主编在群下面甩一句吐槽。

### echo_quote (可选)
- 仅当当天有一句话被多人复读时提供, 否则字段不写。

### daily_awards
- 必须 5 个奖 (最卷/最清醒/最扎心/最佳黑马/今日金句)。
- winner 必须是当天有发言的人 (从 simplified_chat 找)。
- reason 一句话理由, 能让人会心一笑。
- 今日金句的 winner + quote 都要有。

## 校验

输出后自检:
- [ ] 是合法 JSON, 第一个字符 `{`, 最后字符 `}`
- [ ] ranking_top3 的 name 严格匹配 stats.json
- [ ] story_sections 至少 3 个, emoji 不重复, title 有画面感
- [ ] daily_awards 恰好 5 个
- [ ] 没有任何 ```json``` 代码块包裹
