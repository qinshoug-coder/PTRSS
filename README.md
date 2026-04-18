# PTRSS

PTRSS 是一个面向 PT RSS 场景的轻量管理面板，提供：

- PT 站点 RSS / Cookie 配置
- 下载器配置
- Telegram 推送配置
- Web 管理界面
- 后台轮询与状态查看

## 功能概览

- 配置多个 PT 站点
- 配置多个下载器
- 配置 Telegram 推送
- 在 Web 页面中管理基础参数
- 查看轮询和运行状态

---

# 快速开始

## 1. 克隆仓库

```bash
git clone https://github.com/qinshoug-coder/PTRSS.git
cd PTRSS
```

## 2. 准备配置文件

```bash
cp .env.example .env
cp data/config.example.json data/config.json
```

## 3. 修改 `.env`

至少修改：

```env
PTRSS_PUBLIC_BASE_URL=http://YOUR_HOST:7790
PTRSS_WEB_USERNAME=admin
PTRSS_WEB_PASSWORD=change-me
```

## 4. 修改 `data/config.json`

按你的实际环境填写：

- Telegram Bot Token / Chat ID
- PT 站点 RSS URL / Cookie
- 下载器地址 / 用户名 / 密码

## 5. 启动

```bash
docker compose up -d --build
```

默认访问：

- `http://YOUR_HOST:7790`

登录账号密码来自 `.env`：

- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

---

# 目录结构

```text
app/                      应用代码
data/config.example.json  示例配置
data/                     运行数据目录
docker-compose.yml        部署文件
.env.example              环境变量示例
```

---

# 配置说明

## `.env`

建议至少检查：

- `TZ`
- `PTRSS_WEB_PORT`
- `PTRSS_PUBLIC_BASE_URL`
- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

## `data/config.json`

你需要按自己的环境填写：

- `telegram.bot_token`
- `telegram.chat_id`
- 每个站点的 `rss_url`
- 每个站点的 `cookie`
- 每个下载器的 `url`
- 每个下载器的 `username`
- 每个下载器的 `password`

---

# 常见问题

## 1. 页面打不开

先检查容器状态：

```bash
docker compose ps
```

再查看日志：

```bash
docker compose logs -f
```

## 2. 登录不上

请确认 `.env` 里的以下配置是否正确：

- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

如果修改过 `.env`，建议重启容器：

```bash
docker compose up -d --build
```

## 3. Telegram 测试失败

优先检查：

- `telegram.bot_token` 是否填写正确
- `telegram.chat_id` 是否填写正确
- 机器人是否已经和目标聊天建立过会话

## 4. 站点 RSS 拉取失败

优先检查：

- `rss_url` 是否正确
- `cookie` 是否有效
- 站点是否限制访问频率或登录状态

## 5. 下载器连接失败

优先检查：

- 下载器 `url` 是否可访问
- 用户名 / 密码是否正确
- 对应服务是否已经启动

---

# 安全提示

请不要把以下真实信息直接提交到公开仓库：

- PT 站点 Cookie
- Telegram Bot Token / Chat ID
- 下载器账号密码
- 运行日志与状态文件

建议仅在本地保存真实配置，并使用示例配置文件作为模板。
