# PureGamma RSS & Event Collector

一个轻量 Docker RSSHub → RSS/Atom → Python 标准库采集器 → SQLite WAL → Telegram Bot 的部署包。
针对 4 vCPU / 8 GiB / 20 GiB 的 Instabox 进行上限规划；不带浏览器渲染、Redis、AI、交易执行接口。

> 架构、流程图与时序图见 [market/FLOW.md](market/FLOW.md)（Mermaid 源在 `market/diagrams/`，PNG 在 `market/diagrams-png/`）；行情简报设计见 [market/DESIGN.md](market/DESIGN.md)。

## 默认信息源（6 个可用 RSS + 1 个待授权群组）

- `aggrnews`: `@AggrNewswire`，RSSHub；1 分钟
- `watcherguru`: `@WatcherGuru` 官方资讯频道，RSSHub；1 分钟
- `watcherguru_chat`: `@WatcherGuruChat` 群聊读取必须由你的 Telegram 用户帐号通过 MTProto 授权；未授权默认禁用，授权后 1 分钟拉取私有 RSS bridge
- `fed_monetary`: 美联储货币政策官方 RSS；1 分钟
- `fed_all`: 美联储全部官方新闻；5 分钟
- `sec_press`: SEC 新闻稿官方 RSS；5 分钟
- `rsshub_releases`: RSSHub 项目发布；30 分钟

`WatcherGuruChat` 是讨论群组：先前 RSSHub 路由已知返回 503。V3 需要用户本人在 InstaBox 上授权一次 Telegram 会话才能启用真实群聊采集。见本文末尾“V3 更新”。

## 部署到确认身份的 Instabox SSH 服务器

1. 解压到 `/opt/puregamma-event-collector`，例如：

   ```bash
   sudo mkdir -p /opt/puregamma-event-collector
   sudo tar -xzf puregamma-event-collector.tar.gz -C /opt/puregamma-event-collector
   cd /opt/puregamma-event-collector
   ```

   或使用 zip 包（`unzip puregamma-event-collector.zip`）。

2. 先检查磁盘和已有容器：

   ```bash
   docker ps --format 'table {{.Names}}\t{{.Status}}'
   df -h /
   free -h
   ```

3. 请先在 BotFather `/revoke` **轮换曾粘贴进聊天的旧 Bot Token**；新的 Token 仅在服务器交互式输入，不写入仓库或压缩包。检查 `.env` 中 `TELEGRAM_CHAT_ID=<你的 chat_id>`。

4. 安装/检查 Docker Engine + Compose 插件，运行：

   ```bash
   sh scripts/deploy.sh
   python3 scripts/test_telegram.py
   sh scripts/check.sh
   ```

5. 检查特定源是否抓取成功：

   ```bash
   curl -fsS http://127.0.0.1:8765/status
   docker compose logs -f collector
   ```

## 行为与安全说明

- 第一次成功抓取每个订阅源时，只建立**基线**，防止旧消息刷屏。新事件从下一次成功抓取开始推送。
- P0/P1 立即入 Telegram 出站队列；P2 下一整点最多 10 条/摘要消息；P3 入库不推送。
- 对 A（官方）、B（新闻）、C（群组）做信任分级。非官方 B 最高 P1，群组讨论最多 P2，整点摘要推送。不执行自动交易。
- article 去重：source + GUID/URL；event 去重：严格标题清洗 + 6 小时内高相似度。**误合并代价高，策略偏保守**。
- 官方 RSS 直接抓取；RSSHub 只在容器网络内监听，无公网 1200 端口。
- SQLite WAL 事件库保存在 `./data`（60 天维护）；发送队列持久化；Telegram 429 延时退避。
- 使用 Docker Secret 文件保存 Bot Token，`./secrets/telegram_bot_token` 设为 0600；日志从不写出完整 Token。
- 服务仅将健康接口映射到宿主机 `127.0.0.1:8765`。开放此端口给公网会造成信息泄露，不应开放。
- `docker compose down` 可以停止服务，不删除数据库；勿执行 `down -v` 清除资源。

## 配置与调优

来源文件为 `config/sources.json`（标准 JSON），可直接增删源、修改 `interval_seconds`、`trust`、`default_priority`、`enabled`。
修改后执行 `docker compose restart collector`。

100–300 个 RSS 源是待实测的扩展目标，**并不保证在每个网站限流和 RSSHub 路由下可用**。实例 `rsshub` 768MB，`collector` 384MB，总配置 CPU 上限 1.75 vCPU；容器日志轮转。

## 本地测试（无需任何密钥或联网）

```bash
python3 -m unittest discover -s tests -v
SOURCES_PATH=config/sources.json python3 src/main.py --check-config
```

## 存在的明确限制

