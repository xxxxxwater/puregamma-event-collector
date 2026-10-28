# Coinbase 市场简报 v2（market-report）运维手册

框架、数据源实测、约束和指标口径见 [DESIGN.md](DESIGN.md)。这里只写怎么用、怎么查问题。

## 你会收到什么

**定时简报**：每 4 小时一期（北京 00/04/08/12/16/20 点），分 3 组发送，长了会自动拆成多条：

1. **加密**：近 4 小时主动买卖（逐笔成交）和量价资金、24 小时资金估算、盘口买卖挂单比；永续资金费率、持仓、溢价和期限结构；成交额 Top 10；加密和衍生品异动
2. **美股**：时段说明，个股和 ETF 分开的概览；成交额 Top 20 个股；个股资金流入流出榜和涨跌榜；板块和细分行业资金；ETF 类别资金和 Top 10；美股异动
3. **宏观与研判**：Coinbase 股指和商品期货；BTC、ETH、美股500 的 24 小时区间和倾向，附回测和实盘命中率

**异动推送**：只推新出现的重大异动，同一标的、同类、同方向每天推一次。同一轮发现的异动合并成一条推送，每个标的只占一行。每天最多 8 条，北京 00–08 点静音送达。已收盘交易日的涨跌异动只列在简报里，不推送。

**命令**（只响应已配置的聊天，输入 `/` 会弹出菜单）：

| 命令 | 内容 |
| --- | --- |
| `/market` | 完整快照 |
| `/crypto` | 加密资金、衍生品、Top 10、异动 |
| `/deriv` | 衍生品 + 跨资产 |
| `/stocks` | 美股概览 + Top 20 个股 + 个股资金 |
| `/sectors` | 板块和细分行业 |
| `/etf` | ETF 类别资金 + Top 10 |
| `/alerts` | 最新异动 |
| `/macro` | 跨资产 |
| `/predict` | 研判 + 命中率 |
| `/flow` | 资金流向汇总 |
| `/q 代码` | 单标的卡片，如 `/q NVDA`、`/q SPY`、`/q BTC`；同名代码会同时给出加密和股票 |
| `/news` `/logs` `/help` | 原有命令；`/logs` 加了行情服务、数据源健康和自检结果 |

## 组成

| 文件 | 作用 |
| --- | --- |
| `src/market_sources.py` | 所有网络取数：Coinbase 现货、K 线、逐笔、盘口、期货、股票清单；Nasdaq 实时批量行情、日线、行业资料、ETF 列表、交易时段 |
| `src/market_jobs.py` | 15 个采集作业，各自有周期，健康状态记在 `jobs` 表 |
| `src/market_analysis.py` | 现货概览、4 小时量价、统一的资金估算公式 |
| `src/market_crypto.py` | 主动买卖（`side` 是挂单方，代码里已翻转）、盘口、K 线指标、加密异动 |
| `src/market_derivs.py` | 资金费率年化、持仓变化、溢价、基差、跨资产、衍生品异动 |
| `src/market_equities.py` | 实时行情的时段语义、个股和 ETF 划分、ETF 分类、资金、行业、量比、美股异动、股票池 |
| `src/market_forecast.py` | 区间和倾向、回测（带显著性）、到期核对 |
| `src/market_alerts.py` | 推送策略：去重、限额、静音 |
| `src/market_snapshot.py` | 把四个部分组装成快照、`/q` 卡片和异动列表 |
| `src/market_render.py` | Telegram HTML 渲染 |
| `src/market_store.py` | SQLite 契约，采集器也导入它（只用标准库） |
| `src/market_report.py` | 服务主循环、定时简报（续发、兜底）、异动推送、邮件调度 |
| `src/market_digest.py` | 各部分的结构化摘要（存在快照里，邮件和以后的其他渠道都从这里渲染） |
| `src/market_email.py` | HTML 邮件渲染（红涨绿跌、适配手机）和 SMTP 发送 |
| `src/bot_commands.py` | 采集器的命令处理（只读快照） |

全部使用公开只读接口，不使用任何 API Key。

## 常用操作

```sh
cd ~/puregamma-event-collector
docker compose ps market-report collector                          # 都应显示 healthy
docker compose logs --tail=80 market-report                        # 作业、快照、推送日志
docker compose run --rm --no-deps market-report python /app/market_report.py --dry-run   # 采集一轮并预览，不发送
docker compose run --rm --no-deps market-report python /app/market_report.py --once      # 立即手动补发一份简报（不推异动）
python3 -m unittest discover -s tests                              # 全部测试（离线）
```

