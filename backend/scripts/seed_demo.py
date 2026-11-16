import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, ReportOccurrence
from src.services.business import metadata, seed_records, analyze
from src.services import calendar_schedule as cal
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        j=db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name']))
        if not j:
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        if j and not db.scalar(select(Report).where(Report.name=='每日业务复核简报')):
            # 原有的固定秒数计划保留为示例，二十四小时后首次到期。
            db.add(Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',schedule_type='interval',interval_seconds=86400,next_run=time.time()+86400,enabled=True,schedule_revision=1,planned_through=time.time()+86400))
            db.flush()
        if j and not db.scalar(select(Report).where(Report.name=='值班组 · 当地钟点简报')):
            # 日历计划示例：接收地时区 + 工作日 + 当地 08:30。
            from src.services import reports as report_service
            timezone='Asia/Shanghai'
            weekdays=[1,2,3,4,5]
            local_time='08:30'
            now=time.time()
            first=cal.next_slot(timezone,weekdays,local_time,now)
            r=Report(name='值班组 · 当地钟点简报',job_id=j.id,destination='异地值班组 · 本地收件箱',schedule_type='calendar',interval_seconds=0,next_run=first.scheduled_for,enabled=True,timezone=timezone,weekdays=weekdays,local_time=local_time,schedule_revision=1,planned_through=now)
            db.add(r);db.flush()
            report_service._materialize_due(db,r,now)
            db.flush()
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,ReportOccurrence,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
