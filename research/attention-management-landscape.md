# 调研：帮人"做减法"的注意力管理产品与项目

> 调研时间：2026-10-20 ｜ 背景：`dsh-bili-scroll` 的核心不是"刷 B 站"，而是**替人守住注意力**——
> 每天只放进来少数几条、判据是"值不值得占用他一次注意力"。本文找同类产品/项目，看它们各自怎么做减法、缺什么。
> 方法：网页检索 + 项目 README / 官方博客 / 媒体报道。**未逐个安装试用**；标注「待核实」的条目只有单一来源。

---

## 0. 先给一个分层框架

"做减法"有不同的切入深度，从浅到深大致五层：

| 层 | 做法 | 代表 | 减法发生在哪 |
|---|---|---|---|
| L1 拦截 | 把推荐流、短视频入口直接藏掉 | Unhook、BewlyBewly | 界面层：看不到就不会点 |
| L2 重排 | 平台照样给内容，换一套按你价值观排序的算法 | Tournesol、Bluesky 自定义 Feed / Graze / Attie、Alexandria、Science 重排实验 | 排序层：坏的沉下去 |
| L3 聚合 + AI 筛选 | 把多个源拉到一起，AI 按兴趣打分、去重、摘要，只推高分 | TrendRadar、auto-news、ai-news-brief、Folo、Feedly Leo、BestBlogs、DailyBrief | 管道层：大部分内容根本不到你面前 |
| L4 节制型产品 | 产品形态本身限量：一天一期、没有无限滚动 | Kagi News、smol.ai AINews、Particle | 节奏层：读完就没了 |
| L5 主动代理 | 你不去找，代理替你看；只在值得时打扰你 | ChatGPT Pulse、Google CC、Yutori Scouts、follow-builders、**dsh-bili-scroll** | 决策层：连"要不要看"都替你先判一遍 |

`dsh-bili-scroll` 属于 **L5 + L3**：代理替你去平台里看（读字幕原文），按"每天最多 N 条"的硬额度筛，再把结果送回给你。

---

## 1. L1 拦截：去掉干扰入口（无 AI）

- **Unhook**（YouTube 浏览器扩展）：开关式地隐藏首页推荐、结束页推荐、评论、Shorts、热门、自动播放。Firefox 上有 GPL-3.0 的开源分支 **Unhook NG**。
- **BewlyBewly**（B 站扩展）：B 站首页改造，可把网页端推荐换成 App 端推荐、调整首页布局。注意它是"换一个推荐"，**不是去掉推荐**。
- 评价：最便宜也最有效的一层，但只会"减"不会"选"——藏掉推荐之后，好东西也得靠你自己去搜。

## 2. L2 重排：在平台内换算法

- **Tournesol**（EPFL 团队发起的非营利基金会，开源）：用户两两对比视频，众包出"这条视频值不值得被推荐"的分数，浏览器扩展把 Tournesol 的推荐放到你的 YouTube 首页。有抗女巫攻击的治理设计（邮箱认证、担保、EigenTrust 变体）。是"**集体判断什么值得看**"的代表。
- **Bluesky 自定义 Feed / Graze / Attie**：AT Protocol 允许任何人发布自己的 Feed 算法。Graze 是做自定义 Feed 的平台（自称投放了 410 亿条帖子、1200 万用户，后被 Flipboard 收购）；Bluesky 官方推出 **Attie**，用自然语言描述就能生成自定义 Feed。是"**算法可选、可自建**"的代表。
- **研究：LLM 浏览器扩展重排 X 的信息流**
  - *Science*（2025）预注册田野实验（N=1,256，10 天）：用 LLM 识别"反民主态度与党派仇恨"内容并调整排序，**不需要平台配合**，发现排序能双向改变情感极化。内容一条没删，只改顺序。
  - **Alexandria**（arXiv 2505.10839）：一个价值库 + 用户自设权重，LLM 给每条帖子打价值标签，排序分 = 用户权重 · 价值标签。局限：只能重排平台已经推给你的内容。
