# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.hyperliquid import HyperLiquidClient


class HyperLiquidNativeClient(HyperLiquidClient):
    exchange = "hyperliquid"
    dex_prefix = ""
