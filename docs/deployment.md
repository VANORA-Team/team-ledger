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

首次启动前请先按第 3 节完成 Vanora OAuth 客户端与角色配置及 `.env` 填写，随后打开 **http://127.0.0.1:8765** 使用已分配客户端角色的 Vanora 账号登录即可自动同步进入账本；原有环境已装好依赖时直接执行 `python run.py` 即可。

---

## 2. Docker Compose 部署

仓库 `compose.yaml` 定义了 3 个共享 `ledger-data:/app/data` 命名卷的服务：`web`（默认）、`feishu`（profile `feishu`）与 `bot`（profile `telegram`），三者使用同一镜像，容器以非 root 用户 `ledger`（UID `10001`）运行。

### 2.1 使用 GHCR 预构建镜像（推荐）

镜像地址：`ghcr.io/vanora-team/team-ledger`（支持 `linux/amd64` 与 `linux/arm64`）。

```sh
cp .env.example .env
# 按需编辑 .env，填写 VANORA_OIDC_* 与 PUBLIC_URL 等配置
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

## 3. Vanora OAuth 客户端与集中权限接入

系统将**身份认证与权限管理**完整统一到 **Vanora OAuth（OpenID Connect）**，默认兼容无邮箱的纯手机号、微信与 QQ 账号（以 `(iss, sub)` 作为唯一身份标识，不强制 `email` 或 `acr=2`）。`team-ledger` 不再提供本地添加成员、邀请链接、修改角色或停用成员功能。

### 3.1 使用 `configure-vanora.py` 一键配置客户端与角色（推荐）

在 `Vanora-oauth` 部署环境中执行（幂等，可重复运行）：

```sh
python3 configure-vanora.py \
  --team-ledger-url https://ledger.example.com
```

该命令会自动完成以下配置：
1. 在 `vanora` realm 创建/更新 `team-ledger` **Confidential OIDC Client**（开启 Standard Flow、PKCE `S256`、Backchannel logout URL 及 `session required`，移除 `offline_access`）。
2. 设置客户端属性 `vanora.permissions.sync.enabled=true`，启用只读成员同步接口（`GET /realms/vanora/vanora-permissions/members` 与 `/members/{sub}`）及权限变更自动触发 OIDC Back-channel Logout。
3. 创建三个客户端角色：`owner`、`finance`、`member`。
4. 配置 OIDC Protocol Mapper（`team-ledger-client-roles`），将客户端角色写入 ID Token、Access Token 与 UserInfo 的 `resource_access.<client_id>.roles`。
5. 开启 `vanora` realm 的 Admin Events 审计日志。

如需手动在 Vanora 私有管理后台配置，需确保上述 5 项设置一致，并登记：
- **Valid redirect URIs**：`<PUBLIC_URL>/api/auth/oidc/callback`
- **Backchannel logout URL**：`<PUBLIC_URL>/api/auth/oidc/backchannel-logout`（开启 **Backchannel logout session required**）
- 如需在退出账本时联动跳转到 Vanora 全局退出页，另行登记 **Valid post logout redirect URIs**（`<PUBLIC_URL>`）并在 `.env` 中启用 `VANORA_OIDC_END_SESSION=true`。

随后在 Vanora 私有管理后台（`Users -> Role mapping` 或 `Groups -> Role mapping`）为用户分配 `team-ledger` 客户端角色（`owner` / `finance` / `member`），并在 `.env` 中填入：

```dotenv
VANORA_OIDC_ISSUER=https://<你的 Vanora OAuth 域名>/realms/vanora
VANORA_OIDC_CLIENT_ID=team-ledger
VANORA_OIDC_CLIENT_SECRET=<客户端密钥>
# 可选：在只读「团队成员」页为 owner 显示直达 Vanora 私有管理后台的入口按钮
# VANORA_OIDC_ADMIN_URL=https://<你的 Vanora 管理后台域名>/admin/vanora/console/
# 可选：权限同步缓存有效期（秒，默认 30；0 关闭后台定时轮询）
# VANORA_OIDC_PERMISSIONS_TTL=30
```

> 若服务端与 Vanora OAuth 处于同一内网 / Docker 网络，可通过 `VANORA_OIDC_INTERNAL_URL` 将服务端的 Discovery / Token / JWKS / `vanora-permissions` 请求走内网地址（浏览器跳转仍保持公网 `VANORA_OIDC_ISSUER`）；本地或私有 CA 证书环境可指定 `VANORA_OIDC_CA_FILE`。

### 3.2 实时撤销与旧版升级说明

- **实时权限同步**：在 Vanora 管理后台调整用户的客户端角色、所属用户组、启用状态或删除账号时，Vanora OAuth 的 `vanora-permission-sync` 监听器在事务提交后自动断开该用户在此客户端的会话并发送签名 OIDC Back-channel Logout（附带 `https://vanora.dev/events/permissions-changed` 或 `https://vanora.dev/events/account-deleted`），`team-ledger` 立即撤销 Web 会话、清理机器人/AI/报价待确认状态并回拉最新角色。即使网络抖动错过推送，Web、飞书与 Telegram 也会在 TTL 到期（默认 30 秒）后主动从 `vanora-permissions` 接口校验最新权限。
- **从旧版数据库升级或恢复**：已有历史账目与用户投影表会自动保留并通过 `init_db()` 迁移结构；所有现役成员只需在 Vanora 管理后台被分配对应的 `team-ledger` 客户端角色后直接登录，即可按 `(iss, sub)` 自动同步生效（若历史本地用户从未关联 `oidc_sub`，其历史账目仍作为只读历史记录完整保留）。

