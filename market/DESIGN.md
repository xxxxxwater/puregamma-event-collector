# Coinbase 市场简报 v2：框架设计

v1（2026-10-28 上线）解决了"每 4 小时播报 + 命令"。v2 在此基础上：多用 Coinbase 数据，股票细分到个股、行业和 ETF，增加异动检测和可验证的市场研判，并把架构改成"采集作业 → 数据层 → 分析 → 呈现/通知"四层。

## 1. 需求

| # | 需求 | v2 落地 |
| --- | --- | --- |
| R1 | 多用 Coinbase 数据 | 现货、1h K 线、逐笔成交（主动买卖）、盘口深度、永续和交割期货（资金费率、持仓、基差）、股指和商品期货（跨资产）、股票清单和类型 |
| R2 | 预测市场 | 研判模块：未来 24h 波动区间 + 方向倾向（多因子打分），每条都记录在案并事后核对，公布命中率和回测结果 |
| R3 | 股票分个股、行业、ETF | 个股和 ETF 分开排名；行业按板块（11 个）和细分行业汇总；ETF 按 9 个类别汇总 |
| R4 | 区分资金流向和异动 | 个股、行业、ETF 分别算资金流向；异动分涨跌幅、放量、盘中急拉急跌、盘前盘后四类，加密和衍生品另有一套 |
| R5 | 系统性架构优化 | 作业调度 + 数据源健康 + 时间序列存储 + 保留期 + 数据质量自检 |
| R6 | Telegram | 简报分 3 组（加密 / 美股 / 宏观与研判）；新命令；重大异动实时推送（限频、去重、夜间静音） |

## 2. 数据源（2026-10-28 实测）

| 来源 | 能拿到什么 | 限制 |
| --- | --- | --- |
| Coinbase `market/products?SPOT` | 929 个现货：价格、24h 成交额、涨跌、量变 | USD 和 USDC 是同一订单簿（`alias`） |
| Coinbase `candles` | 1 分钟到 1 天的 K 线 | 每次最多 350 根 |
| Coinbase `ticker`（逐笔） | 成交价、数量、`side` | 每次最多 1000 笔；BTC 繁忙时约每分钟 1000 笔；**`side` 是挂单方**（见 C3） |
| Coinbase `product_book` | 500 档盘口 | — |
| Coinbase `products?FUTURE` | 117 个合约：约 25 个币的永续（资金费率每小时结算、持仓、指数价），BTC/ETH 等的交割期货；美股500、科技100、AI、中国、国防指数永续，七巨头+加密期货；黄金、白银、铜、铂、原油、天然气期货 | 指数永续只在股指交易时段交易 |
| Coinbase `products?EQUITY` | 约 9500 个股票代码，带类型：ETF 4977、普通股 4092、ADR 290 等 | 没有价格和成交量；分页不稳定 |
| Nasdaq `quote/watchlist` | 个股和 ETF 实时行情：最新价、涨跌、成交量、前收、开盘价、交易时段状态 | 每次 20 只，1～3 秒 |
| Nasdaq `screener/stocks` | 全部个股的板块、细分行业、市值 | **价格和成交量滞后约 1 个交易日**（见 C1） |
| Nasdaq `screener/etf` | 5543 只 ETF 的名称（用于分类） | 没有成交量 |
| Nasdaq `historical` | 个股和 ETF 日线 | 每次查一只 |
| Nasdaq `market-info` | 交易日、开收盘时间、状态 | — |

## 3. 约束和发现

