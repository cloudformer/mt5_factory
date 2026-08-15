from .breakout import Breakout
from .intraday_multi import IntradayMulti
from .daily_zone import DailyZone
from .ma_cross import MaCross
from .session_orb import SessionOrb
from .weekly_day import WeeklyDay

TEMPLATES = {
    "ma_cross": MaCross,
    "daily_zone": DailyZone,
    "breakout": Breakout,
    "intraday_multi": IntradayMulti,
    "session_orb": SessionOrb,
    "weekly_day": WeeklyDay,
}