---

## 4. 公网反向代理与安全建议

- **先完成 Vanora 客户端角色授权再公网暴露**：`compose.yaml` 与 `run.py` 默认仅绑定 `127.0.0.1:8765`。只有被 Vanora 管理后台显式授予 `team-ledger` 客户端角色（`owner` / `finance` / `member`）的用户才可登录，未授权账号将被拒绝（`not_member`）。
- **HTTPS 反向代理**：使用 Nginx / Caddy 等在同机反代至 `127.0.0.1:8765`（`run.py` 默认信任 `127.0.0.1` 的代理头），并在 `.env` 中设置：
  ```dotenv
  PUBLIC_URL=https://你的记账域名
  COOKIE_SECURE=true
  ```
- **飞书工作台 iframe 嵌入**：如需在飞书工作台内嵌打开，同时设置 `FEISHU_EMBED=true`（或自定义 `FEISHU_FRAME_ANCESTORS`）。当 `COOKIE_SECURE=true` 且启用飞书嵌入时，`COOKIE_SAMESITE` 自动默认为 `none`，OIDC 流程绑定 Cookie（`ledger_oidc_flow`）也会自动使用 `SameSite=None` 以确保回调正常携带，详见 [飞书工作台与内置浏览器嵌入](bots.md#13-飞书工作台与内置浏览器嵌入)。
- **单机 SQLite 约束**：SQLite WAL + 进程间文件锁面向单台服务器的小团队场景，**请勿将数据目录挂载到 NFS 或跨多台主机同时写入**。
- **接口文档与限流**：生产路由默认关闭 Swagger / ReDoc，启用严格 CSP（含 `form-action 'self'`）、Origin 与 CSRF 校验、按 IP 滑动窗口限流以及常规请求 9 MB 体积上限（上传恢复备份使用独立的 `BACKUP_MAX_BYTES` 上限）。

---

## 5. 环境变量参考

完整模板见根目录 [`.env.example`](../.env.example)。

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1`（Docker 中为 `0.0.0.0`） | Web 服务监听地址 |
| `PORT` | `8765` | Web 服务监听端口 |
| `PUBLIC_URL` | `http://127.0.0.1:8765` | 对外访问完整 URL，用于 OIDC 回调与后端注销地址、CSRF Origin 白名单及机器人详情直达链接 `#entry/<id>` |
| `COOKIE_SECURE` | `false` | 登录 Cookie 的 `Secure` 标记，HTTPS 反向代理下必须设为 `true` |
| `COOKIE_SAMESITE` | 自动（`lax`；`COOKIE_SECURE=true` 且开启飞书嵌入时自动为 `none`） | 可显式设为 `lax`、`strict` 或 `none`（设为 `none` 必须配合 `COOKIE_SECURE=true`；OIDC 流程 Cookie 始终为 `lax` 或嵌入态 `none`） |
| `TEAM_TIMEZONE` | `Asia/Shanghai` | 账目默认日期、统计口径及自动备份调度所用时区 |
| `DATA_DIR` | `./data`（Docker 中为 `/app/data`） | SQLite 数据库、凭证、本地备份与锁文件存放目录 |
| `VANORA_OIDC_ISSUER` | 空 | Vanora OAuth 的 Realm Issuer 地址（如 `https://auth.example.com/realms/vanora`），启用 Web 登录必填 |
| `VANORA_OIDC_CLIENT_ID` | 空 | Vanora OAuth 中为本账本创建的 OIDC Client ID，启用 Web 登录必填 |
| `VANORA_OIDC_CLIENT_SECRET` | 空 | Confidential Client 密钥（与 `VANORA_OIDC_CLIENT_SECRET_FILE` 二选一） |
| `VANORA_OIDC_CLIENT_SECRET_FILE` | 空 | 可选密钥文件路径，当 `VANORA_OIDC_CLIENT_SECRET` 为空时从该文件读取 |
| `VANORA_OIDC_SCOPES` | `openid profile email` | OIDC 授权请求的 scope 列表（服务端会自动确保包含 `openid`） |
| `VANORA_OIDC_ADMIN_URL` | 空 | 可选，Vanora 私有管理后台 URL，仅对 `owner` 在只读「团队成员」页展示跳转按钮 |
| `VANORA_OIDC_PERMISSIONS_TTL` | `30` | 可选，Web 与机器人向 Vanora OAuth 同步权限的缓存秒数（设为 `0` 关闭 TTL 后台轮询，仅依赖登录与 Back-channel Logout） |
| `VANORA_OIDC_ROLES_CLAIM` | 空（默认 `resource_access.<client_id>.roles`） | 可选，自定义点分路径读取 ID Token 中的角色列表 |
| `VANORA_OIDC_ACR_VALUES` | 空 | 可选，要求特定认证强度（如设为 `2` 强制已开启 TOTP 的 LoA 2；留空兼容全部登录方式） |
| `VANORA_OIDC_INTERNAL_URL` | 空 | 可选，服务端拉取 Discovery / JWKS、换票与调用 `vanora-permissions` 时替换 `VANORA_OIDC_ISSUER` 的内网基地址 |
| `VANORA_OIDC_CA_FILE` | 空 | 可选，私有 / 本地 HTTPS Vanora OAuth 所用的 PEM CA 证书包路径 |
| `VANORA_OIDC_END_SESSION` | `false` | 设为 `true` 时退出登录同时跳转至 Vanora `end_session_endpoint`（需先登记 Post Logout Redirect URI） |
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

## 6. GitHub Actions 自动测试与发布规则

工作流位于 [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)：

1. **测试阶段（`test`）**：在 Python 3.12 环境下运行全部 `unittest` 测试，并使用 `node --check` 检查 `public/app.js` 与 `public/backups.js` 语法。
2. **构建冒烟与发布（`publish`，PR 不执行）**：先构建 `linux/amd64` 镜像并启动容器验证 `/api/health`、`/api/bootstrap`（确认空账本 `needs_setup == true`）及非 root UID `10001`，通过后使用仓库自带 `GITHUB_TOKEN` 构建并推送 `linux/amd64` + `linux/arm64` 双架构镜像至 GHCR（ARM64 构建基于 QEMU，不包含运行期冒烟验证）。
3. **匿名拉取验证（`verify-public-image`）**：在无登录凭据环境中校验多架构清单（确认包含 AMD64 与 ARM64）并验证匿名拉取。

### 自动标签规则

- 推送 `main` 分支：发布 `latest`、`main` 与完整提交 SHA 标签。
- 推送 `v1.2.3` 格式标签：发布 `1.2.3`、`1.2` 与完整提交 SHA 标签。
- Pull Request：仅运行测试与 JS 语法检查，不发布镜像。
- 支持在 GitHub Actions「Test and publish Docker image」页面手动触发（`workflow_dispatch`）。