- 评价：方向正确（把排序权交还给人），但仍然**留在平台的信息流里**，你还是在刷，只是刷得更"健康"。

## 3. L3 聚合 + AI 筛选：把内容拉出平台再筛

### 开源 / 自托管
| 项目 | 数据源 | 怎么筛 | 推到哪 |
|---|---|---|---|
| **TrendRadar**（sansan0，约数万 star） | 约 35 个平台热榜（含国内平台）+ RSS | 关键词分组 + AI 对照用户写的"兴趣段落"打分；当日汇总 / 当前榜单 / 增量监控三种模式；支持接 MCP | 微信、飞书、钉钉、Telegram、邮件、ntfy、Bark、Slack；可只用 GitHub Actions 免服务器 |
| **auto-news**（finaldie） | Twitter、RSS、YouTube（无字幕可转写）、Reddit、网页、个人笔记 | LLM 按个人兴趣过滤（自称过滤掉 80%+ 噪声），周 Top-K 回顾，从要点生成 TODO | Notion |
| **ai-news-brief**（frankzch，驱动 inbrief.info） | 94 个源：RSS、HN、Reddit、X、YouTube 字幕、GitHub Trending | LLM 相关性过滤 + 重要度打分 + SimHash/pgvector 去重，中英双语摘要 | 网站 |
| **DailyBrief**（及多个 fork） | 23 个源（GitHub 热门、X 文章、行情） | LLM 中文摘要 | 本地 / GitHub Actions |
| **News Buddy / news-agent / margulis94** 等 | 自配 RSS | 按兴趣档案选 + 摘要；margulis94 每条写一句"so what" | 邮件、Telegram、Slack、Discord |
| **Folo**（RSSNext，AGPL-3.0）+ **RSSHub** | RSSHub 把几乎任何站点转成 RSS | AI 摘要 / 翻译（可关） | 阅读器本身 |

### 商业
- **Feedly Leo**：按主题/关键词/公司设"优先"与"静音"规则，读到不喜欢的点"less like this"实时调整。Pro+ 才有完整功能。
- **BestBlogs.dev**：独立开发者做的技术内容聚合，400～600 个优质源，AI 给每篇打 0–100 分并写摘要，再由人工编辑复核。

评价：这一层最成熟、开源项目最多，但有两个通病——
1. **同质化严重**：绝大多数是"AI 新闻日报"，内容源是 RSS/HN/X/GitHub，盯的也是 AI 圈本身；
2. **国内封闭平台覆盖弱**：B 站、小红书、公众号这类没有开放接口的平台，基本只能靠 RSSHub / 热榜，**读不到正文/字幕级别的原文**。

## 4. L4 节制型产品：产品形态本身限量

- **Kagi News**：**一天只出一期**（每天 12:00 Kagi 时间），没有推送、没有下拉刷新、没有无限滚动，约 5 分钟读完；基于社区维护的 RSS 源，AI 把多家报道聚类成一条并附原文链接；前端开源（kite-public），可自托管、有公开数据 API。
- **smol.ai AINews**（swyx / Latent.Space）：每天把顶级 AI Discord、Reddit、X 上的讨论汇总成一封日报（已迁到 news.smol.ai）。很多 Claude Code / OpenClaw 技能直接拿它当上游。
- **Particle**（前 Twitter 工程师）：AI 新闻 App，把多家报道合成一条故事，2026 年 2 月加了"播客片段"——用 embedding 把相关的播客片段挂到新闻上，"替你听播客"。
- 评价：用**节奏**做减法，非常有效，但内容是编辑/社区选的，**不针对个人**。

## 5. L5 主动代理：替你看，只在值得时打扰

