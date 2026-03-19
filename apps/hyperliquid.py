# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.hyperliquid import HyperLiquidClient
from lib.models import AccountConfig
from strategy import StrategyConfig, load_config


class HyperLiquidNativeClient(HyperLiquidClient):
    exchange = "hyperliquid"
    dex_prefix = ""


class Config(StrategyConfig):
    accounts: list[AccountConfig]

    @classmethod
    def load(cls, filepath: str):
        return load_config(cls, filepath)


def client_from_config(cfg: AccountConfig) -> HyperLiquidNativeClient:
    return HyperLiquidNativeClient(
        name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy
    )
