# 部署、环境变量与 CI/CD

[← 返回主 README](../README.md)

本文涵盖本地运行、Docker Compose 部署、HTTPS 反向代理安全建议、完整环境变量清单以及 GitHub Actions 自动镜像发布规则。

---

## 1. 本地运行

- **依赖环境**：Python **3.12** 或更新版本；无 Node、Redis 或外部数据库依赖。
- **核心进程**：
  - Web 服务（同时运行内置定时备份调度器）：`python run.py`（默认 `http://127.0.0.1:8765`）
  - 飞书 / Lark worker（可选，单库仅运行一个）：`python -m app.feishu`
  - Telegram worker（可选，单库仅运行一个）：`python -m app.bot`

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python run.py
```

### Linux / macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python run.py
```

首次启动打开 **http://127.0.0.1:8765** 创建第一个「账本所有者」账号；原有环境已装好依赖时直接执行 `python run.py` 即可。

---

## 2. Docker Compose 部署

仓库 `compose.yaml` 定义了 3 个共享 `ledger-data:/app/data` 命名卷的服务：`web`（默认）、`feishu`（profile `feishu`）与 `bot`（profile `telegram`），三者使用同一镜像，容器以非 root 用户 `ledger`（UID `10001`）运行。

### 2.1 使用 GHCR 预构建镜像（推荐）

镜像地址：`ghcr.io/vanora-team/team-ledger`（支持 `linux/amd64` 与 `linux/arm64`）。

```sh
cp .env.example .env
# 按需编辑 .env
docker compose pull
docker compose up -d --no-build

# 仅启用飞书机器人
docker compose --profile feishu up -d --no-build

# 仅启用 Telegram 机器人
docker compose --profile telegram up -d --no-build

# 同时启用飞书与 Telegram 机器人
docker compose --profile feishu --profile telegram up -d --no-build
```

**锁定特定版本**：在 `.env` 中指定 `LEDGER_IMAGE=ghcr.io/vanora-team/team-ledger:<版本标签>`，然后重新执行 `docker compose pull` 与 `docker compose up -d --no-build`。

### 2.2 从源码本地构建

```sh
cp .env.example .env
# 按需编辑 .env
docker compose up -d --build

# 按需附加机器人 profile
docker compose --profile feishu --profile telegram up -d --build
```

---

## 3. 公网反向代理与安全建议

- **先初始化再公网暴露**：`compose.yaml` 与 `run.py` 默认仅绑定 `127.0.0.1:8765`。上线新实例时，请先通过本机或 SSH 隧道完成首个账本所有者账号初始化，再开放公网域名，避免初始化页面被他人抢先注册。
- **HTTPS 反向代理**：使用 Nginx / Caddy 等在同机反代至 `127.0.0.1:8765`（`run.py` 默认信任 `127.0.0.1` 的代理头），并在 `.env` 中设置：
  ```dotenv
  PUBLIC_URL=https://你的记账域名
  COOKIE_SECURE=true
  ```
