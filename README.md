# Vanora 财务管理（Team Ledger）

[![Docker build](https://github.com/VANORA-Team/team-ledger/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/VANORA-Team/team-ledger/actions/workflows/docker-publish.yml)

面向小团队的自托管多端共同账本（Web + 飞书 / Lark + Telegram + AI 记账助手，原名「一起记」）。基于 Python 3.12、FastAPI 与 SQLite，无前端构建步骤，不需要 Redis 或外部数据库。

成员使用独立账号，按 **账本所有者 / 财务管理员 / 普通成员** 分配权限；出账区分为 **公账支出**（入账即扣款）与 **成员垫付**（记账后待报销、实际转账后再登记报销），无审核流程与分类字段，聚焦小团队真实资金流转。

## 核心特性

- **轻量 Web 工作台**：零构建响应式界面（浅色 Vanora 工作区风格），包含总览、收支明细、报销管理、资金计算、团队成员、机器人与 AI、团队设置 7 个视图；支持 `#entry/<id>` 详情直达链接，PNG / JPG / PDF 凭证弹窗内预览与下载，以及飞书内置浏览器 / 工作台嵌入适配。
- **公账支出、成员垫付与多币种报销**：支持团队收入、公账支出、成员垫付，整数最小单位精确记账；允许逐笔或批量部分报销，可按原记账货币或团队结算货币结算（30 分钟汇率报价锁定），并支持带 UTF-8 BOM 与公式注入防护的 CSV 导出。
- **资金规划与实时参考汇率**：默认接入免 Key 的 Frankfurter 汇率 API（本地缓存），支持自动安排、指定报销预算、手动选择报销项目及模拟未入账新收入，直观计算可用资金、结余与缺口。
- **Web 配置的 AI 自然语言记账**：在 Web「机器人与 AI」配置任意 OpenAI 兼容接口（含本地 Ollama / vLLM），保存即同时生效于 Web、飞书与 Telegram；支持一句话拆解多笔新增、修改、作废、报销与查询，先核对勾选清单再原子事务执行。
- **飞书 / Lark 与 Telegram 机器人**：飞书支持长连接、中国版 / 国际版 / 私有化域名、原地刷新交互卡片、富文本与群聊 `@机器人`；Telegram 支持长轮询菜单、命令与代理。两端共享同一套权限校验、资金计算与一键报销流程。
- **完整备份与定时恢复**：支持服务器本地、WebDAV、S3 兼容存储桶手动与定时自动备份，采用 SQLite Online Backup + SHA-256 清单校验与恢复前保护副本。

## 快速开始

### 方式一：Docker Compose（推荐）

使用 GitHub Actions 自动发布的多架构镜像（`linux/amd64` / `linux/arm64`）：

```sh
cp .env.example .env
# 按需编辑 .env（公网部署请配置 PUBLIC_URL 与 COOKIE_SECURE=true）
docker compose pull
docker compose up -d --no-build

# 按需启用机器人 worker（可单独或同时启用）
docker compose --profile feishu up -d --no-build
docker compose --profile telegram up -d --no-build
```

默认仅监听宿主机 `127.0.0.1:8765`。更多镜像版本锁定、源码构建、反向代理与环境变量说明见 [部署、环境变量与 CI/CD](docs/deployment.md)。

### 方式二：本地 Python 启动

需要 **Python 3.12+**。首次启动无预设账号或示例数据，打开 **http://127.0.0.1:8765** 创建首个「账本所有者」账号即可开始使用（密码不限长度，不能为空）。

**Windows PowerShell：**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python run.py
```

**Linux / macOS：**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python run.py
```

如需同时运行机器人，另开终端执行 `python -m app.feishu` 或 `python -m app.bot`。

## 角色与权限概览

Web、飞书、Telegram 与 AI 记账助手共用同一套权限规则：

| 角色 | 核心权限 |
| --- | --- |
| **普通成员** | 查看团队流水与资金汇总；新增、编辑、提交、作废本人未报销的「成员垫付」及上传凭证；进行只读资金试算。 |
| **财务管理员** | 拥有普通成员权限；管理全团队账目，登记「团队收入」与「公账支出」，登记单笔 / 批量报销付款，永久删除已作废账目，导出 CSV。 |
| **账本所有者** | 拥有财务管理员权限；管理成员账号、角色与成员管理记录，配置团队设置、飞书应用、AI 引擎及数据备份与恢复。 |

> 角色变更或停用账号会立即撤销对应成员的 Web 会话及所有待确认操作。详细权限矩阵与旧版升级规则见 [角色权限、账务规则与资金计算](docs/accounting-and-funds.md)。

## 文档导航

| 文档 | 内容说明 |
| --- | --- |
| [角色权限、账务规则与资金计算](docs/accounting-and-funds.md) | 三角色详细权限、公账支出与成员垫付规则、部分 / 跨币种报销、资金规划公式与汇率 API |
| [AI 自然语言记账助手](docs/ai-assistant.md) | Web 端 AI 引擎配置与测试、自然语言记账能力、待确认清单安全规则与隐私边界 |
| [飞书 / Lark 与 Telegram 机器人](docs/bots.md) | 飞书长连接与工作台嵌入、Telegram 接入与命令清单、群聊机制、机器人资金计算与一键报销 |
| [部署、环境变量与 CI/CD](docs/deployment.md) | 本地与 Docker Compose 部署、反向代理安全建议、完整环境变量表及 GHCR 自动发布规则 |
| [数据存储、备份与恢复](docs/backup-and-restore.md) | 数据目录说明、本地 / WebDAV / S3 备份、定时计划、ZIP 校验、恢复前保护副本与凭据边界 |
| [项目结构与开发测试](docs/development.md) | 完整模块目录、240 项自动化测试、前端语法检查与关键工程设计说明 |

## 测试与验证

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --check public/app.js && node --check public/backups.js
```

自动测试使用独立临时 SQLite 数据库与模拟外部接口，不消耗真实 AI 额度，也不会向飞书或 Telegram 发送消息。完整目录与工程约定见 [项目结构与开发测试](docs/development.md)。
