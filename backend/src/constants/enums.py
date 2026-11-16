from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'

class ScheduleType(str, Enum):
    interval = 'interval'
    calendar = 'calendar'

class OccurrenceStatus(str, Enum):
    pending = 'pending'
    claimed = 'claimed'
    archived = 'archived'
    missed = 'missed'
    superseded = 'superseded'
