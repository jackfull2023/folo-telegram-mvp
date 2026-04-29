# Folo Free -> Telegram MVP

这个 MVP 使用 Folo Free 版的能力：

- 150 个普通 feed subscriptions
- 30 个 RSSHub subscriptions
- 1 个 Action
- Action 支持 Webhook

架构是：

```text
Folo 订阅 RSSHub/X 源
  -> Folo Action Webhook
  -> 本服务接收新 entry
  -> SQLite 去重
  -> 账号权重 + 关键词打分
  -> Telegram Bot 推送频道
```

这样你不需要自己轮询 RSS，也不需要付费使用 X API。

如果 Folo Action/Webhook 不稳定，也可以绕开 Folo 自动化，直接使用内置 Poll 模式作为备用：

```text
RSS/RSSHub 源
  -> 本服务定时轮询
  -> SQLite 去重
  -> 账号权重 + 关键词打分
  -> Telegram Bot 推送频道
```

Folo 仍然可以作为阅读器使用，但 Telegram 推送不再依赖 Folo 的自动化规则。

## 1. 准备 Telegram

1. 在 Telegram 找 `@BotFather` 创建 bot，拿到 bot token。
2. 创建一个频道或使用已有频道。
3. 把 bot 加入频道，并设为管理员。
4. 如果频道是公开频道，`TELEGRAM_CHAT_ID` 可以写成 `@channel_name`。

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
TELEGRAM_CHAT_ID=@your_channel_name
WEBHOOK_SECRET=换成一个长一点的随机字符串
```

## 2. 配置账号与关键词

编辑 `config.example.json`：

- `accounts`：账号权重。Free 版 RSSHub subscriptions 上限是 30，建议第一版只放 20 到 30 个 X 账号。
- `keywords`：正向关键词加分。
- `negative_keywords`：垃圾词扣分。
- `filter.min_score`：最低推送分数。
- `limits.max_pushes_per_hour`：每小时最多推送数。
- `scoring`：更细的评分规则，例如标题关键词、正文关键词、URL 域名、新鲜度阶梯、低质词扣分。
- `poll`：RSS 轮询配置。
- `feeds`：显式配置的 RSS/Atom 源。

评分模型：

```text
score = legacy_account_score + legacy_keyword_score + scoring_rules
```

示例：

```text
karpathy 权重 +10
标题包含 agent +5
URL 是 github.com +3
发布时间在 3 小时内 +2
总分 20，超过 min_score=8，则推送
```

`accounts`、`keywords`、`negative_keywords` 是旧版通用评分。没有配置 `scoring` 时，它们会保持原行为；配置了 `scoring` 后，可以用下面三个开关控制是否继续叠加旧规则：

```json
{
  "scoring": {
    "use_legacy_accounts": true,
    "use_legacy_keywords": false,
    "use_legacy_negative_keywords": false
  }
}
```

调参时建议保留 `telegram.include_debug = true`，观察 Telegram 消息底部的 reason，例如：

```text
score=17 account:OpenAI+6; title:agent+5; url:github.com+3; fresh<=3h+2
```

### 2.1 细分评分规则

新鲜度阶梯：

```json
{
  "scoring": {
    "freshness": [
      { "max_hours": 1, "score": 4 },
      { "max_hours": 3, "score": 2 },
      { "max_hours": 12, "score": 1 }
    ]
  }
}
```

标题命中通常比正文命中更重要：

```json
{
  "scoring": {
    "title_keywords": {
      "agent": 5,
      "benchmark": 4,
      "开源": 3
    },
    "body_keywords": {
      "agent": 2,
      "benchmark": 2,
      "开源": 2
    }
  }
}
```

URL 和站点权重适合给高价值来源加分：

```json
{
  "scoring": {
    "url_keywords": {
      "github.com": 3,
      "arxiv.org": 4
    },
    "site_url_weights": {
      "openai.com": 3,
      "anthropic.com": 3
    }
  }
}
```

Feed 权重和低质词扣分适合压制热榜、营销、健康谣言类内容：

```json
{
  "scoring": {
    "feed_title_weights": {
      "微信 · 24h热文榜": -8,
      "OpenAI News": 4
    },
    "field_rules": [
      {
        "name": "low_quality_cn",
        "fields": ["entry.title", "entry.description", "feed.title"],
        "terms": {
          "震惊": -4,
          "医生": -3,
          "糖尿病": -4,
          "热文榜": -6
        }
      }
    ]
  }
}
```

每次调权重后，可以先用 `poll-once --dry-run` 或手动 POST 测试 payload 看分数，不要直接上生产推送。

## 3. 本地运行

```powershell
cd D:\code\folo-telegram-mvp
python app.py serve
```

健康检查：

```powershell
Invoke-RestMethod http://localhost:8080/health
```

发送一条测试消息到 Telegram：

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:replace_me"
$env:TELEGRAM_CHAT_ID="@your_channel_name"
python app.py test
```

