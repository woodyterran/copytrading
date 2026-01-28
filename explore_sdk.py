from hyperliquid.info import Info
from hyperliquid.utils import constants
import inspect

info = Info(constants.MAINNET_API_URL, skip_ws=True)

print("Info methods containing 'spot':")
for name, method in inspect.getmembers(info):
    if 'spot' in name.lower():
        print(name)

from hyperliquid.exchange import Exchange
print("\nExchange methods containing 'spot':")
# Exchange 需要 account 初始化，这里只看类定义
for name, method in inspect.getmembers(Exchange):
    if 'spot' in name.lower():
        print(name)
