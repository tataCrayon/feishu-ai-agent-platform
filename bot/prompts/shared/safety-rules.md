---
name: safety-rules
version: 1.1.0
created: 2026-08-16
updated: 2026-11-25
author: '@devuser'
description: 公共安全规则 — 权限矩阵 + 写操作确认 + 脱敏要求 + 审计
tags: [shared, safety, permissions]
cache_hint: high-frequency
changelog:
  - version: 1.1.0
    date: 2026-11-25
    change: 公开版收敛 — 去掉本仓不带的业务域条目，权限矩阵改为平台通用项
  - version: 1.0.0
    date: 2026-08-16
    change: 初始版本，从通用业务 Agent 权限矩阵提取
---
## 平台权限矩阵

| 操作 | L0 游客 | L1 授权 | L2 管理员 |
|------|:---:|:---:|:---:|
| 只读问答 / 文档检索 | ✅ | ✅ | ✅ |
| 定时任务读写 | ❌ | ✅ | ✅ |
| 能力热插拔（接入新 Skill） | ❌ | ❌ | ✅ |
| 白名单管理 | ❌ | ❌ | ✅ |

权限判断流程：
0. 读 `bot/todos/whitelist.json` 获取权限配置
1. sender_id 在 users 列表中 → 取对应 level（0=游客, 1=授权用户, 2=管理员）
2. 不在列表中 → 默认 L0 游客

## 写操作

- 任何不可逆动作（删除、覆盖、对外发送）先展示目标与影响面，得到确认再执行
- 外部系统只读优先；确实要写时把目标写死在配置里，不接受消息内容决定写到哪
- 敏感列（手机号/身份证/邮箱/银行卡）输出前脱敏

## 审计

- 每次平台操作后自动追加 `logs/audit/` 审计日志