单次轮询 RSS/RSSHub，不发送 Telegram，只打印解析和评分：

```powershell
python app.py poll-once --dry-run
```

单次真实轮询，会走 SQLite 去重、打分和 Telegram 推送：

```powershell
python app.py poll-once
```

长期轮询：

```powershell
python app.py poll
```

## 4. Docker 运行

```powershell
cd D:\code\folo-telegram-mvp
Copy-Item .env.example .env
docker compose up -d --build
```

服务会监听：

```text
http://localhost:8080
```

如果你想在 Docker 里运行轮询模式，可以先单次试跑：

```bash
docker compose run --rm folo-telegram-mvp python app.py poll-once --dry-run
```

长期生产环境可以启用内置的 `poll` profile。它会启动一个单独的轮询容器，和 webhook 服务共用同一个 `data/radar.sqlite`：

```bash
docker compose --profile poll up -d --build
```

如果你不再需要 Folo Webhook，只想跑轮询，也可以只启动轮询服务：

```bash
docker compose --profile poll up -d --build folo-telegram-poll
```

## 4.1 Poll 模式配置

默认配置不会启用任何轮询源，避免误推无关内容。需要使用 Poll 模式时，先在 `feeds` 里配置确认可访问的 RSS/Atom 源。公共 `rsshub.app` 的 X/Twitter 路由可能因为上游登录配置不可用而返回 404；如果你有自己的 RSSHub 实例，并且 X route 已配置好，可以把 `poll.auto_from_accounts` 改成 `true`，服务会自动根据 `accounts` 生成 RSSHub Twitter 源：

```text
https://rsshub.app/twitter/user/karpathy
https://rsshub.app/twitter/user/sama
```

如果你有自己的 RSSHub 实例，把 `poll.rsshub_base` 改成你的域名：

```json
{
  "poll": {
    "rsshub_base": "https://rsshub.your-domain.com"
  }
}
```

也可以在 `feeds` 里手动添加任意 RSS/Atom 源：

```json
{
  "feeds": [
    {
      "title": "OpenAI Python Releases",
      "url": "https://github.com/openai/openai-python/releases.atom",
      "siteUrl": "https://github.com/openai/openai-python"
    }
  ]
}
```

轮询频率和每个源最多处理的新条目数：

```json
{
  "poll": {
    "interval_seconds": 300,
    "max_items_per_feed": 20,
    "skip_existing_on_first_run": true,
    "fetch_timeout_seconds": 20
  }
}
```

`skip_existing_on_first_run` 为 `true` 时，某个 feed 第一次被轮询时只会把当前已有条目写入基线，不会把历史内容全部推送到 Telegram；从下一轮开始才推送新条目。

也可以用环境变量覆盖轮询间隔：

```bash
POLL_INTERVAL_SECONDS=180 python app.py poll
```

## 5. 暴露公网 Webhook

Folo 需要访问你的 webhook URL。你可以选择：

- VPS + 域名 + HTTPS
- Cloudflare Tunnel
- ngrok
- frp

最终 URL 形态：

```text
https://your-domain.example.com/webhook/你的_WEBHOOK_SECRET
```

如果用 Cloudflare Tunnel，映射到本机：

```text
http://localhost:8080
```

## 6. Folo Free 版设置

1. 在 Folo 添加最多 30 个 RSSHub/X 订阅源。

   示例源：

   ```text
   https://rsshub.app/twitter/user/karpathy
   https://rsshub.app/twitter/user/sama
   ```

   更稳定的方式是使用你自己的 RSSHub 实例。

2. 创建一个 Action。

3. Action 条件选择“指定条件”，不要使用“全部”。推荐先按订阅源分类过滤，例如：

   ```text
   订阅源分类 等于 AI
   ```

   这样 Folo 会把这条规则稳定保存下来，并且只把目标分类的新条目推到 Webhook。

4. Action 动作选择 Webhook。

5. Webhook URL 填：

   ```text
   https://your-domain.example.com/webhook/你的_WEBHOOK_SECRET
   ```

6. 保存后，Folo 有新 entry 时就会 POST 到本服务。

## 7. 去重逻辑

SQLite 文件位置：

```text
data/radar.sqlite
```

优先使用 Folo payload 里的：

```text
entry.guid
entry.id
entry.url
```

都没有时，用 `feed_url + title + published_at` 生成 hash。

## 8. Webhook Payload

