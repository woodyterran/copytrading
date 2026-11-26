import os
import time
import signal
import subprocess
import logging
import math
import queue
import threading
import hmac
import hashlib
import base64
import requests
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import database as db

# 读取项目根目录 .env（如飞书 Webhook 配置）；override=False 保证启动器注入的
# 环境变量(私钥、目标地址等来自 DB)优先级更高，不会被 .env 覆盖。
load_dotenv(override=False)

# --- 配置区域 ---

# 目标交易员地址
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637")

# 跟单比例 (例如 0.1 表示目标开 1 ETH，你开 0.1 ETH)
COPY_RATIO = float(os.getenv("COPY_RATIO", "1.0"))

# 轮询间隔 (秒)
POLL_INTERVAL = int(os.getenv("AUTO_REFRESH_INTERVAL", "5"))

# 仓位偏差阈值 (USD价值)
POSITION_DIFF_THRESHOLD_USD = 10.0

# 下市价单后的冷却期(秒)：期间不再重新评估该币种，
# 等成交反映到账户快照，避免"我方读数滞后→重复下单→再反向平掉"的对敲
POSITION_COOLDOWN_SEC = max(POLL_INTERVAL * 2, 6)

# 成交佐证容差：目标仓位快照的变化必须能被目标成交流解释(否则判定脏读)。
# 采用相对 5% + 绝对下限，兼容分批成交与精度误差。
POSITION_CORROBORATION_TOL = 1e-4

# 允许的最大滑点 (默认 2%)
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.02"))

# 跟单模式: 'full' (同步持仓) 或 'order' (仅同步下单)
# 从环境变量读取，默认为 'full'
SYNC_MODE = os.getenv("SYNC_MODE", "full")

# 交易类型: 'perps' (合约) 或 'spot' (现货)，支持多选 (逗号分隔)
MARKET_TYPE_STR = os.getenv("MARKET_TYPE", "perps")
MARKET_TYPES = [t.strip() for t in MARKET_TYPE_STR.split(",") if t.strip()]
if not MARKET_TYPES:
    MARKET_TYPES = ["perps"]

# 挂单同步开关
SYNC_PERP_ORDERS = os.getenv("SYNC_PERP_ORDERS", "1") == "1"
SYNC_SPOT_ORDERS = os.getenv("SYNC_SPOT_ORDERS", "0") == "1"

# 币种白名单 (逗号分隔，如 "BTC,ETH"；留空表示跟单所有币种)
_copy_coins_raw = os.getenv("COPY_COINS", "")
COPY_COINS = set(c.strip().upper() for c in _copy_coins_raw.split(",") if c.strip()) if _copy_coins_raw.strip() else set()

# 禁用代理以防止 ConnectionResetError
os.environ['no_proxy'] = 'api.hyperliquid.xyz,api-ui.hyperliquid.xyz'

# 通知文件路径 (本地落盘，供看板/早报等本地读取；不依赖任何外部服务)
NOTIFY_FILE = os.getenv("NOTIFY_FILE", "/Users/woodymacmini/LocalApp/OpenClaw/runtime_home/.openclaw/workspace/monitoring/notify.json")

# 统计文件路径 (供早报汇总)
STATS_FILE = os.getenv("STATS_FILE", "/Users/woodymacmini/LocalApp/OpenClaw/runtime_home/.openclaw/workspace/monitoring/daily_stats.json")

# 飞书群机器人 Webhook：直连发送，不再经 OpenClaw。
# 在飞书群里添加"自定义机器人"后拿到的推送地址；若开启了"签名校验"再填 SECRET。
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()

MONITOR_ONLY = os.getenv("MONITOR_ONLY", "0") == "1"

# 通知发送参数：通知在后台线程异步发送，绝不阻塞跟单主循环。
# 单条通知子进程超时(秒)；通道连续失败 FAIL_THRESHOLD 次后熔断，暂停 SKIP_COOLDOWN 秒不再尝试。
NOTIFY_SEND_TIMEOUT = int(os.getenv("NOTIFY_SEND_TIMEOUT", "15"))
NOTIFY_FAIL_THRESHOLD = int(os.getenv("NOTIFY_FAIL_THRESHOLD", "3"))
NOTIFY_SKIP_COOLDOWN = int(os.getenv("NOTIFY_SKIP_COOLDOWN", "300"))
NOTIFY_QUEUE_MAX = int(os.getenv("NOTIFY_QUEUE_MAX", "200"))

# 异常告警：某类健康检查连续失败达到该次数时发一条告警(进入异常态)，恢复时再发一条。
# 期间不重复轰炸，避免断网等持续异常刷屏。
ANOMALY_ALERT_AFTER = int(os.getenv("ANOMALY_ALERT_AFTER", "3"))
# 跟单层错误(保证金不足/撤单失败/下单失败等)即时告警的去重冷却(秒)：同类错误该窗口内只发一次。
TRADE_ALERT_COOLDOWN = int(os.getenv("TRADE_ALERT_COOLDOWN", "1800"))
# 每天发送两账户对比表的整点(本地时间，逗号分隔)。
REPORT_HOURS = [int(h) for h in os.getenv("REPORT_HOURS", "8,20").split(",") if h.strip()]