- **C1 股票列表接口数据滞后：** v1 用的 Nasdaq 股票列表接口，数据比最新交易日晚一天。10-28 拿到的是 10-26 的收盘，却被标成了 10-27。v2 改用 watchlist 实时行情；列表接口只用来取板块、行业、市值，以及给股票池粗排名。
- **C2 watchlist 盘前、盘后、收盘后的字段语义：** 盘前实测：`lastSalePrice` 是盘前价，`netChange` 相对上一交易日收盘，`volume` 是上一交易日全天成交量，`previousClosePrice` 是再前一天的收盘价。所以上一交易日收盘价 = 最新价 − 涨跌额，当日涨跌 = 收盘价 ÷ 前收 − 1。盘后时段的语义还没实测，所以加了自检：第一次进入盘后时，和最后一次盘中数据比对，偏差中位数在 0.5% 以内才采用，否则继续用盘中最后一次的数据。
- **C3 逐笔成交的 `side` 是挂单方：** 用"价格上跳和下跳"检验，ETH、SOL 的 `SELL` 大多伴随价格上跳，最后一笔成交是否落在买价或卖价也对得上。所以**主动买入 = `side=SELL`**。每次采集都会重新校验，校验不过就在 `/logs` 报警。
- **C4 一个 Bot 只能有一个程序接收消息：** 命令仍由采集器处理，只读快照（沿用 v1）。
- **C5 请求预算：** watchlist 每次只能查 20 只，所以股票池限定为成交额前 500 只个股 + 前 200 只 ETF（再加上核心 ETF），每 15 分钟约 36 次请求。ETF 的成交额排名靠每周一次的全量扫描（分批进行）。
- **C6 放量需要基线：** 先用 Nasdaq 日线回填最近 20 个交易日（分批、低速），之后每天收盘后自己记录。基线不足 10 天的股票不判断放量。
- **C7 预测的诚实性：** 方向预测通常接近抛硬币。所以每条研判都会记录，24 小时后核对；同时用历史 K 线回测，公开命中率。区间（波动率）预测更可靠，作为主要输出。

## 4. 架构

```text
┌ 采集作业 market_jobs（各自的周期、失败隔离、健康记录到 jobs 表）──────────────────────┐
│ spot 15m · candles 15m · trades 15m · books 15m · futures 15m · market_info 15m        │
│ equity_live 15m（美东 04:00-20:00 交易日）/ 120m · equity_backfill · etf_sweep 每周     │
│ cb_equity_list 6h · equity_meta 24h · universe 24h · forecast 60m · backtest 24h · prune 60m │
└───────────────┬─────────────────────────────────────────────────────────────────────────┘
                ▼ market_store（SQLite）：candles · crypto_flow · deriv_ticks · equity_meta · equity_universe
                  equity_ticks · equity_daily · forecasts · alerts · quotes · snapshots · broadcasts · jobs
                ▼ 分析（纯函数，无 I/O）
                  market_analysis 现货概览/4h 量价 · market_crypto 主动买卖/盘口/K线异动
                  market_derivs 资金费率/持仓/基差/跨资产 · market_equities 时段语义/分类/资金/行业/ETF/异动
                  market_forecast 区间/倾向/回测/核对 · market_alerts 推送策略
                ▼ market_render → 快照（各版块 HTML）+ quotes 表（单标的查询）
     ┌──────────┴───────────┐
 定时简报（3 组）+ 异动推送    collector 命令（只读）：/market /crypto /deriv /stocks /sectors /etf /alerts /macro /predict /flow /q
```

## 5. 指标口径

### 资金流向估算（统一口径）

- **个股、ETF、币种的 24h：** 资金估算 = 成交额 × clamp(涨跌幅 ÷ 2%, −1, 1)，即涨跌 2% 以上记全额，以内按比例。v1 只按涨跌方向记全额，涨 0.1% 也记全额流入，v2 修正了这一点。
- **加密近 4h：** 1 小时 K 线阳线成交和阴线成交之差（沿用 v1）。另外用逐笔成交算真实的主动买入和主动卖出，覆盖成交额前 6 的币种，并标明覆盖率。
- **行业和 ETF 类别：** 成分个股或 ETF 的资金估算之和。领涨、领跌用成交额加权平均涨幅。

### 美股

- **股票池：** Coinbase 可交易（排除停牌和 `liquidate_only`）∩ Nasdaq 有行情。
- **个股和 ETF 的划分：** 用 Coinbase 的类型标记。普通股、ADR、优先股归为个股；ETF 和信托份额归为 ETF。
- **板块和细分行业：** 来自 Nasdaq。
- **ETF 类别：** 杠杆/反向、波动率、加密、商品、债券、海外、宽基、行业/主题、其他。约 120 只核心 ETF 手工指定类别，其余按名称规则分类。

### 异动

- **个股：**
  - 涨跌幅：|涨跌| ≥ 7% 且成交额 ≥ $5000 万
  - 放量：量比 ≥ 2.5（盘中按时间进度折算）
  - 盘中急变：约 15 分钟内 ±2.5%，或约 60 分钟内 ±4%
  - 盘前、盘后：|变动| ≥ 5%