Folo Webhook 会发送类似结构：

```json
{
  "entry": {
    "id": "entry-id",
    "publishedAt": "2026-04-29T10:00:00.000Z",
    "title": "post title",
    "description": "post description",
    "author": "author",
    "url": "https://x.com/user/status/..."
  },
  "feed": {
    "url": "https://rsshub.app/twitter/user/user",
    "siteUrl": "https://x.com/user",
    "title": "Twitter @user"
  },
  "view": 0
}
```

本服务只依赖 `entry` 和 `feed` 的常见字段。

## 9. MVP 建议参数

```json
{
  "filter": {
    "min_score": 8
  },
  "limits": {
    "max_pushes_per_hour": 20
  }
}
```

第一周建议打开：

```json
"include_debug": true
```

这样 Telegram 消息底部会显示 `score` 和命中原因，方便你调权重。稳定后再改成 `false`。

## 10. 免费版边界

这个 MVP 刻意遵守 Free 版边界：

- RSSHub/X 源控制在 30 个以内。
- 只用 1 个 Folo Action。
- AI 摘要不作为依赖，因为 Free 版每天只有 3 次。
- 不使用 X API。

如果以后想扩展到 100 到 150 个 X 账号，需要升级 Folo Basic，或者绕开 Folo，自己用 RSSHub 轮询。

## 11. Cloudflare Tunnel 实施步骤

Cloudflare Tunnel 的作用是把本机服务：

```text
http://localhost:8080
```

映射成一个公网 HTTPS 地址：

```text
https://folo-radar.your-domain.com
```

然后 Folo Webhook 填：

```text
https://folo-radar.your-domain.com/webhook/你的_WEBHOOK_SECRET
```

### 方案 A：临时测试，不需要域名

适合先验证 Folo 能不能打到你的本机服务。

1. 启动本项目：

```powershell
python app.py serve
```

2. 另开一个 PowerShell，运行 quick tunnel：

```powershell
cloudflared tunnel --url http://localhost:8080
```

3. 终端会输出一个随机域名，形如：

```text
https://xxxx.trycloudflare.com
```

4. 在 Folo Action 的 Webhook URL 填：

```text
https://xxxx.trycloudflare.com/webhook/你的_WEBHOOK_SECRET
```

注意：quick tunnel 适合测试，不适合长期稳定运行。

### 方案 B：正式使用，绑定自己的域名

前提：

- 你有 Cloudflare 账号。
- 你有一个已经接入 Cloudflare 的域名。
- 本机或服务器能长期运行 `cloudflared` 和本项目。

1. 登录 Cloudflare：

```powershell
cloudflared tunnel login
```

浏览器会打开 Cloudflare 登录页，选择你的域名授权。

2. 创建 tunnel：

```powershell
cloudflared tunnel create folo-telegram-radar
```

记下输出里的 tunnel UUID。

3. 创建 DNS 路由：

```powershell
cloudflared tunnel route dns folo-telegram-radar folo-radar.your-domain.com
```

把 `folo-radar.your-domain.com` 换成你自己的子域名。

4. 找到 `cloudflared` 配置目录。

Windows 通常在：

```text
C:\Users\你的用户名\.cloudflared
```

5. 新建或编辑 `config.yml`：

```yaml
tunnel: folo-telegram-radar
credentials-file: C:\Users\你的用户名\.cloudflared\你的-tunnel-uuid.json

ingress:
  - hostname: folo-radar.your-domain.com
    service: http://localhost:8080
  - service: http_status:404
```

6. 启动本项目：

```powershell
python app.py serve
```

7. 运行 tunnel：

```powershell
cloudflared tunnel run folo-telegram-radar
```

8. 测试公网健康检查：

```powershell
Invoke-RestMethod https://folo-radar.your-domain.com/health
```

应该返回：

```json
{
  "ok": true,
  "time": "..."
}
```

9. 在 Folo Action Webhook URL 填：

```text
https://folo-radar.your-domain.com/webhook/你的_WEBHOOK_SECRET
```

### 作为 Windows 服务长期运行

如果你希望电脑重启后 tunnel 自动恢复，可以安装为服务：

```powershell
cloudflared service install
```

本项目本身也需要长期运行。更稳的方式是部署到 VPS，然后用 Docker Compose：

```powershell
docker compose up -d --build
```

Cloudflare Tunnel 只负责把公网 HTTPS 请求转发到服务所在机器的 `localhost:8080`。

## 12. VPS + 域名 + HTTPS 实施步骤

这个方案适合长期运行。推荐结构：

```text
Folo Webhook
  -> https://folo-radar.your-domain.com
  -> Caddy 自动 HTTPS
  -> http://127.0.0.1:8080
  -> folo-telegram-mvp
  -> Telegram Channel
```

