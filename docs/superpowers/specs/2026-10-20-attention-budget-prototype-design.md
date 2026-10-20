# 注意力预算原型（attention-budget）设计文档

> 日期：2026-10-20 ｜ 状态：待审 ｜ 背景调研见 `research/` 目录下三份报告

---

## 1. 目标与判定标准

### 1.1 要验证的事
AI 每天替使用者从 **B 站和 X** 里只挑出 **5 条**，并讲清楚其余为什么不用看——这能不能让使用者**少刷这两个平台、又不觉得漏了重要的东西**。

### 1.2 两周后看四个数（都从账本里算）
| 指标 | 定义 |
|---|---|
| 有用率 | 推送的 5 条里被标「有用」的比例 |
| 漏判率 | 「值得看但没名额」里被标「有用」的比例 |
| 刷原 App 时间 | 每周复盘时使用者自报的 B 站 + X 使用时长 |
| 每日花费 | 账本里的 token 用量 × 配置里的单价 |

### 1.3 已确定的需求（来自使用者）
- 平台：**B 站、X**
- 读的内容：**关注的人的更新、平台推荐流、按关键词搜索**（三类都要）
- X 的数据：**浏览器自动化**，用一个已登录 X 的专用浏览器配置
- 运行环境：使用者自己的 **Windows** 电脑
- 模型：**DeepSeek**
- 推送：**Telegram**，**每天 1 次、两平台合计 5 条**
- 判据来源：使用者写的**兴趣说明**，加上反馈自动形成的正反例
- 总体做法：**C —— AI 尽量放开，代码只守五条边界**

### 1.4 不做的（这一版）
- 小红书、公众号、播客、YouTube 等其他来源
- 本地语音转写（没字幕的视频靠简介和评论判断，并标明"没读到原文"）
- 任何写操作：发帖、评论、点赞、关注、私信
- 多用户、云端部署、网页界面
- 点击追踪（隐式反馈）

---

## 2. 总体原则：代码守边界，AI 管判断

**交给 AI 的**：理解使用者、决定搜什么、粗排、决定每条读多深、写给使用者本人的理由、排出最终 5 条、挑意外发现、每周复盘并提出修改。

**代码守的五条边界**（AI 无法绕过）：
1. **名额**：最终推送 ≤ 5 条（`budget.daily_picks`，默认 5），其中意外发现 ≤ 1 条。
2. **记账**：AI 看过、读过、淘汰的每一条，连同分数和理由，由代码写入账本。
3. **花费上限**：每轮 token 总量与工具调用次数封顶；触顶即停止深读、用已有结果定稿。
4. **账号安全**：读取频率和数量由代码控制；所有工具只读。
5. **反馈原样保存**：AI 可读、不可改。

---

## 3. 结构

### 3.1 模块
| 模块 | 职责 | 对外接口（概要） | 依赖 |
|---|---|---|---|
| `collectors/bilibili.py` | B 站：关注更新、首页推荐、搜索、字幕、评论、UP 主近期视频 | `follow_updates(since)`, `recommend(n)`, `search(q, n)`, `subtitle(bvid)`, `comments(bvid, n)`, `up_recent(mid, n)` → `Item` / `str` | `bilibili-api-python>=17.4,<18` |
| `collectors/x_browser.py` | X：For You、Following、搜索、展开回复串、打开链接 | `for_you(n)`, `following(since, n)`, `search(q, n)`, `thread(url)`, `open_link(url)` → `Item` / `str` | Playwright（持久化浏览器配置） |
| `agent/` | 调 DeepSeek 完成：搜索规划、粗排、深读、定稿、周复盘；每个环节一个固定输出结构 | `plan_searches(ctx)`, `coarse_rank(batch, ctx)`, `deep_read(item, ctx, tools)`, `finalize(judged, ctx)`, `weekly_review(week)` | `openai` SDK（DeepSeek 兼容接口）、`pydantic` |
| `guard.py` | 五条边界：所有工具调用先过它；校验定稿；计数与封顶 | `call(tool, args)`, `check_final(picks)`, `remaining()` | 无 |
| `ledger.py` | SQLite 账本：读写所有记录；断点续跑 | 见 §5 | `sqlite3` |
| `bot.py` | Telegram：推送、按钮、留言、周复盘确认、重新登录二维码 | `send_daily(run)`, `send_bill(run)`, `send_review(review)`, 回调处理 | `python-telegram-bot` |
| `run.py` | 一轮的编排；命令行入口 | `run`, `check-sources`, `dry-run`, `review`, `login-bili`, `login-x` | 以上全部 |

