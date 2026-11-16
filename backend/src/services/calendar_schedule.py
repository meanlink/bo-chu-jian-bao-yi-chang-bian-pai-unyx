"""按接收地时区的日历计划计算。

计划由「时区 + 星期集合 + 当地钟点」定义，每个当地日期独立换算成 UTC 时刻，
而不是在固定秒数上累加（累加会随夏令时漂移）。

规则：
- 跳时（春季拨快）：当地钟点不存在，顺延到该钟点之后的首个有效时刻
  （例如纽约 02:30 不存在，则顺延到 03:00）。
- 重复钟点（秋季拨慢）：同一当地钟点出现两次，只采用较早的 UTC 时刻。

本模块只做纯计算，不依赖数据库，便于单元检查。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_LABELS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
_DAY = timedelta(days=1)
_SEARCH_DAYS = 10  # 星期集合保证 7 天内命中；跳时/整日跳过再多留几天余量


class CalendarError(ValueError):
    """计划定义不合法。"""


@dataclass(frozen=True)
class Slot:
    scheduled_for: float        # UTC 时间戳（秒）
    local: datetime             # 接收地时区的实际执行时刻（含 tzinfo）
    offset: int                 # 该时刻接收地相对 UTC 的偏移秒数
    nominal: datetime           # 计划中写的当地钟点（naive）
    ambiguous: bool = False     # 秋季重复钟点，仅取较早一次
    skipped: bool = False       # 春季跳时，已顺延到首个有效时刻

    @property
    def local_text(self) -> str:
        return self.local.strftime('%Y-%m-%d %H:%M')

    @property
    def offset_text(self) -> str:
        minutes = self.offset // 60
        sign = '+' if minutes >= 0 else '-'
        minutes = abs(minutes)
        return f'UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}'

    @property
    def note(self) -> str:
        if self.skipped:
            if self.local.date() != self.nominal.date():
                return (f'当地日期被时区调整跳过，{self.nominal.strftime("%m-%d %H:%M")} 不存在，'
                        f'顺延至 {self.local.strftime("%m-%d %H:%M")}')
            return (f'夏令时跳时，{self.nominal.strftime("%H:%M")} 不存在，'
                    f'顺延至 {self.local.strftime("%H:%M")}')
        if self.ambiguous:
            return '夏令时重复钟点，仅发送较早一次'
        return ''


def load_zone(name: str) -> ZoneInfo:
    name = (name or '').strip()
    if not name:
        raise CalendarError('请选择接收地时区')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CalendarError(f'无法识别的时区：{name}')


def parse_hhmm(value: str) -> tuple[int, int]:
    text = (value or '').strip()
    parts = text.split(':')
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise CalendarError('钟点格式应为 HH:MM，例如 08:30')
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise CalendarError('钟点超出范围，应在 00:00 至 23:59 之间')
    return hour, minute


def normalize_weekdays(values) -> list[int]:
    if values is None:
        raise CalendarError('请选择星期')
    if isinstance(values, (str, bytes)):
        raise CalendarError('星期应为 1 至 7 的整数列表（周一为 1）')
    try:
        cleaned = sorted({int(v) for v in values})
    except (TypeError, ValueError):
        raise CalendarError('星期应为 1 至 7 的整数列表（周一为 1）')
    if not cleaned:
        raise CalendarError('至少选择一个星期')
    if any(v < 1 or v > 7 for v in cleaned):
        raise CalendarError('星期应为 1 至 7 的整数列表（周一为 1）')
    return cleaned


def normalize_local_time(value: str) -> str:
    hour, minute = parse_hhmm(value)
    return f'{hour:02d}:{minute:02d}'


def weekday_text(values) -> str:
    days = normalize_weekdays(values)
    if len(days) == 7:
        return '每天'
    if days == [1, 2, 3, 4, 5]:
        return '每个工作日'
    return '、'.join(WEEKDAY_LABELS[d - 1] for d in days)


def _wall(ts: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).replace(tzinfo=None)


def _resolve(tz: ZoneInfo, day: date, hour: int, minute: int) -> Slot:
    naive = datetime.combine(day, time(hour, minute))
    fold0 = naive.replace(tzinfo=tz, fold=0)
    utc0 = fold0.astimezone(timezone.utc)
    if utc0.astimezone(tz).replace(tzinfo=None) == naive:
        # 该当地钟点存在。重复钟点 fold=0 即较早的 UTC 时刻。
        fold1 = naive.replace(tzinfo=tz, fold=1)
        ambiguous = fold1.utcoffset() != fold0.utcoffset()
        return Slot(utc0.timestamp(), fold0, int(fold0.utcoffset().total_seconds()), naive, ambiguous=ambiguous)

    # 该当地钟点不存在（跳时）。在两个候选偏移之间二分查找首个「当地钟面 >= 目标」
    # 的有效时刻，即顺延后的首个有效时刻。
    fold1_utc = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    lo = int(min(utc0, fold1_utc).timestamp()) - 1
    hi = int(max(utc0, fold1_utc).timestamp()) + 1
    for _ in range(4):  # 整日被跳过的极端时区（如 Pacific/Apia 2011-12-30）留足余量
        if _wall(hi, tz) >= naive:
            break
        hi += 86400
    if _wall(hi, tz) < naive:
        raise CalendarError('无法为该钟点找到有效的当地时刻')
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _wall(mid, tz) >= naive:
            hi = mid
        else:
            lo = mid
    moment = datetime.fromtimestamp(hi, timezone.utc).astimezone(tz)
    return Slot(float(hi), moment, int(moment.utcoffset().total_seconds()), naive, skipped=True)


def slots_between(tz_name: str, weekdays, local_time: str, start_ts: float, end_ts: float) -> list[Slot]:
    """返回 (start_ts, end_ts] 内按规则应发的场次，按时间升序；同一 UTC 时刻只保留一次。"""
    if end_ts <= start_ts:
        return []
    tz = load_zone(tz_name)
    days = normalize_weekdays(weekdays)
    hour, minute = parse_hhmm(local_time)
    first_day = datetime.fromtimestamp(start_ts, tz).date() - _DAY
    last_day = datetime.fromtimestamp(end_ts, tz).date() + 2 * _DAY
    seen: set[float] = set()
    result: list[Slot] = []
    day = first_day
    while day <= last_day:
        if day.isoweekday() in days:
            slot = _resolve(tz, day, hour, minute)
            if start_ts < slot.scheduled_for <= end_ts and slot.scheduled_for not in seen:
                seen.add(slot.scheduled_for)
                result.append(slot)
        day += _DAY
    result.sort(key=lambda s: s.scheduled_for)
    return result


def next_slot(tz_name: str, weekdays, local_time: str, after_ts: float) -> Slot:
    tz = load_zone(tz_name)
    days = normalize_weekdays(weekdays)
    hour, minute = parse_hhmm(local_time)
    start_date = datetime.fromtimestamp(after_ts, tz).date() - _DAY
    for offset in range(_SEARCH_DAYS + 2):
        day = start_date + timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        slot = _resolve(tz, day, hour, minute)
        if slot.scheduled_for > after_ts:
            return slot
    raise CalendarError('无法计算下一次执行时刻')


def preview(tz_name: str, weekdays, local_time: str, after_ts: float, count: int = 6) -> list[Slot]:
    count = max(1, min(int(count), 30))
    slots = slots_between(tz_name, weekdays, local_time, after_ts, after_ts + 45 * 86400)
    if len(slots) < count:
        end = after_ts + 400 * 86400
        slots = slots_between(tz_name, weekdays, local_time, after_ts, end)
    return slots[:count]


def schedule_label(schedule_type: str, interval_seconds, tz_name, weekdays, local_time) -> str:
    if schedule_type == 'calendar':
        try:
            return f'{weekday_text(weekdays)} {normalize_local_time(local_time)}（{tz_name}）'
        except CalendarError:
            return '日历计划（配置不完整）'
    seconds = int(interval_seconds or 0)
    if seconds and seconds % 86400 == 0:
        return f'每 {seconds // 86400} 天'
    if seconds and seconds % 3600 == 0:
        return f'每 {seconds // 3600} 小时'
    if seconds and seconds % 60 == 0:
        return f'每 {seconds // 60} 分钟'
    return f'每 {seconds} 秒'
