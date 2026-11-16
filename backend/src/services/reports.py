"""分发计划与场次归档。

两种计划并存：
- interval：原有固定秒数周期，next_run 直接累加 interval_seconds，行为不变。
- calendar：按接收地时区 + 星期 + 当地钟点计算场次。每个场次落一行 report_occurrences，
  状态 pending（待发送）-> claimed（已认领）-> archived（已归档）；停机跨过的旧场次
  记为 missed，负责人改期作废旧场次记为 superseded。

并发与重试：
- 认领用条件 UPDATE（pending/过期认领 -> 自己的 claim_token），只有一个进程能改成功。
- 发布与归档在同一数据库事务内完成；提交前崩溃则整体回滚，重试仍从 pending 开始。
- 发布前检查该场次是否已有归档（Delivery.occurrence_id 唯一），崩溃恢复不会重复归档。
"""
import time, os, csv, io, hashlib, json, uuid
from datetime import datetime, timezone as dt_timezone
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from src.models import Job, Report, Delivery, ReportOccurrence, ReportRunOutcome
from src.config.settings import ARTIFACT_DIR
from src.services import calendar_schedule as cal
from src.services.audit import record
from src.constants.enums import ScheduleType, OccurrenceStatus

# 到期后的认领宽限：给「负责人改期」留出先手，避免同一瞬间发布旧场次。
CLAIM_GRACE_SECONDS = 2
# 认领租约：认领进程崩溃后，超过该时长仍未归档的场次允许被恢复并重新认领。
LEASE_SECONDS = 300
MAX_BACKFILL_DAYS = 400


def _now(now=None):
    return time.time() if now is None else now


def schedule_type(report) -> str:
    return report.schedule_type or ScheduleType.interval.value


def is_calendar(report) -> bool:
    return schedule_type(report) == ScheduleType.calendar.value


def occurrence_dict(o, delivery=None):
    return {
        'id': o.id, 'report_id': o.report_id, 'scheduled_for': o.scheduled_for,
        'timezone': o.timezone, 'local_time': o.local_time, 'utc_offset': o.utc_offset,
        'offset_text': _offset_text(o.utc_offset),
        'status': o.status, 'schedule_revision': o.schedule_revision,
        'claimed_at': o.claimed_at, 'archived_at': o.archived_at,
        'delivery_id': o.delivery_id, 'outcome_id': o.outcome_id, 'note': o.note,
        'filename': delivery.filename if delivery else None,
        'checksum': delivery.checksum if delivery else None,
    }


def delivery_dict(d):
    data = {k: getattr(d, k) for k in
            ['id', 'report_id', 'created_at', 'checksum', 'filename', 'destination',
             'occurrence_id', 'scheduled_for', 'scheduled_timezone', 'scheduled_local',
             'scheduled_offset', 'schedule_revision']}
    data['offset_text'] = _offset_text(d.scheduled_offset)
    return data


def _offset_text(offset):
    if offset is None:
        return None
    minutes = int(offset) // 60
    sign = '+' if minutes >= 0 else '-'
    minutes = abs(minutes)
    return f'UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}'


def report_dict(r, now=None):
    calendar = is_calendar(r)
    next_run = r.next_run
    if calendar and not next_run:
        # 只有在从未排过期时才按规则推导；已排定的下一场（含停机期间逾期的场次）
        # 原样展示，避免把待补发的场次藏起来。
        try:
            next_run = cal.next_slot(r.timezone, r.weekdays, r.local_time, _now(now)).scheduled_for
        except (cal.CalendarError, TypeError):
            next_run = r.next_run
    return {
        'id': r.id, 'name': r.name, 'job_id': r.job_id, 'destination': r.destination,
        'schedule_type': schedule_type(r),
        'interval_seconds': None if calendar else r.interval_seconds,
        'next_run': next_run,
        'enabled': r.enabled,
        'timezone': r.timezone, 'weekdays': r.weekdays, 'local_time': r.local_time,
        'schedule_revision': r.schedule_revision,
        'rules': cal.schedule_label(schedule_type(r), r.interval_seconds, r.timezone,
                                    r.weekdays, r.local_time),
        'paused_at': r.paused_at,
    }


def _reject(message: str, status: int = 422):
    raise HTTPException(status, message)


