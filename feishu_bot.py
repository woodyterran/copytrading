# -*- coding: utf-8 -*-
"""
飞书查询机器人（双向）——在群里发指令 / 点按钮，机器人实时回答。

与跟单主程序 (hyperliquid_copy_trader.py) 完全隔离：
  * 独立进程，只读查询 Hyperliquid，绝不下单/撤单，不碰私钥。
  * 用飞书「自建应用」+ 长连接(WebSocket) 订阅消息事件，本地跑，无需公网。

依赖：lark-oapi （官方飞书 SDK）
    venv/bin/pip install lark-oapi

需要在 .env 里配置（自建应用后台拿到）：
    FEISHU_APP_ID=cli_xxx
    FEISHU_APP_SECRET=xxxxxxxx
已有的 TARGET_ADDRESS / MY_ADDRESS / COPY_RATIO 会被复用。

飞书后台配置步骤见文件末尾注释，或问我要「配置清单」。

指令（群里 @机器人 或私聊直接发关键词，带不带 / 都行）：
    帮助 / 菜单        -> 弹出按钮菜单
    对比              -> 两账户对比表（权益/持仓/挂单）
    持仓              -> 两账户持仓明细
    余额 / 权益        -> 两账户账户权益 + 跟单比例
    挂单 / 委托        -> 两账户挂单数量对比
    盈亏 / 浮盈        -> 两账户未实现盈亏
    状态              -> 跟单程序是否在跑 + 关键配置
"""
import os
import glob
import json
import time
import threading
import subprocess

from dotenv import load_dotenv

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    CreateMessageRequest, CreateMessageRequestBody,
    ReplyMessageRequest, ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse,
)

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
load_dotenv(override=False)

APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()

TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637")


def _valid_addr(a):
    return bool(a) and a.startswith("0x") and len(a) == 42 \
        and "YourPublicAddressHere" not in a \
        and a != "0x0000000000000000000000000000000000000000"


def _addr_from_logs():
    """从跟单程序日志里读『我的主账户地址: 0x...』（取最近写入的日志）。"""
    import re
    base = os.path.dirname(os.path.abspath(__file__))
    logs = sorted(glob.glob(os.path.join(base, "bot_*.log")),
                  key=os.path.getmtime, reverse=True)
    pat = re.compile(r"我的主账户地址[:：]\s*(0x[0-9a-fA-F]{40})")
    for lf in logs:
        try:
            with open(lf, "r", errors="ignore") as f:
                hits = pat.findall(f.read())
            if hits:
                return hits[-1]
        except Exception:
            continue
    return None


def _resolve_my_address():
    """解析我方公开地址（本程序只读，绝不签名/下单）。优先级：
       1) .env 的 MY_ADDRESS（真实地址）
       2) 跟单程序日志里的『我的主账户地址』——零配置自动获取
       3) 用私钥推导（仅推导地址）"""
    addr = os.getenv("MY_ADDRESS", "").strip()
    if _valid_addr(addr):
        return addr
    log_addr = _addr_from_logs()
    if _valid_addr(log_addr):
        print(f"[info] 从日志自动获取我方地址: {log_addr}")
        return log_addr
    pk = os.getenv("MY_PRIVATE_KEY", "").strip()
    if pk:
        try:
            from eth_account import Account
            return Account.from_key(pk).address
        except Exception as e:
            print(f"[warn] 私钥推导地址失败: {e}")
    return addr or "0x0000000000000000000000000000000000000000"


