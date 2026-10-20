# 架构对比：TrendRadar vs auto-news vs dsh-bili-scroll —— 做"跨平台注意力预算"该从哪个改起

> 调研时间：2026-10-20 ｜ 三个仓库都克隆下来读过代码（未部署运行）：
> - `sansan0/TrendRadar` @ `657094e`（2026-10-15，v6.11.0）
> - `finaldie/auto-news` @ `4f1a2e0`（**2025-07-19**）
> - `c1overzz/dsh-bili-scroll` @ `9a3e0e6`（2026-10-19，v0.1.14）

---

## 0. 目标先说清楚

"跨平台注意力预算"需要六个环节：

```
① 采集（多平台） → ② 粗筛（便宜：标题/关键词/向量） → ③ 精读（贵：全文/字幕/转写）
      → ④ 全局预算分配（今天所有源加起来只给 N 条） → ⑤ 投递 → ⑥ 反馈回流到判据
```

三个项目各自只做好了其中一段：

| 环节 | TrendRadar | auto-news | dsh-bili-scroll |
|---|---|---|---|
| ① 采集 | ✅✅ 11 个国内热榜（经 newsnow API）+ RSS | ✅ RSS / Reddit / Twitter / YouTube / 网页 / 个人笔记 | ⚠️ 只有 B 站搜索 |
| ② 粗筛 | ✅✅ 兴趣描述 → LLM 抽标签 → 批量给**标题**打分（每批 200 条）→ `min_score` 阈值 | ✅ LLM 打分 + embedding 相似度 | ⚠️ 交给模型自己判断 |
| ③ 精读 | ❌ 主流程只看标题（prompt 原文："只根据标题判断"）；MCP 侧有 Jina Reader 能读全文，但不在推送流程里 | ✅ YouTube 字幕（无字幕可转写）、网页正文摘要 | ✅✅ **读字幕原文**，无字幕可本地 Whisper |
| ④ 全局预算 | ❌ `max_news_per_keyword` 默认 0（不限），而且只影响展示 | ⚠️ Top-K（按源） | ✅✅ **"每天最多 N 条"硬额度**，跨轮累计 |
| ⑤ 投递 | ✅✅ 微信 / 飞书 / 钉钉 / Telegram / 邮件 / ntfy / Bark / Slack + HTML 报告 + 分条拆分 | ⚠️ 只有 Notion | ⚠️ B 站评论 @ / 私信（私信在值班轮里有 bug） |
| ⑥ 反馈 | ❌ | ✅✅ **用户在 Notion 里给 1–5 星**，冷启动用 GPT 打分，热了以后按评分 + 向量相似度排序 | ❌ |

---

## 1. 逐个看架构

### 1.1 TrendRadar —— 确定性的批处理流水线

```
cron / GitHub Actions / Docker
  → crawler/fetcher.py   拉 newsnow API（默认 newsnow.busiyi.world，可自建）+ crawler/rss
  → storage/             SQLite（本地）或远程对象存储，按天落库
  → core/frequency.py    关键词模式；或 ai/filter_pipeline.py（AI 模式）
        ai_interests.txt → 抽标签（兴趣变了按 hash 触发增量更新）→ 标题批量分类打分 → 存结果
  → ai/analyzer.py       可选：AI 写分析简报
  → report/              HTML 报告
  → notification/        dispatcher → splitter（按渠道字数限制拆分）→ senders（8+ 渠道）
另有 mcp_server/：把库里的数据开放给 AI 客户端查询（含 Jina 读全文）
```

- **规模 / 活跃度**：Python，约 4.2 万行（含 HTML/JS 报告模板），v6.x，最近一次提交 5 天前，有 tests 目录。
- **许可：GPL-3.0** —— fork 改出来的东西也必须 GPL 开源。
- **模型接入**：`litellm`，什么模型都能接。
- **强项**：采集面广、推送渠道全、调度/部署成熟（GitHub Actions 可零服务器）、国内用户基础大。
- **根本局限**：它管的是"**热点**"，数据是**热榜标题**——热榜本身就是"大家在看什么"，不是"什么值得我看"；而且只凭标题判断，没有精读，也没有全局预算。

### 1.2 auto-news —— 重型数据平台 + 反馈闭环

```
Airflow DAGs（news_pulling / collect_weekly / journal_daily / todo / sync_dist）
  → af_pull.py   各源 ops_*.py 拉数据（rss / reddit / twitter / youtube / article / journal）
  → af_save.py   dedup → score（LLM） → filter(min_score=4) → rank（embedding 相似度，Milvus）→ 写 Notion
  → Notion       阅读器 + 用户打 1–5 星（user_rating）→ 回流做下一轮的排序依据
  → collect_weekly：周 Top-K 回顾；todo：从要点生成待办
依赖：Postgres + Redis + MySQL + Milvus（+ etcd + MinIO）+ Airflow；最低 2 核 6 GB，推荐 8 核 16 GB
```

- **规模 / 活跃度**：Python，约 1.3 万行；**最后一次提交 2025-07-19，已停 15 个月**；作者精力转到托管版 App（Dots Agent）。
- **许可**：MIT。
- **强项**：唯一把**反馈闭环**做成架构的（评分 → 向量相似度 → 排序）；"周回顾""从要点生成 TODO"也是减法之后的下一步。
- **根本局限**：太重（7 个服务起步），绑死 Notion，没有国内平台，而且基本不维护了。

### 1.3 dsh-bili-scroll —— 代理式：整个流程交给一个 LLM 会话

