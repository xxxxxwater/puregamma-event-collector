# 流程图与时序图（RSS 项目 puregamma-event-collector · market-report v2）

下面的图和代码一致，文中的周期和阈值都是线上实际使用的值。
Mermaid 源文件在 `diagrams/`，PNG 在 `diagrams-png/`。改了源文件后，运行 `python3 market/diagrams/render.py market/diagrams market/diagrams-png` 重新生成 PNG。
口径细节见 [DESIGN.md](DESIGN.md)，生成快照和异动的提示词见 [PROMPT.md](PROMPT.md)。

## 图 1 · 系统总览（流程图）

两个容器：market-report 是行情生产者，collector 负责 RSS 新闻和命令，两者通过只读的 SQLite 快照衔接；全部数据来自 Coinbase 和 Nasdaq 的公开接口。简报通过 Telegram 发送，也可以通过企业邮箱以 HTML 邮件发送。

```mermaid
flowchart TB
  subgraph EXT["① 外部公开数据源（只读，不用 API Key）"]
    CB["Coinbase 公共接口<br/>现货 · 1h K线 · 逐笔成交 · 盘口<br/>永续/交割期货 · 股票清单与类型"]
    NQ["Nasdaq 公共接口<br/>watchlist 实时行情（每批 20 只）<br/>日线 · 板块行业 · ETF 名称 · 交易时段"]
    RSS["RSS 订阅源<br/>AggrNews · Watcher Guru · Fed · SEC"]
  end
  subgraph MR["② market-report 容器（行情生产者）"]
    JOBS["采集作业层 market_jobs<br/>15 个作业 · 各自周期 · 失败隔离"]
    ANA["分析层（纯函数）<br/>crypto · derivs · equities · forecast"]
    SNAP["快照组装 market_snapshot + 渲染 market_render"]
    NOTI["定时简报 + 异动推送<br/>market_report · market_alerts"]
  end
  DB[("③ market_report.sqlite<br/>K线 · 主动买卖 · 衍生品 · 股票行情/日线<br/>快照 · 播报 · 异动 · 研判 · /q 卡片 · 作业健康")]
  subgraph COL["④ collector 容器（RSS 新闻 + 命令）"]
    HUB["RSSHub 容器"]
    ING["RSS 采集 · 去重 · 分类分级 P0–P3"]
    EDB[("events.db<br/>events · outbox · 翻译缓存")]
    OUT["outbox 发送<br/>P0/P1 即时 · P2 整点摘要"]
    LT["LibreTranslate<br/>英译中"]
    CMD["命令轮询 getUpdates<br/>bot_commands（只读）"]
  end
  TG["⑤ Telegram Bot API"]
  USER(["你的 Telegram 聊天"])
  SMTP["⑥ 企业邮箱 SMTP"]
  MAIL(["你的邮箱"])

  CB --> JOBS
  NQ --> JOBS
  JOBS -- "时间序列" --> DB
  DB --> ANA --> SNAP
  SNAP -- "快照 · /q 卡片 · 异动" --> DB
  SNAP --> NOTI
  RSS --> HUB --> ING
  ING --> EDB --> OUT
  OUT <-- "翻译" --> LT
  CMD -- "只读快照" --> DB
  CMD -- "/news /logs" --> EDB
  NOTI -- "每 4 小时 3 组简报 · 重大异动" --> TG
  NOTI -- "HTML 邮件简报（北京 08/20 点，可配置）" --> SMTP --> MAIL
  OUT -- "新闻" --> TG
  TG <-- "getUpdates / 回复" --> CMD
  TG <--> USER
```

![图 1 · 系统总览（流程图）](diagrams-png/01-overview.png)

## 图 2 · 主循环：每 60 秒一轮（流程图）

先跑到期的采集作业，再判断本期简报要不要发：每期只发一次，失败后断点续发，宽限期快结束时发失败通知，过期的简报不补发。

