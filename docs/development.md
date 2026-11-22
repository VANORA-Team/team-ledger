# 项目结构与开发测试

[← 返回主 README](../README.md)

本文提供完整的仓库目录说明、本地测试与检查命令，以及关键工程实现约定，便于二次开发与维护。

---

## 1. 仓库目录与模块职责

### 根目录与部署文件

```text
run.py                               Web 服务启动入口（加载 .env 并启动 Uvicorn）
compose.yaml                         Docker Compose 编排（web / telegram / feishu 三服务）
Dockerfile                           Python 3.12-slim 非 root（UID 10001）镜像与健康检查
.env.example                         环境变量配置模板
requirements.txt                     运行期依赖（fastapi / uvicorn / tzdata / lark-oapi / boto3）
requirements-dev.txt                 测试附加依赖（httpx）
.github/workflows/docker-publish.yml CI 测试、JS 语法检查、AMD64 冒烟与双架构 GHCR 发布
docs/                                分主题详细文档
```

### 后端核心模块（`app/`）

```text
app/config.py               .env 加载、DATA_DIR 路径与 TEAM_TIMEZONE 时区解析
app/locks.py                跨进程文件锁（POSIX flock / Windows msvcrt），保护数据库与备份切换
app/db.py                   SQLite WAL 连接、27 张表定义、会话事务校验与启动自动幂等迁移
app/domain.py               PBKDF2 密码哈希、三角色权限校验、账目与部分报销核心规则
app/main.py                 FastAPI 应用、安全响应头、限流、认证、账目 / 成员 / 导出 / 集成 API 与 SPA 壳层

app/fx.py                   Frankfurter v1/v2 汇率拉取、SQLite 缓存与跨币种资金汇总
app/reimbursement_plan.py   报销计划模式（自动 / 预算 / 手动勾选）校验与计算
app/funds_plan.py           Web / 机器人 / AI 共用的按真实余额批量报销规划器

app/ai.py                   OpenAI 兼容 Chat Completions 客户端、DB/env 配置、连通测试与 JSON 修复
app/ai_context.py           面向 AI 的紧凑账本摘要与上下文裁剪构建器
app/ai_plan.py              AI 多动作干跑校验、30 分钟清单持久化、单事务执行与只读查询引擎

app/bot.py                  Telegram 长轮询循环、代理支持与持久回复队列
app/bot_ui.py               Telegram 与飞书共用的菜单、分页查询、可编辑草稿、详情与操作确认
app/bot_funds.py            Telegram 与飞书共用的 /funds 资金计算、模拟进账与一键报销交互

app/feishu.py               飞书 / Lark 进程守护、多区域域名、文本 / post 解析与持久收发队列
app/feishu_ui.py            飞书交互卡片渲染、im.v1.message.patch 原地刷新与 fsui_* 表隔离适配
app/feishu_ws.py            飞书长连接 WebSocket 卡片回传事件帧分发

app/backups.py              SQLite Online Backup ZIP 打包、SHA-256 清单校验、恢复回滚与内置 15s 调度器
app/backup_storage.py       WebDAV（Basic 认证）与 S3 兼容存储桶（boto3）读写适配器
app/backup_api.py           账本所有者专属的 /api/backups/* 备份恢复路由
```

### 前端零构建静态资源（`public/`）

```text
public/index.html           SPA 入口页面与 8 个原生 <dialog> 弹窗 / 抽屉（Vanora 财务管理）
public/app.js               7 个哈希路由视图、权限显隐、AI 助手抽屉、凭证预览与飞书 WebView 适配
public/style.css            浅色 Vanora 建筑工作区主题、响应式布局与安全区样式
public/backups.js           团队设置内嵌的数据备份、测试连接、自动计划与恢复前端逻辑
public/backups.css          备份与恢复面板样式
public/vanora-logo.png      品牌图标与页面 Favicon
public/fonts/               自托管 Space Grotesk Variable 与 IBM Plex Mono 字体文件（.woff2）
```

### 自动化测试（`tests/`）

共 **10 个测试模块、240 项测试用例**：

```text
tests/test_ledger.py              认证、CSRF、三角色 RBAC、成员日志、凭证预览/下载、嵌入安全头与导出
tests/test_ai_agent.py            Web AI 配置与测试、多动作规划、原子执行、权限过滤与机器人共享配置
tests/test_bot_ui.py              Telegram 菜单、分步草稿、角色限制、降级失效与防重确认
tests/test_backups.py             本地 / WebDAV / S3 备份、校验、自动调度、旧版备份恢复与失败回滚
tests/test_funds.py               汇率缓存/降级、可用资金、模拟进账与机器人资金规划
tests/test_expense_types.py       公账支出 vs 成员垫付、旧版分红自动迁移与余额口径
tests/test_feishu.py              飞书多区域配置、post 富文本、群聊、错误隔离与永久错误丢弃
tests/test_feishu_ui.py           飞书交互卡片样式、原地刷新、话题回复与 AI 卡片执行
tests/test_partial_payments.py    多币种部分报销、30 分钟汇率报价锁定与尾差控制
tests/test_reimbursement_plans.py 自动 / 指定预算 / 手动项目三种报销安排边界测试
```

---

## 2. 运行测试与语法检查

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --check public/app.js && node --check public/backups.js
```

测试说明：
- 所有测试使用独立临时目录和独立 SQLite 数据库（可通过 `LEDGER_TEST_TMP` 指定临时根目录），不会读写仓库下的 `data/`。
- 外部汇率、AI 接口、Telegram、飞书与远程存储均通过模拟对象验证，不依赖公网，也不消耗真实 API 额度。

---

## 3. 关键工程约定

- **静态资源零构建与自动防缓存**：前端不依赖打包工具，由 `GET /` 在返回 `index.html` 时为 `app.js`、`style.css`、`backups.js`、`backups.css` 自动附加 12 位 SHA-256 `?v=` 查询参数，避免升级后浏览器缓存旧脚本。
- **自动启动迁移**：不依赖外部迁移工具；Web、Telegram 与飞书进程启动及恢复备份后均自动调用 `init_db()`，在 `BEGIN IMMEDIATE` 事务中安全完成表结构与历史数据幂等升级。
- **外部 I/O 移出数据库写事务**：汇率拉取、AI HTTP 调用与机器人消息投递均在 SQLite 写事务之外执行，避免长耗时网络请求阻塞单机 SQLite 锁。
- **飞书 UI 隔离复用**：`app/feishu_ui.py` 通过将 `tg_*` 交互状态表映射至独立的 `fsui_*` 表并复用 `bot_ui.py` / `bot_funds.py`，确保飞书与 Telegram 交互功能完全对齐且状态互不干扰。