MY_ADDRESS = _resolve_my_address()
COPY_RATIO = float(os.getenv("COPY_RATIO", "1.0"))
POLL_INTERVAL = int(os.getenv("AUTO_REFRESH_INTERVAL", "5"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = "hyperliquid_copy_trader.py"

# 只读 Info（主网），不需要私钥
info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=15)

# 飞书 API 客户端（用于发消息/卡片）
client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()


# ---------------------------------------------------------------------------
# 账户快照（只读）——与跟单程序的对比表口径一致
# ---------------------------------------------------------------------------
def _fmt_usd(v):
    return f"${v:,.2f}"


def _fmt_num(v, nd=4):
    s = f"{v:.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _short(addr):
    return f"{addr[:6]}...{addr[-4:]}"


def fetch_snapshot(address):
    """抓取单账户快照：权益 + 持仓 + 挂单。失败返回 None。"""
    try:
        us = info.user_state(address)
    except Exception as e:
        print(f"[warn] 获取账户状态失败 {address}: {e}")
        return None
    try:
        orders = info.open_orders(address)
    except Exception as e:
        print(f"[warn] 获取挂单失败 {address}: {e}")
        orders = []
    try:
        acct_value = float(us.get("marginSummary", {}).get("accountValue", 0) or 0)
    except Exception:
        acct_value = 0.0
    positions = {}
    for p in us.get("assetPositions", []):
        core = p.get("position", p)
        szi = float(core.get("szi", 0) or 0)
        if szi == 0:
            continue
        positions[core["coin"]] = {
            "szi": szi,
            "entry": float(core.get("entryPx") or 0),
            "value": float(core.get("positionValue") or 0),
            "upnl": float(core.get("unrealizedPnl") or 0),
        }
    order_counts = {}
    for o in orders:
        order_counts[o["coin"]] = order_counts.get(o["coin"], 0) + 1
    return {
        "acct_value": acct_value,
        "positions": positions,
        "order_counts": order_counts,
        "order_total": len(orders),
    }


# ---------------------------------------------------------------------------
# 各查询的 Markdown 生成
# ---------------------------------------------------------------------------
def _header_line():
    return (f"**🎯目标** `{_short(TARGET_ADDRESS)}`　"
            f"**👤我方** `{_short(MY_ADDRESS)}`　|　跟单比例 **{COPY_RATIO}**")


def md_comparison():
    tgt = fetch_snapshot(TARGET_ADDRESS)
    mine = fetch_snapshot(MY_ADDRESS)
    if tgt is None or mine is None:
        return None
    md = [_header_line(), ""]
    md.append("**💰 账户权益**")
    md.append("| 账户 | 权益 |")
    md.append("| :-- | --: |")
    md.append(f"| 🎯目标 | {_fmt_usd(tgt['acct_value'])} |")
    md.append(f"| 👤我方 | {_fmt_usd(mine['acct_value'])} |")
    md.append("")
    coins = sorted(set(tgt['positions']) | set(mine['positions']))
    md.append("**📈 持仓对比**")
    if not coins:
        md.append("_两账户均无持仓_")
    else:
        md.append("| 账户 | 数量 | 均价 | 市值 | 浮盈 | 币种 | 方向 |")
        md.append("| :-- | --: | --: | --: | --: | :-- | :-: |")
        for c in coins:
            for tag, snap in (("🎯", tgt['positions'].get(c)),
                              ("👤", mine['positions'].get(c))):
                if not snap:
                    md.append(f"| {tag} | - | - | - | - | {c} | - |")
                    continue
                side = "多" if snap['szi'] > 0 else "空"
                md.append(
                    f"| {tag} | {_fmt_num(abs(snap['szi']))} | "
                    f"{_fmt_num(snap['entry'], 2)} | {_fmt_usd(snap['value'])} | "
                    f"{snap['upnl']:+,.2f} | {c} | {side} |")
    md.append("")
    ocoins = sorted(set(tgt['order_counts']) | set(mine['order_counts']))
    md.append("**📋 挂单对比**")
    md.append("| 币种 | 🎯目标 | 👤我方 |")
    md.append("| :-- | --: | --: |")
    for c in ocoins:
        md.append(f"| {c} | {tgt['order_counts'].get(c, 0)} | {mine['order_counts'].get(c, 0)} |")
    md.append(f"| **合计** | **{tgt['order_total']}** | **{mine['order_total']}** |")
    return "\n".join(md)


def md_positions():
    tgt = fetch_snapshot(TARGET_ADDRESS)
    mine = fetch_snapshot(MY_ADDRESS)
    if tgt is None or mine is None:
        return None
    coins = sorted(set(tgt['positions']) | set(mine['positions']))
    md = [_header_line(), "", "**📈 持仓明细**"]
    if not coins:
        md.append("_两账户均无持仓_")
        return "\n".join(md)
    md.append("| 账户 | 币种 | 方向 | 数量 | 均价 | 市值 | 浮盈 |")
    md.append("| :-- | :-- | :-: | --: | --: | --: | --: |")
    for c in coins:
        for tag, snap in (("🎯目标", tgt['positions'].get(c)),
                          ("👤我方", mine['positions'].get(c))):
            if not snap:
                md.append(f"| {tag} | {c} | - | - | - | - | - |")
                continue
            side = "多" if snap['szi'] > 0 else "空"
            md.append(
                f"| {tag} | {c} | {side} | {_fmt_num(abs(snap['szi']))} | "
                f"{_fmt_num(snap['entry'], 2)} | {_fmt_usd(snap['value'])} | "
                f"{snap['upnl']:+,.2f} |")
    return "\n".join(md)


def md_balance():
    tgt = fetch_snapshot(TARGET_ADDRESS)
    mine = fetch_snapshot(MY_ADDRESS)
    if tgt is None or mine is None:
        return None
    ratio = (mine['acct_value'] / tgt['acct_value']) if tgt['acct_value'] else 0.0
    md = [_header_line(), "", "**💰 账户权益**"]
    md.append("| 账户 | 权益 |")
    md.append("| :-- | --: |")
    md.append(f"| 🎯目标 | {_fmt_usd(tgt['acct_value'])} |")
    md.append(f"| 👤我方 | {_fmt_usd(mine['acct_value'])} |")
    md.append("")
    md.append(f"我方 / 目标 权益比 ≈ **{ratio:.3f}**（设定跟单比例 {COPY_RATIO}）")
    return "\n".join(md)


def md_orders():
    tgt = fetch_snapshot(TARGET_ADDRESS)
    mine = fetch_snapshot(MY_ADDRESS)
    if tgt is None or mine is None:
        return None
    ocoins = sorted(set(tgt['order_counts']) | set(mine['order_counts']))
    md = [_header_line(), "", "**📋 挂单对比**"]
    if not ocoins:
        md.append("_两账户均无挂单_")
        return "\n".join(md)
    md.append("| 币种 | 🎯目标 | 👤我方 |")
    md.append("| :-- | --: | --: |")
    for c in ocoins:
        md.append(f"| {c} | {tgt['order_counts'].get(c, 0)} | {mine['order_counts'].get(c, 0)} |")
    md.append(f"| **合计** | **{tgt['order_total']}** | **{mine['order_total']}** |")
    return "\n".join(md)


def md_pnl():
    tgt = fetch_snapshot(TARGET_ADDRESS)
    mine = fetch_snapshot(MY_ADDRESS)
    if tgt is None or mine is None:
        return None
    t_upnl = sum(p['upnl'] for p in tgt['positions'].values())
    m_upnl = sum(p['upnl'] for p in mine['positions'].values())
    md = [_header_line(), "", "**📊 未实现盈亏（浮动）**"]
    md.append("| 账户 | 持仓数 | 浮动盈亏 |")
    md.append("| :-- | --: | --: |")
    md.append(f"| 🎯目标 | {len(tgt['positions'])} | {t_upnl:+,.2f} |")
    md.append(f"| 👤我方 | {len(mine['positions'])} | {m_upnl:+,.2f} |")
    return "\n".join(md)


def _copier_running():
    """检查跟单主程序是否在跑：扫描 bot_*.pid，判断进程存活且确为本脚本。"""
    for pid_file in glob.glob(os.path.join(BASE_DIR, "bot_*.pid")):
        try:
            with open(pid_file) as f:
                pid = int((f.read() or "0").strip() or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except Exception:
            continue
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
            if SCRIPT_NAME in out:
                return True, pid
        except Exception:
            return True, pid
    return False, None


def md_status():
    running, pid = _copier_running()
    # 最近日志更新时间
    logs = glob.glob(os.path.join(BASE_DIR, "bot_*.log"))
    last = max((os.path.getmtime(p) for p in logs), default=0)
    last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "—"
    state = f"🟢 运行中 (PID {pid})" if running else "🔴 未运行"
    md = ["**🤖 跟单程序状态**", ""]
    md.append(f"- 状态: **{state}**")
    md.append(f"- 日志最后更新: {last_str}")
    md.append(f"- 🎯目标: `{_short(TARGET_ADDRESS)}`")
    md.append(f"- 👤我方: `{_short(MY_ADDRESS)}`")
    md.append(f"- 跟单比例: **{COPY_RATIO}** | 轮询间隔: {POLL_INTERVAL}s")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# 卡片构造
# ---------------------------------------------------------------------------
def markdown_card(md_text, header_title=None, template="blue"):
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": md_text}]},
    }
    if header_title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": header_title},
            "template": template,
        }
    return card


