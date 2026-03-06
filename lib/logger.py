# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Sleep is overrated anyway
import os
import sys

from loguru import logger

__all__ = ["logger"]

logger.level("TRACE", icon=".")
logger.level("DEBUG", icon="·")
logger.level("INFO", icon="i")
logger.level("WARNING", icon="!")
logger.level("ERROR", icon="x")
logger.level("SUCCESS", icon="+")
logger.level("CRITICAL", icon="#")


_GRP_COLORS = ["magenta", "cyan", "yellow", "blue", "green", "red"]


def _grp_color(name: str) -> str:
    return _GRP_COLORS[sum(ord(c) for c in name) % len(_GRP_COLORS)]


def formatter(record):
    time = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>"
    # level = "<level>{level.name:<8}</level>"
    level = "<level>{level.icon}</level>"
    message = "<level>{message}</level>"

    pre = []
    if grp := record["extra"].get("group"):
        color = _grp_color(grp)
        pre.append(f"<{color}>{grp}</{color}>")

    if acc := record["extra"].get("account"):
        pre.append(f"<cyan>{acc}</cyan>")

    pre = "/".join(pre)
    pre = f"{pre} › " if pre else ""
    message = f"{pre}{message}"

    extra = sorted(record["extra"].items(), key=lambda x: x[0])
    extra = [(k, v) for k, v in extra if k not in ("account", "group")]
    extra = [f"<cyan>{k}</cyan>=<yellow>{v}</yellow>" for k, v in extra]
    extra = " ".join(extra)
    extra = f" {extra}" if extra else ""

    return f"{time} | {level} | {message}{extra}\n"


# https://github.com/Delgan/loguru/blob/0.7.3/loguru/_defaults.py#L32-L38
level = (os.environ.get("LOGURU_LEVEL") or "DEBUG").upper()
logger.remove()
logger.add(sys.stderr, format=formatter, level=level)