- **ETF：** 不含杠杆/反向类。涨跌幅 ≥ 3%，量比 ≥ 3。
- **加密：**
  - 1 小时急变：BTC、ETH ≥ 1.5%，其他 ≥ 4%
  - 1 小时量比 ≥ 3
  - 突破 7 日高点或跌破 7 日低点
- **衍生品：**
  - 资金费率年化 ≥ +30% 或 ≤ −20%（拥挤）
  - 持仓 4 小时内变化 ±15%
  - 溢价 |≥ 0.5%|
- **跨资产：** 股指 24 小时 ±3%，商品 ±4%。

### 研判（模型 v1）

- **标的：** BTC、ETH、美股500 永续。
- **区间：** EWMA 波动率（小时收益，半衰期 24h）→ 未来 24h 的 1σ 区间（约 68%）。
- **倾向：** 因子打分，分数在 −100 到 +100 之间，≥ +20 记为偏多，≤ −20 记为偏空，其余为中性。因子和权重如下，缺失的因子按比例重新分配权重：
  - 趋势（相对 7 日均线）0.30
  - 动量（24h）0.20
  - 主动买卖 0.15
  - 盘口 0.05
  - 资金费率（反向）0.10
  - 持仓和价格同向 0.10
  - 市场宽度 0.05
  - 美股（只用于加密）0.05
- **核对：** 每小时记录一次研判，24 小时后核对方向和区间。回测只用量价两个因子，在近 60 天的小时 K 线上做。

## 6. 通知和命令

- **定时简报：** 每 4 小时一期，分 3 组：加密 / 美股 / 宏观与研判。超长的组会按版块自动拆分。
- **异动推送：**
  - 只推重大异动。
  - 同一标的、同一类型、同一方向每天最多推一次。
  - 同一轮检测到的多个异动合并成一条。
  - 每天最多 8 条。
  - 北京时间 00:00–08:00 静音送达（不响铃，简报也一样）。
  - 可以用 `ALERTS_ENABLED=false` 关闭。
- **命令：**

| 命令 | 内容 |
| --- | --- |
| `/market` | 完整快照 |
| `/crypto` | 加密资金、衍生品、Top 10、异动 |
| `/deriv` | 衍生品 + 跨资产 |
| `/stocks` | 美股概览、个股 Top 20、个股资金榜 |
| `/sectors` | 板块和细分行业 |
| `/etf` | ETF Top 10 + 类别资金 |
| `/alerts` | 最新异动 |
| `/macro` | 跨资产 |
| `/predict` | 研判 + 命中率 |
| `/flow` | 资金流向汇总 |
| `/q 代码` | 单个股票、ETF 或币种 |
| `/news` `/logs` `/help` | 原有命令；`/logs` 增加数据源健康 |

## 7. 可靠性和数据质量

- **作业隔离：** 每个作业独立捕获异常，记录到 `jobs` 表，包括最近成功时间、错误、耗时和失败次数。失败的作业 5 分钟后重试。
- **沿用旧数据和兜底：** 沿用 v1 的机制（1 小时内的成功数据可以沿用，并标明）。快照 `schema=2`，不兼容 v1 的快照。
- **自检：**
  - `side` 语义：每次采集逐笔成交都校验
  - 股票列表数据日期：和 AAPL 日线比对
  - 盘后字段语义：第一次进入盘后时比对
  - 校验不过的项会在 `/logs` 里显示
- **保留期：**

| 数据 | 保留 |
| --- | --- |
| `equity_ticks` | 5 天 |
| `crypto_flow`、`deriv_ticks` | 8 天 |
| 小时 K 线 | 70 天 |
| `equity_daily`、`forecasts` | 150 天 |
| `alerts` | 30 天 |
| 快照 | 7 天 |

- **投递：** 沿用 v1 的"每期一次、断点续发、至少一次"语义；异动推送在发送前先落库去重。

## 8. 交付

- **测试：** 每个分析模块都有离线测试，用实测得到的真实字段样例做固定数据，调度和推送策略用假时钟测试。
- **部署：** 先备份，迁移数据库（新增表和列），然后重建 `market-report`、重启 `collector`。
- **上线后验证：** 美股开盘后核对实时数据，收盘后核对盘后字段语义。

## 9. 邮件渠道（v2.2，pg-test 自带，默认关闭）