def _btn(text, cmd, btype="default"):
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btype,
        "behaviors": [{"type": "callback", "value": {"cmd": cmd}}],
    }


def menu_card():
    """按钮菜单卡片：点按钮即查询。"""
    rows = [
        [_btn("📊 账户对比", "对比", "primary"), _btn("📈 持仓明细", "持仓")],
        [_btn("💰 账户权益", "余额"), _btn("📋 挂单对比", "挂单")],
        [_btn("📉 浮动盈亏", "盈亏"), _btn("🤖 运行状态", "状态")],
    ]
    elements = []
    for row in rows:
        elements.append({
            "tag": "column_set",
            "horizontal_spacing": "default",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
                for b in row
            ],
        })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🤖 跟单机器人 · 查询菜单"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "点下方按钮查询，或直接发指令：\n`对比` `持仓` `余额` `挂单` `盈亏` `状态` `帮助`"},
                *elements,
            ]
        },
    }


# 指令 -> (标题, 生成函数)。生成函数返回 None 表示抓取失败。
COMMANDS = {
    "对比": ("📊 账户对比", md_comparison),
    "持仓": ("📈 持仓明细", md_positions),
    "余额": ("💰 账户权益", md_balance),
    "挂单": ("📋 挂单对比", md_orders),
    "盈亏": ("📉 浮动盈亏", md_pnl),
    "状态": ("🤖 运行状态", md_status),
}

