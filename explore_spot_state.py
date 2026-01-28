from hyperliquid.info import Info
from hyperliquid.utils import constants
import json

info = Info(constants.MAINNET_API_URL, skip_ws=True)
# 随便找个地址，或者用默认的 TARGET_ADDRESS
addr = "0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637"
try:
    state = info.spot_user_state(addr)
    print("Spot User State Keys:", state.keys())
    print("Balances:", json.dumps(state.get('balances', [])[:2], indent=2))
except Exception as e:
    print(e)
