from hyperliquid.info import Info
from hyperliquid.utils import constants
import json

info = Info(constants.MAINNET_API_URL, skip_ws=True)
spot_meta = info.spot_meta()
# 打印前 3 个 spot asset
print(json.dumps(spot_meta['universe'][:3], indent=2))