在 `compose.override.yaml` 的 `market-report` 下可以调这些参数，改完执行 `docker compose up -d --no-deps --force-recreate market-report`：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `REPORT_INTERVAL_HOURS` | 4 | 定时简报间隔，必须能整除 24 |
| `REPORT_GRACE_MINUTES` | 45 | 宽限期 |
| `SNAPSHOT_REFRESH_MINUTES` | 15 | 快照刷新间隔 |
| `ALERTS_ENABLED` | true | 关掉异动推送；简报和 `/alerts` 里照常显示 |
| `ALERTS_MAX_PER_DAY` | 8 | 每天最多推送条数 |
| `QUIET_HOURS` | 0-8 | 北京时间静音时段，简报和推送都静音送达 |

## 邮件推送（每日一封，取代原来 PureGamma AI 的企业邮箱推送）

现状：SMTP（Gandi，`noreply@puregamma.ai`）已经配置好，发送默认关闭。生产机的旧推送已在 2026-10-28 暂停，详见 DESIGN.md 第 9 节。

开启步骤：

1. **放入客户名单。** 把名单写入 `secrets/email_recipients.json`（权限 0600），格式如下：

   ```json
   [{"email": "a@example.com", "locale": "zh", "timezone": "Asia/Shanghai", "local_time": "08:30"},
    {"email": "b@example.com", "locale": "en", "timezone": "UTC", "local_time": "08:30"}]
   ```

   `timezone` 和 `local_time` 可以不填：中文用户默认 Asia/Shanghai，英文用户默认 UTC，时间默认 08:30。不想再发的人，删掉那一行，或者写 `"enabled": false`。

2. **先发测试邮件。** 确认邮件正常后再继续：

   ```sh
   docker compose run --rm --no-deps market-report python /app/market_report.py --email-test 你的邮箱 --locale zh
   docker compose run --rm --no-deps market-report python /app/market_report.py --email-test 你的邮箱 --locale en
   ```

3. **正式开启。** 把 `compose.override.yaml` 里的 `EMAIL_ENABLED` 改成 `'true'`，然后执行：

   ```sh
   docker compose up -d --no-deps --force-recreate market-report
   ```

之后每位客户每天在自己当地时间 08:30 单独收到一封，不会重复。发送情况在 Telegram `/logs` 里有一行"✉️ 邮件简报"。

只预览、不发信：

```sh
docker compose run --rm --no-deps market-report python /app/market_report.py --email-preview /data/email_preview.html --locale en
```

恢复旧推送（不推荐，会和新推送重复）：在生产机上执行恢复说明里的 `DEL` 命令。

## 自检和排查

- **`/logs` 显示"数据源 n/15 正常"：** 异常的作业会列出原因。失败的作业 5 分钟后自动重试，不影响其他作业。
- **"数据自检未通过：逐笔方向"：** Coinbase 逐笔成交的 `side` 语义可能变了。这时主动买卖的方向可能是反的，需要人工确认。
- **"数据自检未通过：盘后字段"：** 盘后行情的字段语义和盘前不同。程序会自动改用盘中最后一次的数据。
- **时段后面带"（盘后字段语义待校验）"：** 第一次进入盘后时会自动校验，通过后这个标注就会消失。
- **"主动买卖覆盖率 xx%，数据积累中"：** 刚启动后 4 小时内会这样，属于正常现象。
- **量比显示得少：** 需要日线基线（每只股票至少 10 天）。启动后会分批回填（每 15 分钟 20 只），大约 12 小时内补完。
- **ETF 排名不全：** 每周全量扫描一次，每轮 800 只，第一次大约 1.5 小时扫完。扫完前股票池里只有核心 ETF 和已经扫到的 ETF。
- **某个版块显示"沿用 HH:MM 的数据"或"暂不可用"：** 处理方式同 v1，见 DESIGN.md 第 7 节。

## 回滚

`backups/market-v3-<时间戳>/` 里有部署 v2 之前的 `compose.override.yaml`、`market_report.sqlite`，以及原版的 `bot_commands.py.before`。v1 是直接在原文件上升级成 v2 的，没有单独保留 v1 的 `market_*.py`，所以回滚的结果是"没有行情服务、只有原版 RSS 命令"。步骤：

1. 复制回 `compose.override.yaml` 和 `src/bot_commands.py`。
2. 删除 `src/market_*.py`。
3. 执行 `docker compose rm -sf market-report && docker compose up -d --force-recreate collector`。
