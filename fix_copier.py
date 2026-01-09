import os

# 目标文件路径
file_path = "/Users/wanlong/wCloud/极空间双向同步/Trae/freqbot/跟单/hyperliquid_copy_trader.py"

# 正确的完整代码
correct_code = r'''import os
import time
import logging
import math
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

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

# 日志设置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = loggi  if not self.private_key or "YourPrivateKeyHere" in self.private_key:
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
        self.sz_decimals = {asset['name']: asset['szDecimals'] for asset in slogging.basic  return self.info.user_state(address)

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
        
        all_coins = set(target_positions.keys()) | set(my_positions.keys())
        
        for coin in all_coins:
            t_sz = target_positions.get(coin, 0.0)
            m_sz = my_posit        self.sz_decimals = {asset['name']: asset['szDecimals'] for asset in slogging.basic  retdiff) < 0.0001:
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
          res}")
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
            desired_sz = target_total_sz *    
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
                    logger.info(f"跟随挂单: {coin       @ {px}")
                    try:
                        self.exchange.order(coin, is_buy, sz_to_place, px, {"limit": {"tif": "Gtc"}})
                        time.sleep(0.1)
                    except Exception as e:
                        logger.error(f"挂单失败: {e}")

    def run(self):
        logger.info("跟单程序已启动...")
        while True:
            try:
                target_state = self.get_user_state(TARGET_ADDRESS)
                my_state = self.get_user_state(self.my_address)
                self.sync_positions(target_state, my_state)
                self.sync_open_orders(target_state, my_state)
            except Exception as e:
                logger.error(f"轮询错误: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    HyperliquidCopier().run()
'''

# 写入文件
with open(file_path, "w", encoding="utf-8") as f:
    f.write(correct_code)

print(f"✅ 文件已成功修复: {file_path}")