def _validate_calendar(data):
    try:
        timezone = (data.timezone or '').strip()
        cal.load_zone(timezone)
        weekdays = cal.normalize_weekdays(data.weekdays)
        local_time = cal.normalize_local_time(data.local_time or '')
    except cal.CalendarError as e:
        _reject(str(e))
    return timezone, weekdays, local_time


def _lock(db, report):
    """串行化同一计划的调度与改期：MySQL 下 SELECT ... FOR UPDATE，SQLite 为空操作。"""
    db.execute(select(Report.id).where(Report.id == report.id).with_for_update())


def create(db, data, now=None):
    now = _now(now)
    j = db.get(Job, data.job_id)
    if not j or j.status != 'completed' or not j.approved:
        raise HTTPException(409, '请先选择已完成且获批准的分析结果')
    if data.schedule_type == ScheduleType.calendar.value:
        timezone, weekdays, local_time = _validate_calendar(data)
        if data.delay_seconds:
            _reject('日历计划按当地钟点执行，不接受 delay_seconds')
        try:
            first = cal.next_slot(timezone, weekdays, local_time, now)
        except cal.CalendarError as e:
            _reject(str(e))
        r = Report(name=data.name, job_id=j.id, destination=data.destination,
                   schedule_type=ScheduleType.calendar.value, interval_seconds=0,
                   next_run=first.scheduled_for, timezone=timezone, weekdays=weekdays,
                   local_time=local_time, enabled=True, schedule_revision=1,
                   planned_through=now)
    else:
        r = Report(name=data.name, job_id=j.id, destination=data.destination,
                   schedule_type=ScheduleType.interval.value,
                   interval_seconds=data.interval_seconds,
                   next_run=now + data.delay_seconds, enabled=True, schedule_revision=1,
                   planned_through=now + data.delay_seconds)
    db.add(r)
    db.flush()
    if is_calendar(r):
        _materialize_due(db, r, now)
    return r


def _materialize_due(db, report, now):
    """把应发场次写入 occurrences（幂等），并预排下一场待发送场次。

    - (planned_through, now] 内的场次 -> pending（若跨过停机，会在 dispatch 中判定
      补发/错过）。
    - now 之后最近的一场也提前落为 pending，页面的「待发送」与改期作废都有据可依。
    - 停机再久也只回溯 MAX_BACKFILL_DAYS 天，避免场次无限增长。
    """
    cursor = report.planned_through or 0
    if report.paused_at:
        cursor = max(cursor, report.paused_at)
    try:
        if cursor < now:
            start = max(cursor, now - MAX_BACKFILL_DAYS * 86400)
            slots = cal.slots_between(report.timezone, report.weekdays, report.local_time,
                                      start, now)
        else:
            slots = []
        upcoming = cal.next_slot(report.timezone, report.weekdays, report.local_time, now)
    except cal.CalendarError:
        return []
    horizon = max([now] + [slot.scheduled_for for slot in slots])
    if upcoming.scheduled_for > horizon:
        slots.append(upcoming)
    fresh = []
    for slot in slots:
        existing = db.scalar(select(ReportOccurrence).where(
            ReportOccurrence.report_id == report.id,
            ReportOccurrence.scheduled_for == slot.scheduled_for).limit(1))
        if existing is not None:
            if (existing.status == OccurrenceStatus.superseded.value
                    and slot.scheduled_for > now):
                # 改期后新规则恰好落在被作废的同一时刻：复用该场次记录。
                existing.status = OccurrenceStatus.pending.value
                existing.schedule_revision = report.schedule_revision
                existing.note = slot.note
                existing.timezone = report.timezone
                existing.local_time = slot.local_text
                existing.utc_offset = slot.offset
            continue
        o = ReportOccurrence(report_id=report.id, scheduled_for=slot.scheduled_for,
                             timezone=report.timezone, local_time=slot.local_text,
                             utc_offset=slot.offset, status=OccurrenceStatus.pending.value,
                             schedule_revision=report.schedule_revision,
                             note=slot.note, created_at=now)
        try:
            with db.begin_nested():
                db.add(o)
                db.flush()
        except IntegrityError:
            continue
        fresh.append(o)
    if slots:
        report.planned_through = max(report.planned_through or 0,
                                     max(slot.scheduled_for for slot in slots))
    report.next_run = upcoming.scheduled_for
    db.flush()
    return fresh


