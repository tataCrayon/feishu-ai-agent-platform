---
name: help-reply
version: 2.3.0
created: 2026-08-01
updated: 2026-11-23
author: '@devuser'
description: help 帮助清单回复文本（备用模板，运行时由 _help_node 动态生成）
tags: [reply, help, capabilities]
cache_hint: low-frequency
token_estimate: 320
changelog:
  - version: 2.3.0
    date: 2026-11-23
    change: 清单里通用业务助手改名「开发助手」，与 agent 更名 dev-bot 同口径
  - version: 2.2.0
    date: 2026-10-07
    change: 清单新增「案例沉淀」（文档/记录号入库 + 飞书案例库镜像，与动态清单同口径）
  - version: 2.1.0
    date: 2026-09-17
    change: 清单新增「分支合并检查」（merge-check-agent 上线，与 dev-bot 秒回清单同口径）
  - version: 2.0.0
    date: 2026-09-14
    change: 自我介绍改「团队数字助手」，去 AgentMate/🧠；清单与 _help_node 动态格式对齐（简短单行列表）
  - version: 1.0.0
    date: 2026-08-01
    change: 初始版本，完整 AgentMate 能力清单
---
{greeting}！我是**团队数字助手**。

**我能帮你**
• AI 资讯 / 日报：技术动态、AI 新闻、日报查询
• 定时任务：创建定时/周期任务
• 数字先知：市场数据概率预测
• 团队 Skill 目录：已有技能查询、立项查重、审查缺项
• 开发助手：代码搜索/分析、需求分析

直接说需求就行，不用记命令～