# 关键词别名 -> 规范指令
ALIASES = {
    "对比": "对比", "对比表": "对比", "compare": "对比", "比较": "对比",
    "持仓": "持仓", "仓位": "持仓", "pos": "持仓", "position": "持仓",
    "余额": "余额", "权益": "余额", "资金": "余额", "balance": "余额",
    "挂单": "挂单", "委托": "挂单", "订单": "挂单", "orders": "挂单",
    "盈亏": "盈亏", "浮盈": "盈亏", "盈利": "盈亏", "pnl": "盈亏",
    "状态": "状态", "运行": "状态", "status": "状态",
    "帮助": "帮助", "菜单": "帮助", "help": "帮助", "?": "帮助", "？": "帮助",
}


def normalize(text):
    """清洗消息文本：去掉 @提及占位符、斜杠前缀，返回规范指令 or None。"""
    if not text:
        return None
    t = text.strip()
    # 去掉 @_user_1 之类的提及占位符
    for token in ["@_user_1", "@_user_2", "@_user_3", "@_all"]:
        t = t.replace(token, "")
    t = t.strip().lstrip("/").strip().lower()
    if not t:
        return None
    if t in ALIASES:
        return ALIASES[t]
    # 包含匹配（容忍"看下持仓"这类）
    for kw, cmd in ALIASES.items():
        if kw and kw in t:
            return cmd
    return None


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _send_card_to_chat(chat_id, card):
    try:
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(chat_id)
                          .msg_type("interactive")
                          .content(json.dumps(card))
                          .build()) \
            .build()
        resp = client.im.v1.message.create(req)
        if not resp.success():
            print(f"[err] 发送卡片失败: {resp.code} {resp.msg}")
    except Exception as e:
        print(f"[err] 发送卡片异常: {e}")


