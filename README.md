# Vanora 财务管理（Team Ledger）

[![Docker build](https://github.com/VANORA-Team/team-ledger/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/VANORA-Team/team-ledger/actions/workflows/docker-publish.yml)

面向小团队的自托管多端共同账本（Web + 飞书 / Lark + Telegram + AI 记账助手，原名「一起记」）。基于 Python 3.12、FastAPI 与 SQLite，无前端构建步骤，不需要 Redis 或外部数据库。

成员认证与权限统一由 **Vanora OAuth（OpenID Connect）** 集中管理，通过 `team-ledger` 客户端角色按 **账本所有者（`owner`）/ 财务管理员（`finance`）/ 普通成员（`member`）** 授权；出账区分为 **公账支出**（入账即扣款）与 **成员垫付**（记账后待报销、实际转账后再登记报销），无审核流程与分类字段，聚焦小团队真实资金流转。

## 核心特性

- **Vanora OAuth 统一认证与集中权限管理**：接入 Vanora-oauth（OpenID Connect + PKCE `S256` + `RS256` JWKS 验签 + OIDC Client Roles + Back-channel Logout + 只读权限同步接口），兼容无邮箱（手机号 / 微信 / QQ）账号；成员、角色（`owner` / `finance` / `member`）、账号启停与管理审计日志全部在 Vanora 管理后台维护，账本内「团队成员」为只读同步名录，保留个人显示名称自定义与直达 Vanora 账户中心。
- **轻量 Web 工作台**：零构建响应式界面（浅色 Vanora 工作区风格），包含总览、收支明细、报销管理、资金计算、团队成员、机器人与 AI、团队设置 7 个视图；支持 `#entry/<id>` 详情直达链接，PNG / JPG / PDF 凭证弹窗内预览与下载，以及飞书内置浏览器 / 工作台嵌入适配。
- **公账支出、成员垫付与多币种报销**：支持团队收入、公账支出、成员垫付，整数最小单位精确记账；允许逐笔或批量部分报销，可按原记账货币或团队结算货币结算（30 分钟汇率报价锁定），并支持带 UTF-8 BOM 与公式注入防护的 CSV 导出。
- **资金规划与实时参考汇率**：默认接入免 Key 的 Frankfurter 汇率 API（本地缓存），支持自动安排、指定报销预算、手动选择报销项目及模拟未入账新收入，直观计算可用资金、结余与缺口。
- **Web 配置的 AI 自然语言记账**：在 Web「机器人与 AI」配置任意 OpenAI 兼容接口（含本地 Ollama / vLLM），保存即同时生效于 Web、飞书与 Telegram；支持一句话拆解多笔新增、修改、作废、报销与查询，先核对勾选清单再原子事务执行。
- **飞书 / Lark 与 Telegram 机器人**：飞书支持长连接、中国版 / 国际版 / 私有化域名、原地刷新交互卡片、富文本与群聊 `@机器人`；Telegram 支持长轮询菜单、命令与代理。两端共享同一套权限校验、资金计算与一键报销流程。
- **完整备份与定时恢复**：支持服务器本地、WebDAV、S3 兼容存储桶手动与定时自动备份，采用 SQLite Online Backup + SHA-256 清单校验与恢复前保护副本，并兼容恢复旧版密码认证时代的备份数据库。

## 快速开始

### 准备 Vanora OAuth 客户端与角色

Web 登录与权限管理均依赖 Vanora OAuth（`vanora` realm）。推荐直接使用 `Vanora-oauth` 仓库提供的 `configure-vanora.py --team-ledger-url <PUBLIC_URL>` 一键幂等创建 `team-ledger` 机密 OIDC 客户端（自动开启 Standard Flow、PKCE `S256`、Backchannel Logout、`vanora.permissions.sync.enabled=true`、创建 `owner` / `finance` / `member` 客户端角色并配置 `resource_access.<client_id>.roles` ID Token 映射与 Admin Events 审计）：

- **Valid redirect URI**：`<PUBLIC_URL>/api/auth/oidc/callback`
- **Backchannel logout URL**：`<PUBLIC_URL>/api/auth/oidc/backchannel-logout`

在 Vanora 私有管理后台为用户（或用户组）分配 `team-ledger` 客户端角色（`owner` / `finance` / `member`），并在 `.env` 中填入 `VANORA_OIDC_ISSUER`、`VANORA_OIDC_CLIENT_ID` 与 `VANORA_OIDC_CLIENT_SECRET`（未配置时服务仍可启动，首页会展示当前实例的回调地址与配置指引）。完整配置与安全说明见 [部署、环境变量与 CI/CD](docs/deployment.md)。

### 方式一：Docker Compose（推荐）

使用 GitHub Actions 自动发布的多架构镜像（`linux/amd64` / `linux/arm64`）：

```sh
cp .env.example .env
# 编辑 .env，配置 VANORA_OIDC_*（公网部署同时配置 PUBLIC_URL 与 COOKIE_SECURE=true）
docker compose pull
docker compose up -d --no-build

# 按需启用机器人 worker（可单独或同时启用）
docker compose --profile feishu up -d --no-build
docker compose --profile telegram up -d --no-build
```

默认仅监听宿主机 `127.0.0.1:8765`。更多镜像版本锁定、源码构建、反向代理与环境变量说明见 [部署、环境变量与 CI/CD](docs/deployment.md)。

### 方式二：本地 Python 启动

需要 **Python 3.12+**。首次启动无预设账号或示例数据，配置好 `.env` 中的 `VANORA_OIDC_*` 并在 Vanora OAuth 为账号分配 `owner` / `finance` / `member` 客户端角色后，打开 **http://127.0.0.1:8765** 使用 Vanora 账号登录即可自动同步进入账本（`owner` 首次空库登录时可顺便设定团队名称）。

**Windows PowerShell：**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
# 在 .env 中填写 VANORA_OIDC_ISSUER / VANORA_OIDC_CLIENT_ID / VANORA_OIDC_CLIENT_SECRET
.\.venv\Scripts\python run.py
```

**Linux / macOS：**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# 在 .env 中填写 VANORA_OIDC_ISSUER / VANORA_OIDC_CLIENT_ID / VANORA_OIDC_CLIENT_SECRET
.venv/bin/python run.py
```

如需同时运行机器人，另开终端执行 `python -m app.feishu` 或 `python -m app.bot`。

## 角色与权限概览

Web、飞书、Telegram 与 AI 记账助手共用同一套权限规则：

| 角色 | 核心权限 |
| --- | --- |
| **普通成员（`member`）** | 查看团队流水与资金汇总；新增、编辑、提交、作废本人未报销的「成员垫付」及上传凭证；修改个人显示名称；进行只读资金试算。 |
| **财务管理员（`finance`）** | 拥有普通成员权限；管理全团队账目，登记「团队收入」与「公账支出」，登记单笔 / 批量报销付款，永久删除已作废账目，导出 CSV，手动触发成员权限同步。 |
| **账本所有者（`owner`）** | 拥有财务管理员权限；配置团队设置、飞书应用、AI 引擎及数据备份与恢复，查看直达 Vanora 管理后台入口。 |

> 成员增删、角色分配、账号启停与权限审计日志全部在 Vanora OAuth 私有管理后台维护；角色变更、停用或删号会通过 Back-channel Logout 与 TTL 权限同步自动撤销 Web 会话及机器人/AI 待确认操作。详细权限矩阵见 [角色权限、账务规则与资金计算](docs/accounting-and-funds.md)。

## 文档导航

| 文档 | 内容说明 |
| --- | --- |
| [角色权限、账务规则与资金计算](docs/accounting-and-funds.md) | Vanora OAuth 客户端角色映射、三角色权限矩阵、公账支出与成员垫付规则、部分 / 跨币种报销与汇率 API |
| [AI 自然语言记账助手](docs/ai-assistant.md) | Web 端 AI 引擎配置与测试、自然语言记账能力、待确认清单安全规则与隐私边界 |
| [飞书 / Lark 与 Telegram 机器人](docs/bots.md) | 飞书长连接与工作台嵌入、Telegram 接入与命令清单、群聊机制、机器人资金计算与一键报销 |
| [部署、环境变量与 CI/CD](docs/deployment.md) | Vanora OAuth 客户端登记、本地与 Docker 部署、旧版迁移、反向代理安全建议与环境变量表 |
| [数据存储、备份与恢复](docs/backup-and-restore.md) | 数据目录说明、本地 / WebDAV / S3 备份、定时计划、ZIP 校验、旧版备份恢复与凭据边界 |
| [项目结构与开发测试](docs/development.md) | 完整模块目录、自动化测试、前端语法检查与关键工程设计说明 |

## 测试与验证

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --check public/app.js && node --check public/backups.js
```

自动测试使用独立临时 SQLite 数据库、内存 RS256 OIDC 测试桩与模拟外部接口，不消耗真实 AI 额度，也不会请求真实 Vanora OAuth、飞书或 Telegram 服务。完整目录与工程约定见 [项目结构与开发测试](docs/development.md)。