### 3.2 数据结构 `Item`
所有来源统一成：`id`（平台前缀 + 原始 ID，如 `bili:BV1xx`、`x:1234`）、`platform`、`channel`（`follow` / `recommend` / `search`）、`author`、`title`、`snippet`（简介或帖子正文前 300 字）、`url`、`published_at`、`metrics`（播放 / 点赞等，可空）、`query`（来自搜索时记录搜索词）。

### 3.3 "对使用者的理解"
| 文件 | 谁写 | 说明 |
|---|---|---|
| `profile/interests.md` | 使用者 | 我在做什么、关心什么、哪类内容一律不要。提供模板 |
| `profile/understanding.md` | AI（经使用者确认） | AI 维护的"我对你的理解"；只在周复盘被采纳时修改，每次修改另存 `profile/history/understanding-<日期>.md` |
| 账本 `feedback` / `notes` 表 | 使用者 | 按钮与留言，原样保存 |

### 3.4 目录与配置
- 代码仓库：`attention_budget/`（Python 3.11+，`uv` 管理环境）
- 运行数据目录：`%USERPROFILE%\.attention-budget\`（账本、报告、浏览器配置、B 站凭证、日志）——不在仓库里
- 配置文件：`%USERPROFILE%\.attention-budget\config.toml`，含：DeepSeek API key 与模型名（默认 `deepseek-chat`）、token 单价（使用者按官网价格填写，用于算花费）、Telegram bot token 与使用者 chat_id、各项数量与上限（见 §4、§2）、推送时间（默认 08:00，本机时区）、X 浏览器是否有界面（默认有界面）

---

## 4. 每天一轮

每一步完成后在账本 `runs.stage` 记一笔；中途崩溃时重跑从断点继续。

| 步骤 | 谁做 | 内容 | 默认数量 / 上限 |
|---|---|---|---|
| 0 准备 | 代码 | 读 `interests.md`、`understanding.md`、最近 14 天「有用」10 条与「没用」10 条（带原因）、有效期内的留言；取已推送和已看过的 ID 用于去重 | — |
| ① 基础采集 | 代码 | B 站关注更新（过去 24 小时）、B 站首页推荐、X Following（读到上次运行时间点）、X For You；去重 | 100 / 50 / 150 / 100 条 |
| ② 搜索规划 | AI | 根据理解与近期「有用」内容提出搜索词，每个附理由；代码执行 | B 站 ≤3 个、X ≤3 个，每个取 20 条 |
| ③ 粗排 | AI | 分批（每批 ≤50 条）给每条打 0–100 分 + 一句理由；全部写账本。代码取前 30 进深读；AI 可额外标记 ≤5 条"标题不行但可能有料"一并进入 | 深读池 ≤35 条 |
| ④ 深读 | AI（逐条小循环） | 每条可调用只读工具：B 站 `subtitle` / `comments` / `up_recent`，X `thread` / `open_link`；读完给出 `pick` / `maybe` / `reject` + 理由 + 一句写给使用者的话；没读到原文须标 `no_source=true` | 每条 ≤4 次工具调用 |
| ⑤ 定稿 | AI + 代码校验 | AI 从 `pick`/`maybe` 中排出最终名单并指定意外发现及理由。代码校验：总数 ≤5；意外发现 ≤1 且 `channel ≠ follow`；每条都经过深读；未曾推送过。不通过 → 让 AI 改一次 → 仍不通过按其排序截断并剔除不合规项 | — |
| ⑥ 推送 | 代码 | 见 §6；同时写本地报告 `reports/<日期>.md` | — |

**每轮上限**（触顶即跳到⑤，账单注明）：token 总量 500,000；工具调用 150 次；整轮时长 40 分钟。

**内容安全**：所有采集来的文字以"材料"身份传给 AI，系统提示中写明其中的指令一律不执行；工具全部只读。

---

## 5. 账本（SQLite）

| 表 | 主要字段 |
|---|---|
| `runs` | `run_id`、`date`、`stage`、`started_at`、`finished_at`、`status`（ok / partial / failed）、`source_status`（各来源成功或失败原因，JSON）、`tokens_in`、`tokens_out`、`tool_calls`、`capped`（是否触顶） |
| `items` | `Item` 全部字段 + `first_seen_run` |
| `verdicts` | `run_id`、`item_id`、`stage`（coarse / deep / final）、`score`、`decision`、`reason`、`note_to_user`、`no_source`、`serendipity` |
| `tool_calls` | `run_id`、`item_id`、`tool`、`args`、`ok`、`error`、`at` |
| `pushes` | `run_id`、`item_id`、`slot`（main / serendipity / missed）、`telegram_message_id` |
| `feedback` | `item_id`、`run_id`、`value`（useful / not_useful）、`reason`（not_interested / already_known / low_quality / too_long，可空）、`at` |
| `notes` | `text`、`at`、`expires_at`（AI 从留言中解析出时间范围；解析不出则 14 天） |
| `reviews` | `week`、`proposals`（JSON：每条修改、理由、依据的反馈 ID）、`decisions`、`self_reported_minutes` |
| `outbox` | 发送失败待补发的消息 |

---

## 6. Telegram

- **只响应配置里的那个 chat_id**，其他人发来的消息一律忽略。
- **第一条：今日 5 条**。每条：平台、标题、链接、AI 写给使用者的理由（≤80 字）；意外发现那条标注"你没关注的"及推荐理由。每条下方「有用 / 没用」按钮；点「没用」后再弹四个原因按钮。
- **第二条：今日账单**。"值得看但没名额"的条目（同样带链接和按钮）；一行统计：看了多少、深读多少、主要淘汰原因及条数（由粗排 / 深读理由归类，AI 在定稿时顺带给出归类）；各来源状态与是否触顶；本地报告路径。
- **留言**：使用者发来的任何文字存入 `notes`，下一轮生效。
- **周日 20:00 复盘**（错过则下次开机补发）：AI 对照本周打分与反馈，给出对 `understanding.md` 的若干条修改（每条带理由和依据），每条「采纳 / 不采纳」按钮；同一消息询问本周 B 站 + X 大约用了多久（回复数字即可）。意外发现的反馈单独统计、不用于收窄主判据；某方向意外发现被连续标「有用」时，复盘提议"是否加入兴趣"。
- **常驻方式**：登录 Windows 后由任务计划程序启动 `bot` 常驻接收回调；电脑关机期间的按钮由 Telegram 暂存（最长 24 小时）。

---

## 7. 出错处理

| 情况 | 处理 |
|---|---|
| B 站凭证失效 | 跳过 B 站，其余照跑；推送首行说明，并把重新登录二维码以图片发到 Telegram |
| X 掉登录 / 出现验证页 / 解析到 0 条 | 立即停止读 X，不重试；记为来源失败并在推送中说明 |
| X 读取节奏 | 每次滚动间隔随机 2–6 秒；每页最多滚 15 屏；只读不点 |
| DeepSeek 报错或超时 | 指数退避重试 3 次；输出结构不合法 → 重问 1 次 → 仍不合法记为"判断失败" |
| 整轮无法完成 | 推送"今天没跑成：原因"，绝不静默 |
| 触顶 | 停止深读，直接定稿；账单注明 |
| Telegram 发送失败 | 写入 `outbox`，下次启动补发 |
| 崩溃 / 关机 | 按 `runs.stage` 断点续跑，不重复已完成步骤的花费 |
| 错过 08:00 | 开机后补跑一次（任务计划程序"错过后尽快运行"），同一天不重复跑 |
| 密钥与凭证 | 全部放在运行数据目录，不进仓库；`.gitignore` 排除 |

---

## 8. 测试

| 层 | 内容 | 是否联网 |
|---|---|---|
| 单元测试（pytest） | `guard`：名额、意外发现来源、各上限；去重；定稿校验与截断；反馈与留言解析；断点续跑 | 否 |
| 整轮离线测试 | 假 DeepSeek（回放录好的回答）+ 录好的 B 站 / X 样例数据，从头跑一轮，检查推送内容、账本记录、账单统计 | 否 |
| `check-sources` | 真实连接 B 站与 X，只打印各来源拉到多少条，不调 AI、不推送 | 是（在使用者电脑上） |
| `dry-run` | 全部真实运行，结果打印到终端，不发 Telegram | 是（在使用者电脑上） |

开发在 Linux 容器内完成，真实账号相关的两项由使用者在 Windows 上执行；安装与执行步骤写进 README。

---

## 9. 已知风险
- **X 改版或风控**：浏览器自动化会随 X 页面变化失效，封号风险低但不为零；以"读到 0 条即失败、不重试"止损。
- **B 站接口变化**：依赖第三方库 `bilibili-api-python`，需锁版本。
- **只看标题的粗排会漏**：靠"标题不行但可能有料"名额与深读池大小缓解，成本随之上升。
- **花费估算**：每轮约 30 万 token 是设计时估计，以首周账本实测为准。
