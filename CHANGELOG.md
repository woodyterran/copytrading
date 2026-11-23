# Changelog

## [1.1.0] - 2026-04-11

### 新增
- **监控模式**（`app.py`）：新增"仅监控通知"功能，可在不执行跟单的情况下监听目标账户变化并发送通知，独立于跟单机器人运行，有单独的 PID 和日志文件

### 修复
- **进程意外停止**（`app.py`）：`subprocess.Popen` 加入 `start_new_session=True`，使机器人/监控进程脱离 Streamlit 进程组，避免 Streamlit 重启或终端关闭时 SIGHUP 信号杀死子进程
- **撤单失败后继续挂单**（`hyperliquid_copy_trader.py`）：`sync_open_orders` 中 `bulk_cancel` 失败（异常或返回非 ok）时立即 `return`，不再继续执行挂单，防止因旧单占用保证金导致新单报 `Insufficient margin`
- **挂单变化通知缺失**（`hyperliquid_copy_trader.py`）：`monitor_open_orders` 订单指纹由 `(coin, side, price)` 改为 `(coin, side, price, sz)`，数量变化也能正确触发通知

### 改进
- **通知内容**（`hyperliquid_copy_trader.py`）：挂单变化通知从"当前状态快照"改为差异对比，明确列出取消的单（❌）和新增的单（✅），以及当前总挂单数
- **进程自动重启**（`hyperliquid_copy_trader.py`）：`__main__` 加入外层重启循环，意外崩溃后 10 秒自动重启；SIGTERM 信号（停止按钮）会立即中断内层循环并优雅退出，不触发重启

---

## [1.0.0] - 2026-03-25

基于 `dc675fa` 提交的版本。

### 功能
- Streamlit Web 界面，支持配置、启动/停止跟单机器人、查看实时挂单/持仓/成交
- 合约 / 现货跟单，支持同步比例、最大滑点配置
- 两种跟单模式：同步持仓（Full Sync）/ 仅同步下单（Orders Only）
- 合约/现货挂单同步开关
- 历史成交记录入库，支持 CSV 导出
- OpenClaw 飞书 + iMessage 双渠道通知
- Agent 私钥模式支持（主账户地址与签名地址分离）
- 模拟操作模式（无私钥时自动启用，不真实下单）
- API 重试逻辑，应对 SSL/网络波动
- 运行日志实时查看、清空、下载