def _claim(db, occurrence, now):
    """原子认领：pending 或租约过期的 claimed 场次 -> 自己的 claim_token。"""
    token = uuid.uuid4().hex
    stmt = update(ReportOccurrence).where(
        ReportOccurrence.id == occurrence.id,
    ).where(
        (ReportOccurrence.status == OccurrenceStatus.pending.value) |
        ((ReportOccurrence.status == OccurrenceStatus.claimed.value) &
         (ReportOccurrence.claimed_at < now - LEASE_SECONDS))
    ).values(status=OccurrenceStatus.claimed.value, claim_token=token, claimed_at=now)
    result = db.execute(stmt)
    db.flush()
    if result.rowcount != 1:
        return False
    occurrence.status = OccurrenceStatus.claimed.value
    occurrence.claim_token = token
    occurrence.claimed_at = now
    return True


def _mark_missed(db, occurrence, note):
    """CAS 标记错过；返回是否由本进程成功标记（并发下只会有一次）。"""
    result = db.execute(update(ReportOccurrence).where(
        ReportOccurrence.id == occurrence.id,
        ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                     OccurrenceStatus.claimed.value)),
    ).values(status=OccurrenceStatus.missed.value, note=note,
             claim_token=None, claimed_at=None))
    return result.rowcount == 1


def _local_text(occurrence):
    try:
        return datetime.fromtimestamp(occurrence.scheduled_for, dt_timezone.utc).astimezone(
            cal.load_zone(occurrence.timezone)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return datetime.fromtimestamp(occurrence.scheduled_for, dt_timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def dispatch(db, report, now=None, grace=CLAIM_GRACE_SECONDS):
    """处理一个日历计划到期的场次：补最近一场，更早的记为错过并说明。"""
    now = _now(now)
    archived = []
    _lock(db, report)
    _materialize_due(db, report, now)
    due = list(db.scalars(select(ReportOccurrence).where(
        ReportOccurrence.report_id == report.id,
        ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                     OccurrenceStatus.claimed.value)),
        ReportOccurrence.scheduled_for <= now - grace,
    ).order_by(ReportOccurrence.scheduled_for)))
    if not due:
        return archived
    # 只补到期集合里最新的一场；更早的一律错过，绝不「顺延」到下一场去发布。
    latest = due[-1]
    older = due[:-1]
    if not _claim(db, latest, now):
        # 最近一场已被另一调度进程认领或归档：由它补发，本进程本轮不发布。
        return archived
    missed = []
    for o in older:
        note = (f'停机跨过本场（原定 {_local_text(o)}），已由最近一场'
                f'（{_local_text(latest)}）补发替代，本场不再发布')
        if _mark_missed(db, o, note):
            missed.append(o.id)
    if missed:
        outcome = ReportRunOutcome(
            report_id=report.id, kind='recovered', created_at=now,
            catch_up_occurrence_id=latest.id,
            missed_occurrence_ids=missed,
            reason=f'调度中断跨过 {len(missed) + 1} 场应发时点，仅补发最近一场（'
                   f'{_local_text(latest)}），其余 {len(missed)} 场记为错过',
        )
        db.add(outcome)
        db.flush()
        db.execute(update(ReportOccurrence).where(
            ReportOccurrence.id.in_(missed)
        ).values(outcome_id=outcome.id))
        record(db, 'worker', 'report.occurrences.recovered',
               {'id': report.id, 'outcome': outcome.id, 'catch_up': latest.id,
                'missed': missed, 'catch_up_local': _local_text(latest)})
        db.flush()
    delivery = _publish(db, report, latest, now)
    if delivery:
        archived.append(delivery)
    db.flush()
    return archived


