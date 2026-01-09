import os
import time
import logging
import math
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
logger = logging.getLogger(__name__)
load_dotenv()
            raise ValueError('错误: 请配置正确的私钥')
        factor = 10 ** decimals
        return math.floor(sz * factor) / factor

    def round_px(self, coin, px):
        return float(f'{px:.6g}')

    def sync_positions(self, target_state, my_state):
        m_pos = {p['coin']: float(p['szi']) for p in my_state['assetPositions']}
        for coin in set(t_pos.kekeys()):