def _reply_card_to_message(message_id, card):
    try:
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                          .msg_type("interactive")
                          .content(json.dumps(card))
                          .build()) \
            .build()
        resp = client.im.v1.message.reply(req)
        if not resp.success():
            print(f"[err] 回复卡片失败: {resp.code} {resp.msg}")
    except Exception as e:
        print(f"[err] 回复卡片异常: {e}")


def build_reply_card(cmd):
    """根据规范指令生成要发送的卡片。"""
    if cmd == "帮助":
        return menu_card()
    title, fn = COMMANDS[cmd]
    md = fn()
    if md is None:
        return markdown_card("⚠️ 账户数据抓取失败，请稍后重试。", header_title=title, template="red")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return markdown_card(md + f"\n\n_更新于 {stamp}_", header_title=f"{title} · {stamp[11:]}")


# ---------------------------------------------------------------------------
# 事件处理
# ---------------------------------------------------------------------------
def on_message(data: P2ImMessageReceiveV1):
    try:
        msg = data.event.message
        if msg.message_type != "text":
            return
        content = json.loads(msg.content or "{}")
        raw = content.get("text", "")
        cmd = normalize(raw)
        if cmd is None:
            return  # 不是指令，忽略（群聊里不打扰）
        card = build_reply_card(cmd)
        # 用"回复"方式，保持在原消息线程下
        threading.Thread(
            target=_reply_card_to_message, args=(msg.message_id, card), daemon=True
        ).start()
    except Exception as e:
        print(f"[err] 处理消息异常: {e}")


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """按钮点击：异步查询并发卡片回群，立即返回一个 toast。"""
    cmd = "帮助"
    chat_id = None
    try:
        action = data.event.action
        chat_id = data.event.context.open_chat_id
        value = action.value or {}
        cmd = value.get("cmd", "帮助")
    except Exception as e:
        print(f"[err] 解析按钮事件异常: {e}")

    def worker(_cmd, _chat):
        try:
            card = build_reply_card(_cmd)
            if _chat:
                _send_card_to_chat(_chat, card)
        except Exception as e:
            print(f"[err] 按钮查询异常: {e}")

    threading.Thread(target=worker, args=(cmd, chat_id), daemon=True).start()

    return P2CardActionTriggerResponse({
        "toast": {"type": "info", "content": f"正在查询：{cmd} …"}
    })


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main():
    if not APP_ID or not APP_SECRET:
        raise SystemExit(
            "缺少 FEISHU_APP_ID / FEISHU_APP_SECRET。\n"
            "请在飞书开放平台创建「自建应用」后，把 App ID / App Secret 写进 .env：\n"
            "  FEISHU_APP_ID=cli_xxx\n  FEISHU_APP_SECRET=xxxxxxxx")

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_card_action_trigger(on_card_action) \
        .build()

    print("飞书查询机器人已启动（长连接）。在群里 @机器人 发『帮助』试试。")
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                        log_level=lark.LogLevel.INFO)
    ws.start()  # 阻塞，自动重连


if __name__ == "__main__":
    main()


# ===========================================================================
# 飞书后台配置清单（一次性）：
#   1. 开放平台 open.feishu.cn -> 开发者后台 -> 创建企业自建应用。
#   2. 「添加应用能力」-> 机器人。
#   3. 「权限管理」开启：
#        im:message            （接收消息）
#        im:message:send_as_bot（以应用身份发消息）
#      （群聊里要收到消息还需把机器人拉进群，或开启“接收群聊中@机器人消息”）
#   4. 「事件与回调」-> 订阅方式选「长连接」(不需要公网)；
#      添加事件：接收消息 im.message.receive_v1；
#      添加回调：卡片回传交互 card.action.trigger。
#   5. 「凭证与基础信息」拿 App ID / App Secret 填进 .env。
#   6. 发布版本并通过审核（企业自建应用通常自助发布即可）。
#   7. 把机器人拉进要用的群。
# 启动：  venv/bin/python feishu_bot.py
# ===========================================================================