def _publish(db, report, occurrence, now):
    existing = db.scalar(select(Delivery).where(Delivery.occurrence_id == occurrence.id))
    if existing:
        # 上次进程在归档提交前中断留下的归档，直接补齐状态，不重复发布。
        db.execute(update(ReportOccurrence).where(
            ReportOccurrence.id == occurrence.id,
            ReportOccurrence.status == OccurrenceStatus.claimed.value,
        ).values(status=OccurrenceStatus.archived.value, archived_at=existing.created_at,
                 delivery_id=existing.id, claim_token=None))
        db.flush()
        return None
    j = db.get(Job, report.job_id)
    if not j or not j.approved:
        return None
    data = render_csv(j)
    delivery = Delivery(report_id=report.id, created_at=now,
                        checksum=hashlib.sha256(data).hexdigest(), filename='',
                        destination=report.destination,
                        occurrence_id=occurrence.id,
                        scheduled_for=occurrence.scheduled_for,
                        scheduled_timezone=occurrence.timezone,
                        scheduled_local=occurrence.local_time,
                        scheduled_offset=occurrence.utc_offset,
                        schedule_revision=occurrence.schedule_revision)
    db.add(delivery)
    db.flush()
    delivery.filename = f'report-{report.id}-delivery-{delivery.id}.csv'
    _write_artifacts(delivery, j, data)
    result = db.execute(update(ReportOccurrence).where(
        ReportOccurrence.id == occurrence.id,
        ReportOccurrence.status == OccurrenceStatus.claimed.value,
        ReportOccurrence.claim_token == occurrence.claim_token,
    ).values(status=OccurrenceStatus.archived.value, archived_at=now,
             delivery_id=delivery.id, claim_token=None))
    db.flush()
    if result.rowcount != 1:
        db.delete(delivery)
        db.flush()
        for name in (delivery.filename, delivery.filename + '.json'):
            try:
                os.remove(os.path.join(ARTIFACT_DIR, name))
            except OSError:
                pass
        return None
    occurrence.status = OccurrenceStatus.archived.value
    occurrence.archived_at = now
    occurrence.delivery_id = delivery.id
    return delivery


def render_csv(j):
    out = io.StringIO()
    out.write('﻿')
    table = j.result.get('table', {})
    cols = table.get('columns', [])
    writer = csv.writer(out)

    def safe(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        text = str(v)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text

    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows', []):
        writer.writerow([safe(row.get(c['key'], '')) for c in cols])
    return out.getvalue().encode('utf-8')


def _write_artifacts(delivery, j, data):
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR, delivery.filename), 'wb') as f:
        f.write(data)
    with open(os.path.join(ARTIFACT_DIR, delivery.filename + '.json'), 'w') as f:
        json.dump({'job': j.name, 'params': j.params, 'result': j.result,
                   'checksum': delivery.checksum,
                   'schedule': {'scheduled_for': delivery.scheduled_for,
                                'timezone': delivery.scheduled_timezone,
                                'local': delivery.scheduled_local,
                                'revision': delivery.schedule_revision}},
                  f, ensure_ascii=False, allow_nan=False)


def deliver(db, report, now=None, occurrence=None):
    """发布一次分发；日历计划必须带场次。"""
    now = _now(now)
    if is_calendar(report):
        if occurrence is None:
            return None
        return _publish(db, report, occurrence, now)
    return _interval_deliver(db, report, now)


def _interval_deliver(db, report, now):
    j = db.get(Job, report.job_id)
    if not j or not j.approved:
        return None
    # 原子推进 next_run：两个调度进程只有一个能改成功，另一个本轮跳过。
    expected = report.next_run
    advanced = db.execute(update(Report).where(
        Report.id == report.id, Report.next_run == expected,
    ).values(next_run=now + report.interval_seconds,
             planned_through=now + report.interval_seconds))
    db.flush()
    if advanced.rowcount != 1:
        return None
    report.next_run = now + report.interval_seconds
    report.planned_through = report.next_run
    data = render_csv(j)
    delivery = Delivery(report_id=report.id, created_at=now,
                        checksum=hashlib.sha256(data).hexdigest(), filename='',
                        destination=report.destination,
                        scheduled_for=now, scheduled_timezone='UTC',
                        scheduled_local=datetime.fromtimestamp(now, dt_timezone.utc).strftime('%Y-%m-%d %H:%M'),
                        scheduled_offset=0, schedule_revision=report.schedule_revision)
    db.add(delivery)
    db.flush()
    delivery.filename = f'report-{report.id}-delivery-{delivery.id}.csv'
    _write_artifacts(delivery, j, data)
    return delivery


def run_tick(db, now=None, grace=CLAIM_GRACE_SECONDS):
    """一次调度轮询；返回本轮归档的 (report_id, delivery)。"""
    now = _now(now)
    archived = []
    reports = list(db.scalars(select(Report).where(Report.enabled == True).order_by(Report.id)))
    for r in reports:
        j = db.get(Job, r.job_id)
        if not j or not j.approved:
            continue
        if is_calendar(r):
            archived.extend((r.id, d) for d in dispatch(db, r, now=now, grace=grace))
        elif r.next_run <= now:
            d = _interval_deliver(db, r, now)
            if d:
                archived.append((r.id, d))
    db.flush()
    return archived


