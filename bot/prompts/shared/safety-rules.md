---
name: safety-rules
version: 1.0.0
created: 2026-08-16
updated: 2026-08-16
author: '@devuser'
description: 公共安全规则 — RWA 权限矩阵 + SQL 安全拦截 + 脱敏要求
tags: [shared, safety, permissions]
cache_hint: high-frequency
changelog:
  - version: 1.0.0
    date: 2026-08-16
    change: 初始版本，从 rwa-bot 权限矩阵提取
---
## 平台权限矩阵

| 操作 | L0 游客 | L1 授权 | L2 管理员 |
|------|:---:|:---:|:---:|
| SQL SELECT | ✅ ≤20 行 | ✅ ≤200 行 | ✅ ≤2000 行 |
| SQL 写入 | ❌ | ❌ | ✅ |
| CMDB 查看 | ❌ | ✅ 只读 | ✅ |
| CMDB 发布 | ❌ | ❌ | ✅ 需确认 |
| Nacos 查看 | ❌ | ✅ 只读 | ✅ |
| Nacos 修改 | ❌ | ❌ | ✅ 需确认 |
| 白名单管理 | ❌ | ❌ | ✅ |

权限判断流程：
0. 读 `bot/todos/whitelist.json` 获取权限配置
1. sender_id 在 users 列表中 → 取对应 level（0=游客, 1=授权用户, 2=管理员）
2. 不在列表中 → 默认 L0 游客

## SQL 安全

- 强制拦截 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE/GRANT/REVOKE（L2 除外，拦截 DROP/TRUNCATE）
- L2 写操作前告知用户仅 UNDO 风险
- 敏感列（手机号/身份证/邮箱/银行卡）自动脱敏

## 发布安全

- 禁止 prod-k8s 环境自动发布，需管理员额外确认
- 发布前必须展示构建配置和环境信息供确认

## 审计

- 每次平台操作后自动追加 `logs/audit/` 审计日志