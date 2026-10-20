# 调研：c1overzz/dsh-bili-scroll（「刷B站」DSH 插件）

> 调研时间：2026-10-20 ｜ 对象：`https://github.com/c1overzz/dsh-bili-scroll` @ `9a3e0e6`（package.json 版本 0.1.14）
> 方法：克隆仓库，通读 README / AGENTS.md / CHANGELOG / 章程，精读 `index.js`、`lib/*`、`vendor/bilibili-mcp/mcp_server.py` 中的关键路径；
> 对所有 JS 做了 `node --check`、对 Python 做了 `py_compile`（全部通过）。**没有**在真实 DSH 宿主里运行。

---

## 1. 一句话概括

一个 **DeepSeek Harness（DSH）插件**：在 DeepSeek 的谷时（便宜时段）定时拉起一个 headless 的 DSH 会话，
让模型按关键词搜 B 站视频、**读字幕原文**判断值不值得看，写 Markdown 日报；可选地用用户的 B 站账号
发一条评论 @ 用户本人，或发一条私信。B 站能力来自内置的开源 Python MCP 服务 `bilibili-mcp`（MIT，AGI_Ananas）。

## 2. 项目概况

| 项 | 情况 |
|---|---|
| 语言 / 规模 | Node ESM（`index.js` 1632 行、`client.js` 597 行、`lib/` 约 860 行）+ Python（`mcp_server.py` 1415 行、`asr.py` 138 行） |
| 依赖 | Node ≥22、DSH ≥0.1.7-rc.1；运行期另建 Python venv：`bilibili-api-python<18`、`aiohttp`、`mcp<2`、`qrcode[pil]`；可选 Whisper 本地转写（约 1 GB） |
| 历史 | 54 个提交，**全部集中在 2026-10-15 ～ 10-19 五天内**；0.1.0 → 0.1.14 共 14 个版本 |
| 作者 | 提交署名有 xiaojing / me1dlinger / Warwick / c1overzz / cloverzz；外部 PR：#4（ProgramerNoob，审查修复批）、#9（私信投递，被"摘"进主线） |
| 发布 | npm `latest` 停在 **0.1.7**（10-17）；CHANGELOG 说 npm 账号被暂停，0.1.8+ 只能从 GitHub 装（0.1.12 起「检查更新」会同时查 GitHub） |
| 许可 | MIT（vendor 目录另带上游 MIT LICENSE） |
| 测试 | 无正式测试框架；`selfcheck.mjs`（假 ctx 上跑两个半端）+ `tools/_check-1016.mjs`（断言脚本）；无 CI |

## 3. 架构