def reschedule(db, report, data, now=None):
    """负责人改期：旧规则下未发布的场次作废，已归档场次保留原定时刻和时区。"""
    now = _now(now)
    _lock(db, report)
    if data.schedule_type == ScheduleType.calendar.value:
        timezone, weekdays, local_time = _validate_calendar(data)
        if data.delay_seconds:
            _reject('日历计划按当地钟点执行，不接受 delay_seconds')
        try:
            first = cal.next_slot(timezone, weekdays, local_time, now)
        except cal.CalendarError as e:
            _reject(str(e))
    pending = list(db.scalars(select(ReportOccurrence).where(
        ReportOccurrence.report_id == report.id,
        ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                     OccurrenceStatus.claimed.value)),
    ).order_by(ReportOccurrence.scheduled_for)))
    superseded = []
    for o in pending:
        result = db.execute(update(ReportOccurrence).where(
            ReportOccurrence.id == o.id,
            ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                         OccurrenceStatus.claimed.value)),
        ).values(status=OccurrenceStatus.superseded.value, claim_token=None,
                 claimed_at=None,
                 note=f'负责人改期，旧计划（版本 {o.schedule_revision}）场次作废，不再发布'))
        if result.rowcount == 1:
            superseded.append(o.id)
    db.flush()
    report.name = data.name
    report.destination = data.destination
    report.enabled = True
    report.paused_at = None
    report.schedule_revision = (report.schedule_revision or 1) + 1
    if data.schedule_type == ScheduleType.calendar.value:
        report.schedule_type = ScheduleType.calendar.value
        report.timezone, report.weekdays, report.local_time = timezone, weekdays, local_time
        report.interval_seconds = 0
        report.next_run = first.scheduled_for
        report.planned_through = now
        _materialize_due(db, report, now)
    else:
        report.schedule_type = ScheduleType.interval.value
        report.interval_seconds = data.interval_seconds
        report.timezone = report.weekdays = report.local_time = None
        report.next_run = now + data.delay_seconds
        report.planned_through = report.next_run
    db.flush()
    return report, superseded


def toggle(db, report, now=None):
    now = _now(now)
    _lock(db, report)
    report.enabled = not report.enabled
    if report.enabled:
        # 重新启用从当前时刻起算，不补暂停期间未发的场次。
        report.paused_at = None
        if is_calendar(report):
            try:
                report.next_run = cal.next_slot(report.timezone, report.weekdays,
                                                report.local_time, now).scheduled_for
                report.planned_through = now
                _materialize_due(db, report, now)
            except cal.CalendarError:
                report.planned_through = now
        else:
            report.next_run = now + report.interval_seconds
            report.planned_through = report.next_run
    else:
        # 暂停即撤销尚未发布的场次：恢复后不补发暂停期间的时点。
        pending = list(db.scalars(select(ReportOccurrence).where(
            ReportOccurrence.report_id == report.id,
            ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                         OccurrenceStatus.claimed.value)))))
        for o in pending:
            db.execute(update(ReportOccurrence).where(
                ReportOccurrence.id == o.id,
                ReportOccurrence.status.in_((OccurrenceStatus.pending.value,
                                             OccurrenceStatus.claimed.value)),
            ).values(status=OccurrenceStatus.superseded.value, claim_token=None,
                     claimed_at=None, note='计划被暂停，本场未发送，恢复后不补发'))
        report.paused_at = now
    db.flush()
    return report


def recovery(db, now=None):
    """中断恢复：把认领超时仍未归档的场次退回待发送，由后续轮询补发。

    归档是终态；已归档场次不会被重新发布，因此恢复和重试都不会重复归档。
    """
    now = _now(now)
    rows = list(db.scalars(select(ReportOccurrence).where(
        ReportOccurrence.status == OccurrenceStatus.claimed.value,
        ReportOccurrence.claimed_at < now - LEASE_SECONDS)))
    for o in rows:
        db.execute(update(ReportOccurrence).where(
            ReportOccurrence.id == o.id,
            ReportOccurrence.status == OccurrenceStatus.claimed.value,
        ).values(status=OccurrenceStatus.pending.value, claim_token=None, claimed_at=None))
    db.flush()
    return [o.id for o in rows]