```
DSH 插件宿主（Node）
  → 谷时定时器 → spawn `dsh --profile headless --patch overlay "<长提示词>"`
  → headless 会话里的模型按章程 duty.md 自己完成：搜 → 读字幕 → 判 → 写报告 / 判重表 / 归档 → 评论或私信
       工具：内置 Python bilibili-mcp，按白名单只注册 4～5 个
  → 宿主事后数报告里的 `### BV…` 行记配额、补占位报告、90 秒后复核评论可见性
```

- **规模 / 活跃度**：Node + Python，约 5 千行；5 天 54 个提交，迭代很快但 bug 也多（见 `dsh-bili-scroll.md`）。
- **许可**：MIT。
- **强项**：**判据对**——"值不值得占用他一次注意力"、读原文、硬额度、按用户自己的 `AGENTS.md` 人设判断。
- **根本局限**：
  - 流程在提示词里，**额度靠解析模型写的 Markdown 来数**，不确定、难测试；
  - 每多一个平台，模型就要多读一堆，**成本随来源线性涨**；
  - 绑定 DSH 宿主，换个环境跑不了。

---

## 2. 三条路线

### 路线 A：从 dsh-bili-scroll 扩成多平台
往 overlay 里再挂小红书、公众号等 MCP，章程里写"所有平台共享 N 条额度"。
- ✅ 判据和人设现成；改动最小，最快能看到效果。
- ❌ 采集和粗筛全靠 LLM，来源越多越贵越慢；预算靠提示词约束；DSH 锁定；要先还一批技术债。
- **适合**：先验证"跨平台预算"这个想法本身对你有没有用（一两周的原型）。

### 路线 B：从 TrendRadar 改起 ⭐ 推荐
保留它的 ①采集 ②粗筛 ⑤投递，在 AI 筛选和推送之间**插一个"精读 + 全局预算"阶段**，再补 ⑥反馈。
- ✅ 采集和推送这两块最费工夫的已经做好，而且有人持续维护；流水线是确定性代码，**预算可以在代码里硬执行**而不是靠提示词；粗筛便宜（只有标题），精读只花在少数候选上，成本可控。
- ❌ GPL-3.0；代码量大，上游迭代快，长期维护 fork 有合并成本；热榜源偏"热点"，需要补"订阅型"源（B 站关注的 UP 主、公众号、播客——可以走它已经支持的 RSS 通道接 RSSHub / wewe-rss）。
- **适合**：要做一个自己长期用、能扩到多平台的东西。

### 路线 C：从 auto-news 改起
- ✅ 反馈闭环、YouTube 转写现成；MIT。
- ❌ 已停更 15 个月、7 个服务的重栈、绑 Notion、没有国内平台——改它约等于重写。
- **结论**：**不建议当底座**，但它的反馈设计值得原样借过来。

---

## 3. 推荐做法：TrendRadar 做底座，另外两个各借一样

```
TrendRadar 现有：采集（热榜 + RSS/RSSHub） → AI 标题粗筛（min_score）
        │
        ▼  新增「注意力预算」阶段（大致插在 _run_analysis_pipeline 与 _send_notification_if_needed 之间）
   ③ 精读：对粗筛后的 Top-K（比如 30 条）拉原文
        · 网页 → Jina Reader（它的 MCP 里已有 article_reader）
        · B 站 → 字幕（借 dsh-bili-scroll 的 bilibili-mcp / asr.py）
   ③' 判断：LLM 按用户人设 + "值不值得占用一次注意力"判据，给每条写"为什么值得"   ← 借 dsh-bili-scroll 的章程
   ④ 预算：所有来源一起排序，代码里硬截断到每天 N 条；留 1 个"意外发现"名额；
          记录"今天筛掉了多少、主要原因"
        │
        ▼
   ⑤ 推送：复用 TrendRadar 的 dispatcher（微信 / 飞书 / Telegram …）
   ⑥ 反馈：每条附"有用 / 没用"，评分存 SQLite，下一轮作为排序特征   ← 借 auto-news 的 user_rating 思路
```

### 分步落地
1. **只加预算截断**：AI 筛选结果按分数跨源排序，代码里截到 N 条再推。半天的事，马上能感受到"减法"。
2. **加精读**：只对 Top-K 拉全文/字幕，用人设判断重排、写理由。成本只花在少数几条上。
3. **补订阅型源**：B 站 UP 主、公众号、播客，通过 RSS 通道接 RSSHub / wewe-rss，B 站字幕复用 bilibili-mcp。
4. **加反馈**：推送里带评分入口（Telegram 按钮最简单），存库，下一轮排序加权。
5. **防茧房和说明**：保留"意外发现"名额，报告末尾写"筛掉 X 条，主要因为……"。

### 两个前置决定
- **许可**：基于 TrendRadar 改就得 GPL-3.0 开源。如果想闭源或用 MIT，换个做法：**不 fork**，让新的"预算层"作为独立程序读 TrendRadar 的 SQLite 输出或调它的 MCP 接口——代价是多一个进程，好处是不背 fork，也不受 GPL 约束（前提是只通过数据或接口交互）。
- **数据源**：默认的 newsnow 接口是第三方托管的公共实例，长期用建议自建（TrendRadar 配置里支持改 `api_url`），并保留它的 `expected_domain` 校验。

---

## 4. 一句话结论

**底座选 TrendRadar**（采集、推送、调度都现成，而且活跃），**判据借 dsh-bili-scroll**（读原文、硬额度、人设），**反馈借 auto-news**（评分回流排序）。
如果只想先花一两周验证"跨平台预算"对自己有没有用，可以先在 dsh-bili-scroll 上加一个平台试试，确认值得做再迁到 TrendRadar 上。
