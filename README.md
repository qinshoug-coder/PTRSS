# PTRSS

PTRSS 是一个面向 PT RSS 场景的轻量管理面板，提供：

- 站点 RSS / Cookie 配置
- 下载器配置
- Telegram 推送配置
- Web 管理界面
- 后台轮询与状态查看

## 当前仓库说明

这个仓库保存的是 **可同步 / 可部署的源码版本**。

出于安全原因，以下内容不会进入仓库：

- 真实站点 Cookie
- 真实 Telegram Bot Token / Chat ID
- 真实下载器账号密码
- 运行日志、状态、会话文件

## 快速启动

```bash
git clone https://github.com/qinshoug-coder/PTRSS.git
cd PTRSS
cp .env.example .env
cp data/config.example.json data/config.json
# 按你的实际环境修改 .env 和 data/config.json

docker compose up -d --build
```

默认访问：

- `http://YOUR_HOST:7790`

默认登录来自 `.env`：

- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

## 目录说明

```text
app/                      应用代码
data/config.example.json  示例配置
data/                     运行数据目录（真实运行文件不进仓库）
docker-compose.yml        部署文件
.env.example              环境变量示例
```

## 首次使用要改的东西

### 1. `.env`

至少修改：

- `PTRSS_PUBLIC_BASE_URL`
- `PTRSS_WEB_USERNAME`
- `PTRSS_WEB_PASSWORD`

### 2. `data/config.json`

从 `data/config.example.json` 复制后，填写你自己的：

- Telegram Bot Token / Chat ID
- PT 站点 RSS URL / Cookie
- 下载器地址 / 用户名 / 密码

## 注意

- 仓库版不会自动带上你的现网配置
- 仓库代码更新后，也不会自动改动正在运行的 PTRSS 容器
- 如果要把仓库版上线到现网，需要单独部署或单独同步
