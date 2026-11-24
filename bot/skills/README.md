# Skills — SKILL.md 快速接入目录

> 把标准 Claude Code Skill 丢进本目录，重启 bot 即上线为独立路由能力。

## 接入步骤（零 Python 代码）

```
bot/skills/
└── my-skill/            # 目录名即默认 skill 名
    └── SKILL.md         # 标准 Claude Code 格式
    └── (其他资源文件)    # 随目录一起同步给 Claude Code
```

然后重启：`python -m app.main`（若 cc-connect daemon 有长会话，建议一并 `cc-connect daemon restart`）。

## SKILL.md 格式

```markdown
---
name: my-skill              # 必填（缺省取目录名）；归一为 [a-z0-9_-]
description: 做什么 + 什么时候用。此文案直接用作路由工具描述，写清触发场景与关键词
permission_level: 0         # 可选，0=游客 / 1=授权 / 2=管理员（默认 0）
enabled: true               # 可选，false 时不上线
---
（正文：Claude Code 渐进加载的技能指令……）
```

## 上线后自动获得

- 路由工具 `route_to_skill_my_skill`（名字来自 name 字段）
- LangGraph 执行节点 + judge/recover 纠错闭环
- `/help` 能力清单展示、复杂任务分解参与资格
- 分发时以 `[use skill: my-skill]` 前缀触发 Claude Code 原生技能加载

## 注意

- `.claude/skills/` 是**生成物目录**（启动时从本目录复制同步），请勿手工编辑
- description 是路由依据，务必写成「能力关键词 + 触发场景」的路由级描述；
  缺 description 的 Skill 会被跳过并打 warning
- 新增 skill 后已存活的 Claude Code 会话感知不到，需重启 cc-connect daemon