def upcoming(report, count=6, after=None):
    now = _now(after)
    count = max(1, min(int(count), 30))
    if not is_calendar(report):
        return [{'scheduled_for': now + (i + 1) * report.interval_seconds,
                 'local': None, 'timezone': None, 'offset_text': None, 'note': '',
                 'skipped': False, 'ambiguous': False} for i in range(count)]
    try:
        slots = cal.preview(report.timezone, report.weekdays, report.local_time, now, count)
    except cal.CalendarError:
        return []
    return [{'scheduled_for': s.scheduled_for, 'local': s.local_text,
             'timezone': report.timezone, 'offset_text': s.offset_text,
             'note': s.note, 'skipped': s.skipped, 'ambiguous': s.ambiguous}
            for s in slots]


def preview(data, now=None):
    now = _now(now)
    try:
        cal.load_zone(data.timezone)
        weekdays = cal.normalize_weekdays(data.weekdays)
        local_time = cal.normalize_local_time(data.local_time)
    except cal.CalendarError as e:
        _reject(str(e))
    after = data.after if data.after is not None else now
    slots = cal.preview(data.timezone, weekdays, local_time, after, data.count)
    return {
        'timezone': data.timezone,
        'weekdays': weekdays,
        'local_time': local_time,
        'label': cal.schedule_label('calendar', 0, data.timezone, weekdays, local_time),
        'slots': [{'scheduled_for': s.scheduled_for, 'local': s.local_text,
                   'offset': s.offset, 'offset_text': s.offset_text,
                   'note': s.note, 'skipped': s.skipped, 'ambiguous': s.ambiguous}
                  for s in slots],
    }


def history(db, report_id):
    """补发与错过的依据：场次状态 + 对应的调度说明 + 补发替代场次。"""
    rows = list(db.scalars(select(ReportOccurrence).where(
        ReportOccurrence.report_id == report_id,
        ReportOccurrence.status.in_((OccurrenceStatus.missed.value,
                                     OccurrenceStatus.superseded.value)),
    ).order_by(ReportOccurrence.scheduled_for.desc()).limit(100)))
    outcomes = {o.id: o for o in db.scalars(select(ReportRunOutcome).where(
        ReportRunOutcome.report_id == report_id).order_by(ReportRunOutcome.id.desc()).limit(100))}
    result = []
    for o in rows:
        outcome = outcomes.get(o.outcome_id) if o.outcome_id else None
        catch_up = db.get(ReportOccurrence, outcome.catch_up_occurrence_id) \
            if outcome and outcome.catch_up_occurrence_id else None
        result.append({
            **occurrence_dict(o),
            'reason': o.note or (outcome.reason if outcome else ''),
            'outcome': None if not outcome else {
                'id': outcome.id, 'kind': outcome.kind, 'reason': outcome.reason,
                'catch_up': None if not catch_up else {
                    'id': catch_up.id, 'scheduled_for': catch_up.scheduled_for,
                    'local_time': catch_up.local_time, 'timezone': catch_up.timezone,
                    'status': catch_up.status,
                    'delivery_id': catch_up.delivery_id,
                },
            },
        })
    return result


def occurrences(db, report, limit=50):
    report_id = report.id if hasattr(report, 'id') else report
    rows = list(db.scalars(select(ReportOccurrence).where(
        ReportOccurrence.report_id == report_id
    ).order_by(ReportOccurrence.scheduled_for.desc()).limit(limit)))
    if not is_calendar(report if hasattr(report, 'id') else db.get(Report, report_id)):
        # 固定秒数计划没有场次表记录，直接以归档记录作为已发送场次。
        return [{'id': None, 'report_id': report_id, 'scheduled_for': d.scheduled_for or d.created_at,
                 'timezone': d.scheduled_timezone or 'UTC', 'local_time': d.scheduled_local or '',
                 'utc_offset': d.scheduled_offset, 'offset_text': _offset_text(d.scheduled_offset),
                 'status': OccurrenceStatus.archived.value, 'schedule_revision': d.schedule_revision,
                 'claimed_at': None, 'archived_at': d.created_at, 'delivery_id': d.id,
                 'outcome_id': None, 'note': '固定秒数计划触发',
                 'filename': d.filename, 'checksum': d.checksum}
                for d in db.scalars(select(Delivery).where(
                    Delivery.report_id == report_id).order_by(Delivery.id.desc()).limit(limit))]
    deliveries = {d.occurrence_id: d for d in db.scalars(select(Delivery).where(
        Delivery.report_id == report_id, Delivery.occurrence_id != None))}
    return [occurrence_dict(o, deliveries.get(o.id)) for o in rows]