# 日志设置
# 级别可用环境变量 LOG_LEVEL 覆盖 (默认 INFO)；设为 DEBUG 可打开挂单检查等细粒度日志
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def feishu_sign(timestamp, secret):
    """飞书自定义机器人"签名校验"：HMAC-SHA256(key=f"{ts}\\n{secret}") 后 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def feishu_post_sync(message, timeout=NOTIFY_SEND_TIMEOUT):
    """同步直发飞书 Webhook(签名可选)。message 为含 msg_type 的消息体字典。

    供类内发送与进程崩溃处理共用；崩溃场景下后台线程可能随进程一起消亡，
    故崩溃告警走同步直发，best-effort。返回 (ok, detail)。
    """
    if not message or not FEISHU_WEBHOOK_URL:
        return False, "未配置 FEISHU_WEBHOOK_URL 或消息为空"
    payload = dict(message)
    if FEISHU_WEBHOOK_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(ts, FEISHU_WEBHOOK_SECRET)
    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=timeout)
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        ret_code = data.get("code", data.get("StatusCode", -1))
        if resp.status_code == 200 and ret_code == 0:
            return True, "ok"
        detail = data.get("msg") or data.get("StatusMessage") or resp.text[:150]
        return False, f"HTTP {resp.status_code} code={ret_code} {detail}"
    except requests.Timeout:
        return False, f"超时({timeout}s)"
    except Exception as e:
        return False, str(e)


def feishu_send_sync(text, timeout=NOTIFY_SEND_TIMEOUT):
    """同步直发纯文本消息。"""
    if not text:
        return False, "消息为空"
    return feishu_post_sync({"msg_type": "text", "content": {"text": text}}, timeout)


def feishu_markdown_card(md_text, header_title=None, template="blue"):
    """用 markdown 文本构造一张交互式卡片(schema 2.0，markdown 组件支持竖线表格)。"""
    return feishu_elements_card(
        [{"tag": "markdown", "content": md_text}], header_title, template)


def feishu_elements_card(elements, header_title=None, template="blue"):
    """用卡片元素列表(markdown 段 / 原生 table 段混排)构造交互式卡片(schema 2.0)。"""
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": list(elements)},
    }
    if header_title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": header_title},
            "template": template,
        }
    return card


def feishu_send_card_sync(card, timeout=NOTIFY_SEND_TIMEOUT):
    """同步直发交互式卡片。"""
    return feishu_post_sync({"msg_type": "interactive", "card": card}, timeout)

class MockExchange:
    """模拟交易所，用于无私钥模式下的模拟跟单"""
    def __init__(self, account_address):
        self.account_address = account_address
        self.positions = {}  # coin -> float(szi)
        self.orders = []     # list of order dicts
        self.order_id_counter = 1

    def market_open(self, coin, is_buy, sz, px, slippage):
        # 模拟成交
        side = "B" if is_buy else "A"
        logger.info(f"[模拟操作] 市价{'买入' if is_buy else '卖出'} {coin} 数量:{sz} 价格:{px}")
        
        # 更新模拟持仓
        current_sz = self.positions.get(coin, 0.0)
        delta = sz if is_buy else -sz
        new_sz = current_sz + delta
        self.positions[coin] = new_sz
        
        # 简单的浮点数精度处理
        if abs(self.positions[coin]) < 1e-6:
             self.positions[coin] = 0.0

        return {'status': 'ok', 'response': {'data': {'statuses': [{}]}}}

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False):
        # 模拟挂单
        side = "B" if is_buy else "A"
        oid = self.order_id_counter
        self.order_id_counter += 1
        
        logger.info(f"[模拟操作] 限价挂单 {coin} {side} 数量:{sz} 价格:{limit_px}")
        
        new_order = {
            'coin': coin,
            'side': side,
            'limitPx': str(limit_px),
            'sz': str(sz),
            'oid': oid,
            'timestamp': int(time.time() * 1000)
        }
        self.orders.append(new_order)
        
        return {'status': 'ok', 'response': {'data': {'statuses': [{}]}}}

    def bulk_cancel(self, cancels):
        # 模拟撤单
        logger.info(f"[模拟操作] 批量撤单: {cancels}")
        
        # 从模拟挂单列表中移除
        # cancels 是 [{'coin': 'PURR', 'oid': 123}, ...]
        ids_to_cancel = set(c['oid'] for c in cancels)
        self.orders = [o for o in self.orders if o['oid'] not in ids_to_cancel]
        
        return {'status': 'ok', 'response': {'data': {'statuses': [{}]}}}

class HyperliquidCopier:
    def __init__(self):
        self.private_key = os.getenv("MY_PRIVATE_KEY")
        
        # 检查是否为模拟模式 (私钥为空或包含占位符)
        self.is_dry_run = False
        if not self.private_key or "YourPrivateKeyHere" in self.private_key:
            self.is_dry_run = True
            self.private_key = None # 明确设为 None
        
        if self.is_dry_run:
            logger.warning("⚠️ 私钥未设置，进入 [模拟操作模式]")
            # 模拟模式下使用随机或配置的地址
            self.my_address = os.getenv("MY_ADDRESS", "0x0000000000000000000000000000000000000000")
            self.account = None
        else:
            try:
                self.account = Account.from_key(self.private_key)
                # 允许显式指定主账户地址，否则使用私钥推导的地址
                self.my_address = os.getenv("MY_ADDRESS", self.account.address)
                if not self.my_address or "YourPublicAddressHere" in self.my_address:
                     self.my_address = self.account.address
            except Exception as e:
                logger.error(f"私钥格式错误，切换至模拟模式: {e}")
                self.is_dry_run = True
                self.my_address = os.getenv("MY_ADDRESS", "0x0000000000000000000000000000000000000000")
                self.account = None

        logger.info(f"启动跟单程序 | 我的主账户地址: {self.my_address}")
        if not self.is_dry_run:
            logger.info(f"签名代理地址(Agent): {self.account.address}")
        else:
             logger.info(f"运行模式: [模拟跟单]")

        logger.info(f"目标地址: {TARGET_ADDRESS} | 跟单比例: {COPY_RATIO}")

        # 初始化 SDK (timeout=15 防止网络挂起)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=15)

        if self.is_dry_run:
            self.exchange = MockExchange(self.my_address)
        else:
            # 关键: Exchange 初始化时，如果使用 Agent 模式，需要传入主账户地址作为 account_address
            self.exchange = Exchange(self.account, constants.MAINNET_API_URL, account_address=self.my_address, timeout=15)

        
        # 初始化 Spot Universe
        self.spot_universe = set()
        self.spot_token_to_pair = {} # "PURR" -> "PURR/USDC"
        try:
            spot_meta = self.info.spot_meta()
            for u in spot_meta["universe"]:
                self.spot_universe.add(u['name']) # e.g. "PURR/USDC"
                # Map token names to pair name
                tokens = spot_meta["tokens"]
                # Universe has indices into tokens list
                base_idx, quote_idx = u['tokens']
                base_token = tokens[base_idx]['name']
                # Store mapping if not exists (prefer canonical)
                if base_token not in self.spot_token_to_pair:
                    self.spot_token_to_pair[base_token] = u['name']
        except Exception as e:
            logger.warning(f"Failed to load spot meta: {e}")

        # 模式2所需的基准状态
        self.target_baseline = {}
        self.my_baseline = {}
        self.initialized_baseline = False
        
        # 历史记录缓存 (去重用)
        self.seen_oids = set()
        self.seen_fill_hashes = set()
        self.last_position_snapshot = {}
        # 下单冷却：coin -> 最近一次市价单的时间戳，防止我方读数滞后导致重复下单
        self._position_cooldown = {}
        # 成交佐证：目标合约仓位只能由"成交(fill)"改变。用成交流校验快照变化，
        # 识别 Hyperliquid 节点"持续返回过期仓位"的脏读(两轮确认挡不住持续性脏读)。
        self._target_fill_net = {}        # coin -> 运行期间累计签名成交量(B=+, A=-)
        self._confirmed_target_pos = {}   # coin -> 最近一次经成交佐证的目标仓位
        self._confirmed_fill_net = {}     # coin -> 确认上述仓位时的成交流基准
        
        # 挂单指纹记录
        self.last_target_keys = None
        # 挂单防抖：上一轮目标挂单指纹集合 (None 表示还没有快照)
        # 要求连续两轮一致才执行全撤全挂，过滤 API 脏读导致的反复撤挂
        self._prev_target_order_keys = None

        # 通知后台发送：主循环只入队(非阻塞)，由守护线程串行发送飞书 Webhook，
        # 通道故障不再卡住跟单。连续失败即熔断，暂停一段时间不再尝试。
        self._notify_queue = queue.Queue(maxsize=NOTIFY_QUEUE_MAX)
        self._chan_fail = {"feishu": 0}
        self._chan_skip_until = {"feishu": 0.0}
        self._notify_thread = threading.Thread(
            target=self._notify_worker, name="notify-worker", daemon=True)
        self._notify_thread.start()
        # 本次启动是否已发过"运行正常"通知(首个成功周期后置 True，每次启动仅一条)
        self._startup_notified = False
        # 异常告警状态：key -> 连续失败次数；_alerted 记录已进入异常态(已发告警)的 key。
        # 进入异常态发一条，恢复时发一条，期间不重复。
        self._fail_counts = {}
        self._alerted = set()
        # 跟单层即时告警去重：key -> 上次发送时间戳，冷却期内同类不重复。
        self._alert_last = {}
        # 最近一次因保证金不足而舍弃的最低价买单(供报告展示提示)。
        # 元素: {"coin","side","px","sz"}；_last_discarded_ts 为发生时间。
        self._last_discarded = []
        self._last_discarded_ts = 0.0

        # 每天定时对比表：在 REPORT_HOURS 指定的整点各发一次两账户对比。
        self._report_thread = threading.Thread(
            target=self._report_scheduler, name="report-scheduler", daemon=True)
        self._report_thread.start()

        logger.info(f"跟单模式: {SYNC_MODE} ({'同步持仓' if SYNC_MODE == 'full' else '仅同步下单'})")
        logger.info(f"交易类型: {', '.join(MARKET_TYPES)}")
        if COPY_COINS:
            logger.info(f"币种白名单: {', '.join(sorted(COPY_COINS))}（仅跟单这些币种）")
        else:
            logger.info("币种白名单: 未设置（跟单所有币种）")

    def get_sz_decimals(self, coin):
        """获取币种数量精度"""
        # Normalize coin name
        normalized = self.info.name_to_coin.get(coin, coin)
        asset_id = self.info.coin_to_asset.get(normalized)
        if asset_id is None:
            return 4 # 默认精度
        return self.info.asset_to_sz_decimals.get(asset_id, 4)

    def is_spot_asset(self, coin):
        """判断是否为现货资产"""
        if coin in self.spot_universe:
            return True
        # Also check mapped token names if not a pair name
        if coin in self.spot_token_to_pair:
             # But wait, "PURR" might be Perp too.
             # If MARKET_TYPE is spot, we might want to treat it as spot?
             # For now, strict check on pair name or large ID
             pass
        
        # Fallback to asset ID check
        normalized = self.info.name_to_coin.get(coin, coin)
        asset_id = self.info.coin_to_asset.get(normalized)
        if asset_id is None:
            return False
        return asset_id >= 10000

    def get_user_state(self, address):
        # 模拟模式下，如果是查询我的地址，直接返回内存中的模拟状态
        if self.is_dry_run and address == self.my_address:
             state = {'assetPositions': [], 'openOrders': []}
             
             # 构造持仓
             # self.exchange 是 MockExchange 实例
             for coin, szi in self.exchange.positions.items():
                 if szi != 0:
                     state['assetPositions'].append({
                         'position': {
                             'coin': coin,
                             'szi': str(szi),
                             'entryPx': 0.0
                         }
                     })
             
             # 构造挂单
             state['openOrders'] = list(self.exchange.orders)
             return state

        state = {'assetPositions': [], 'openOrders': []}
        success = True
        
        # 1. 获取现货状态
        if 'spot' in MARKET_TYPES:
            try:
                raw_state = self.info.spot_user_state(address)
                # 转换为统一格式
                # Spot state: {'balances': [{'coin': 'PURR', 'total': '100.0', ...}]}
                # Unified state: {'assetPositions': [{'position': {'coin': 'PURR', 'szi': '100.0'}}]}
                for b in raw_state.get('balances', []):
                    if b['coin'] == 'USDC':
                        continue
                    
                    # Normalize coin name to Pair Name (e.g. PURR -> PURR/USDC)
                    coin_name = b['coin']
                    if coin_name in self.spot_token_to_pair:
                        coin_name = self.spot_token_to_pair[coin_name]
                    
                    state['assetPositions'].append({
                        'position': {
                            'coin': coin_name,
                            'szi': b['total'],
                            'entryPx': 0.0, # 现货没有持仓均价概念(或API不返回)
                        }
                    })
            except Exception as e:
                logger.error(f"获取现货状态失败 {address}: {e}")
                success = False
        
        # 2. 获取合约状态
        if 'perps' in MARKET_TYPES:
            try:
                perp_state = self.info.user_state(address)
                # 合约状态直接追加，不需要额外 normalize，除了过滤掉空仓位可能在外部做
                if 'assetPositions' in perp_state:
                    state['assetPositions'].extend(perp_state['assetPositions'])
            except Exception as e:
                logger.error(f"获取合约状态失败 {address}: {e}")
                success = False

        # 3. 获取挂单并过滤
        try:
            orders = self.info.open_orders(address)
            if address == self.my_address:
                logger.debug(f"原始挂单获取: {len(orders)} 个 | 地址: {address}")
                
            filtered_orders = []
            for o in orders:
                # Normalize coin name to Pair Name (e.g. PURR -> PURR/USDC)
                # Ensure consistency with assetPositions and internal logic
                original_coin = o['coin']
                if o['coin'] in self.spot_token_to_pair:
                    o['coin'] = self.spot_token_to_pair[o['coin']]

                # If order coin is just "PURR", it might be Perp or Spot depending on context?
                # Usually open_orders returns canonical names.
                # If we are in Spot mode, we want Spot orders.
                # If open_orders returns "PURR/USDC", then is_spot_asset is True.
                # If it returns "PURR", is_spot_asset is False (Perp).
                
                is_spot = self.is_spot_asset(o['coin'])
                
                if address == self.my_address:
                    logger.debug(f"挂单检查: {original_coin} -> {o['coin']} | IsSpot: {is_spot} | MarketTypes: {MARKET_TYPES}")
                
                if 'spot' in MARKET_TYPES and is_spot:
                    filtered_orders.append(o)
                elif 'perps' in MARKET_TYPES and not is_spot:
                    filtered_orders.append(o)
            state['openOrders'] = filtered_orders
        except Exception as e:
            logger.warning(f"获取挂单失败 {address}: {e}")
            # 挂单失败通常不致命，但也可能导致误撤单。安全起见，如果不完整，也视为失败。
            state['openOrders'] = []
            success = False
            
        return state if success else None

    def round_sz(self, coin, sz):
        """根据币种精度修剪数量"""
        decimals = self.get_sz_decimals(coin)
        factor = 10 ** decimals
        return math.floor(sz * factor) / factor

    def round_px(self, coin, px):
        """保留有效数字"""
        # 直接使用 float 转换，避免 .6g 导致的精度丢失或不一致
        # Hyperliquid API 返回的 px 字符串通常已经是标准化的
        return float(px)

    def _corroborated_target_size(self, coin, snap):
        """用目标成交流佐证仓位快照的变化，过滤"持续性脏读"。

        目标合约仓位只能因成交而改变。若快照相对上次"已佐证仓位"的变化量，
        能被期间的目标成交流(_target_fill_net)解释，则采纳新快照并重新锚定；
        否则判定为脏读，返回上次已佐证的仓位。

        返回 (t_sz, seeded)：seeded=True 表示本币种首次见到、仅播种基准、本轮不应交易。
        """
        if coin not in self._confirmed_target_pos:
            # 首次见到该币种：以快照播种基准，本轮不据单次读数交易
            self._confirmed_target_pos[coin] = snap
            self._confirmed_fill_net[coin] = self._target_fill_net.get(coin, 0.0)
            logger.info(f"[{coin}] 建立目标仓位基准={snap}，待成交佐证后再跟随")
            return snap, True

        confirmed = self._confirmed_target_pos[coin]
        observed_delta = snap - confirmed
        if abs(observed_delta) < 1e-9:
            return confirmed, False  # 快照无变化，按已确认值对齐(仍可修正我方漂移)

        fills_delta = self._target_fill_net.get(coin, 0.0) - self._confirmed_fill_net[coin]
        if abs(observed_delta - fills_delta) <= abs(observed_delta) * 0.05 + POSITION_CORROBORATION_TOL:
            # 变化被目标成交佐证 → 采纳新快照并重新锚定(顺带消除长期漂移)
            self._confirmed_target_pos[coin] = snap
            self._confirmed_fill_net[coin] = self._target_fill_net.get(coin, 0.0)
            logger.info(f"[{coin}] 目标仓位变化已被成交佐证: {confirmed} → {snap}")
            return snap, False

        # 变化无成交佐证 → 脏读，忽略快照，仍按上次确认值
        logger.warning(
            f"[{coin}] 目标仓位变化无成交佐证 (快照:{snap} 确认:{confirmed} "
            f"成交流Δ:{fills_delta:.5f})，判定为脏读，忽略本次读数"
        )
        return confirmed, False

    def sync_positions(self, target_state, my_state):
        """同步仓位 (市价单修补)"""
        def parse_positions(state):
            pos_map = {}
            for p in state.get('assetPositions', []):
                core = p.get('position', p)
                coin = core.get('coin')
                if coin:
                    pos_map[coin] = float(core.get('szi', 0))
            return pos_map

        target_positions = parse_positions(target_state)
        my_positions = parse_positions(my_state)

        # 模式2: 初始化基准
        if SYNC_MODE == 'order' and not self.initialized_baseline:
            self.target_baseline = target_positions.copy()
            self.my_baseline = my_positions.copy()
            self.initialized_baseline = True
            logger.info("已初始化 '仅同步下单' 模式的基准仓位，忽略初始差异。")
            return

        all_coins = set(target_positions.keys()) | set(my_positions.keys())
        # 币种白名单过滤：只处理白名单内的币种，白名单为空则不限制
        if COPY_COINS:
            all_coins = {c for c in all_coins if c.upper() in COPY_COINS}

        for coin in all_coins:
            # 冷却：刚下过单的币种，等成交反映到快照前不重复评估，防止超买/超卖后反向对敲
            last_trade_ts = self._position_cooldown.get(coin, 0)
            if time.time() - last_trade_ts < POSITION_COOLDOWN_SEC:
                logger.info(f"[{coin}] 处于下单冷却期，跳过本轮评估(等成交反映到账户)")
                continue

            snap = target_positions.get(coin, 0.0)

            # 成交佐证：拒绝无成交支撑的仓位跳变(持续性脏读的唯一可靠防线)
            t_sz, seeded = self._corroborated_target_size(coin, snap)
            if seeded:
                # 首次见到该币种：只播种基准，本轮不据单次读数交易
                continue

            m_sz = my_positions.get(coin, 0.0)
            
            # 计算目标持仓量 (根据模式)
            if SYNC_MODE == 'full':
                # 全量同步: 直接对齐目标绝对值
                target_goal = t_sz * COPY_RATIO
            else:
                # 仅同步下单: 基于基准的增量
                t_base = self.target_baseline.get(coin, 0.0)
                m_base = self.my_baseline.get(coin, 0.0)
                t_delta = t_sz - t_base
                # 目标没有变化，跳过——不干预用户自己的仓位
                if abs(t_delta) < 0.0001:
                    continue
                target_goal = m_base + t_delta * COPY_RATIO

            # 计算需要调整的差额
            diff = target_goal - m_sz
            
            if abs(diff) < 0.0001:
                continue

            price_ctx = self.info.all_mids()
            current_price = float(price_ctx.get(coin, 0.0))
            if current_price == 0:
                continue

            diff_usd = abs(diff) * current_price
            
            if diff_usd > POSITION_DIFF_THRESHOLD_USD:
                logger.warning(f"[{coin}] 仓位偏差 | 目标: {t_sz}, 我: {m_sz}, 需调整: {diff:.4f} (${diff_usd:.2f})")
                
                is_buy = diff > 0
                rounded_sz = self.round_sz(coin, abs(diff))
                
                if rounded_sz == 0:
                    continue

                try:
                    logger.info(f"[{coin}] 执行市价{'买入' if is_buy else '卖出'} {rounded_sz}")
                    # 先置冷却时间戳：无论成交回报是否及时，都先冻结该币种一个冷却期，
                    # 避免下轮因我方快照未更新而重复下单
                    self._position_cooldown[coin] = time.time()
                    res = self.exchange.market_open(coin, is_buy, rounded_sz, current_price, SLIPPAGE)
                    if res['status'] == 'ok':
                        logger.info(f"[{coin}] 市价单成交")
                    else:
                        logger.error(f"[{coin}] 下单失败: {res}")
                        self._alert_now(f"mkt_fail_{coin}",
                                        f"{coin} 市价单失败，未能跟上目标仓位: {str(res)[:150]}")
                except Exception as e:
                    logger.error(f"[{coin}] 下单异常: {e}")
                    self._alert_now(f"mkt_exc_{coin}", f"{coin} 市价下单异常: {e}")

    def sync_open_orders(self, target_state, my_state):
        """同步挂单 (新逻辑: 优先清理，高价优先挂单，保证金检查，支持过滤)"""
        target_orders = target_state.get('openOrders', [])
        my_orders = my_state.get('openOrders', [])
        
        # --- 过滤逻辑 (Local) ---
        def is_allowed_order(o):
            coin = o['coin']
            # 币种白名单过滤
            if COPY_COINS and coin.upper() not in COPY_COINS:
                return False
            is_spot = self.is_spot_asset(coin)
            if is_spot:
                return SYNC_SPOT_ORDERS
            else:
                return SYNC_PERP_ORDERS

        # 过滤
        target_orders = [o for o in target_orders if is_allowed_order(o)]
        my_orders = [o for o in my_orders if is_allowed_order(o)]
        # -----------------------

        # DEBUG LOG
        # logger.info(f"[DEBUG] 同步检查 | 目标挂单数: {len(target_orders)} | 我的挂单数: {len(my_orders)}")
        if len(my_orders) > 0:
            pass
            # logger.info(f"[DEBUG] 我的首个挂单: {my_orders[0]}")
        
        # 1. 构建指纹
        #   目标指纹(目标自身数量): 用于防抖与变化检测
        #   期望指纹(目标数量×跟单比例): 我方挂单"应有的样子"，与我方现有挂单做增量对比
        def target_key(o):
            return (o['coin'], o['side'],
                    self.round_px(o['coin'], float(o['limitPx'])),
                    self.round_sz(o['coin'], float(o['sz'])))

        def desired_key(o):
            return (o['coin'], o['side'],
                    self.round_px(o['coin'], float(o['limitPx'])),
                    self.round_sz(o['coin'], float(o['sz']) * COPY_RATIO))

        def my_key(o):
            return (o['coin'], o['side'],
                    self.round_px(o['coin'], float(o['limitPx'])),
                    self.round_sz(o['coin'], float(o['sz'])))

        # 2. 检测变化 (基于目标挂单的指纹集合)
        current_target_keys = set(target_key(o) for o in target_orders)

        # 防抖：目标挂单集合需连续两轮一致才执行同步，过滤 API 脏读导致的反复撤挂
        prev_raw = self._prev_target_order_keys
        self._prev_target_order_keys = current_target_keys
        if prev_raw != current_target_keys:
            # 尚未连续两轮一致（含首轮），等待下轮确认
            if self.last_target_keys is not None:
                logger.info(f"挂单读数变化未确认 (本轮 {len(current_target_keys)} 笔)，等待下轮确认，本轮跳过")
            return

        # 如果目标挂单自上次同步以来没变，则无需处理
        if self.last_target_keys == current_target_keys:
            return

        if self.last_target_keys is not None:
            self.send_order_change_notification(target_orders)

        # 3. 增量对比(按指纹多重集，正确处理重复挂单)：只撤我方多余的、只挂缺失的，已一致的保持不动
        desired_by_key = {}
        for o in target_orders:
            desired_by_key.setdefault(desired_key(o), []).append(o)
        my_by_key = {}
        for o in my_orders:
            my_by_key.setdefault(my_key(o), []).append(o)

        to_cancel = []
        to_create = []
        for k in set(my_by_key) | set(desired_by_key):
            mine = my_by_key.get(k, [])
            want = desired_by_key.get(k, [])
            if len(mine) > len(want):
                to_cancel.extend(mine[len(want):])   # 多出来的撤掉
            elif len(want) > len(mine):
                to_create.extend(want[len(mine):])   # 缺失的补挂

        if not to_cancel and not to_create:
            logger.info(f"挂单已与目标一致 ({len(current_target_keys)} 笔)，无需调整")
            self.last_target_keys = current_target_keys
            return

        logger.info(f"检测到挂单变化，增量同步: 撤 {len(to_cancel)} 笔 / 挂 {len(to_create)} 笔 "
                    f"(目标 {len(target_orders)} 笔, 我方 {len(my_orders)} 笔)")

        # 4. 撤掉我方多余挂单
        if to_cancel:
            cancels = [{"coin": o['coin'], "oid": o['oid']} for o in to_cancel]
            try:
                res = self.exchange.bulk_cancel(cancels)
                if res['status'] == 'ok':
                    logger.info(f"已撤销多余挂单 ({len(cancels)} 个)")
                else:
                    logger.error(f"撤单请求失败: {res}")
                    self._alert_now("cancel_fail",
                                    f"撤单请求失败，已放弃本轮挂单同步: {str(res)[:150]}")
                    return  # 撤单失败，放弃本次同步，防止保证金不足

                # 撤单后稍微等待，让 margin 释放生效
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"撤单异常: {e}")
                self._alert_now("cancel_exc",
                                f"撤单异常，已放弃本轮挂单同步: {e}")
                return  # 撤单异常，放弃本次同步，防止保证金不足

        if not to_create:
            self.last_target_keys = current_target_keys
            return

        # 5. 补挂缺失的目标挂单: 从高价往低价 (Price DESC)，逐个下单直到保证金不足
        #    一旦保证金不足即停止：由于按价格从高到低挂，剩余未挂的天然是"最低价"的那批，
        #    即优先舍弃最低价买单(远离市价、最不易成交者)。被舍弃的挂单会记录并在报告中提示。
        to_create.sort(key=lambda x: self.round_px(x['coin'], float(x['limitPx'])), reverse=True)
        logger.info(f"计划补挂 {len(to_create)} 个挂单，按价格从高到低...")

        # 本轮重新评估舍弃情况：若全部挂上则清空(报告不再显示旧提示)
        self._last_discarded = []
        self._last_discarded_ts = 0.0

        for idx, target_order in enumerate(to_create):
            coin = target_order['coin']
            side = target_order['side']
            px = self.round_px(coin, float(target_order['limitPx']))

            # 计算跟单数量
            target_sz = float(target_order['sz'])
            sz_to_place = self.round_sz(coin, target_sz * COPY_RATIO)

            if sz_to_place == 0:
                continue

            is_buy = (side == 'B')
            margin_hit = False

            try:
                # 检查日志避免刷屏
                logger.info(f"尝试挂单: {coin} {side} {sz_to_place} @ {px}")
                res = self.exchange.order(coin, is_buy, sz_to_place, px, {"limit": {"tif": "Gtc"}})

                if res['status'] == 'ok':
                    status = res['response']['data']['statuses'][0]
                    if 'error' in status:
                        err_msg = status['error']
                        logger.error(f"挂单业务错误: {err_msg}")
                        # 检查是否为 margin 相关错误
                        if 'Margin' in err_msg or 'balance' in err_msg.lower():
                            margin_hit = True
                        else:
                            self._alert_now(f"order_err_{coin}",
                                            f"{coin} 挂单业务错误: {err_msg[:120]}")
                    else:
                        pass # 成功
                else:
                    logger.error(f"挂单请求失败: {res}")
                    self._alert_now("order_req_fail",
                                    f"挂单请求失败: {str(res)[:150]}")
            except Exception as e:
                logger.error(f"挂单异常: {e}")
                if 'margin' in str(e).lower():
                    margin_hit = True
                else:
                    self._alert_now("order_exc", f"挂单异常: {e}")

            if margin_hit:
                # 保证金不足：舍弃"当前这笔 + 剩余未挂的"(均为最低价一侧)，记录并告警后停止
                logger.warning("⚠️ 保证金不足，舍弃剩余最低价挂单并停止本轮补挂")
                self._record_margin_discard(to_create[idx:])
                break

            # 稍微间隔一下避免速率限制
            time.sleep(0.1)

        # 更新状态指纹
        self.last_target_keys = current_target_keys

    def _record_margin_discard(self, discarded_orders):
        """记录因保证金不足被舍弃的挂单(最低价一侧)，存状态供报告提示，并发一条带明细的告警。"""
        items = []
        for o in discarded_orders:
            coin = o['coin']
            items.append({
                "coin": coin,
                "side": o['side'],  # 'B' 买 / 'A' 卖
                "px": self.round_px(coin, float(o['limitPx'])),
                "sz": self.round_sz(coin, float(o['sz']) * COPY_RATIO),
            })
        self._last_discarded = items
        self._last_discarded_ts = time.time()

        buys = [it for it in items if it['side'] == 'B']
        sells = [it for it in items if it['side'] == 'A']

        def _fmt_items(lst, n=6):
            parts = [f"{it['coin']} {'买' if it['side']=='B' else '卖'}@{it['px']}×{it['sz']}"
                     for it in lst[:n]]
            if len(lst) > n:
                parts.append(f"…等共{len(lst)}笔")
            return "；".join(parts)

        msg = f"⚠️ 保证金不足，已舍弃 {len(items)} 笔最低价挂单"
        if buys:
            msg += f"\n舍弃最低价买单 {len(buys)} 笔: {_fmt_items(buys)}"
        if sells:
            msg += f"\n舍弃卖单 {len(sells)} 笔: {_fmt_items(sells)}"
        self._alert_now("margin", msg)

    def monitor_open_orders(self, target_state):
        target_orders = target_state.get('openOrders', [])

        def is_allowed_order(o):
            coin = o['coin']
            # 币种白名单过滤
            if COPY_COINS and coin.upper() not in COPY_COINS:
                return False
            is_spot = self.is_spot_asset(coin)
            if is_spot:
                return SYNC_SPOT_ORDERS
            else:
                return SYNC_PERP_ORDERS

        target_orders = [o for o in target_orders if is_allowed_order(o)]

        def get_order_key(o):
            return (o['coin'], o['side'], self.round_px(o['coin'], float(o['limitPx'])), self.round_sz(o['coin'], float(o['sz'])))

        current_target_keys = set(get_order_key(o) for o in target_orders)
        if self.last_target_keys == current_target_keys:
            return

        if self.last_target_keys is not None:
            added_keys = current_target_keys - self.last_target_keys
            removed_keys = self.last_target_keys - current_target_keys
            self.send_order_change_notification(target_orders, added_keys, removed_keys)

        self.last_target_keys = current_target_keys

    def _enqueue(self, message, label):
        """把消息体(含 msg_type 的字典)投递到后台队列；队列满则丢最旧保最新。"""
        item = (message, label)
        try:
            self._notify_queue.put_nowait(item)
        except queue.Full:
            try:
                self._notify_queue.get_nowait()
                self._notify_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._notify_queue.put_nowait(item)
            except queue.Full:
                logger.warning("通知队列持续积压，丢弃一条通知")

    def send_notification(self, text):
        """发送纯文本通知：仅入队，立即返回，绝不阻塞跟单主循环。"""
        if not text:
            return
        self._enqueue({"msg_type": "text", "content": {"text": text}}, text[:30])

    def send_card(self, card, label="卡片消息"):
        """发送交互式卡片(如对比表)：仅入队，立即返回。"""
        if not card:
            return
        self._enqueue({"msg_type": "interactive", "card": card}, label)

    def _notify_worker(self):
        """后台守护线程：串行取出通知并发送飞书 Webhook，与跟单主循环完全解耦。"""
        while True:
            message, label = self._notify_queue.get()
            try:
                self._send_feishu_webhook(message, label)
            except Exception as e:
                logger.error(f"通知发送线程异常: {e}")
            finally:
                self._notify_queue.task_done()

    def _send_feishu_webhook(self, message, label=""):
        """后台线程中实际发送飞书 Webhook；带熔断，连续失败则暂停一段时间。"""
        if not message:
            return
        if not FEISHU_WEBHOOK_URL:
            if not getattr(self, "_warned_no_webhook", False):
                logger.warning("未配置 FEISHU_WEBHOOK_URL，跳过飞书通知（仅告警一次）")
                self._warned_no_webhook = True
            return
        if time.time() < self._chan_skip_until.get("feishu", 0.0):
            return  # 熔断冷却期内，直接跳过
        ok, detail = feishu_post_sync(message)
        if ok:
            logger.info(f"飞书通知已发送: {label}...")
            self._record_channel_result("feishu", True)
        else:
            logger.error(f"飞书通知发送失败: {detail}")
            self._record_channel_result("feishu", False)

    def _record_channel_result(self, channel, ok):
        """记录通道成败：成功清零，连续失败达阈值则熔断暂停。"""
        if ok:
            self._chan_fail[channel] = 0
            return
        self._chan_fail[channel] = self._chan_fail.get(channel, 0) + 1
        if self._chan_fail[channel] >= NOTIFY_FAIL_THRESHOLD:
            self._chan_skip_until[channel] = time.time() + NOTIFY_SKIP_COOLDOWN
            logger.warning(
                f"通道[{channel}]连续失败{self._chan_fail[channel]}次，"
                f"暂停{NOTIFY_SKIP_COOLDOWN}s不再尝试")
            self._chan_fail[channel] = 0

    def write_notify_file(self, change_type, detail):
        """将变化写入通知文件，供 OpenClaw 读取"""
        import json
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            record = {"time": timestamp, "type": change_type, "detail": detail}
            
            # 读取现有内容
            existing = []
            if os.path.exists(NOTIFY_FILE):
                try:
                    with open(NOTIFY_FILE, 'r') as f:
                        existing = json.load(f)
                except:
                    existing = []
            
            # 追加新记录
            existing.append(record)
            
            # 写回文件
            with open(NOTIFY_FILE, 'w') as f:
                json.dump(existing, f, ensure_ascii=False)
                
            logger.info(f"已写入通知文件: {change_type} - {detail}")
            
        except Exception as e:
            logger.error(f"写入通知文件失败: {e}")
    
    def update_daily_stats(self, change_type):
        """更新每日统计，供早报汇总使用"""
        import json
        try:
            today = time.strftime("%Y-%m-%d")
            
            # 读取现有统计
            stats = {"date": today, "trades": 0, "orders": 0}
            if os.path.exists(STATS_FILE):
                try:
                    with open(STATS_FILE, 'r') as f:
                        stats = json.load(f)
                    # 新的一天，重置统计
                    if stats.get("date") != today:
                        stats = {"date": today, "trades": 0, "orders": 0}
                except:
                    stats = {"date": today, "trades": 0, "orders": 0}
            
            # 更新计数
            if change_type == "trade":
                stats["trades"] = stats.get("trades", 0) + 1
            elif change_type == "order":
                stats["orders"] = stats.get("orders", 0) + 1
            
            # 写回
            with open(STATS_FILE, 'w') as f:
                json.dump(stats, f, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"更新每日统计失败: {e}")

    def send_order_change_notification(self, target_orders, added_keys=None, removed_keys=None):
        try:
            ts = time.strftime("%H:%M:%S")
            lines = [f"【挂单变动】{ts}"]

            if removed_keys:
                lines.append(f"❌ 取消 {len(removed_keys)} 笔:")
                for key in list(removed_keys)[:3]:
                    coin, side, px, sz = key
                    lines.append(f"  {coin} {side} {sz}@{px}")

            if added_keys:
                lines.append(f"✅ 新增 {len(added_keys)} 笔:")
                for key in list(added_keys)[:3]:
                    coin, side, px, sz = key
                    lines.append(f"  {coin} {side} {sz}@{px}")

            lines.append(f"当前共 {len(target_orders)} 笔挂单")
            self.send_notification("\n".join(lines))

        except Exception as e:
            logger.error(f"构建挂单通知失败: {e}")

    def update_history(self, target_state, notify=True):
        """更新历史记录到数据库，并触发成交通知"""
        try:
            # 1. 记录挂单
            for o in target_state['openOrders']:
                if o['oid'] not in self.seen_oids:
                    db.log_order(TARGET_ADDRESS, o)
                    self.seen_oids.add(o['oid'])
                    # 仅在 notify=True 时才写入通知文件（避免初始化时发送旧通知）
                    if notify:
                        side_text = "卖出" if o['side'] == 'A' else "买入"
                        self.write_notify_file("order", f"新挂单 {o['coin']} {side_text} {o['sz']}@{o['limitPx']}")
                        # 更新每日统计
                        self.update_daily_stats("order")
            
            # 2. 记录持仓
            current_positions = {}
            for p in target_state['assetPositions']:
                core = p.get('position', p)
                if float(core.get('szi', 0)) != 0:
                    current_positions[core['coin']] = core

            for coin, pos in current_positions.items():
                prev_pos = self.last_position_snapshot.get(coin)
                is_changed = False
                if not prev_pos:
                    is_changed = True
                else:
                    if float(prev_pos['szi']) != float(pos['szi']):
                        is_changed = True
                    elif float(prev_pos.get('entryPx', 0)) != float(pos.get('entryPx', 0)):
                        is_changed = True
                
                if is_changed:
                    db.log_position(TARGET_ADDRESS, pos)
                    self.last_position_snapshot[coin] = pos.copy()

            # 3. 记录成交
            fills = []
            max_retries = 3
            for i in range(max_retries):
                try:
                    fills = self.info.user_fills(TARGET_ADDRESS)
                    break
                except Exception as e:
                    if i == max_retries - 1:
                        logger.warning(f"获取成交记录失败: {e}")
                    else:
                        time.sleep(1)

            new_fills = []
            for fill in fills:
                fill_hash = fill.get('hash') or f"{fill.get('tid')}_{fill.get('coin')}"
                
                if fill_hash not in self.seen_fill_hashes:
                    db.log_trade(TARGET_ADDRESS, fill)
                    self.seen_fill_hashes.add(fill_hash)
                    new_fills.append(fill)
                    # 累计目标成交流(供仓位佐证)：B=买入(+)，A=卖出(-)
                    fcoin = fill['coin']
                    if fcoin in self.spot_token_to_pair:
                        fcoin = self.spot_token_to_pair[fcoin]
                    signed = float(fill['sz']) if fill.get('side') == 'B' else -float(fill['sz'])
                    self._target_fill_net[fcoin] = self._target_fill_net.get(fcoin, 0.0) + signed

            # 发送成交通知 (仅当 notify=True 且有新成交时，且符合币种白名单)
            if notify and new_fills:
                for fill in new_fills:
                    if COPY_COINS and fill['coin'].upper() not in COPY_COINS:
                        continue
                    ts = time.strftime("%H:%M:%S")
                    side_text = "买入" if fill['side'] == 'B' else "卖出"
                    msg = f"【成交变动】{ts}\n{fill['coin']} {side_text} {fill['sz']}@{fill['px']}"
                    self.send_notification(msg)
                    # 写入通知文件供 OpenClaw 读取
                    self.write_notify_file("trade", f"{fill['coin']} {side_text} {fill['sz']}@{fill['px']}")
                    # 更新每日统计
                    self.update_daily_stats("trade")
                    
        except Exception as e:
            logger.error(f"历史记录更新失败: {e}")

    def init_history_cache(self):
        """初始化历史缓存：DB 中已有的 fill 静默加载，停机期间漏掉的 fill 补发通知"""
        logger.info("初始化历史数据缓存...")
        try:
            fills = []
            try:
                fills = self.info.user_fills(TARGET_ADDRESS)
            except Exception as e:
                logger.warning(f"初始化时获取成交失败: {e}")

            missed_fills = []
            for fill in fills:
                fill_hash = fill.get('hash') or f"{fill.get('tid')}_{fill.get('coin')}"
                self.seen_fill_hashes.add(fill_hash)
                # 不在 DB 中 = 停机期间发生的新成交，需要补发通知
                if not db.fill_exists_in_db(fill_hash):
                    db.log_trade(TARGET_ADDRESS, fill)
                    missed_fills.append(fill)

            if missed_fills:
                logger.info(f"检测到 {len(missed_fills)} 笔停机期间的漏报成交，补发通知")
                for fill in missed_fills:
                    fill_time = time.strftime("%m-%d %H:%M", time.localtime(fill['time'] / 1000))
                    side_text = "买入" if fill['side'] == 'B' else "卖出"
                    msg = f"【补发成交】{fill_time}\n{fill['coin']} {side_text} {fill['sz']}@{fill['px']}"
                    self.send_notification(msg)
                    self.write_notify_file("trade", f"[补发]{fill['coin']} {side_text} {fill['sz']}@{fill['px']}")

            # 初始化仓位/挂单快照（不发通知）
            target_state = self.get_user_state(TARGET_ADDRESS)
            if target_state:
                self.update_history(target_state, notify=False)
        except Exception as e:
            logger.warning(f"初始化缓存失败: {e}")

    def _notify_startup_ok(self):
        """进程启动后，首个成功周期完成时发一次"运行正常"通知(每进程/每次启动仅一次)。

        放在成功获取到目标状态并走完一轮(监控或同步)之后，确保通知代表"已正常运行"，
        而不是进程刚拉起、此刻 API 可能还连不上的"已启动"。
        """
        if getattr(self, "_startup_notified", False):
            return
        self._startup_notified = True
        mode_str = "仅监控" if MONITOR_ONLY else "跟单模式"
        coins = ",".join(sorted(COPY_COINS)) if COPY_COINS else "全部"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.send_notification(
            f"【运行正常】{mode_str}已启动并正常运行\n"
            f"时间: {ts}\n"
            f"目标: {TARGET_ADDRESS[:6]}...{TARGET_ADDRESS[-4:]}\n"
            f"币种: {coins} | 跟单比例: {COPY_RATIO}")

    def _note_failure(self, key, label):
        """记录一次某类健康检查失败；连续达到阈值且尚未告警时，立即发一条异常告警。"""
        n = self._fail_counts.get(key, 0) + 1
        self._fail_counts[key] = n
        if n >= ANOMALY_ALERT_AFTER and key not in self._alerted:
            self._alerted.add(key)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.send_notification(f"⚠️【运行异常】{ts}\n{label}\n(已连续 {n} 次)")

    def _note_recovery(self, key, label):
        """某类健康检查成功；若此前处于异常态，发一条恢复通知并清零计数。"""
        was_alerted = key in self._alerted
        if self._fail_counts.get(key, 0) or was_alerted:
            self._fail_counts[key] = 0
            self._alerted.discard(key)
            if was_alerted:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                self.send_notification(f"✅【已恢复】{ts}\n{label}")

    def _alert_now(self, key, text, cooldown=TRADE_ALERT_COOLDOWN):
        """跟单层错误即时告警：首次立即发，cooldown 秒内同 key 不重复(防持续错误刷屏)。"""
        now = time.time()
        if now - self._alert_last.get(key, 0.0) < cooldown:
            return
        self._alert_last[key] = now
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.send_notification(f"⚠️【跟单异常】{ts}\n{text}")

    # ---------- 每天定时两账户对比表 ----------
    def _seconds_until_next_report(self):
        import datetime
        now = datetime.datetime.now()
        cands = []
        for h in REPORT_HOURS:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            cands.append(t)
        if not cands:
            return 3600.0
        return max(1.0, (min(cands) - now).total_seconds())

    def _report_scheduler(self):
        """守护线程：在 REPORT_HOURS 指定整点各发一次两账户对比表。"""
        if not REPORT_HOURS:
            return
        while True:
            try:
                time.sleep(self._seconds_until_next_report())
                self.send_account_comparison()
                time.sleep(61)  # 越过触发分钟，避免同一整点重复触发
            except Exception as e:
                logger.error(f"定时对比表任务异常: {e}")
                time.sleep(60)

    def _fetch_report_snapshot(self, address):
        """抓取单账户快照：账户权益 + 合约持仓 + 挂单。失败返回 None。"""
        try:
            us = self.info.user_state(address)
        except Exception as e:
            logger.error(f"对比表获取账户状态失败 {address}: {e}")
            return None
        try:
            orders = self.info.open_orders(address)
        except Exception as e:
            logger.warning(f"对比表获取挂单失败 {address}: {e}")
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
        order_list = []
        for o in orders:
            order_counts[o["coin"]] = order_counts.get(o["coin"], 0) + 1
            try:
                order_list.append({
                    "coin": o["coin"],
                    "side": o.get("side", ""),  # 'B' 买 / 'A' 卖
                    "px": float(o.get("limitPx") or 0),
                    "sz": float(o.get("sz") or 0),
                })
            except Exception:
                continue
        return {"acct_value": acct_value, "positions": positions,
                "order_counts": order_counts, "order_total": len(orders),
                "orders": order_list}

    @staticmethod
    def _fmt_usd(v):
        return f"${v:,.2f}"

    @staticmethod
    def _fmt_num(v, nd=4):
        s = f"{v:.{nd}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    # ---- 飞书原生卡片元素(markdown 段 / 原生 table 段) ----
    @staticmethod
    def _md_el(text):
        return {"tag": "markdown", "content": text}

    @staticmethod
    def _table_el(columns, rows, page_size=10):
        """原生 table 组件：紧凑行高 + 灰底表头 + 百分比列宽，真正逐列控宽。"""
        return {
            "tag": "table",
            "page_size": page_size,
            "row_height": "low",
            "header_style": {"background_style": "grey", "text_size": "normal",
                             "bold": True, "lines": 1},
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def _orders_table_el(tgt_orders, mine_orders):
        """逐价位挂单明细对比(原生 table)。排序：卖单在上、买单在下，各自按价格降序；
        目标有、我方缺的买单价位标 ⚠️。无数据返回 None。"""
        def grp(orders):
            d = {}
            for o in (orders or []):
                key = (o["coin"], o["side"], round(o["px"], 8))
                d[key] = d.get(key, 0.0) + o["sz"]
            return d
        tg = grp(tgt_orders)
        mg = grp(mine_orders)
        # 卖(A)在前、买(B)在后；同侧按价格降序
        keys = sorted(set(tg) | set(mg),
                      key=lambda k: (k[0], 0 if k[1] == 'A' else 1, -k[2]))
        if not keys:
            return None
        _f = HyperliquidCopier._fmt_num
        columns = [
            {"name": "coin", "display_name": "币种", "data_type": "text", "width": "16%", "horizontal_align": "left"},
            {"name": "side", "display_name": "方向", "data_type": "text", "width": "12%", "horizontal_align": "center"},
            {"name": "px", "display_name": "价格", "data_type": "text", "width": "26%", "horizontal_align": "right"},
            {"name": "tgt", "display_name": "🎯目标", "data_type": "text", "width": "23%", "horizontal_align": "right"},
            {"name": "mine", "display_name": "👤我方", "data_type": "text", "width": "23%", "horizontal_align": "right"},
        ]
        rows = []
        for (coin, side, px) in keys:
            sd = "买" if side == 'B' else ("卖" if side == 'A' else side)
            t = tg.get((coin, side, px))
            m = mg.get((coin, side, px))
            tcell = _f(t) if t is not None else "—"
            flag = " ⚠️" if (t is not None and m is None and side == 'B') else ""
            mcell = (_f(m) if m is not None else "—") + flag
            rows.append({"coin": coin, "side": sd, "px": _f(px, 2), "tgt": tcell, "mine": mcell})
        return HyperliquidCopier._table_el(columns, rows)

    @staticmethod
    def _positions_table_el(tgt, mine):
        """逐币种持仓对比(原生 table，目标/我方各一行)。无持仓返回 None。"""
        coins = sorted(set(tgt['positions']) | set(mine['positions']))
        if not coins:
            return None
        _f = HyperliquidCopier._fmt_num
        _u = HyperliquidCopier._fmt_usd
        columns = [
            {"name": "acct", "display_name": "账户", "data_type": "text", "width": "15%", "horizontal_align": "left"},
            {"name": "coin", "display_name": "币种", "data_type": "text", "width": "12%", "horizontal_align": "left"},
            {"name": "side", "display_name": "方向", "data_type": "text", "width": "10%", "horizontal_align": "center"},
            {"name": "sz", "display_name": "数量", "data_type": "text", "width": "16%", "horizontal_align": "right"},
            {"name": "entry", "display_name": "均价", "data_type": "text", "width": "16%", "horizontal_align": "right"},
            {"name": "value", "display_name": "市值", "data_type": "text", "width": "16%", "horizontal_align": "right"},
            {"name": "upnl", "display_name": "浮盈", "data_type": "text", "width": "15%", "horizontal_align": "right"},
        ]
        rows = []
        for c in coins:
            for tag, snap in (("🎯目标", tgt['positions'].get(c)),
                              ("👤我方", mine['positions'].get(c))):
                if not snap:
                    rows.append({"acct": tag, "coin": c, "side": "-", "sz": "-",
                                 "entry": "-", "value": "-", "upnl": "-"})
                    continue
                side = "多" if snap['szi'] > 0 else "空"
                rows.append({"acct": tag, "coin": c, "side": side,
                             "sz": _f(abs(snap['szi'])), "entry": _f(snap['entry'], 2),
                             "value": _u(snap['value']), "upnl": f"{snap['upnl']:+,.2f}"})
        return HyperliquidCopier._table_el(columns, rows)

    def _discard_banner_md(self):
        """若最近(6h内)因保证金不足舍弃过最低价挂单，返回提示横幅行；否则返回 []。"""
        if not self._last_discarded or (time.time() - self._last_discarded_ts > 6 * 3600):
            return []
        items = self._last_discarded
        buys = [it for it in items if it['side'] == 'B']
        when = time.strftime('%m-%d %H:%M', time.localtime(self._last_discarded_ts))
        parts = [f"{it['coin']}{'买' if it['side']=='B' else '卖'}@"
                 f"{self._fmt_num(it['px'], 2)}×{self._fmt_num(it['sz'])}" for it in items[:8]]
        more = f" …等共{len(items)}笔" if len(items) > 8 else ""
        return [f"**⚠️ 保证金不足：已舍弃 {len(items)} 笔最低价挂单**"
                f"（其中买单 {len(buys)} 笔，{when}）",
                "；".join(parts) + more, ""]

    def build_account_comparison_elements(self):
        """构建两账户(目标 vs 我方)对比的卡片元素列表(markdown 段 + 原生 table)；抓取失败返回 None。"""
        tgt = self._fetch_report_snapshot(TARGET_ADDRESS)
        mine = self._fetch_report_snapshot(self.my_address)
        if tgt is None or mine is None:
            return None
        els = []
        # 顶部地址/比例 + 保证金舍弃提示(若近期发生)
        head = [f"**🎯目标** `{TARGET_ADDRESS[:6]}...{TARGET_ADDRESS[-4:]}`　"
                f"**👤我方** `{self.my_address[:6]}...{self.my_address[-4:]}`　|　跟单比例 **{COPY_RATIO}**"]
        banner = self._discard_banner_md()
        if banner:
            head += [""] + [ln for ln in banner if ln]
        els.append(self._md_el("\n".join(head)))
        # 账户权益(两行小表，保留 markdown)
        els.append(self._md_el(
            "**💰 账户权益**\n| 账户 | 权益 |\n| :-- | --: |\n"
            f"| 🎯目标 | {self._fmt_usd(tgt['acct_value'])} |\n"
            f"| 👤我方 | {self._fmt_usd(mine['acct_value'])} |"))
        # 持仓对比(原生 table)
        els.append(self._md_el("**📈 持仓对比**"))
        pt = self._positions_table_el(tgt, mine)
        els.append(pt if pt else self._md_el("_两账户均无持仓_"))
        # 挂单明细对比(原生 table，逐价位对齐，展示双方所有挂单)
        els.append(self._md_el(
            f"**📋 挂单明细对比**　(🎯{tgt['order_total']} 笔 vs 👤{mine['order_total']} 笔)"))
        ot = self._orders_table_el(tgt.get('orders'), mine.get('orders'))
        els.append(ot if ot else self._md_el("_两账户均无挂单_"))
        return els

    def send_account_comparison(self):
        """生成并发送两账户对比表(交互式卡片 + 原生 table，走异步队列)。"""
        els = self.build_account_comparison_elements()
        if els:
            title = f"📊 账户对比 {time.strftime('%Y-%m-%d %H:%M')}"
            self.send_card(feishu_elements_card(els, header_title=title), label="账户对比表")
            logger.info("已发送两账户对比表")
        else:
            logger.warning("对比表生成失败(账户状态抓取失败)，本次跳过")
            self._alert_now("report_fail", "两账户对比表生成失败：账户状态抓取失败")

    def run(self):
        logger.info("跟单程序已启动...")
        if MONITOR_ONLY:
            logger.info("运行模式: 仅监控通知")

        # 启动前先初始化缓存
        self.init_history_cache()

        while True:
            try:
                # 1. 获取目标状态
                target_state = self.get_user_state(TARGET_ADDRESS)

                if target_state is None:
                    logger.warning(f"获取目标状态失败，跳过本次同步")
                    self._note_failure("target_state", "持续获取目标状态失败（网络/API 异常），已暂停同步")
                    time.sleep(POLL_INTERVAL)
                    continue
                self._note_recovery("target_state", "目标状态获取已恢复正常，同步继续")

                # 无论什么模式，都更新历史并检查成交通知
                self.update_history(target_state, notify=True)

                if MONITOR_ONLY:
                    self.monitor_open_orders(target_state)
                    self._note_recovery("loop", "轮询循环已恢复正常")
                    self._notify_startup_ok()  # 首个成功周期后发一次"运行正常"
                    time.sleep(POLL_INTERVAL)
                    continue

                # 2. 获取我的状态
                my_state = self.get_user_state(self.my_address)


                if my_state is None:
                    logger.warning(f"获取我的状态失败 (可能由于网络或API限制)，跳过本次同步")
                    self._note_failure("my_state", "持续获取我的账户状态失败（网络/API 异常），已暂停同步")
                    time.sleep(POLL_INTERVAL)
                    continue
                self._note_recovery("my_state", "我的账户状态获取已恢复正常，同步继续")

                # 3. 执行同步
                self.sync_positions(target_state, my_state)
                self.sync_open_orders(target_state, my_state)

                self._note_recovery("loop", "轮询循环已恢复正常")
                self._notify_startup_ok()  # 首个成功周期后发一次"运行正常"

            except Exception as e:
                logger.error(f"轮询出错: {e}")
                self._note_failure("loop", f"轮询循环抛出异常: {e}")

            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    # 用全局标志区分"用户主动停止"和"意外崩溃"
    _graceful_stop = False

    def _handle_sigterm(signum, frame):
        global _graceful_stop
        _graceful_stop = True
        logger.info("收到停止信号 (SIGTERM)，正在退出...")
        raise SystemExit(0)  # 中断内层 while True，触发外层 break

    signal.signal(signal.SIGTERM, _handle_sigterm)

    while not _graceful_stop:
        try:
            HyperliquidCopier().run()
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，停止运行")
            break
        except Exception as e:
            if _graceful_stop:
                break
            import traceback
            logger.error(f"程序意外崩溃: {e}\n{traceback.format_exc()}")
            logger.error("10秒后自动重启...")
            # 崩溃立即告警(同步直发：后台通知线程已随实例消亡)
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                ok, detail = feishu_send_sync(
                    f"🛑【程序崩溃】{ts}\n{e}\n10 秒后自动重启...")
                if not ok:
                    logger.error(f"崩溃告警发送失败: {detail}")
            except Exception as ne:
                logger.error(f"崩溃告警发送异常: {ne}")
            time.sleep(10)
