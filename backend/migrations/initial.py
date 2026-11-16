"""增量迁移：为已有数据库补齐日历排程所需的列与表。

create_all 只建缺失的表，不会给旧表加列，因此这里显式检查并用 ALTER TABLE 补齐。
可重复执行。
"""
from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models


REPORT_COLUMNS = {
    'schedule_type': "VARCHAR(20) NOT NULL DEFAULT 'interval'",
    'timezone': 'VARCHAR(64) NULL',
    'weekdays': 'JSON NULL',
    'local_time': 'VARCHAR(5) NULL',
    'schedule_revision': 'INTEGER NOT NULL DEFAULT 1',
    'planned_through': 'DOUBLE NOT NULL DEFAULT 0',
    'paused_at': 'DOUBLE NULL',
}
DELIVERY_COLUMNS = {
    'occurrence_id': 'INTEGER NULL',
    'scheduled_for': 'DOUBLE NULL',
    'scheduled_timezone': 'VARCHAR(64) NULL',
    'scheduled_local': 'VARCHAR(32) NULL',
    'scheduled_offset': 'INTEGER NULL',
    'schedule_revision': 'INTEGER NULL',
}


def _add_columns(table, columns):
    existing = {c['name'] for c in inspect(engine).get_columns(table)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


def migrate():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if 'reports' in tables:
        _add_columns('reports', REPORT_COLUMNS)
    if 'deliveries' in tables:
        _add_columns('deliveries', DELIVERY_COLUMNS)
        # 同一场次只允许一份归档；旧数据可能全为 NULL，MySQL 唯一索引允许多个 NULL。
        indexes = inspect(engine).get_indexes('deliveries')
        covered = any('occurrence_id' in (i.get('column_names') or []) and i.get('unique')
                      for i in indexes)
        if not covered:
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        'CREATE UNIQUE INDEX uq_delivery_occurrence ON deliveries (occurrence_id)'))
            except Exception:
                pass


if __name__ == '__main__':
    migrate()