- **现状（2026-10-28 定）：**
  - 客户的每日简报邮件继续由生产机（chris-aliyun-jp）上的 PureGamma AI SaaS 发送：worker 每分钟检查一次，每个账户自动开通，在当地时间 08:30 收一封，每封扣 Credits，用户在「每日推送」里改时间或关闭。
  - pg-test 不给客户发信，也不导出订阅名单，`EMAIL_ENABLED` 保持 `false`。内容和格式的升级走第 10 节。
- **当天的暂停和恢复：** 旧推送曾被暂停两次：11:55:47 UTC 在 Redis 放了锁 `pg:lock:dispatch_due_daily_briefs`，12:15 又改了容器里的 scheduler。12:32 删掉了锁，12:40 还原了 scheduler，12:41 分发恢复正常。
- **这个渠道的用途：** 给自己或内部名单发信，以及预览和测试。规则如下。
- **收件人：**
  - 名单放在 `secrets/email_recipients.json`，权限 0600，格式是 `[{email, locale, timezone, local_time, enabled}]`。`EMAIL_TO` 是备用名单。
  - 数据库里只保存邮箱的哈希值，日志里从不出现邮箱地址。
- **频率：**
  - 每位收件人每天一封，在其当地时间 `local_time` 发出（默认 08:30；中文用户默认时区为 Asia/Shanghai，英文用户为 UTC，与旧 SaaS 一致）。
  - 按"收件人哈希 + 当地日期"去重，写在 `email_deliveries` 表里。
  - 过了 45 分钟宽限期仍未送达的不补发。失败的收件人每 5 分钟单独重试，已发送的人不会重复收到。
- **隐私：** 每封邮件只发给一个收件人，绝不把多个客户放进同一个 To 字段。
- **语言：**
  - 中文版：红涨绿跌，金额用"$亿/$万"，时间用北京时间。
  - 英文版：绿涨红跌，金额用 $B/$M/$K，时间用 UTC，所有标签、行业、ETF 类别、异动和研判都是英文。
  - 邮件里不出现 Telegram 命令，结尾有退订说明（"如需退订，请直接回复本邮件"）。回复会进入发件邮箱；也可以设置 `EMAIL_REPLY_TO`，让回复转到别的地址。
- **版式：**
  - 只用表格和内联样式，不用图片、脚本和外链，宽 600px，适配手机，数据卡每行两张。
  - 结构：今日要点、加密资金与 Top 10、衍生品、加密异动、美股概览与 Top 20、行业、ETF、美股异动、跨资产、研判，最后是免责声明。
  - 某部分沿用了旧数据时，显示黄色提示条；暂不可用时写明原因。
- **SMTP：**
  - 使用 Gandi `mail.gandi.net:587`，STARTTLS，发件人 `noreply@puregamma.ai`。凭据取自旧生产配置，已验证可以登录。
  - 密码只放在 `secrets/smtp_password` 里。
- **开关：**
  - 默认 `EMAIL_ENABLED=false`。
  - `--email-preview PATH --locale zh|en` 只生成预览，不发信。
  - `--email-test 地址 --locale zh|en` 手动发一封测试邮件。

## 10. PureGamma AI 每日简报邮件接入（v2.3）

2026-10-28 用户选了方案 A：生产机继续发每日简报，同时升级邮件的格式和内容。用户、偏好、发送时间、Credits 和退订方式都不变。时序图见 `diagrams/08-saas-daily-email.mmd`。

- **旧邮件的问题：**
  - SaaS 把 4 份 AI 日报的 Markdown 拼成一封邮件，但发信模块只做 HTML 转义，所以 `##`、`**`、`---` 原样显示。
  - 页眉的 logo 是黑色单色 PNG，放在黑底上看不见，有的客户端还会显示成坏图。
  - 主题永远是"PureGamma AI"。
- **目标：**
  1. 格式：正确渲染 Markdown，页眉换成文字标识，主题带上日期。
  2. 内容：换成本项目的市场简报（中文/英文），数字和 Telegram 一致，不经过 AI 改写。
  3. 不变：收件人仍是账户邮箱，按每个人的时区和时间发（默认 08:30），每封照常扣 Credits，幂等键不变。个性化日报照常生成，照常进网页收件箱。
