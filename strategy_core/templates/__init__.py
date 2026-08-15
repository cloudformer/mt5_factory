from .breakout import Breakout
from .intraday_multi import IntradayMulti
from .daily_zone import DailyZone
from .ma_cross import MaCross

TEMPLATES = {
    "ma_cross": MaCross,
    "daily_zone": DailyZone,
    "breakout": Breakout,
    "intraday_multi": IntradayMulti,
}