- 本包没有尝试连接 SSH 主机，也不会自动安装 Docker 到未知系统。
- 基于 RSS/公开 Telegram 页面，延迟不可保证为秒级；RSSHub 缓存 60 秒。RSSHub 失败/站点反爬将体现在 `sources.last_error`。
- Telegram 超时重试可能发生极少量重复发送（至少一次投递，不保证精确一次）。
- Chat 讨论群没有 Telegram 用户会话授权时，公开 RSS 抓取不代表完整接入。
- 按标题精确/高阈值去重，跨语言报道不一定归并，不使用任何 AI 含糊判定。

## 从本地电脑一键部署到 InstaBox（SSH Token）

将本 ZIP/TAR 包解压到本地 macOS/Linux 电脑（需要 `ssh` 和 `tar`），在项目目录执行：

```bash
sh scripts/install_over_ssh.sh
```

根据提示粘贴 `用户名@ssh.instabox.sh`（不会回显输入），不必把这个 SSH Token 放进 Shell 历史记录或配置文件。首次连接应核对 InstaBox 网关的 SSH 主机指纹。脚本会：

1. 无修改检查指定服务器的 Docker Compose 与磁盘空间；
2. 将最小服务文件复制到远端持久化 `$HOME/puregamma-event-collector`；
3. 用交互式 TTY 让你输入**在 BotFather 轮换后的新 Telegram Bot Token**；
4. 启动 Docker Compose，发送 Telegram 测试消息，输出容器与来源状态。

只有成功收到测试 Telegram 消息、RSSHub 和采集器健康检查通过，才能称为部署成功。对 `WatcherGuruChat` 的公开预览抓取仍需单独确认状态，它不具备私有群成员权限。

### 非常重要：关闭 InstaBox 自动暂停

InstaBox 默认长时间空闲（约 30 分钟）会自动暂停机器；Docker 内的定时抓取也将停止。使用本地已登录的 InstaBox CLI 执行：

```bash
instabox auto-suspend instabox-pg-test 0
```

如果 CLI 返回权限或计划不支持，应在 InstaBox 控制台确认该实例有持续运行权限；未关闭自动暂停前，不应宣称 24/7 实时推送成功。

---

## V3 更新：WatcherGuruChat 有授权的群聊采集器

旧 RSSHub `/telegram/channel/WatcherGuruChat` 因无公开消息预览返回 503。现已禁用该无效路由，改为 MTProto 只读侧车 `group-reader`，**没有 Telegram 用户会话授权时，群采集不算修复完成**。已有 `@AggrNewswire`、`@WatcherGuru` 和 Fed/SEC RSS 不受影响。

首次部署新代码：Mac 本地解压 V3 ZIP 后进入目录运行：

```sh
sh scripts/upgrade_over_ssh.sh
```

此命令仅覆盖程序、Compose 与源配置；不会覆盖已有数据库、`.env`、Bot Token 文件。尝试备份 SQLite 与来源配置到服务器 `backups/`。

授权讨论群读取：**你必须先在 Telegram 用自己的帐号加入 `@WatcherGuruChat`**。再在 <https://my.telegram.org> 获得 Telegram `API_ID` 和 `API_HASH`。这两个值不是 Telegram Bot Token，不要发给聊天，也不要上传到 GitHub。

在 Mac 终端运行下面指令，会在 InstaBox 虚拟机上交互式输入 API_ID/API_HASH、手机号、Telegram 验证码，以及可能的两步验证密码，授权资料只保留于服务器：

```sh
sh scripts/enable_group_over_ssh.sh
```

用户会话保存为 `data/group/watcherguru.session`。**这相当于授权凭据**：目录需保持私有，不加入版本管理，不对外开放端口，也不要发送给其他人。

该侧车每 20 秒检查新群消息，限定财经关键词、24–2000 字符，最多保留 250 条，发给原 Collector 的内部 RSS。未验证的讨论只归类为 C 信任等级、最多 P2 整点摘要，不作为官方事实。首次连接只建立最新消息的基线，不发送群历史消息。

### InstaBox 真正的持续运行（必须有支持的账户权限）

在已通过 `instabox login` 的 Mac 本地终端执行：

```sh
instabox auto-suspend instabox-pg-test 0
```

如果显示 `manual lifecycle` 未开放/权限不足，则**无法依靠 Docker restart 保证 24/7 运行**。需要 InstaBox 账号获得 manual lifecycle 许可，或搬到支持持续运行的 VPS。不要用循环 SSH、ping 等方式规避暂停限制。

### 验收首条真实新闻

```sh
sh scripts/check_real_news_over_ssh.sh
```

只将 SQLite 中 `outbox.status=sent` 且带 Telegram `message_id` 的**真实 RSS 事件**判为成功。安装时的 Bot 测试消息不会纳入此项。首次抓取只做基线，P2 事件等下一个 UTC 整点汇总；若还没有新消息，结果应明确为未验证，而非伪称成功。