- **架构：**

  ```
  market-brief（生产机，本项目，独立 compose 项目）
    15 个采集作业 → SQLite → 每 15 分钟一份快照 → GET /brief?locale=zh|en
                                                     ↑ 请求里只有 locale
  SaaS worker：dispatch_due_daily_briefs（每分钟）→ 每位到点的用户
    → 日报照常生成（网页收件箱等渠道不变）
    → 邮件：fetch_market_brief(locale)
         ├ 有新鲜的简报 → 主题、HTML、纯文本都用简报
         └ 取不到、无效或超过 2 小时 → 用日报合并稿（新格式渲染）
    → send_notification（幂等键、Credits、收件人绑定账户邮箱，都不变）
  ```

- **接口：**
  - `GET /brief?locale=zh|en`：返回 200 和 `{ok, locale, snapshot_id, taken_at, age_seconds, subject, html, text, parts}`。`parts` 是各部分的状态（`ok`、`reused` 或 `unavailable`），HTML 就是第 9 节的版式（约 66KB，低于 Gmail 102KB 的截断线）。
  - 还没有快照，或者所有数据源都失败时，返回 503；locale 不对返回 400。同一份快照每种语言只渲染一次。
  - `GET /health`：返回心跳、快照年龄和作业健康（`jobs_ok/jobs_total`、`jobs_failing`）。心跳超过 10 分钟没更新就返回 503。
  - 页脚的退订句换成账户说明（`BRIEF_MANAGE_ZH/EN`）："如需修改发送时间或关闭，请登录 puregamma.ai，在「每日推送」中设置。"页眉文字用 `BRIEF_WORDMARK=PureGamma AI`。
- **SaaS 改动（向 puregamma-ai 的 main 提 PR）：**
  - `packages/notifications/market_brief.py`：取简报，超时 8 秒；校验字段，HTML 不超过 400KB；按语言缓存 120 秒，失败后 60 秒内不再请求。`MARKET_BRIEF_URL` 为空时整个功能关闭。另带 `--check`、`--preview`、`--test-send` 三个运维命令，`--test-send` 只接受一个地址，主题前加"[测试]"。
  - `packages/workers/tasks.py`：邮件分支先取简报，取不到就用原来的合并稿，并给合并稿加上带日期的主题（"PureGamma AI 每日简报 · 10月29日"）。
  - `packages/notifications/dispatcher.py`：只在 email 渠道把 `email_subject`、`email_html` 交给发信模块，数据库里仍然只存纯文本。
  - `packages/notifications/email.py`：轻量 Markdown 渲染（标题、粗体、斜体、代码、链接、列表、引用、分隔线、表格），用文字标识代替图片 logo，可以传入完整的 HTML 和主题。登录验证码等其他邮件也会用上新的页眉。
  - `docker-compose.production.yml`：只给 worker 加 `MARKET_BRIEF_URL`，默认 `http://market-brief:8790`，设成空值就关闭。
- **部署：**
  - sidecar 放在 `/puregamma/market-brief`，检出本仓库的固定提交，用 `docker compose -p market-brief -f deploy/market-brief/compose.yaml up -d` 启动。它是独立的 compose 项目，SaaS 部署时的 `up --remove-orphans` 不会删到它。
  - sidecar 用生产机已有的 python:3.12-slim 镜像，以非 root 用户运行，根文件系统只读，限 0.5 核、384MB，只接在 `puregamma-ai_egress` 网络上，不占宿主机端口。
  - SaaS 在当前 release 目录打补丁，只重建 worker 镜像，旧镜像打标签留作回滚。先用新镜像给用户本人发测试邮件，用户确认后才重建 worker 容器。不做数据库迁移，api 和 scheduler 不动。
- **回滚：**
  - 只撤内容：停掉 sidecar（`docker compose -p market-brief stop`），2 分钟内自动退回日报合并稿，新格式保留。
  - 全部撤回：在 release 目录执行 `git apply -R` 撤掉补丁，把旧镜像标签打回 `puregamma-ai-worker:latest`，然后执行 `up -d --no-deps --no-build worker`。
- **隐私与安全：** sidecar 只收到 locale，不接触任何用户数据。它不带 Telegram、SMTP 或任何密钥。
- **已知限制：**
  - 生产机上是冷启动：主动买卖的覆盖率要 4 小时才满，量比基线大约 12 小时补齐，实盘命中率从零开始累计。在这之前，邮件里相应位置会如实标注。
  - api 容器没有重建，所以从 api 发出的邮件（例如登录验证码）在下次 SaaS 部署前还是旧页眉。
  - 以后 SaaS 换别的提交重新部署时，必须带上这个 PR，否则会退回旧邮件。
