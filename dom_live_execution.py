"""
dom_live_execution.py

LIVE TRADING PLUG-IN FOR DOM STRATEGY.

You will implement these two functions using your existing Binance
code from execute_trades.py.

IMPORTANT:
- This file is owned by YOU. The assistant does not put any live
  Binance calls here.
- When these functions are implemented, execute_dom_trade.py will
  start placing real trades when the plan requires it.
"""

from typing import Optional


def sell_all_to_usdc(token: str) -> None:
    """
    Sell all free balance of `token` into USDC using a MARKET order.

    HIGH-LEVEL STEPS (you implement using your existing code):

    1. Get your Binance client (whatever you use in execute_trades.py)
       e.g. client = get_binance_client()

    2. Fetch free balance for `token`, e.g. get_free_balance("SOL").
       - If balance is None or very small (< ~$1 worth), return early.

    3. Determine the trading symbol, e.g. f"{token}USDC" (BTCUSDC, SOLUSDC, ...).

    4. Optionally fetch symbol info / price filters to round quantity
       correctly to exchange step sizes.

    5. Place a MARKET SELL order for the full quantity.

    6. Log the result (and optionally send a Telegram error if it fails).

    PSEUDOCODE SHAPE (this is NOT real code):

        balance = get_free_balance(token)
        if balance_value_usd < 1:
            return

        symbol = f"{token}USDC"
        qty = adjust_to_lot_size(balance, symbol)
        place_market_sell(symbol, qty)

    You should COPY the relevant helper functions and calls
    from execute_trades.py and reuse them here.
    """
    raise NotImplementedError("Implement sell_all_to_usdc(token) using your Binance API code.")


def buy_with_all_usdc(target_token: str) -> None:
    """
    Buy `target_token` with ALL available USDC using a MARKET order.

    HIGH-LEVEL STEPS (you implement using your existing code):

    1. Get your Binance client.

    2. Fetch free USDC balance (e.g. get_free_balance("USDC")).
       - If < $1, return early.

    3. Determine the symbol, e.g. f"{target_token}USDC".

    4. Optionally fetch the current price and symbol filters to compute
       the correct quantity in base units.

    5. Compute the order size so that you use (almost) the full USDC
       balance without violating filters.

    6. Place a MARKET BUY order.

    7. Log the result (and optionally send Telegram error if it fails).

    PSEUDOCODE SHAPE (not real code):

        usdc_balance = get_free_balance("USDC")
        if usdc_balance < 1:
            return

        symbol = f"{target_token}USDC"
        price = get_last_price(symbol)
        qty = adjust_to_lot_size(usdc_balance / price, symbol)
        place_market_buy(symbol, qty)

    Again, COPY appropriate helper calls from execute_trades.py.
    """
    raise NotImplementedError("Implement buy_with_all_usdc(target_token) using your Binance API code.")
