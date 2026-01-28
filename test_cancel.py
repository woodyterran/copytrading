import os
import logging
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_orders():
    private_key = os.getenv("MY_PRIVATE_KEY")
    account = Account.from_key(private_key)
    address = account.address
    logger.info(f"My Address: {address}")

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=address)

    # 1. List Orders
    orders = info.open_orders(address)
    logger.info(f"Raw Orders count: {len(orders)}")
    for o in orders:
        logger.info(f"Order: {o['coin']} {o['side']} {o['limitPx']} oid={o['oid']}")

    # 2. Check Spot Meta
    spot_universe = set()
    spot_token_to_pair = {}
    try:
        spot_meta = info.spot_meta()
        for u in spot_meta["universe"]:
            spot_universe.add(u['name'])
            base_idx, quote_idx = u['tokens']
            base_token = spot_meta["tokens"][base_idx]['name']
            spot_token_to_pair[base_token] = u['name']
    except Exception as e:
        logger.error(f"Failed to load spot meta: {e}")

    logger.info(f"Spot Universe size: {len(spot_universe)}")

    # 3. Check is_spot logic
    for o in orders:
        coin = o['coin']
        if coin in spot_token_to_pair:
            coin = spot_token_to_pair[coin]
        
        is_spot = False
        if coin in spot_universe:
            is_spot = True
        
        logger.info(f"Coin: {o['coin']} -> {coin} | Is Spot: {is_spot}")

    # 4. Try Bulk Cancel if any
    if orders:
        logger.info("Attempting to cancel first order...")
        o = orders[0]
        # Normalize coin if needed? usually cancel takes the coin name from open_orders
        # But if it's spot, it might need the pair name?
        # The SDK bulk_cancel expects 'coin' and 'oid'.
        
        # Let's try with the raw coin name first, then normalized if failed.
        cancels = [{"coin": o['coin'], "oid": o['oid']}]
        logger.info(f"Cancelling: {cancels}")
        try:
            res = exchange.bulk_cancel(cancels)
            logger.info(f"Cancel Result: {res}")
        except Exception as e:
            logger.error(f"Cancel Failed: {e}")

if __name__ == "__main__":
    test_orders()