- **ChatGPT Pulse**（OpenAI，2025-09 发布，最初仅 Pro 移动端）：夜里根据你的聊天记录、记忆、反馈（可连 Gmail / 日历）做研究，早上给一组可展开的卡片。是"**基于对你的理解，主动给你少量东西**"的代表。
- **Google CC**（Google Labs，2025-12 实验）：连 Gmail / 日历 / Drive，每天早上发一封 "Your Day Ahead" 邮件；**交互完全在邮件里**——直接回复就能纠错、加待办，每封有赞/踩反馈。
- **Yutori Scouts**：用自然语言描述你要盯什么，一组云端代理（必要时操作浏览器）持续监控指定网址或全网，**有相关更新才通知**；另有 Scouting API。
- **follow-builders**（zarazhangrui，Claude Code / OpenClaw 技能，约 3.7k star）：只追一小批"有原创观点的 AI 构建者"（X 上约 25 人 + 6 个播客 + 2 个官方博客），合成日报/周报推 Telegram 或邮件。是"**先把源做减法，再把内容做减法**"的思路。
- **Huxe**（前 NotebookLM 团队）：个人音频简报（读你的邮件和日历生成"每日播报"、话题电台）。⚠️ 有报道称 2026 年 5 月停服、之后苹果与其达成人才 + 授权协议——**单一来源，待核实**。

## 6. 失败与教训

- **Artifact**（Instagram 联合创始人做的 AI 新闻 App）2024 年初关停，理由是市场机会不够大——"帮人读新闻"本身很难成为大生意。
- **Huxe** 若停服属实，说明"个人简报"作为独立 App 难以留存，更可能被大平台吸收（Pulse、CC 都是大厂内置）。
- 启示：**注意力减法产品很难靠"多一个 App"存活**——它要么寄生在你已有的入口里（邮件、IM、B 站私信），要么成为大模型助手的一个能力。`dsh-bili-scroll` 把结果送回 B 站评论/私信、跑在用户自己的 DSH 里，恰好避开了"再装一个 App"。

---

## 7. dsh-bili-scroll 在这张图里的位置

| 维度 | dsh-bili-scroll | 同类常见做法 |
|---|---|---|
| 读什么 | **字幕原文**（无字幕可本地 Whisper 转写） | 标题 + 摘要 / RSS 片段（auto-news、ai-news-brief 也读 YouTube 字幕） |
| 判据 | "值不值得占用他一次注意力"，按用户自己的 `AGENTS.md` 人设判 | 兴趣关键词 / 兴趣段落打分 |
| 额度 | **每天最多 N 条的硬额度**，跨轮累计 | 多数是 Top-K 或阈值，额度不是一等概念 |
| 投递 | 回到平台内：评论 @ 你 / 私信 | 飞书、微信、Telegram、邮件、Notion |
| 运行 | 用户本机的 DSH，谷时跑省 token | GitHub Actions / Docker / SaaS |
| 覆盖 | 只有 B 站 | 多源 |

**它独特的地方**：读原文而不是读摘要；"注意力额度"是硬约束；判据来自用户自己写的人设；国内封闭平台、视频内容——恰好是 L3 那批开源项目覆盖最弱的地方。

**它缺的**：只有一个平台；没有反馈闭环（你看没看、喜不喜欢，不会回流到判据）；没有防信息茧房的机制。

---

## 8. 机会点（这一赛道普遍还没做好的）

