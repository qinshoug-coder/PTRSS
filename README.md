# PTRSS

PTRSS 是一个面向 PT RSS 场景的轻量管理面板，主要提供：

- PT 站点 RSS / Cookie 配置
- 下载器配置
- Telegram 推送配置
- Web 管理界面
- 后台轮询与状态查看

## 这是什么仓库

这个仓库保存的是 **可同步、可部署、可继续开发的源码版本**，不是你现网运行目录的原样打包。

为了避免把敏感信息带进 GitHub，仓库里默认 **不会包含**：

- 真实 PT 站点 Cookie
- 真实 RSS passkey / 私有参数
- 真实 Telegram Bot Token / Chat ID
- 真实下载器地址 / 账号 / 密码
- 运行日志、状态文件、会话文件

也就是说：

- **仓库** 负责保存源码、部署结构、示例配置、版本历史
- **现网运行目录** 负责保存你的真实配置和运行状态

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

建议正式部署时，把 `PTRSS_WEB_PASSWORD` 改成你自己的强密码。

## 4. 修改 `data/config.json`

把你自己的真实配置填进去，例如：

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
data/                     运行数据目录（真实运行文件不进仓库）
docker-compose.yml        部署文件
.env.example              环境变量示例
```

---

# 首次部署要改的内容

## `.env`

建议至少检查这几项：

- `TZ`
- `PTRSS_WEB_PORT`
- `PTRSS_PUBLIC_BASE_URL`
- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

## `data/config.json`

你需要换成自己的真实值：

- `telegram.bot_token`
- `telegram.chat_id`
- 每个站点的 `rss_url`
- 每个站点的 `cookie`
- 每个下载器的 `url`
- 每个下载器的 `username`
- 每个下载器的 `password`

---

# 常见问题

## 1. 为什么仓库里没有真实配置？

因为这些内容包含敏感信息，不适合进入 GitHub。

## 2. 仓库更新后，现网会自动变吗？

不会。

PTRSS 当前应该按两层理解：

- **仓库版**：代码、文档、示例配置
- **现网版**：真实配置、真实运行数据、实际容器

仓库推送成功，不等于现网已经更新。

## 3. 改了宿主机文件，为什么现网没变？

因为 PTRSS 当前运行时是容器内 `/app/app.py` 在工作。  
宿主机源码改完后，如果要上线到现网，通常还需要：

1. 把代码同步到容器里
2. 重启 `ptrss` 容器
3. 再验证网页 / Telegram 实际表现

## 4. 这仓库适合直接公开吗？

可以，但前提是你已经确认：

- 没有真实 cookie
- 没有真实 token
- 没有真实下载器密码
- 没有真实状态文件 / 会话文件

目前这个仓库已经按这个方向做了基础脱敏。

---

# 建议工作流

日后建议按这套来：

## 只整理仓库，不上线现网

适用于：

- 调 README
- 补 `.gitignore`
- 清理目录结构
- 做示例配置
- 整理代码可读性

这种改动只需要：

```bash
git add .
git commit -m "your message"
git push
```

## 需要上线到现网

适用于：

- 改业务逻辑
- 改 Web 行为
- 改 Telegram 推送逻辑
- 改轮询 / 下载流程

这类改动应该分两步：

1. 先进入仓库
2. 再单独确认是否同步到现网

---

# 同步规则

更详细的仓库版 / 现网版同步规则，见：

- `SYNC_RULES.md`
