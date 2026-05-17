# hermes-napcat

让 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 接入 **QQ**，基于 OneBot 11 反向 WebSocket 协议。

支持私聊、群聊（@bot 触发）、文本、图片、语音、视频、文件、消息引用、群内多用户独立会话。

---

## 架构

```
┌──────────┐  reverse WS   ┌──────────────┐  HTTP API   ┌──────────┐
│  NapCat  │ ────────────► │ hermes-napcat│ ◄────────── │  Hermes  │
│ (QQ 客户端)│ inbound events│   adapter    │ outbound msg │  Agent   │
└──────────┘                └──────────────┘             └──────────┘
   端口 6099 WebUI                                          端口 18800
   端口 3000 HTTP API                                       (反向 WS 服务)
```

- **NapCat** 是 QQ 客户端，扫码登录后挂在服务器
- **Hermes** 是 AI Agent 主程序
- **hermes-napcat**（本插件）在两者之间：
  - 启动一个反向 WebSocket 服务（默认 `18800`），接收 NapCat 推送的 QQ 事件
  - 通过 NapCat 的 HTTP API（默认 `3000`）发回复消息、上传媒体

---

## 快速开始

### 0. 前置条件

- 一台 Linux 服务器（支持 Docker）
- 已安装好 [hermes-agent](https://github.com/NousResearch/hermes-agent)
  ```bash
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  ```

### 1. 安装插件

```bash
hermes plugins install Aliang1337/hermes-napcat
```

安装时 hermes 会**交互式提示**让你填关键环境变量（`NAPCAT_HTTP_API`、`NAPCAT_ACCESS_TOKEN` 等），按提示填即可。

### 2. 部署 NapCat（Docker 方式，推荐）

```bash
# 准备目录
mkdir -p /opt/napcat/{QQ,config,logs}

# 启动容器
docker run -d --name napcat --restart always \
    --network host \
    -e NAPCAT_GID=0 -e NAPCAT_UID=0 \
    -e WEBUI_PORT=6099 \
    -v /opt/napcat/QQ:/app/.config/QQ \
    -v /opt/napcat/config:/app/napcat/config \
    -v /opt/napcat/logs:/app/napcat/logs \
    mlikiowa/napcat-docker:latest

# 拿到 WebUI 登录 token（从日志找）
docker logs napcat 2>&1 | grep "WebUi Token"
```

### 3. 打开 NapCat WebUI 扫码登录 QQ

```
http://<服务器IP>:6099/webui?token=<上面拿到的 token>
```

进去后用**手机 QQ 扫描二维码**完成登录。

### 4. 在 WebUI 里配置网络服务

进入 **"网络配置"**，**新建两个**：

**A. HTTP 服务器**（让 hermes 能调 NapCat 发消息）

| 字段 | 值 |
|------|------|
| 类型 | HTTP 服务器 |
| 名称 | hermes-http |
| 启用 | ✅ |
| 监听地址 | `127.0.0.1` |
| 监听端口 | `3000` |
| Token | 跟 `NAPCAT_ACCESS_TOKEN` 一致 |
| 启用心跳 | ✅ |

**B. WebSocket 客户端**（NapCat 主动连 hermes 推事件）

| 字段 | 值 |
|------|------|
| 类型 | WebSocket 客户端（反向 WS） |
| 名称 | hermes-ws |
| 启用 | ✅ |
| 反向 URL | `ws://127.0.0.1:18800/onebot/v11/ws` |
| Token | 跟上面 HTTP 服务器**同一个** |
| 启用心跳 | ✅ |

### 5. 启动/重启 hermes gateway

```bash
# 系统服务方式（推荐）
hermes gateway install --system --run-as-user root
systemctl start hermes-gateway
systemctl enable hermes-gateway

# 或前台运行
hermes gateway run
```

### 6. 测试

私聊机器人或在已配置的群里 `@机器人 你好` —— 应该收到回复。

---

## 环境变量

`hermes plugins install` 装完会自动写到 `~/.hermes/.env`。

| 变量 | 必填 | 默认 | 说明 |
|------|:----:|------|------|
| `NAPCAT_HTTP_API` | ✅ | — | NapCat HTTP API 地址，如 `http://127.0.0.1:3000` |
| `NAPCAT_WS_HOST` | | `0.0.0.0` | 反向 WS 监听地址 |
| `NAPCAT_WS_PORT` | | `18800` | 反向 WS 监听端口 |
| `NAPCAT_ACCESS_TOKEN` | | 空 | Bearer Token（HTTP + WS 共用） |
| `NAPCAT_SELF_ID` | | 空 | 机器人 QQ 号，留空自动从 WS 握手识别 |
| `NAPCAT_ALLOWED_USERS` | | 空 | 私聊白名单 QQ 号，逗号分隔。空 = 拒绝所有未授权私聊 |
| `NAPCAT_ALLOW_ALL_USERS` | | `false` | 开放所有私聊（**慎用**，会吃 LLM token） |
| `NAPCAT_GROUP_ALLOWLIST` | | 空 | 监听的群 ID，逗号分隔。空 = 监听所有群 |
| `NAPCAT_REQUIRE_MENTION` | | `true` | 群里是否必须 @bot 才响应（`false` = 接收所有群消息） |
| `NAPCAT_HOME_CHANNEL` | | 空 | cron 任务投递目标，如 `private:123456` 或 `group:789` |

修改后必须 `systemctl restart hermes-gateway` 才生效。

---

## 功能矩阵

| 能力 | 支持 |
|------|:----:|
| 文本消息（私聊/群聊） | ✅ |
| 群里 @bot 触发 | ✅（默认 require_mention=true） |
| 多人独立会话 | ✅（开启 `hermes config set gateway.group_sessions_per_user true`） |
| 消息引用（reply quote） | ✅（群聊自动引用原消息） |
| 图片识别（vision） | ✅（自动下载到本地缓存供 vision tool 使用） |
| 语音转文字（STT） | ✅（自动下载，由 hermes STT pipeline 处理） |
| 发送图片/语音/视频 | ✅ |
| 文件上传 | ✅（调用 NapCat 的 `upload_group_file` / `upload_private_file`） |
| @mention 段识别 | ✅（消息里的 `@QQ号` 会保留以便 LLM 知道被 @ 的是谁） |
| 群成员级白名单 | ❌（当前版本只支持群级白名单，未来可能添加） |
| 复杂转发消息 | ❌（暂未实现 forward_msg 段） |

---

## 群聊使用

1. 把机器人 QQ 拉进群
2. （推荐）配置 `NAPCAT_GROUP_ALLOWLIST=群1,群2` 限制只在指定群响应
3. 群里 `@机器人 你的问题`
4. （推荐）开启每用户独立会话：
   ```bash
   hermes config set gateway.group_sessions_per_user true
   systemctl restart hermes-gateway
   ```

群里支持所有 hermes 斜杠命令，例如：
- `@机器人 /new` 开始新会话
- `@机器人 /model` 切换模型
- `@机器人 /reset` 重置上下文

---

## 排查

```bash
# 看 hermes 日志
journalctl -u hermes-gateway -f | grep -iE "napcat"

# 看 NapCat 日志
docker logs -f napcat

# 看端口状态
ss -tlnp | grep -E ":(18800|3000|6099) "
```

**常见问题：**

| 现象 | 原因 | 处理 |
|------|------|------|
| 启动时 `HTTP API probe failed` warning | NapCat HTTP API 还没启用 | 在 WebUI 配好 HTTP 服务器（端口 3000） |
| 收不到群消息 | 群没在 `NAPCAT_GROUP_ALLOWLIST` 里 | 把群 ID 加进去并重启 |
| `Cannot connect to host 127.0.0.1:3000` 反复刷 | NapCat 反向 WS 没连过来 | 检查 WebUI WS 客户端配置 URL/Token 是否一致 |
| 群里 @ 没反应 | 没启用 `require_mention`/`group_allowlist` 不对 | 看日志确认事件有没有进 hermes |

---

## 协议

OneBot 11（[规范](https://github.com/botuniverse/onebot-11)）。本插件实现：

**入站事件（NapCat → hermes，反向 WS）：**
- `post_type=message` 的 `private` 和 `group` 子类型
- segment 类型：`text` / `at` / `image` / `record` / `video` / `reply` / `face`
- 自动忽略 `meta_event`（heartbeat / lifecycle）

**出站 API（hermes → NapCat，HTTP）：**
- `get_login_info` — 探测 bot 身份
- `send_private_msg` / `send_group_msg` — 发消息
- `get_msg` — 拉引用消息内容
- `upload_group_file` / `upload_private_file` — 上传文件

---

## License

MIT

---

## 致谢

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 提供 OneBot 11 中转
- 参考 [@hyl_aa/openclaw-napcat](https://github.com/Aliang1337/openclaw-napcat) 的协议实现
