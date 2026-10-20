# 注意力预算（attention-budget）

每天早上，AI 替你把 **B 站和小红书** 先看一遍，只挑 **5 条** 推到 Telegram，并且说清楚：为什么推这几条、其余的为什么不用看。你点「有用 / 没用」，它会越来越懂你。

- 判断交给 AI（DeepSeek）：搜什么、读多深、推哪几条、写给你的理由、每周复盘。
- 边界由代码保证：最多 5 条、每一条被筛掉都有记录、每天花费有上限、只读不写（不发帖、不评论、不点赞）、你的反馈原样保存。

设计文档：`docs/superpowers/specs/2026-10-20-attention-budget-prototype-design.md`

---

## 安装（Windows，一次性）

1. **装 uv**（Python 环境管理工具）：在 PowerShell 里运行
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. **下载本项目**并进入目录，运行 `uv sync`（会自动装好 Python 3.12 和依赖）。
   之后所有命令都写成 `uv run attention-budget <命令>`。
3. **准备 xiaohongshu-mcp**（读小红书用的工具）：运行 `uv run attention-budget build-xhs-mcp`。
   它会把一个修过的 xiaohongshu-mcp 编译到数据目录的 `tools\xiaohongshu-mcp\` 下（需要时自动下载便携版 Go，
   全部放在数据目录里，不用管理员权限，大约几分钟）。
   - 为什么不用官方发布的 exe：它依赖的 leakless 固定用 2978 端口做锁，拿不到就一直等。有些 Windows 会把
     2930–3029 这一段端口保留给系统（`netsh interface ipv4 show excludedportrange protocol=tcp` 可以看到），
     这时官方版的浏览器永远起不来。修过的版本改用一个空闲端口。
   - 用了这个命令就不用填 `xhs.exe_path`；填了的话以你填的为准。
   第一次运行时它还会自己下载约 150MB 的浏览器，属正常现象。
4. **生成配置**：`uv run attention-budget init`
   会创建 `%USERPROFILE%\.attention-budget\config.toml` 和 `profile\interests.md`。

## 填配置（`config.toml`）

| 项 | 怎么填 |
|---|---|
| `deepseek.api_key` | 在 https://platform.deepseek.com 申请 |
| `deepseek.price_in_per_m` / `price_out_per_m` | 按 DeepSeek 官网当前价格填（元 / 百万 token），只用来算每天花了多少钱 |
| `telegram.bot_token` | 在 Telegram 里找 @BotFather，发 `/newbot`，按提示建一个机器人，得到 token |
| `telegram.chat_id` | 先给你的机器人随便发一句话，然后在浏览器打开 `https://api.telegram.org/bot<你的token>/getUpdates`，找到 `"chat":{"id": 数字` 里的那个数字 |
| `xhs.exe_path` | 留空即可（用第 3 步编译的版本）；想用别的 xiaohongshu-mcp 时再填完整路径 |
| `xhs.key_creators` | （可选）最多 10 位你特别在意的小红书博主：打开对方主页 → 分享 → 复制链接，粘贴进来 |

> 网络：Telegram 在国内需要代理。在运行前设置环境变量 `HTTPS_PROXY`（例如 `http://127.0.0.1:7890`）即可，程序会自动走代理。
> 程序和本机 xiaohongshu-mcp（127.0.0.1）之间的通信一律不走代理，不需要再设 `NO_PROXY`。

## 写兴趣说明（`profile\interests.md`）

花 10 分钟写三段：**我最近在做什么**、**我关心的方向（越具体越好）**、**一律不要的内容**。模板里有示例。这是 AI 判断"值不值得占用你一次注意力"的起点；之后你的反馈和每周复盘会不断补充它对你的理解（存在 `profile\understanding.md`，只有你点了"采纳"才会改，旧版本留在 `profile\history\`）。

## 登录

- **B 站**：`uv run attention-budget login-bili`，用 B 站 App 扫弹出的二维码。程序会检查拿到的凭据是否完整、是否有效，不完整时会说明原因并且不保存。
- **小红书**：`uv run attention-budget login-xhs`，用小红书 App 扫码。
  - ⚠️ **同一个小红书账号不能同时在两个网页端登录**：登录之后，不要再在电脑浏览器里登录小红书网页版，否则会互相挤掉（手机 App 不受影响）。
  - 新号会被要求实名认证，建议先实名。
  - 这是第三方工具读取页面，作者称其原项目运行一年多未被封号，但风险不为零。本程序只调用只读接口，并控制读取频率。

## 验证（只能在你自己的电脑上做）

1. `uv run attention-budget check-sources` —— 各来源各拉一次，打印条数；B 站三类、小红书三到四类都应 > 0 条。不调 AI、不推送。
2. `uv run attention-budget run --dry-run` —— 真跑一轮，结果打印在终端，不发 Telegram；报告写在 `reports-dryrun\`。试跑用的是单独的账本，**不会占掉当天的正式推送**。

## 让它每天自动跑

`uv run attention-budget install-tasks` 会在「任务计划程序」里装三个任务：

| 任务 | 时间 | 做什么 |
|---|---|---|
| `attention-budget-daily` | 每天 08:00（电脑没开就开机后补跑一次） | 跑一轮并推送 |
| `attention-budget-review` | 每周日 20:00 | 推送本周复盘，逐条「采纳 / 不采纳」 |
| `attention-budget-bot` | 登录 Windows 时 | 常驻接收你的按钮和留言 |

如果提示"拒绝访问"，用管理员身份打开 PowerShell 再运行一次。运行日志在 `%USERPROFILE%\.attention-budget\logs\`。

不想要了：`uv run attention-budget uninstall-tasks`。

## 平时怎么用

- 每天一条推送：5 条（不够就少推，绝不凑数），每条带理由和「👍 有用 / 👎 没用」。点"没用"会再问一个原因。
- 第二条是**今日账单**："值得看但没名额"的几条（同样能点反馈）、今天看了多少 / 深读多少 / 主要筛掉了什么、花了多少钱。
- **直接给机器人发文字**就是留言，例如"这两周多推 Agent 记忆""别再推某某"。带时间的按时间失效，没写时间的 14 天后失效。
- 登录过期时，推送最前面会说明，并把新的登录二维码直接发到 Telegram，3 分钟内用手机扫即可。

## 两周后看什么

`%USERPROFILE%\.attention-budget\ledger.db` 里记着一切。每周复盘消息会给出：有用率、"没名额"里的有用率（漏判）、意外发现的有用率；并问你这周大概刷了多久 B 站和小红书。

## 开发

```
uv sync
uv run pytest
```