1. **跨平台的统一注意力预算**：现在每个工具管一个源。真正的"减法"应该是"今天全部来源加起来只给我 5 条"，B 站、小红书、公众号、YouTube、播客一起抢这 5 个名额。几乎没有项目把"预算"做成一等概念。
2. **读原文级别的国内平台适配**：B 站字幕、公众号全文、小红书笔记、播客转写。RSSHub / wewe-rss 能解决一部分订阅，但"读原文再判断"在国内平台上基本是空白——这是 dsh-bili-scroll 已经验证过的路线。
3. **反馈闭环**：Feedly 的"less like this"、Google CC 的赞/踩、Pulse 的反馈是少数做了的。开源项目几乎都是开环的。最轻的做法：日报每条带一个"有用/没用"，回流到判据或人设。
4. **防茧房配额**：减法做得越好，越容易只剩"你已知关心的"。可以留 1 个名额给"你没订阅但高质量"的内容（参考 Tournesol 的集体判断、Kagi News 的编辑精选）。
5. **说明为什么值得 + 为什么没推**：dsh-bili-scroll 的"为什么值得"和"因无字幕跳过 N 条"已经是好苗头；把"今天筛掉了多少、主要因为什么"讲清楚，才能让人放心不看其余的。
6. **投递在已有入口里**：教训见第 6 节。IM、邮件、平台私信，比再做一个 App 更容易留存。
7. **节奏而非实时**：学 Kagi News——一天一次、读完就没了，比"有新内容就推"更符合减法的本意。

---

## 来源

- Unhook NG：https://addons.mozilla.org/firefox/addon/unhook-ng/
- BewlyBewly 介绍：https://m.thepaper.cn/newsDetail_forward_26876596
- Tournesol 论文：https://deepai.org/publication/tournesol-permissionless-collaborative-algorithmic-governance-with-security-guarantees ；VentureBeat：https://venturebeat.com/ai/researchers-turn-to-crowdsourcing-for-better-youtube-recommendations
- Graze：https://techcrunch.com/2025/01/31/custom-feed-builder-graze-is-building-a-business-on-bluesky-and-investors-are-paying-attention/ ；Flipboard 收购：https://thenextweb.com/news/flipboard-acquires-graze-custom-feeds-dsa-europe ；Attie：https://werd.io/bluesky-leans-into-ai-with-attie-an-app-for-building-custom-feeds/
- Science 重排实验：https://arxiv.org/abs/2411.14652 ；Alexandria：https://arxiv.org/pdf/2505.10839
- TrendRadar：https://github.com/sansan0/TrendRadar ；https://ossinsight.io/analyze/sansan0/TrendRadar
- auto-news：https://github.com/finaldie/auto-news
- ai-news-brief：https://github.com/frankzch/ai-news-brief
- DailyBrief：https://github.com/Yida1118/DailyBrief
- News Buddy：https://github.com/Harshagarwal06/buddy_agent ；news-agent：https://github.com/maxikrueger/news-agent ；margulis94：https://github.com/martinolszanecki-dot/margulis94
- Folo：https://github.com/RSSNext/folo ；RSSHub：https://github.com/DIYgod/RSSHub
- Feedly Leo：https://futuretools.io/tools/feedly-leo
- BestBlogs.dev：https://www.bestblogs.dev/en/how-it-works
- Kagi News：https://www.macsparky.com/blog/2025/10/kagi-news-a-news-site-that-respects-your-time/ ；https://mintlify.wiki/kagisearch/kite-public/introduction
- smol.ai AINews：https://buttondown.com/ainews
- Particle：https://techcrunch.com/2026/02/23/particles-ai-news-app-listens-to-podcasts-for-interesting-clips-so-you-you-dont-have-to/
- ChatGPT Pulse：https://www.tomsguide.com/ai/chatgpt-pulse-is-here-now-ai-starts-the-chat-and-curates-your-feed
- Google CC：https://9to5google.com/2025/12/16/google-labs-cc/
- Yutori Scouts：https://yutori.com/blog/introducing-scouts
- follow-builders：https://clawhub.ai/zarazhangrui/follow-builders ；https://gittrend.io/repo/zarazhangrui/follow-builders
- Huxe：https://techcrunch.com/2025/09/23/former-notebooklm-devs-new-app-huxe-taps-audio-to-help-you-with-news-and-research ；停服/苹果协议（待核实）：https://toolnavs.com/article/2462-apple-strikes-hiring-and-licensing-deal-with-huxe-the-notebooklm-founders-audio