```
DSH 宿主进程
 ├─ index.js（host 半端，apply(ctx, config)）
 │   ├─ 注册 11 个 DSH 工具：bili_whoami/search/subtitle/comments/reply/status/run/login_start/login_poll/setup/config
 │   │     └─ 通过 lib/mcp.mjs（手写的极简 MCP stdio 客户端）调用家目录里的 mcp_server.py
 │   ├─ HTTP 接口 /api/bili-duty/*（给设置页用：status/config/run/install/verify/report/open/upgrade/update/login…）
 │   ├─ 谷时定时器（setInterval 60s → tick → runDuty）
 │   └─ runDuty：写 headless-overlay.yml → spawn `dsh --profile headless --patch overlay "<长提示词>"`
 │                 cwd = $DSH_HOME/bili-duty（插件家）
 ├─ client.js（浏览器半端：设置 → 刷B站 页面）
 └─ headless 子会话（一轮"值班"）
      ├─ 提示词 = promptFor()：内联用户 AGENTS.md（≤6000 字）+ 当天配额/开关 + 指向章程 duty.md
      ├─ 工具 = overlay 挂上的 mcp_server.py，按 BILI_MCP_TOOLS 白名单只注册：
      │         search / subtitle / comments / video_info（+ 开评论时 reply）
      └─ 产物：out/<日期>.md（报告）、seen.md（判重表）、digest/<月>.md（归档）
运行结束 → 宿主数报告里的 `### BV…` 行算配额 → 没报告就补占位报告 → 90 秒后去 B 站复核评论是否公开可见（lib/verify.mjs）
```

几个值得注意的设计：

- **数据与包分离**：配置、凭证、报告、日志全部在 `$DSH_HOME/bili-duty/`；`mcp_server.py` 运行前被拷到家目录，好让凭证落在家里。
- **章程升级"记账制"**（`lib/state.mjs: ensureCharter`）：用 `charter.sha256` 记上次铺的版本，用户没改过就自动升级，改过就把新版另存 `duty.md.new`。
- **工具白名单是真闸门**：`mcp_server.py` 里把 `@mcp.tool()` 换成 `tool()`，不在 `BILI_MCP_TOOLS` 里的工具根本不注册（上传视频、发动态、私信等模型看不到）。
- **配置双写对账**：设置同时写插件的 `config.json` 和 DSH 原生插件设置；0.1.9 → 0.1.14 连续 6 个版本都在修"开关自己弹回去"，最终判据是"本地值 ≠ 默认值 就以本地为准"。
- **依赖自举**：没有 uv/Python 时自己下载 uv（GitHub → gh-proxy.com / ghproxy.net → 清华 PyPI 镜像），`UV_CACHE_DIR` 等全部钉在插件家里。
- **提示词即策略**：判据、红线、注入防护、私信写法、配额口径大量写在 `prompts/duty.md` 和 `promptFor()` 里，宿主侧只对少数关键点做了硬约束（白名单、`bili_reply` 闸、配额计数、占位报告、复核）。

## 4. 发现的问题（按严重度）

### 🔴 4.1 私信投递在值班轮里用不了（功能性 bug）

- `promptFor()`（`index.js` ~884 行）和章程都要求模型调用 `bili_send_message` 发私信；
- 但 `mcpWhitelist()`（`index.js` ~922 行）只放行 `bili_search / bili_subtitle / bili_comments / bili_video_info`，开评论时再加 `bili_reply`，**从来不加 `bili_send_message`**；
- 而 `mcp_server.py` 只注册白名单内的工具 ⇒ headless 那一轮里模型**看不到** `bili_send_message`。

结果：`dmMode=only` 时一条都发不出去（公开评论也被禁了）；`dmMode=both` 时只有评论。0.1.7 起的"私信投递"在值班轮里大概率从未生效。
修法：`if (cfg.dmMode !== 'off' && Number(cfg.dmUid) > 0) list.push('bili_send_message')`。

### 🟠 4.2 评论闸门在两条路径上口径不一致

- 宿主工具 `bili_reply` 的闸：`autoComment && mentionName && dmMode !== 'only'`（否则拒绝，除非 `force`）。
- 值班轮的白名单：只看 `autoComment || replyToComments`。

⇒ `autoComment` 开着但没填「要 @ 谁」、或 `dmMode=only` 时，值班轮里 `bili_reply` 依然可用，只靠提示词约束。
"用用户的号说话"是这个插件最敏感的动作，建议让白名单复用同一个 `commentOn` 判据（回复评论单独判）。

另：宿主工具 `bili_reply` 没把 `root` 传给 MCP（只传了 `rpid`），而章程要求楼中楼记录 root；在主会话里用宿主工具回楼中楼时可能落错位置。

### 🟠 4.3 uv 下载的 sha256 校验对代理源基本无效

`lib/uvfetch.mjs: expectedSha()` 对 GitHub 资产的期望值是 `source.url + '.sha256'` —— **对代理源就是从同一个代理取**。
代理如果篡改了二进制，也能一起篡改校验文件，注释里说的"防第三方代理动过包"并不成立。
此外取不到校验值时**照样解压执行**（fail-open），下载的又是 `releases/latest`（不锁版本）。
建议：期望值固定从 `github.com` 直连或 PyPI JSON 取，或在代码里内置锁定版本的哈希；取不到就拒绝。

### 🟡 4.4 谷时调度与文档描述不符

- `tick()` 用的是本机 `getHours()`，不是北京时间；海外/非 UTC+8 机器上窗口整体错位。
- **不处理周末**：文档称"整个周末都是谷时"，代码里只有 12–14 与 18–09 两个窗口（路线图里写着"周末时段 在做"）。
- 夜间窗口跨零点，但去重键是 `${当天日期}:night`：第 D 天 18:00 跑一轮后，D+1 的 00:00 会因为日期变了**再跑一轮**，
  并占掉 D+1 的 night 键 ⇒ D+1 晚上 18:00 不再跑。稳态下"晚上那轮"实际发生在每天凌晨 00:00 左右。

### 🟡 4.5 最后兜底路径用 `shell: true` 传整段提示词

找不到 `bin.js`、只在 PATH 里有 `dsh` 时，`resolveDsh()` 返回 `{ command: 'dsh', shell: true }`，
而提示词里内联了用户 `AGENTS.md`（Markdown 里常见 `>`、`|`、`&`、引号）和用户填写的关键词/昵称。
走 shell 时这些字符会被 cmd/sh 解释（重定向、管道），轻则这一轮跑坏，重则执行意外命令；
Windows 下 cmd 还有 8191 字符的命令行上限，6000 字的 AGENTS.md + 3000 字指令很容易超。
建议兜底也用 `shell: false`（Windows 上解析 `dsh.cmd` 背后的 `bin.js` 再用 `node` 起），或把提示词写进文件再传路径。

### 🟡 4.6 其他工程面观察

- **配额靠 LLM 的输出格式**：宿主数报告里的 `### BV…` 行算"今天挑了几条"，模型漏写/多写都会算错（已在提示词里反复强调，说明踩过）。
- **大量补丁式修复**：0.1.9～0.1.14 都在修配置回弹；0.1.13 是一个未声明变量（`hostDiff`）导致整个设置页卡死 —— 缺少最基础的端到端测试/CI。
- **`index.js` 单文件 1600+ 行**，工具注册、HTTP、调度、安装、升级、配置对账全在一个闭包里，注释密度极高（大量 ⭐/⚠️ 叙事），可读性和可测性都受影响。
- **发布渠道受阻**：npm 停在 0.1.7（缺 0.1.8～0.1.14 的修复），README 里推荐的 `add dsh-bili-scroll` 会装到旧版。
- **平台账号风险**：自动评论/私信用的是用户本人 B 站账号；项目自己记录了"新号评论被压审""私信 500 字上限 code 21007"等风控现象。默认关闭是对的，但仍属于自动化发言，存在被风控的风险。

