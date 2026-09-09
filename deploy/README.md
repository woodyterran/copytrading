# 部署与自动恢复

本目录包含跟单机器人在 macOS 上的开机自启 / 崩溃自愈配置。

## 组件

| 组件 | 作用 | 自愈方式 |
|---|---|---|
| Streamlit 界面 (`app.py`) | 配置与启停机器人的 Web UI | LaunchAgent `com.copytrading.streamlit`（`RunAtLoad` + `KeepAlive`，已有） |
| 跟单机器人 (`hyperliquid_copy_trader.py`) | 实际跟单进程，由界面按需启动 | 看门狗 `bot_watchdog.py` |
| 看门狗 (`bot_watchdog.py`) | 巡检并恢复"应运行但已停止"的机器人 | LaunchAgent `com.copytrading.botwatchdog`（`RunAtLoad` + 每 60s） |

## 恢复语义（关键）

看门狗以 **PID 文件 `bot_<md5(email)>.pid` 作为"期望状态"标记**，与界面启停按钮保持一致：

- 界面点"启动机器人" → 写 PID 文件；点"停止" → 删除 PID 文件。
- **PID 文件存在 = 希望它运行**。看门狗只在"PID 文件存在但进程已死（或 PID 被复用成别的进程）"时才重新拉起。
- 因此：重启 / 崩溃后会自动恢复；但用户在界面主动停止后（文件已删）**不会被擅自复活**。

即：**机器人只有在宕机前处于运行状态，重启后才会自动拉起**——这是有意为之的安全默认（不擅自开启实盘交易）。

## 在新机器上安装看门狗

```bash
# 1. 放置 LaunchAgent（按实际用户名调整路径）
cp deploy/com.copytrading.botwatchdog.plist ~/Library/LaunchAgents/

# 2. 加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.copytrading.botwatchdog.plist

# 3. 确认已注册
launchctl list | grep botwatchdog
```

卸载：`launchctl bootout gui/$(id -u)/com.copytrading.botwatchdog`

## 手动巡检 / 排错

```bash
# 立即触发一次巡检
launchctl kickstart -k gui/$(id -u)/com.copytrading.botwatchdog

# 看门狗动作日志
tail -f watchdog.log

# launchd 层面的 stdout/stderr
tail -f /tmp/copytrading-botwatchdog.log /tmp/copytrading-botwatchdog.error.log
```

> 注意：plist 内为绝对路径（`/Users/woodymacmini/...`）。换用户名 / 换路径时需同步修改 plist 与本文件。
