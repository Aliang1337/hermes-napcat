# hermes-napcat 已安装 ✅

## 下一步

### 1. 部署 NapCat（QQ 消息中转）

```bash
mkdir -p /opt/napcat/{QQ,config,logs}

docker run -d --name napcat --restart always \
    --network host \
    -e NAPCAT_GID=0 -e NAPCAT_UID=0 \
    -e WEBUI_PORT=6099 \
    -v /opt/napcat/QQ:/app/.config/QQ \
    -v /opt/napcat/config:/app/napcat/config \
    -v /opt/napcat/logs:/app/napcat/logs \
    mlikiowa/napcat-docker:latest
```

### 2. 浏览器打开 WebUI 扫码登录

```bash
# 拿 WebUI token
docker logs napcat 2>&1 | grep "WebUi Token"
```

访问 `http://<服务器IP>:6099/webui?token=<上面拿到的 token>`，用**手机 QQ 扫码登录**。

### 3. 在 WebUI 配两个网络服务

进入 "网络配置" → 新建：

- **HTTP 服务器**：监听 `127.0.0.1:3000`，token 与 `NAPCAT_ACCESS_TOKEN` 一致
- **WebSocket 客户端（反向 WS）**：URL `ws://127.0.0.1:18800/onebot/v11/ws`，token 相同

### 4. 启动 hermes gateway

```bash
hermes gateway install --system --run-as-user root
systemctl restart hermes-gateway
journalctl -u hermes-gateway -f
```

看到 `NapCat: WS client connected` 即成功，可以在 QQ 私聊机器人测试。

---

完整文档：https://github.com/Aliang1337/hermes-napcat