```mermaid
flowchart TD
  A(["每 60 秒一轮 tick(now)"]) --> B["写心跳 heartbeat"]
  B --> C["run_due：按顺序执行到期的采集作业<br/>失败 5 分钟后重试 · 结果写入 jobs 健康表"]
  C --> D{"本期 slot 已发送？<br/>UTC 00/04/08/12/16/20<br/>= 北京 08/12/16/20/00/04"}
  D -- "未发 / 发送中" --> E{"距 slot ≤ 45 分钟？"}
  E -- "否" --> E2["发送中 → 标记 partial / expired<br/>未发 → 跳过，不补发过期简报"]
  E -- "是，且到了重试时间" --> F["生成新快照（见图 3）<br/>同时做异动推送判定"]
  F --> G{"至少一个部分有数据？<br/>成功或沿用 ≤1 小时的旧数据"}
  G -- "是" --> H["按 3 组拼消息<br/>1/3 加密 · 2/3 美股 · 3/3 宏观与研判<br/>单条 ≤4000 字，超长按版块拆分"]
  G -- "否，离宽限期结束还早" --> R["5 分钟后重试"]
  G -- "否，宽限期将结束" --> N["生成“本期数据获取失败”通知"]
  H --> S["broadcast_start：消息列表先落库"]
  N --> S
  S --> T["逐条 sendMessage<br/>北京 00–08 点静音送达<br/>每成功一条记 parts_sent，失败只续发剩余"]
  T --> U["broadcast_finish：sent / failed"]
  D -- "已发送" --> V{"距上次快照 ≥ 15 分钟？"}
  E2 --> V
  V -- "是" --> W["生成快照（供命令查询和异动推送）"]
  V -- "否" --> X(["等待下一轮"])
  W --> X
  U --> X
  R --> X
```

![图 2 · 主循环：每 60 秒一轮（流程图）](diagrams-png/02-tick-flow.png)

## 图 3 · 市场快照生成（流程图）

加密、衍生品与跨资产、美股、研判四个部分各算各的；哪部分失败了，就沿用它 1 小时内成功的旧数据，没有就显示“暂不可用”。

```mermaid
flowchart TD
  S(["build_snapshot(now)<br/>四个部分各自计算、互不影响"]) --> C1 & D1 & E1 & F1
  subgraph PC["① 加密"]
    C1["现货缓存（≤1 小时）<br/>USD/USDC/USDT 合并 · alias 去重<br/>稳定币不参与排名"] --> C2["近 4h 量价（8 根完整 1h K线）<br/>阳线成交记流入 · 阴线记流出 · 对比前 4h"]
    C2 --> C3["近 4h 主动买卖（逐笔成交）<br/>side 是挂单方：SELL 记主动买入<br/>标注覆盖率"]
    C3 --> C4["盘口 ±1% 买/卖挂单比"]
    C4 --> C5["Top 10 · K线指标<br/>1h 涨跌 · 1h 量比 · 7 日高低点"]
  end
  subgraph PD["② 衍生品与跨资产"]
    D1["期货缓存（≤1 小时）"] --> D2["永续：资金费率年化 = 费率 × 8760<br/>持仓 = 张数 × 合约面值 × 价格<br/>4h/24h 持仓变化 · 溢价"]
    D2 --> D3["交割期货年化基差<br/>(期货 ÷ 现货 − 1) × 365 ÷ 剩余天数"]
    D3 --> D4["跨资产：美股500 · 科技100 · AI · 中国 · 国防<br/>黄金 · 白银 · 铂 · 铜 · 原油 · 天然气"]
  end
  subgraph PE["③ 美股"]
    E1["watchlist 行情缓存（≤3 小时）<br/>股票池：个股前 500 + ETF 前 200 + 核心 ETF"] --> E2["时段判定：盘中 / 盘前 / 盘后 / 休市<br/>交易日来自 market-info"]
    E2 --> E3["还原时段语义<br/>盘前：收盘价 = 最新价 − 涨跌额<br/>盘后/休市：自检通过才用，否则用盘中最后一次"]
    E3 --> E4["资金估算 = 成交额 × clamp(涨跌 ÷ 2%, −1, 1)<br/>量比 = 成交量 ÷ (20 日均量 × 时间进度^0.7)"]
    E4 --> E5["个股 Top 20 · 个股资金榜 · 涨跌榜<br/>11 个板块 · 细分行业 · ETF 9 类"]
  end
  subgraph PF["④ 研判"]
    F1["每小时：24h 区间（EWMA 波动率，半衰期 24h）<br/>多因子打分：≥ +20 偏多 · ≤ −20 偏空"] --> F2["附回测命中率 · 有效样本 · 显著性 · 实盘核对"]
  end
  C5 --> Q{"该部分成功？"}
  D4 --> Q
  E5 --> Q
  F2 --> Q
  Q -- "成功" --> SV["保存快照 schema=2<br/>+ 每个标的的 /q 卡片<br/>+ 异动列表（见图 4）"]
  Q -- "失败" --> Q2{"1 小时内有该部分<br/>成功的快照？"}
  Q2 -- "有" --> RU["沿用，标注“沿用 HH:MM 的数据”"]
  Q2 -- "无" --> UN["显示“暂不可用（原因）”"]
  RU --> SV
  UN --> SV
```

