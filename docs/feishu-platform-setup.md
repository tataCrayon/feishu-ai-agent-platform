# 飞书开放平台配置指南

> ⚠️ **必须在飞书开放平台完成以下配置后，cc-connect 才能正常连接！**

## 完整配置清单（8步）

### Step 1：创建企业自建应用

1. 访问 https://open.feishu.cn → 开发者后台
2. 点击"创建应用" → 选择"自建应用"
3. 填写应用名称（如"RWA业务助手"）、描述、图标
4. 记录 **App ID** 和 **App Secret**（已在 config.toml 中填入）

### Step 2：启用机器人能力

- 应用详情页 → "应用能力" → "添加应用能力" → 选择"机器人" → 启用

### Step 3：配置权限（必须）

进入"权限管理"，搜索并开启以下权限：

| 权限标识 | 用途 | 必须？ |
|---------|------|-------|
| `im:message.group_at_msg:readonly` | 接收群聊@机器人消息 | ✅ |
| `im:message.p2p_msg:readonly` | 接收私聊消息 | ✅ |
| `im:message:send_as_bot` | 机器人发送消息 | ✅ |
| `im:message.group_msg` | 读取群消息内容 | ✅ |
| `contact:user.base:readonly` | 获取用户信息 | 推荐 |
| `im:resource` | 获取消息中图片/文件 | 推荐 |

### Step 4：配置事件订阅（WebSocket长连接模式）⚠️ 关键

1. "开发配置" → "事件与回调" → "事件配置"
2. **订阅方式** 选择「使用长连接接收事件」（不要选"将事件发送至请求网址"）
3. **添加事件**：`im.message.receive_v1`
4. 保存

### Step 5：配置回调（交互卡片）⚠️ 关键

1. 同页面 → "回调配置"
2. **订阅方式** 选择「使用长连接接收事件」
3. **添加回调**：`card.action.trigger`（交互卡片按钮点击）
4. 保存

> ⚠️ **如果不添加 `card.action.trigger`，飞书卡片按钮将无响应！**
> 如果无法添加该回调，在 config.toml 中设 `enable_feishu_card = false`

### Step 6：创建版本并发布

1. "版本管理与发布" → "创建版本"
2. 填写版本号（v1.0.0）和版本说明
3. 配置可用范围：建议先选"全部员工"
4. 点击"申请上线"

### Step 7：管理员审批

企业管理员在飞书管理后台审批应用上线请求。审批通过后配置生效。

### Step 8：验证连接

启动 cc-connect 后，日志应显示：
```
platform started project=dev-bot platform=feishu
connected to wss://msg-frontier.feishu.cn/ws/v2?...
```

## 快捷方式：CLI 配置

```bash
# 自动绑定已有飞书应用
cc-connect feishu setup --project dev-bot --app "cli_example00000000:<FEISHU_APP_SECRET — 见 .env>"
```

## 验证清单

- [ ] 应用已创建，App ID/Secret 已获取
- [ ] 机器人能力已启用
- [ ] 权限已申请（至少5个必需权限）
- [ ] 事件订阅选择了"长连接"模式
- [ ] im.message.receive_v1 事件已添加
- [ ] card.action.trigger 回调已添加
- [ ] 版本已创建并申请上线
- [ ] 管理员已审批通过
- [ ] ANTHROPIC_API_KEY 环境变量已设置
