import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, Delivery
from src.schemas.report import ReportInput, PreviewInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports
from src.services.audit import record
router = APIRouter()


def _require_report(db, id):
    r = db.get(Report, id)
    if not r:
        raise HTTPException(404, '分发计划不存在')
    return r


@router.get('/reports')
def listing(db=Depends(get_db), u=Depends(current_user)):
    return [reports.report_dict(r) for r in db.scalars(select(Report).order_by(Report.id.desc()))]


@router.post('/reports', status_code=201)
def create(data: ReportInput, db=Depends(get_db), u=Depends(admin_only)):
    r = reports.create(db, data)
    record(db, u.email, 'report.schedule',
           {'id': r.id, 'destination': r.destination, 'type': reports.schedule_type(r),
            'timezone': r.timezone, 'weekdays': r.weekdays, 'local_time': r.local_time,
            'interval_seconds': None if reports.is_calendar(r) else r.interval_seconds})
    db.commit()
    return reports.report_dict(r)


@router.post('/reports/preview')
def preview(data: PreviewInput, db=Depends(get_db), u=Depends(current_user)):
    """按规则预览后续场次，不写库。"""
    return reports.preview(data)


@router.get('/reports/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    r = _require_report(db, id)
    return {**reports.report_dict(r), 'upcoming': reports.upcoming(r, 6),
            'occurrences': reports.occurrences(db, r),
            'history': reports.history(db, r.id)}


@router.post('/reports/{id}/toggle')
def toggle(id: int, db=Depends(get_db), u=Depends(admin_only)):
    r = _require_report(db, id)
    reports.toggle(db, r)
    record(db, u.email, 'report.toggle', {'id': id, 'enabled': r.enabled})
    db.commit()
    return reports.report_dict(r)


@router.post('/reports/{id}/reschedule')
def reschedule(id: int, data: ReportInput, db=Depends(get_db), u=Depends(admin_only)):
    r = _require_report(db, id)
    if data.job_id != r.job_id:
        raise HTTPException(422, '改期不能更换分析结果，请新建分发计划')
    r, superseded = reports.reschedule(db, r, data)
    record(db, u.email, 'report.reschedule',
           {'id': id, 'revision': r.schedule_revision, 'type': reports.schedule_type(r),
            'timezone': r.timezone, 'weekdays': r.weekdays, 'local_time': r.local_time,
            'interval_seconds': None if reports.is_calendar(r) else r.interval_seconds,
            'superseded': superseded})
    db.commit()
    return {**reports.report_dict(r), 'superseded': superseded}


@router.get('/reports/{id}/occurrences')
def occurrences(id: int, db=Depends(get_db), u=Depends(current_user)):
    return reports.occurrences(db, _require_report(db, id))


@router.get('/reports/{id}/upcoming')
def upcoming(id: int, count: int = 6, db=Depends(get_db), u=Depends(current_user)):
    return reports.upcoming(_require_report(db, id), count)


@router.get('/reports/{id}/history')
def history(id: int, db=Depends(get_db), u=Depends(current_user)):
    _require_report(db, id)
    return reports.history(db, id)


@router.get('/deliveries')
def deliveries(db=Depends(get_db), u=Depends(current_user)):
    return [reports.delivery_dict(d) for d in
            db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]


@router.get('/deliveries/{id}/download')
def download(id: int, format: str = 'csv', db=Depends(get_db), u=Depends(current_user)):
    d = db.get(Delivery, id)
    if not d:
        raise HTTPException(404, '归档不存在')
    if format not in ('csv', 'json'):
        raise HTTPException(422, '支持 CSV 或 JSON')
    filename = d.filename + ('.json' if format == 'json' else '')
    p = os.path.join(ARTIFACT_DIR, filename)
    if not os.path.exists(p):
        raise HTTPException(404, '归档文件不存在，请重新安排分发')
    return FileResponse(p, filename=filename,
                        media_type='application/json' if format == 'json' else 'text/csv; charset=utf-8')