![图 3 · 市场快照生成（流程图）](diagrams-png/03-snapshot-flow.png)

## 图 4 · 异动判定与推送（流程图）

每类异动的触发阈值和重大阈值（括号里）都写在图上。所有异动去重后入库，只有新出现的重大异动才推送：每天最多 8 条，夜间静音送达。

```mermaid
flowchart TD
  subgraph SRC["异动检测（每次快照，阈值 / 重大阈值）"]
    A1["加密（成交额前 30 的币种）<br/>1h 急变：BTC/ETH ≥1.5%（≥2.5%）<br/>其他 ≥4%（≥8% 且量比 ≥3 且 1h 成交 ≥$200万）<br/>1h 量比 ≥3 · 7 日新高 / 新低（只展示）"]
    A2["衍生品与跨资产<br/>资金费率年化 ≥+30% 或 ≤−20%（BTC/ETH ≥60%）<br/>持仓 4h ±15%（±30%） · 溢价 ±0.5%（±1.5%）<br/>股指 24h ±3% · 商品 ±4%（×1.7）"]
    A3["美股个股（成交 ≥$5000万）<br/>涨跌 ≥7%（盘中 ≥12% 且成交 ≥$2亿）<br/>量比 ≥2.5（盘中 ≥5 且涨跌 ≥5% 且成交 ≥$2亿）<br/>15 分钟 ±2.5% · 60 分钟 ±4%（×1.6 且成交 ≥$1亿）<br/>盘前盘后 ±5%（±10% 且成交 ≥$2亿）"]
    A4["美股 ETF（不含杠杆/反向、波动率）<br/>涨跌 ≥3%（盘中 ≥6% 且成交 ≥$2亿）<br/>量比 ≥3 · 急变和盘外同个股"]
  end
  A1 & A2 & A3 & A4 --> SEV["严重度 = 超过阈值的倍数<br/>量比封顶 3 倍 · 盘中急变 × 1.2"]
  SEV --> AL["写入 alerts，按 key 去重<br/>key = 类型 : 标的 : 方向 : 日期"]
  AL --> NEW{"这条异动今天是第一次出现？"}
  NEW -- "否" --> SHOW(["只在简报和 /alerts 里展示"])
  NEW -- "是" --> MAJ{"达到重大阈值？<br/>已收盘交易日的涨跌和放量不算"}
  MAJ -- "否" --> SHOW
  MAJ -- "是" --> CAP{"今天已推送 < 8 条？<br/>（ALERTS_MAX_PER_DAY）"}
  CAP -- "否" --> SHOW
  CAP -- "是" --> BAT["同一轮的重大异动合并成 1 条<br/>按严重度排序 · 每个标的 1 行 · 最多 6 行<br/>同标的的其他异动一并标记已推送"]
  BAT --> QUIET{"北京时间 00–08 点？<br/>（QUIET_HOURS）"}
  QUIET -- "是" --> SIL["静音送达 disable_notification"]
  QUIET -- "否" --> RING["正常提醒"]
  SIL --> SEND["sendMessage “⚡ 异动提醒” → mark_pushed"]
  RING --> SEND
```

![图 4 · 异动判定与推送（流程图）](diagrams-png/04-anomaly-flow.png)

## 图 5 · 定时简报与异动推送（时序图）

一个简报时段内，采集作业、外部接口、数据库、快照组装、异动策略和 Telegram 之间的完整交互。

