from sqlalchemy import String, Double, ForeignKey, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Report(Base):
    __tablename__ = 'reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    destination: Mapped[str] = mapped_column(String(120))
    # schedule_type=interval：沿用固定秒数周期；calendar：按接收地时区、星期和钟点排期。
    # 日历计划的 interval_seconds 存 0，接口输出为 null。
    schedule_type: Mapped[str] = mapped_column(String(20), default='interval')
    interval_seconds: Mapped[int] = mapped_column(default=86400)
    next_run: Mapped[float] = mapped_column(Double)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    weekdays: Mapped[list | None] = mapped_column(JSON, nullable=True)
    local_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_revision: Mapped[int] = mapped_column(default=1)
    # 已生成（待发送或已判定）场次中最新一场的 scheduled_for，用于停机后补齐缺口。
    planned_through: Mapped[float] = mapped_column(Double, default=0.0)
    paused_at: Mapped[float | None] = mapped_column(Double, nullable=True)
class Delivery(Base):
    __tablename__ = 'deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    created_at: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(120))
    # 归档时冻结的场次快照：改期后仍保留原定当地时刻、时区和计划版本。
    occurrence_id: Mapped[int | None] = mapped_column(ForeignKey('report_occurrences.id'), nullable=True, unique=True)
    scheduled_for: Mapped[float | None] = mapped_column(Double, nullable=True)
    scheduled_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduled_local: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scheduled_offset: Mapped[int | None] = mapped_column(nullable=True)
    schedule_revision: Mapped[int | None] = mapped_column(nullable=True)
class ReportOccurrence(Base):
    """日历计划的一个应发场次。

    状态流转：pending（待发送）-> claimed（某调度进程已认领）-> archived（已归档）；
    停机跨过的旧场次 pending/claimed -> missed（错过）；
    负责人改期时未发布的旧场次 pending/claimed -> superseded（作废，不再发布）。

    唯一约束 (report_id, scheduled_for) 保证同一场次只存在一行，抢到认领的进程
    才发布，另一个进程认领失败即跳过。
    """
    __tablename__ = 'report_occurrences'
    __table_args__ = (UniqueConstraint('report_id', 'scheduled_for', name='uq_occurrence_slot'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'), index=True)
    scheduled_for: Mapped[float] = mapped_column(Double, index=True)
    timezone: Mapped[str] = mapped_column(String(64))
    local_time: Mapped[str] = mapped_column(String(32))
    utc_offset: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    schedule_revision: Mapped[int] = mapped_column(default=1)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    archived_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    delivery_id: Mapped[int | None] = mapped_column(nullable=True)
    outcome_id: Mapped[int | None] = mapped_column(ForeignKey('report_run_outcomes.id'), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[float] = mapped_column(Double)
class ReportRunOutcome(Base):
    """同一轮调度中对一批到期场次的处理依据（补发哪一场、哪些错过、为什么）。"""
    __tablename__ = 'report_run_outcomes'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # catch_up / missed_only / delivered
    created_at: Mapped[float] = mapped_column(Double)
    catch_up_occurrence_id: Mapped[int | None] = mapped_column(nullable=True)
    missed_occurrence_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default='')