- **飞书工作台 iframe 嵌入**：如需在飞书工作台内嵌打开，同时设置 `FEISHU_EMBED=true`（或自定义 `FEISHU_FRAME_ANCESTORS`）。当 `COOKIE_SECURE=true` 且启用飞书嵌入时，`COOKIE_SAMESITE` 自动默认为 `none`，详见 [飞书工作台与内置浏览器嵌入](bots.md#13-飞书工作台与内置浏览器嵌入)。
- **单机 SQLite 约束**：SQLite WAL + 进程间文件锁面向单台服务器的小团队场景，**请勿将数据目录挂载到 NFS 或跨多台主机同时写入**。
- **接口文档与限流**：生产路由默认关闭 Swagger / ReDoc，启用严格 CSP、Origin 与 CSRF 校验、按 IP 滑动窗口限流以及常规请求 9 MB 体积上限（上传恢复备份使用独立的 `BACKUP_MAX_BYTES` 上限）。

---

## 4. 环境变量参考

完整模板见根目录 [`.env.example`](../.env.example)。

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1`（Docker 中为 `0.0.0.0`） | Web 服务监听地址 |
| `PORT` | `8765` | Web 服务监听端口 |
| `PUBLIC_URL` | `http://127.0.0.1:8765` | 对外访问完整 URL，用于 CSRF Origin 白名单及机器人详情直达链接 `#entry/<id>` |
| `COOKIE_SECURE` | `false` | 登录 Cookie 的 `Secure` 标记，HTTPS 反向代理下必须设为 `true` |
| `COOKIE_SAMESITE` | 自动（`lax`；`COOKIE_SECURE=true` 且开启飞书嵌入时自动为 `none`） | 可显式设为 `lax`、`strict` 或 `none`（设为 `none` 必须配合 `COOKIE_SECURE=true`） |
| `TEAM_TIMEZONE` | `Asia/Shanghai` | 账目默认日期、统计口径及自动备份调度所用时区 |
| `DATA_DIR` | `./data`（Docker 中为 `/app/data`） | SQLite 数据库、凭证、本地备份与锁文件存放目录 |
| `TELEGRAM_BOT_TOKEN` | 空 | Telegram BotFather 提供的 Token（仅通过环境变量配置） |
| `TELEGRAM_PROXY_URL` | 空 | 可选 HTTP 代理（如 `http://127.0.0.1:7890`），仅作用于 Telegram API |
| `FEISHU_APP_ID` | 空 | 飞书应用 App ID（Web「机器人与 AI」配置优先） |
| `FEISHU_APP_SECRET` | 空 | 飞书应用 App Secret（Web「机器人与 AI」配置优先） |
| `FEISHU_DOMAIN` | `feishu` | `feishu`（中国版）、`lark`（国际版）或自定义 `https://` 域名（Web 配置优先） |
| `FEISHU_EMBED` | `false` | 设为 `true` 允许飞书 / Lark 官方域名以 iframe 嵌入 Web |
| `FEISHU_FRAME_ANCESTORS` | 空 | 可选，空格分隔的自定义 `https://` `frame-ancestors` 白名单 |
| `FX_API_URL` | `https://api.frankfurter.app/latest` | 参考汇率接口地址，兼容 Frankfurter v1 与 v2 |
| `FX_CACHE_HOURS` | `12` | SQLite 汇率缓存有效期（小时） |
| `BACKUP_MAX_BYTES` | `268435456`（256 MiB） | 备份 ZIP 上传与远程下载单文件上限（解压上限为该值的 4 倍、最多 50,000 文件） |
| `LEDGER_IMAGE` | `ghcr.io/vanora-team/team-ledger:latest` | Docker Compose 拉取的镜像与版本标签 |

> **AI 配置说明**：AI 引擎现已在 Web「机器人与 AI」直接配置并存入数据库，`.env.example` 不再列出 `AI_*` 变量。旧部署残留的 `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` / `AI_RESPONSE_FORMAT` / `AI_TIMEOUT` / `AI_CONTEXT_ENTRIES` / `AI_CONTEXT_CHARS` / `AI_MAX_ACTIONS` 仅在 Web 尚未保存或清除前作为自动回退生效。

---

## 5. GitHub Actions 自动测试与发布规则

工作流位于 [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)：

1. **测试阶段（`test`）**：在 Python 3.12 环境下运行全部 `unittest` 测试，并使用 `node --check` 检查 `public/app.js` 与 `public/backups.js` 语法。
2. **构建冒烟与发布（`publish`，PR 不执行）**：先构建 `linux/amd64` 镜像并启动容器验证 `/api/health`、`/api/bootstrap`（确认空账本 `needs_setup == true`）及非 root UID `10001`，通过后使用仓库自带 `GITHUB_TOKEN` 构建并推送 `linux/amd64` + `linux/arm64` 双架构镜像至 GHCR（ARM64 构建基于 QEMU，不包含运行期冒烟验证）。
3. **匿名拉取验证（`verify-public-image`）**：在无登录凭据环境中校验多架构清单（确认包含 AMD64 与 ARM64）并验证匿名拉取。

### 自动标签规则

- 推送 `main` 分支：发布 `latest`、`main` 与完整提交 SHA 标签。
- 推送 `v1.2.3` 格式标签：发布 `1.2.3`、`1.2` 与完整提交 SHA 标签。
- Pull Request：仅运行测试与 JS 语法检查，不发布镜像。
- 支持在 GitHub Actions「Test and publish Docker image」页面手动触发（`workflow_dispatch`）。
