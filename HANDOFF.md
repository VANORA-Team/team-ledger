# 权限管理完整迁移至 Vanora-oauth · 交接说明

更新时间：2026-11-22  
涉及仓库：
- `C:\Users\suyu1\Documents\Github\Vanora-oauth`（身份与集中权限权威源）
- `C:\Users\suyu1\Documents\Github\team-ledger`（只读权限消费端）

## 架构摘要

将 `team-ledger` 的全部成员管理与权限控制完整迁移至 `Vanora-oauth`：

1. **权限模型（OIDC Client Roles）**：在 `vanora` realm 的 `team-ledger` 机密 OIDC 客户端下维护三个原生客户端角色 `owner`、`finance`、`member`（支持直接用户映射与用户组继承，多角色按 `owner > finance > member` 取最高权限），并由 Vanora 私有管理后台统一操作与记录 Admin Events 审计日志。
2. **Vanora-oauth 新增能力**：
   - `configure-vanora.py` 新增 `--team-ledger-url`（及 `--team-ledger-client-id` / `--team-ledger-backchannel-url` / `--team-ledger-post-logout-url` / `--team-ledger-only`），幂等创建客户端、3 个客户端角色、`team-ledger-client-roles` Protocol Mapper（写入 ID Token / Access Token / UserInfo 的 `resource_access.<client_id>.roles`）、客户端属性 `vanora.permissions.sync.enabled=true` 与 realm Admin Events。
   - `VanoraPermissionsResourceProviderFactory` 提供客户端认证的只读接口：`GET /realms/vanora/vanora-permissions/members` 与 `GET /realms/vanora/vanora-permissions/members/{subject}`（校验 `vanora` realm、机密客户端 Basic Auth 与 `vanora.permissions.sync.enabled=true` 属性，展开直赋、复合 realm 角色与用户组/子组角色，自动排除 service account，并对无该客户端角色用户隐藏 PII）。
   - `VanoraPermissionSyncEventListener`（全局 `EventListenerProviderFactory`）监听客户端角色映射、用户组成员、复合 realm 角色映射、客户端角色删除及用户启停/删除事件，在事务提交后通过 `enlistAfterCompletion` 断开受影响用户的在线与离线客户端会话，并发送签名 OIDC Back-channel Logout（附带 `https://vanora.dev/events/permissions-changed` 或 `https://vanora.dev/events/account-deleted`）。
   - `Caddyfile` 显式放行 `GET /realms/vanora/vanora-permissions/members` 与 `/members/*`。
3. **team-ledger 改造**：
   - 移除全部本地成员与权限管理接口与 UI（`POST /api/members`、`PATCH /api/members/{id}`、`/api/invites`、`/api/auth/invite`、`GET /api/member-events`、本地邀请/添加/编辑成员弹窗，以及 `VANORA_OIDC_OWNER_SUBJECT` / `VANORA_OIDC_OWNER_USER_ID`）。
   - 新增 `app/permissions_sync.py` 维护 SQLite `users` 只读投影（保留外键与个人 `PATCH /api/me` 自定义显示名称）、TTL 权限同步与权限降级/停用时的即时会话与待确认状态清理。
   - 「团队成员」页改造为只读成员名录，提供 `POST /api/members/sync`（`owner`/`finance`）与可选 `VANORA_OIDC_ADMIN_URL` 后台直达入口（仅 `owner` 可见）。
   - Web（`current()`）、Telegram（`app/bot.py`）与 Feishu（`app/feishu.py`）均接入 Back-channel Logout 即时失效 + TTL 周期回拉双保险。
