# BidClub 每日播客摘要

本工具从公开的 [BidClub API](https://bidclub.ai/api-docs) 拉取前一天发布的
全部播客单集，并把现成的中英双语摘要推送到 Telegram 和 Lark（飞书）。
它被设计为由 cron 每天在北京时间 08:00
执行一次。

较长的摘要会自动拆分成多条消息发送，因此两个平台的
长度上限都不会导致发送失败。

## 功能特性

- **无需账号，无需 API key。** BidClub 的单集接口完全公开，不需要鉴权。
- **「昨天」按你配置的时区解析**，而不是服务器时区，因此同一条 crontab
  在 UTC 主机和北京时区主机上产出的摘要完全一致。
- **摘要是现成的。** BidClub 为每一集提供 TL;DR、分章节的完整摘要和一段式
  简介，且中英文各一份。本地不生成任何内容，因此既没有 LLM 费用，
  也没有生成摘要的等待时间。
- **自动分条发送。** Telegram 单条消息上限 4096 字符，Lark 自定义机器人限制
  整个请求体不超过 20 KB。分片会尽量填满额度、标上 `(2/5)` 这样的编号，
  并错开发送节奏，避免触发任一平台的限流。
- **按目的地精确送达一次。** 本地台账记录了哪一集已经进过哪个会话，
  因此重跑、cron 重试或放宽回看窗口都不会重复推送 —— 而发送失败的渠道
  会在第二天重试。
- **只降级，不失败。** 节目被下架就回退到列表里的元数据，Telegram 报
  格式错误就把该分片改成纯文本重发，Lark 卡片被拒则依次回退到富文本、
  再到纯文本。但摘要只是「取不到」（超时、5xx）的节目会被整条扣下，
  留到下次再发 —— 一次超时不该让「摘要不可用」被当成已送达写进
  台账，否则这条降级结果就永久固化了。
- **防注入。** 单集标题属于上游文本：发往 Telegram 前会做 HTML 转义，
  发往 Lark 前会剥掉 `<at>` 提醒标签，这样被构造过的标题就无法
  @ 整个工作群。

## 环境要求

- Python 3.13+
- `requests`（在没有系统时区数据库的主机上还需要 `tzdata`）

## 安装

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 复制配置模板并填入自己的凭据：

```bash
cp config.ini.template config.ini
```

3. 验证每个通知渠道都连得通：

```bash
python3 bidclub_digest.py --check
```

## 配置

`config.ini` 共有八个 section。每个配置键都在 `config.ini.template` 里
有行内注释说明；下面列出的是最常需要改动的几项。

注释请单独占一行。行内注释只认 `;`，这是刻意为之 —— 这样 URL 或模板字符串
里的 `#` 就永远不会被静默截断。

### 通知渠道

```ini
[telegram]
enabled = true
bot_token = 123456789:AA...
; 一个或多个目标，逗号分隔；机器人必须在每个会话里
chat_id = -1001234567890, @my_channel
parse_mode = html
disable_notification = true

[lark]
enabled = true
webhook_url = https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
; 仅当机器人开启了签名校验时才需要填
secret =
msg_type = interactive
```

至少要配置一个渠道，否则脚本会直接报错退出。
每个 Telegram 会话独立记账，因此之后再加第二个会话时，
不会把历史内容重放给第一个会话。

### 抓哪一天、抓哪些单集

```ini
[window]
timezone = Asia/Shanghai
date_basis = published_at
lookback_days = 3
notify_on_first_run = false

[content]
summary_field = tldr
language = zh
max_episodes = 12
```

- `timezone` 决定「昨天」指的是哪一天。摘要始终汇报的是该时区下的
  前一个自然日。
- `date_basis` 决定用哪个字段判定单集归属于哪一天。`published_at` 是真实的
  发布时刻，也是 API 唯一用来排序的字段。另一个选项 `date` 是节目自己声明的
  日期；大约三分之一的单集里，它与 `published_at` 换算出的本地日期
  对不上。
- `lookback_days` 把扫描范围放宽到昨天以前。当天较晚发布的单集，往往会
  一直拖到第二天才陆续进入 API，因此只在 08:00 扫一遍昨天，
  就会永久性地漏掉这些迟到的单集。有台账兜底，窗口放宽也绝不会重复推送，
  只会把漏掉的补上。如果想要严格「只看昨天」的行为，
  把它设成 `1`。
- `language` 可选 `zh`、`en`、`primary` 或 `both`。每一集都带一种主语言，
  外加另一种语言的译文，所以设成 `zh` 时，无论节目本身是中文还是英文，
  读到的都是中文。
- `summary_field` 可选 `tldr`（要点列表，默认值）、`digest`（分章节的完整
  正文，长度是前者的数倍）或 `dek`（一段话）。

### 分片与发送节奏

```ini
[output]
telegram_chunk_chars = 3500
telegram_max_chunks = 20
telegram_delay_seconds = 3.2
lark_chunk_bytes = 8000
lark_max_chunks = 20
lark_delay_seconds = 1.0
startup_jitter_seconds = 180
title = BidClub Daily Podcast
```

两个额度刻意用了不同的单位 —— Telegram 数的是字符，Lark 数的是序列化后
请求体的字节数。详见下文的
「消息分片」一节。

如果你的 Lark 机器人配置了必需关键词，把该关键词写进 `title`：
机器人只会在 `text` 和 `title` 字段里检索这个关键词。

## 使用方法

```bash
# 昨天的摘要（cron 跑的就是这一条）
python3 bidclub_digest.py

# 指定某一天，按配置的时区解释
python3 bidclub_digest.py --date 2026-08-28

# 只渲染并打印每个分片，不发送、不记账
python3 bidclub_digest.py --dry-run --date 2026-08-28

# 校验凭据与会话成员资格，不发送任何内容
python3 bidclub_digest.py --check

# 向每个已启用的渠道发一条简短的测试消息
python3 bidclub_digest.py --test

# 把某一天已经发过的内容重新推送一遍
python3 bidclub_digest.py --force --date 2026-08-28 --lookback 1
```

| 参数 | 含义 |
| --- | --- |
| `-c`, `--config PATH` | `config.ini` 的路径（默认取脚本同目录下的文件） |
| `--date YYYY-MM-DD` | 目标日期，按配置的时区解释（默认：昨天） |
| `--lookback N` | 本次运行覆盖 `[window] lookback_days` |
| `--limit N` | 本次运行覆盖 `[content] max_episodes` |
| `--force` | 忽略台账；把窗口内的每一集重新推送一遍 |
| `--dry-run` | 只渲染并打印；不发网络请求，也不写台账 |
| `--check` | 仅做预检：`getMe`、`getChat`、webhook 格式、签名自检 |
| `--test` | 走真实发送路径发一条简短的测试消息；所有已启用的目标都必须接收成功 |
| `--telegram-only` / `--lark-only` | 本次运行只用其中一个渠道，关掉另一个 |
| `--no-jitter` | 跳过 `startup_jitter_seconds` |
| `-v`, `--verbose` | 强制开启 `DEBUG` 日志 |
| `--version` | 打印版本号后退出 |

退出码：`0` 成功或无事可做，`1` 致命错误，`2` `--check`/`--test`
失败，`3` 部分送达（下次运行时重试），`130` 被中断。

## 定时任务

脚本自己按 `[window] timezone` 解析「昨天」，所以主机时区只决定任务*什么时候*
触发，绝不会影响它汇报的是*哪一天*。

```bash
# 北京时间 08:00，服务器时区为 Asia/Shanghai
0 8 * * * cd $YOUR_DIR/bidclub-digest && bash crontab.sh &

# 服务器时区为 UTC（北京 08:00 = UTC 00:00）
0 0 * * * cd $YOUR_DIR/bidclub-digest && bash crontab.sh &
```

如果吃不准主机时区，与其猜，不如直接在 crontab 里钉死：

```bash
CRON_TZ=Asia/Shanghai
0 8 * * * cd $YOUR_DIR/bidclub-digest && bash crontab.sh &
```

运行重叠是安全的：`bidclub_digest.lock` 上的锁会让后一次运行立即退出，
不发送任何内容。`crontab.sh` 的输出重定向到独立的
`bidclub_digest_cron.log`，与服务自己写的轮转日志分开，因此同一个文件里
不会出现重复的日志行。

## 去重机制

`sent_episodes.json` 以单集 slug 为键，记录哪些目的地已经收到过它 ——
每个会话记为 `telegram:<chat_id>`，webhook 记为 `lark`。

- 只有当某一集落在日期窗口内，**并且**该目的地不在它的 `delivered`
  映射里时，这一集才会被选中推给该目的地。
- 只有当承载了该 slug 任何内容的**每一个**分片都发送成功，它才会被标记为
  已送达。部分失败会在第二天整条重试；宁可重复发一半，
  也不能留下缺口。
- 首次运行时，若 `notify_on_first_run = false`，则只推送目标日期那天的内容，
  窗口内更早的单集直接记为已送达，这样首次运行就不会把一周的历史
  一股脑刷进会话。
- 台账读不出来时，会被重命名为 `sent_episodes.json.corrupt-<epoch>`，
  本次运行按首次运行处理。最坏也只是重发一天，绝不会重发一周。
- 超过 `state_retention_days`（默认 90 天）的记录会在写盘时清理掉。

## 消息分片

摘要会先渲染成一组原子行 —— 每行都是一个完整的逻辑行，行内用到的标记
标签都在本行内开闭。分片只在**行与行之间**切开，因此切分永远不会把标签
拦腰截断，也就不会触发经典的「实体未闭合」
拒收错误。

| | Telegram | Lark |
| --- | --- | --- |
| 硬上限 | 实体解析后 4096 字符 | 请求体 20480 字节 |
| 计量单位 | `max(code points, UTF-16 units)` | 序列化请求体的 UTF-8 字节数 |
| 默认额度 | 3500 字符 | 8000 字节正文 |
| 发送节奏 | 消息间隔 3.2 秒（群内每分钟 20 条上限） | 1.0 秒（每个机器人每分钟 100 条） |
| 格式 | HTML | 卡片 markdown → 富文本 → 纯文本 |

如果单独一行仍然超长，会先剥掉它的标记，按句子边界切开，
再用二分查找定位精确的字符边界 —— 在混合文本上按比例估算一定会切过头：
一个 emoji 只算一个码点，却占两个 Telegram 计量单位；
而一个汉字只算一个字符，
却要占三个 UTF-8 字节。

如果整份摘要需要的消息条数超过 `telegram_max_chunks` /
`lark_max_chunks`，末尾的单集会被替换成一份紧凑的标题加链接清单，
而不是被静默丢弃。

`startup_jitter_seconds` 会把首次发送随机延后一小段时间，因为 Lark 的文档
提醒过：正好卡在整点发出的消息容易撞上限流报错 —— 而这个任务恰恰是
08:00 整点触发的。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `Configuration file not found` | `cp config.ini.template config.ini` |
| `No notification channel configured` | 填好 `[telegram] bot_token` + `chat_id`，或者 `[lark] webhook_url` |
| `--check` 报 `401 Unauthorized` | Telegram bot token 填错了，或者已被吊销 |
| `--check` 报 `chat not found` | 机器人不在那个会话里。先把它拉进群 |
| Lark `19001` | webhook token 无效或已被重新生成；把新 URL 复制过来 |
| Lark `19021` | `[lark] secret` 填错，或主机时钟偏差超过一小时 |
| Lark `19022` | 本机 IP 不在机器人的 IP 白名单里 |
| Lark `19024` | 机器人要求带关键词；把关键词写进 `[output] title` |
| `Another run holds ...lock` | 上一次运行还没结束。无害 —— 它会以 `0` 退出 |
| `max_pages ... this scan is INCOMPLETE` | 窗口需要的行数超过了 `page_size × max_pages`。调大 `[bidclub] max_pages` 或调小 `lookback_days` |
| `Holding back N episode(s)` | 这些节目的详情拉取超时了，下次运行会补发 |
| 什么都没发出，日志显示 "already delivered" | 台账里已经有记录。用 `--force` 重推 |
| 摘要漏掉了一集迟到的内容 | BidClub 有些单集是发布几小时后才入库的；`lookback_days = 3` 会在第二天早上补上 |

加 `-v` 可以打开 `DEBUG` 日志，加 `--dry-run` 可以看到究竟会发出什么内容、
以及每个分片有多大。

## 文件结构

```
podcast/bidclub/
├── bidclub_digest.py      # 服务主体：抓取 → 渲染 → 分片 → 推送 → 记账
├── config.ini.template    # 配置模板（复制为 config.ini）
├── crontab.sh             # 供 cron 调用的一次性启动脚本
├── requirements.txt       # Python 依赖
├── .gitignore             # 忽略 config.ini、运行期状态和日志
├── README.md              # 英文文档
└── README_cn.md           # 中文文档
```

运行期文件全部被 git 忽略：`config.ini`、`sent_episodes.json`（送达台账）、
`bidclub_digest.lock`、按大小轮转的 `bidclub_digest.log`，以及
`bidclub_digest_cron.log` —— `crontab.sh` 用它兜住日志系统初始化之前就
失败的情况，比如依赖缺失。

## 说明

- `config.ini` 里存着 bot token 和 webhook 密钥。该文件已被 git 忽略，
  日志器也会在所有输出（包括异常文本）中把这两者脱敏。
- BidClub API 要求推送时附上回链到单集及其原始来源的链接；这两项通过
  `include_episode_link` 和 `include_source_link`
  默认开启。
- `transcript_md` 在抓到单集后就立刻丢弃。它约占返回体积的 80%，
  而且从不参与渲染。

## 许可证

MIT
