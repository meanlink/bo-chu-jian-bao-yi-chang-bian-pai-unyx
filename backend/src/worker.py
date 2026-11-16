import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset
from src.services.business import analyze
from src.services import reports as report_service
from src.services.audit import record
logging.basicConfig(level=logging.INFO)


def execute(job_id):
    with Session() as db:
        j = db.get(Job, job_id)
        if not j or j.status != 'queued':
            return
        j.status = 'running'
        db.commit()
        try:
            d = db.get(Dataset, j.dataset_id)
            result = analyze(d.records, j.params)
            json.dumps(result, allow_nan=False)
            j.result = result
            j.status = 'completed'
            j.finished_at = time.time()
            record(db, 'worker', 'job.completed', {'id': j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status = 'failed'
            j.error = str(e)[:990]
            j.finished_at = time.time()
            record(db, 'worker', 'job.failed', {'id': j.id, 'error': j.error})
        db.commit()


def tick(now=None):
    with Session() as db:
        queued = list(db.scalars(select(Job.id).where(Job.status == 'queued').limit(3)))
    for job_id in queued:
        execute(job_id)
    with Session() as db:
        # 认领与归档在同一事务内完成；并发 worker 只有一个能认领同一场次。
        archived = report_service.run_tick(db, now=now)
        for report_id, d in archived:
            record(db, 'worker', 'report.delivered',
                   {'id': report_id, 'delivery': d.id, 'occurrence': d.occurrence_id,
                    'scheduled_for': d.scheduled_for, 'timezone': d.scheduled_timezone,
                    'local': d.scheduled_local})
        db.commit()


def recover():
    """进程启动恢复：运行中的分析任务回队列，超时未归档的场次退回待发送。"""
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status == 'running')):
            j.status = 'queued'
            record(db, 'worker', 'job.recovered', {'id': j.id})
        released = report_service.recovery(db)
        for oid in released:
            record(db, 'worker', 'report.occurrence.recovered', {'occurrence': oid})
        db.commit()
    return released


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    recover()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop('analysis:queue', timeout=1)
            if item:
                execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
