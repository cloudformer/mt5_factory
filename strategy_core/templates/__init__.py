from .breakout import Breakout
from .intraday_multi import IntradayMulti
from .ma_cross import MaCross

TEMPLATES = {
    "ma_cross": MaCross,
    "breakout": Breakout,
    "intraday_multi": IntradayMulti,
}
