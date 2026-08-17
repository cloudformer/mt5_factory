from .breakout import Breakout
from .intraday_multi import IntradayMulti
from .daily_zone import DailyZone
from .fable import Fable
from .ma_cross import MaCross
from .pullback import Pullback
from .reversion import Reversion
from .session_orb import SessionOrb
from .squeeze import Squeeze
from .weekly_day import WeeklyDay

TEMPLATES = {
    "ma_cross": MaCross,
    "daily_zone": DailyZone,
    "breakout": Breakout,
    "intraday_multi": IntradayMulti,
    "session_orb": SessionOrb,
    "weekly_day": WeeklyDay,
    "fable": Fable,
    "reversion": Reversion,
    "pullback": Pullback,
    "squeeze": Squeeze,
}
