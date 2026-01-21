import os
import time
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
TARGET_ADDRESS = "0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637"

# 跟单比例 (例如 0.1 表示目标开 1 ETH，你开 0.1 ETH)
COPY_RATIO = 0.1 

# 轮询间隔 (秒)
POLL_INTERVAL = 5

# 仓位偏差阈值 (USD价值)
POSITION_DIFF_THRESHOLD_USD = 10.0

# 允许的最大滑点 (默认 2%)
SLIPPAGE = 0.02

# 跟单模式: 'full' (同步持仓) 或 'order' (仅同步下单)
# 从环境变量读取，默认为 'full'
SYNC_MODE = os.getenv("SYNC_MODE", "full")

# 日志设置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class HyperliquidCopier:
    def __init__(self):
        self.private_key = os.getenv("MY_PRIVATE_KEY")
        if not self.private_key or "YourPrivateKeyHere" in self.private_key:
            raise ValueError("错误: 请先在 .env 文件中填入正确的私钥 (MY_PRIVATE_KEY)")
        
        self.account = Account.from_key(self.private_key)
        self.my_address = self.account.address
        logger.info(f"启动跟单程序 | 我的地址: {self.my_address}")
        logger.info(f"目标地址: {TARGET_ADDRESS} | 跟单比例: {COPY_RATIO}")

        # 初始化 SDK
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(self.account, constants.MAINNET_API_URL, account_address=self.my_address)
        
        # 获取元数据
        self.meta = self.info.meta()
        self.coin_to_asset = {asset['name']: i for i, asset in enumerate(self.meta['universe'])}
        self.sz_decimals = {asset['name']: asset['szDecimals'] for asset in self.meta['universe']}
        
        # 模式2所需的基准状态
        self.target_baseline = {}
        self.my_baseline = {}
        self.initialized_baseline = False
        
        # 历史记录缓存 (去重用)
        self.seen_oids = set()
        self.seen_fill_hashes = set()
        self.last_position_snapshot = {}

        logger.info(f"跟单模式: {SYNC_MODE} ({'同步持仓' if SYNC_MODE == 'full' else '仅同步下单'})")

    def get_user_state(self, address):
        return self.info.user_state(address)

    def round_sz(self, coin, sz):
        """根据币种精度修剪数量"""
        decimals = self.sz_decimals.get(coin, 4)
        factor = 10 ** decimals
        return math.floor(sz * factor) / factor

    def round_px(self, coin, px):
        """保留有效数字"""
        return float(f"{px:.6g}")

    def sync_positions(self, target_state, my_state):
        """同步仓位 (市价单修补)"""
        target_positions = {p['coin']: float(p['szi']) for p in target_state['assetPositions']}
        my_positions = {p['coin']: float(p['szi']) for p in my_state['assetPositions']}
        
        # 模式2: 初始化基准
        if SYNC_MODE == 'order' and not self.initialized_baseline:
            self.target_baseline = target_positions.copy()
            self.my_baseline = my_positions.copy()
            self.initialized_baseline = True
            logger.info("已初始化 '仅同步下单' 模式的基准仓位，忽略初始差异。")
            return

        all_coins = set(target_positions.keys()) | set(my_positions.keys())
        
        for coin in all_coins:
            t_sz = target_positions.get(coin, 0.0)
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
                    res = self.exchange.market_open(coin, is_buy, rounded_sz, current_price, SLIPPAGE)
                    if res['status'] == 'ok':
                        logger.info(f"[{coin}] 市价单成交")
                    else:
                        logger.error(f"[{coin}] 下单失败: {res}")
                except Exception as e:
                    logger.error(f"[{coin}] 下单异常: {e}")

    def sync_open_orders(self, target_state, my_state):
        """同步挂单"""
        target_orders = target_state['openOrders']
        my_orders = my_state['openOrders']
        
        def get_fingerprint(o):
            return (o['coin'], o['side'], self.round_px(o['coin'], float(o['limitPx'])))

        t_map = {}
        for o in target_orders:
            fp = get_fingerprint(o)
            t_map[fp] = t_map.get(fp, 0.0) + float(o['sz'])

        m_map = {}
        m_orders_by_fp = {} 
        for o in my_orders:
            fp = get_fingerprint(o)
            m_map[fp] = m_map.get(fp, 0.0) + float(o['sz'])
            if fp not in m_orders_by_fp: m_orders_by_fp[fp] = []
            m_orders_by_fp[fp].append(o)

        # 撤单
        cancels = []
        for fp, my_total_sz in m_map.items():
            target_total_sz = t_map.get(fp, 0.0)
            desired_sz = target_total_sz * COPY_RATIO
            if target_total_sz == 0 or my_total_sz > desired_sz * 1.1:
                for o in m_orders_by_fp[fp]:
                    cancels.append({"asset": self.coin_to_asset[o['coin']], "oid": o['oid']})
        
        if cancels:
            logger.info(f"撤销 {len(cancels)} 个过时挂单")
            try:
                self.exchange.cancel_orders(cancels)
            except Exception as e:
                logger.error(f"撤单失败: {e}")

        # 挂单
        for fp, target_total_sz in t_map.items():
            coin, side, px = fp
            my_total_sz = m_map.get(fp, 0.0)
            desired_sz = target_total_sz * COPY_RATIO
            
            if my_total_sz < desired_sz * 0.9:
                sz_to_place = self.round_sz(coin, desired_sz - my_total_sz)
                if sz_to_place > 0:
                    is_buy = (side == 'B')
                    logger.info(f"跟随挂单: {coin} {side} {sz_to_place} @ {px}")
                    try:
                        self.exchange.order(coin, is_buy, sz_to_place, px, {"limit": {"tif": "Gtc"}})
                        time.sleep(0.1)
                    except Exception as e:
                        logger.error(f"挂单失败: {e}")

    def update_history(self, target_state):
        """更新历史记录到数据库"""
        try:
            # 1. 记录挂单
            # 注意: 这里只记录看到的 open orders。如果需要记录 cancel/fill，需要更复杂的逻辑或 stream。
            # 目前只记录出现过的挂单 (oid 唯一)
            for o in target_state['openOrders']:
                if o['oid'] not in self.seen_oids:
                    db.log_order(TARGET_ADDRESS, o)
                    self.seen_oids.add(o['oid'])
            
            # 2. 记录持仓 (仅当发生变化时)
            # 过滤掉 szi=0 的空仓位
            current_positions = {p['coin']: p for p in target_state['assetPositions'] if float(p['szi']) != 0}
            
            for coin, pos in current_positions.items():
                prev_pos = self.last_position_snapshot.get(coin)
                # 检查是否发生变化 (数量或入场价)
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
            # user_fills 接口获取最近成交
            fills = self.info.user_fills(TARGET_ADDRESS)
            for fill in fills:
                # 构建唯一标识，SDK 返回的 fill可能有 hash，也可能没有，用 tid+coin 兜底
                # 注意: fill 结构可能随 SDK 版本变化，这里做一定容错
                fill_hash = fill.get('hash') or f"{fill.get('tid')}_{fill.get('coin')}"
                
                if fill_hash not in self.seen_fill_hashes:
                    db.log_trade(TARGET_ADDRESS, fill)
                    self.seen_fill_hashes.add(fill_hash)
                    
        except Exception as e:
            # 历史记录错误不应中断主流程
            logger.error(f"历史记录更新失败: {e}")

    def run(self):
        logger.info("跟单程序已启动...")
        while True:
            try:
                # 1. 获取目标状态
                target_state = self.get_user_state(TARGET_ADDRESS)
                
                # 更新历史记录
                self.update_history(target_state)

                # 2. 获取我的状态
                my_state = self.get_user_state(self.my_address)
                
                # 3. 执行同步
                self.sync_positions(target_state, my_state)
                self.sync_open_orders(target_state, my_state)
                
            except Exception as e:
                logger.error(f"轮询出错: {e}")
            
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    HyperliquidCopier().run()
