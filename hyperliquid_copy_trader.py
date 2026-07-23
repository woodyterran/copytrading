import os
import time
import signal
import subprocess
import logging
import math
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import database as db

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

OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/Users/woodymacmini/.npm-global/bin/openclaw")

# 通知文件路径 (供 OpenClaw 读取)
NOTIFY_FILE = os.getenv("NOTIFY_FILE", "/Users/woodymacmini/LocalApp/OpenClaw/runtime_home/.openclaw/workspace/monitoring/notify.json")

# 统计文件路径 (供早报汇总)
STATS_FILE = os.getenv("STATS_FILE", "/Users/woodymacmini/LocalApp/OpenClaw/runtime_home/.openclaw/workspace/monitoring/daily_stats.json")
IMESSAGE_TARGET = os.getenv("IMESSAGE_TARGET", "woodyterran@gmail.com")
# 飞书通知目标 chat_id (p2p 会话)
FEISHU_TARGET = os.getenv("FEISHU_TARGET", "oc_e8f5806f393d77eb4cc3b770f1b3e50b")
OPENCLAW_DRY_RUN = os.getenv("OPENCLAW_DRY_RUN", "0") == "1"
MONITOR_ONLY = os.getenv("MONITOR_ONLY", "0") == "1"

# 日志设置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
                logger.info(f"[DEBUG] 原始挂单获取: {len(orders)} 个 | 地址: {address}")
                
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
                    logger.info(f"[DEBUG] 挂单检查: {original_coin} -> {o['coin']} | IsSpot: {is_spot} | MarketTypes: {MARKET_TYPES}")
                
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
                except Exception as e:
                    logger.error(f"[{coin}] 下单异常: {e}")

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
        
        # 1. 构建指纹映射
        # 指纹: (coin, side, price, sz) -> 详情
        def get_order_key(o):
            return (o['coin'], o['side'], self.round_px(o['coin'], float(o['limitPx'])), self.round_sz(o['coin'], float(o['sz'])))

        target_map = {get_order_key(o): o for o in target_orders}
        my_map = {get_order_key(o): o for o in my_orders}
        
        # 2. 检测变化 (基于目标挂单的指纹集合)
        current_target_keys = set(target_map.keys())

        # 防抖：目标挂单集合需连续两轮一致才执行同步，过滤 API 脏读导致的反复撤挂
        prev_raw = self._prev_target_order_keys
        self._prev_target_order_keys = current_target_keys
        if prev_raw != current_target_keys:
            # 尚未连续两轮一致（含首轮），等待下轮确认
            if self.last_target_keys is not None:
                logger.info(f"挂单读数变化未确认 (本轮 {len(current_target_keys)} 笔)，等待下轮确认，本轮跳过")
            return

        # 如果是第一次运行，或者 target_keys 发生了变化，则执行同步
        if self.last_target_keys == current_target_keys:
            return

        if self.last_target_keys is not None:
            self.send_imessage_notification(target_orders)

        logger.info(f"检测到挂单变化，开始全量同步... (目标挂单数: {len(target_orders)})")

        # 3. 取消我账户中已有的所有挂单
        if my_orders:
            cancels = [{"coin": o['coin'], "oid": o['oid']} for o in my_orders]
            logger.info(f"取消我账户所有挂单 ({len(cancels)} 个)")
            try:
                res = self.exchange.bulk_cancel(cancels)
                if res['status'] == 'ok':
                    logger.info("撤单请求已发送")
                else:
                    logger.error(f"撤单请求失败: {res}")
                    return  # 撤单失败，放弃本次同步，防止保证金不足

                # 撤单后稍微等待，让 margin 释放生效
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"撤单异常: {e}")
                return  # 撤单异常，放弃本次同步，防止保证金不足

        # 4. 准备下单列表 (全部目标挂单)
        to_create = list(target_orders)
        
        if not to_create:
            self.last_target_keys = current_target_keys
            return

        # 5. 排序: 从高价往低价 (Price DESC)
        # key 结构: (coin, side, price)
        # 这里直接用 target_order 对象排序
        to_create.sort(key=lambda x: self.round_px(x['coin'], float(x['limitPx'])), reverse=True)
        
        # 6. 逐个下单直到保证金不足
        logger.info(f"计划执行 {len(to_create)} 个挂单，按价格从高到低...")
        
        for target_order in to_create:
            coin = target_order['coin']
            side = target_order['side']
            px = self.round_px(coin, float(target_order['limitPx']))
            
            # 计算跟单数量
            target_sz = float(target_order['sz'])
            sz_to_place = self.round_sz(coin, target_sz * COPY_RATIO)
            
            if sz_to_place == 0:
                continue

            is_buy = (side == 'B')
            
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
                            logger.warning("⚠️ 保证金不足，停止继续挂单")
                            break
                    else:
                        pass # 成功
                else:
                    logger.error(f"挂单请求失败: {res}")
            except Exception as e:
                logger.error(f"挂单异常: {e}")
                if 'margin' in str(e).lower():
                    logger.warning("⚠️ 捕获保证金异常，停止继续挂单")
                    break
            
            # 稍微间隔一下避免速率限制
            time.sleep(0.1)

        # 更新状态指纹
        self.last_target_keys = current_target_keys

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
            self.send_imessage_notification(target_orders, added_keys, removed_keys)

        self.last_target_keys = current_target_keys

    def _notify_env(self):
        """构造 openclaw 子进程的环境变量，确保 PATH 包含 node"""
        env = os.environ.copy()
        extra_paths = ["/usr/local/bin", "/usr/bin", "/opt/homebrew/bin",
                       os.path.expanduser("~/.npm-global/bin")]
        current_path = env.get("PATH", "")
        for p in extra_paths:
            if p not in current_path:
                current_path = p + ":" + current_path
        env["PATH"] = current_path
        return env

    def send_feishu_text(self, text):
        """通过 OpenClaw 发送飞书通知"""
        if not text: return
        try:
            cmd = [OPENCLAW_BIN, "message", "send",
                   "--channel", "feishu",
                   "--target", FEISHU_TARGET,
                   "--message", text]
            if OPENCLAW_DRY_RUN:
                cmd.append("--dry-run")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=self._notify_env())
            if result.returncode == 0:
                logger.info(f"飞书通知已发送: {text[:30]}...")
            else:
                logger.error(f"飞书通知发送失败 (code {result.returncode}): {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.error("飞书通知发送超时 (30s)")
        except Exception as e:
            logger.error(f"发送飞书通知失败: {e}")

    def send_imessage_text(self, text):
        """通过 OpenClaw 发送通知：同时推送 iMessage 和飞书"""
        if not text: return
        self.send_feishu_text(text)
        try:
            cmd = [OPENCLAW_BIN, "message", "send",
                   "--channel", "imessage",
                   "--target", IMESSAGE_TARGET,
                   "--message", text]
            if OPENCLAW_DRY_RUN:
                cmd.append("--dry-run")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=self._notify_env())
            if result.returncode == 0:
                logger.info(f"iMessage通知已发送: {text[:30]}...")
            else:
                logger.error(f"iMessage通知发送失败 (code {result.returncode}): {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.error("iMessage通知发送超时 (30s)")
        except Exception as e:
            logger.error(f"发送iMessage失败: {e}")

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

    def send_imessage_notification(self, target_orders, added_keys=None, removed_keys=None):
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
            self.send_imessage_text("\n".join(lines))

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
                    self.send_imessage_text(msg)
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
                    self.send_imessage_text(msg)
                    self.write_notify_file("trade", f"[补发]{fill['coin']} {side_text} {fill['sz']}@{fill['px']}")

            # 初始化仓位/挂单快照（不发通知）
            target_state = self.get_user_state(TARGET_ADDRESS)
            if target_state:
                self.update_history(target_state, notify=False)
        except Exception as e:
            logger.warning(f"初始化缓存失败: {e}")

    def run(self):
        logger.info("跟单程序已启动...")
        if MONITOR_ONLY:
            logger.info("运行模式: 仅监控通知")
            
        # 启动自检通知
        try:
            mode_str = "仅监控" if MONITOR_ONLY else "跟单模式"
            self.send_imessage_text(f"【系统启动】{mode_str}已启动\n目标: {TARGET_ADDRESS[:6]}...")
        except Exception as e:
            logger.error(f"启动通知发送失败: {e}")
            
        # 启动前先初始化缓存
        self.init_history_cache()
        
        while True:
            try:
                # 1. 获取目标状态
                target_state = self.get_user_state(TARGET_ADDRESS)
                
                if target_state is None:
                    logger.warning(f"获取目标状态失败，跳过本次同步")
                    time.sleep(POLL_INTERVAL)
                    continue

                # 无论什么模式，都更新历史并检查成交通知
                self.update_history(target_state, notify=True)

                if MONITOR_ONLY:
                    self.monitor_open_orders(target_state)
                    time.sleep(POLL_INTERVAL)
                    continue

                # 2. 获取我的状态
                my_state = self.get_user_state(self.my_address)

                
                if my_state is None:
                    logger.warning(f"获取我的状态失败 (可能由于网络或API限制)，跳过本次同步")
                    time.sleep(POLL_INTERVAL)
                    continue
                
                # 3. 执行同步
                self.sync_positions(target_state, my_state)
                self.sync_open_orders(target_state, my_state)
                
            except Exception as e:
                logger.error(f"轮询出错: {e}")
            
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
            time.sleep(10)