```mermaid
sequenceDiagram
  autonumber
  participant L as market-report 主循环
  participant J as 采集作业（15 个）
  participant CB as Coinbase 公共接口
  participant NQ as Nasdaq 公共接口
  participant DB as market_report.sqlite
  participant S as 快照组装
  participant A as 异动策略
  participant TG as Telegram Bot API
  actor U as 你
  Note over L: 每 60 秒一轮，slot 为 UTC 00/04/08/12/16/20
  L->>DB: 写心跳
  L->>J: run_due(now)
  J->>CB: 现货 · 期货 · K线增量 · 逐笔 · 盘口
  CB-->>J: JSON
  J->>NQ: 交易时段 · watchlist 每批 20 只 · 日线 · ETF 扫描
  NQ-->>J: JSON
  J->>DB: 写时间序列表和 jobs 健康
  L->>DB: 查本期 broadcasts
  alt 本期未发，且在 45 分钟宽限期内
    L->>S: build(now)
    S->>DB: 读 K线 · 主动买卖 · 衍生品 · 日线基线
    S-->>L: 版块、指标、状态、/q 卡片、异动列表
    opt 某个部分失败
      L->>DB: last_good（1 小时内）沿用并标注，否则占位
    end
    L->>DB: 保存快照，替换 /q 卡片
    L->>A: plan(异动)
    A->>DB: add_alert（按 key 去重）
    opt 有新的重大异动，且今天推送不满 8 条
      L->>TG: sendMessage 异动提醒（00–08 点静音）
      TG-->>U: 推送
      L->>DB: mark_pushed
    end
    L->>DB: broadcast_start（3 组消息先落库）
    loop 每条消息，失败只续发剩余
      L->>TG: sendMessage（00–08 点静音）
      TG-->>U: 简报 1/3 · 2/3 · 3/3
      L->>DB: parts_sent 加 1
    end
    L->>DB: broadcast_finish = sent
  else 所有数据源都失败
    L->>L: 5 分钟后重试
    L->>TG: 宽限期结束前发送“本期数据获取失败”
  end
```

![图 5 · 定时简报与异动推送（时序图）](diagrams-png/05-seq-brief.png)

## 图 6 · Telegram 命令查询（时序图）

/market、/q 等命令由 collector 的轮询线程处理，只读快照；同一个 Bot 只能有一个程序接收消息。

```mermaid
sequenceDiagram
  autonumber
  actor U as 你
  participant TG as Telegram Bot API
  participant C as collector 命令轮询线程
  participant MS as market_store（只读）
  participant DB as market_report.sqlite
  participant E as events.db
  Note over C: 启动时 getMe 并 setMyCommands，注册 14 个命令菜单
  U->>TG: /q NVDA（或 /market、/stocks、/sectors、/etf、/alerts、/predict …）
  C->>TG: getUpdates（长轮询 25 秒）
  TG-->>C: update
  C->>C: 只认已配置的 chat_id，忽略启动前的消息，解析 /命令@bot
  alt 行情命令
    C->>MS: view_messages(视图) 或 quote_messages(代码)
    MS->>DB: 只读打开（mode=ro），读最新快照或 quotes
    DB-->>MS: 版块 HTML 或标的卡片
    MS-->>C: 消息列表（注明数据时间，超过 1 小时提示过期）
  else /news 或 /logs
    C->>E: 读 outbox、sources
    C->>MS: status_line（行情服务、15 个数据源健康、数据自检）
  end
  C->>TG: sendMessage（第一条引用原消息，超长自动分条）
  TG-->>U: 回复
```

![图 6 · Telegram 命令查询（时序图）](diagrams-png/06-seq-command.png)

## 图 7 · RSS 新闻推送（时序图）

RSS 新闻原有的链路：去重、分级、翻译、发送。

```mermaid
sequenceDiagram
  autonumber
  participant F as RSS 源 / RSSHub
  participant C as collector 主循环（每 2 秒）
  participant E as events.db
  participant LT as LibreTranslate
  participant TG as Telegram Bot API
  actor U as 你
  C->>F: 拉取到期的订阅源（ETag / Last-Modified）
  F-->>C: RSS / Atom
  C->>C: 解析，6 小时内同事件去重，分类，分级 P0–P3
  C->>E: 写 events 和 outbox（P0/P1 立即，P2 下一个整点，P3 不发）
  C->>E: 取到期的 outbox
  C->>LT: 英译中（结果缓存）
  LT-->>C: 中文标题
  C->>TG: sendMessage（P0/P1 单条，P2 每 5 条合并为摘要）
  TG-->>U: 新闻
  C->>E: 标记已发送，失败按 retry 延后
```

![图 7 · RSS 新闻推送（时序图）](diagrams-png/07-seq-news.png)