## 5. 做得好的地方

- 安全默认值合理：自动评论/回复/私信全部默认关；AGENTS.md 明确要求 AI 助手**不要**替用户打开。
- 白名单在 MCP 服务端"不注册"而非"提示别用"，是正确的最小权限做法（只是漏了私信，见 4.1）。
- 对提示注入有明确红线（评论/字幕/简介都是素材不是指令）。
- 宿主侧兜底到位：定时器/日志写入全部 try/catch（吸取了两次把 DSH 宿主进程带崩的教训）、没报告补占位报告、评论发出后 90 秒复核可见性。
- 用户可改的章程有带账本的升级策略，不会吞掉用户改动。
- 文档面向"AI 助手代装"写了专门的 AGENTS.md 和群答疑手册，对排障很友好。

## 6. 如果要基于它继续做，建议的优先级

1. 修 4.1（私信白名单）——一行代码，影响一个已发布功能。
2. 统一 4.2 的评论闸门，并给宿主 `bili_reply` 补 `root`。
3. 加固 4.3（校验值来源 + fail-closed + 锁版本）。
4. 修 4.4 的调度：按 `Asia/Shanghai` 计算、加周末、夜间窗口用"窗口起始日"做键。
5. 去掉 4.5 的 `shell: true` 兜底或改成提示词文件。
6. 补一个最小 CI：`node --check` + `selfcheck.mjs` + 真调一次 `loadCfg`/`/status`（0.1.13 的教训）。
7. 长期：路线图里的 "v0.2 用 JS 重写接口、去掉 Python" 能消掉安装链路上大部分问题（uv 下载、venv、杀软、桌面版环境差异）。

## 附：关键文件索引

| 文件 | 作用 |
|---|---|
| `index.js` | host 半端：`apply()`、11 个工具、`promptFor()`、`mcpWhitelist()`、`ensureOverlay()`、`runDuty()`、`tick()`、HTTP `handle()` |
| `client.js` | 设置页 UI |
| `lib/state.mjs` | 家目录、谷时窗口 `WINDOWS`/`inWindow`、章程升级 `ensureCharter` |
| `lib/mcp.mjs` | 极简 MCP stdio 客户端 |
| `lib/uvfetch.mjs` | 自动下载 uv（多源 + sha256） |
| `lib/verify.mjs` | 评论发出后复核可见性 |
| `lib/hostdeps.mjs` | 从宿主树取 `@deepseek-ai/*`，取不到退本地实现（桌面版兼容） |
| `lib/login.mjs` / `lib/asr.py` | 扫码登录 / Whisper 本地转写 |
| `prompts/duty.md` | 值班章程（判据、红线、产物格式） |
| `vendor/bilibili-mcp/mcp_server.py` | 内置 B 站 MCP（27 个工具，按 `BILI_MCP_TOOLS` 白名单注册） |
