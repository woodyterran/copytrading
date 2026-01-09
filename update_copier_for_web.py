import os

target_file = "/Users/wanlong/wCloud/极空间双向同步/Trae/fre        ing
import math
import sys
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# 加载环境变量
load_dotenv()

# --- 配置区域 (优先从环境变量读取) ---
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637")
COPY_RATIO = float(os.getenv("COPY_RATIO", "0.1"))
POLL_INTERVAL = 5
POSITION_DIFF_THRESHOLD_USD = 10.0
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.02"))

# 日志设置 (输出到控制台，会被 Streamlit 重定向到文件)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
t__(self):
        self.private_key = os.getenv("MY_PRIVATE_KEY")
        if not self.private_key or "YourPrivateKeyHere" in self.private_key:
            logger.error("私钥未配置！请在前端配置私钥。")
            raise ValueError("私钥未配置")
        
        self.account = Account.from_key(self.private_key)
        self.my_address = self.account.address
        logger.info(f"启动跟单程序 | 我的地址: {self.my_address}")
        logger.info(f"目标地址: {TARGET_ADDRESS} | 跟单比例: {COPY_RATIO}")

        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(self.account, constants.MAINNET_API_URL, account_address=self.my_address)
        
        self.meta = self.info.meta()
        self.coin_to_asset = {asset['name']: i for i, asset in enumerate(self.meta['universe'])}
        self.sz_decimals = {asset['name']: asset['szDecimals'] for asset in self.meta['universe']}

    def get_user_state(self, address):
        return self.info.user_state(address)

    def round_sz(self, coin, sz):
        decimals = self.sz_decimals.get(coin, 4)
         def round_px(self, coin, px):
        return float(f"{px:.6g}")

    def sync_positions(self, target_state, my_state):
        t_pos = {p['coin']: float(p['szi']) for p in target_state['assetPositions']}
        m_pos = {p['coin']: float(p['szi']) for p in my_state['assetPositions']}
        
        all_coins = set(t_pos.keys()) | set(m_pos.keys())
        
        for coin in all_coins:
            t_sz = t_pos.get(coin, 0.0)
            m_sz = m_pos.get(coin, 0.0)
            
            desired_sz = t_sz * COPY_RATIO
            diff = desired_sz - m_sz
            
            if abs(diff) < 0.0001:
                continue

            price_ctx = self.info.all_mids()
            current_price = float(price_ctx.get(coin, 0.0))
            if current_price == 0:
                continue

            diff_usd = abs(diff) * current_price
            
            if diff_usd > POSITION_DIFF_THRESHOLD_USD:
                logger.warning(f"[{coin}] 仓位偏差 | 目标: {t_sz}, 我: {m_sz}, 需调整: {d:.2f})")
                
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
                        logger.error(f"[{coin}] 市价单失败: {res}")
                except Exception as e:
                    logger.error(f"[{coin}] 下单异常: {e}")

    def sync_open_orders(self, target_state, my_state):
        target_orders = target_state['openOrders']
        my_orders = my_state['openOrders']
        
        def get_fingerprint(o):
            return (o['coin'], o['side'], self.round_px(o['coin'], float(o['limitPx'rget_orders:
            fp = get_fingerprint(o)
            t_map[fp] = t_map.get(fp, 0.0) + float(o['sz'])

        m_map = {}
        m_orders_by_fp = {} 
        for o in my_orders:
            fp = get_fingerprint(o)
            m_map[fp] = m_map.get(fp, 0.0) + float(o['sz'])
            if fp not in m_orders_by_fp: m_orders_by_fp[fp] = []
            m_orders_by_fp[fp].append(o)

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
                logger.error(f"撤单失r fp, target_total_sz in t_map.items():
            coin, side, px = fp
            my_total_sz = m_map.get(fp, 0.0)
            desired_sz = target_total_sz * COPY_RATIO
            
            if my_total_sz < desired_sz * 0.9:
                sz_to_place = self.round_sz(coin, desired_sz - my_total_sz)
                if sz_to_place > 0:
                    is_buy = (side == 'B')
                    logger.info(f"跟随挂单: {coin} {'买' if is_buy else '卖'} {sz_to_place} @ {px}")
                    try:
                        self.exchange.order(coin, is_buy, sz_to_place, px, {"limit": {"tif": "Gtc"}})
                        time.sleep(0.1)
                    except Exception as e:
                        logger.error(f"挂单失败: {e}")

    def run(self):
        logger.info("跟单程序已启动 (守护进程模式)...")
        while True:
            try:
                target_state = self.get_user_state(TARGET_ADDRESS)
                my_state = self.get_user_state(self.my_address)
    my_state)
                self.sync_open_orders(target_state, my_state)
            except Exception as e:
                logger.error(f"轮询错误: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        HyperliquidCopier().run()
    except Exception as e:
        logger.fatal(f"程序崩溃: {e}")
        sys.exit(1)
'''

with open(target_file, "w", encoding="utf-8") as f:
    f.write(content)

print(f"跟单脚本已更新，支持 Web 控制: {target_file}")