### 12.1 准备条件

你需要：

- 一台 VPS，推荐 Ubuntu 22.04 或 24.04。
- 一个域名，例如 `your-domain.com`。
- 一个子域名，例如 `folo-radar.your-domain.com`。
- Telegram Bot token 和 chat id。

### 12.2 配置 DNS

到你的域名 DNS 控制台添加一条 A 记录：

```text
类型: A
名称: folo-radar
值: 你的 VPS 公网 IP
TTL: Auto
```

等待 DNS 生效后，在本机测试：

```powershell
nslookup folo-radar.your-domain.com
```

能解析到 VPS IP 即可。

### 12.3 登录 VPS

```powershell
ssh root@你的VPS公网IP
```

### 12.4 安装 Docker

Ubuntu 上可以这样安装：

```bash
apt update
apt install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

确认：

```bash
docker --version
docker compose version
```

### 12.5 上传项目到 VPS

在 VPS 上创建目录：

```bash
mkdir -p /opt/folo-telegram-mvp
```

把本地 `D:\code\folo-telegram-mvp` 目录上传到 VPS 的：

```text
/opt/folo-telegram-mvp
```

可以用 Git、SCP、SFTP，或者先压缩再上传。

如果用 `scp`，在 Windows PowerShell 里执行：

```powershell
scp -r D:\code\folo-telegram-mvp root@你的VPS公网IP:/opt/
```

### 12.6 配置环境变量

在 VPS 上：

```bash
cd /opt/folo-telegram-mvp
cp .env.example .env
nano .env
```

填入：

```env
TELEGRAM_BOT_TOKEN=你的_bot_token
TELEGRAM_CHAT_ID=-1003992742615
WEBHOOK_SECRET=换成一个长随机字符串
```

生成随机 secret：

```bash
openssl rand -hex 24
```

### 12.7 启动 MVP 服务

```bash
cd /opt/folo-telegram-mvp
docker compose up -d --build
```

确认容器运行：

```bash
docker compose ps
```

本机健康检查：

```bash
curl http://127.0.0.1:8080/health
```

应该返回：

```json
{"ok":true,"time":"..."}
```

### 12.8 安装 Caddy 自动 HTTPS

Caddy 会自动申请和续期 Let's Encrypt 证书。

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" > /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy
```

编辑 Caddy 配置：

```bash
nano /etc/caddy/Caddyfile
```

写入：

```caddyfile
folo-radar.your-domain.com {
    reverse_proxy 127.0.0.1:8080
}
```

检查并重载：

```bash
caddy fmt --overwrite /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

### 12.9 配置防火墙

只开放 SSH、HTTP、HTTPS：

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
ufw status
```

`docker-compose.yml` 已经把应用端口绑定到本机：

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

所以公网不能直接访问 `8080`，只能走 Caddy 的 HTTPS。

### 12.10 测试 HTTPS

在本机或 VPS 上测试：

```bash
curl https://folo-radar.your-domain.com/health
```

成功后，Folo Webhook URL 填：

```text
https://folo-radar.your-domain.com/webhook/你的_WEBHOOK_SECRET
```

### 12.11 运维命令

查看日志：

```bash
cd /opt/folo-telegram-mvp
docker compose logs -f
```

重启服务：

```bash
docker compose restart
```

更新配置后重启：

```bash
docker compose up -d --build
```

查看 Caddy 日志：

```bash
journalctl -u caddy -f
```

### 12.12 常见问题

如果 HTTPS 证书申请失败，通常是：

- DNS 还没生效。
- VPS 防火墙没开放 80 或 443。
- 云厂商安全组没开放 80 或 443。
- Caddyfile 里的域名写错。

如果 Folo 调用失败，先测试：

```bash
curl https://folo-radar.your-domain.com/health
```

再确认 Folo URL 末尾包含正确的 `WEBHOOK_SECRET`。

### 12.13 使用项目内部署脚本

本项目也提供了一个部署脚本：

```text
scripts/deploy-vps.sh
```

针对你的域名，可以这样使用：

```bash
cd /opt/folo-telegram-mvp
chmod +x scripts/deploy-vps.sh
DOMAIN=folo-radar.zaishijizhidan.dpdns.org scripts/deploy-vps.sh
```

脚本会完成：

- 安装 Docker。
- 启动 `folo-telegram-mvp` 容器。
- 安装 Caddy。
- 写入 `/etc/caddy/Caddyfile`。
- 开放 `22`、`80`、`443` 防火墙端口。
- 测试本机 `http://127.0.0.1:8080/health`。
