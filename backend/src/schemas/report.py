from pydantic import BaseModel, Field, field_validator
class ReportInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    job_id: int
    destination: str = Field(default='业务复核组', min_length=2, max_length=120)
    schedule_type: str = Field(default='interval')
    # 固定秒数计划（沿用原有行为）
    interval_seconds: int = Field(default=86400, ge=60, le=2592000)
    delay_seconds: int = Field(default=0, ge=0, le=2592000)
    # 日历计划
    timezone: str | None = Field(default=None, max_length=64)
    weekdays: list[int] | None = None
    local_time: str | None = Field(default=None, max_length=5)
    @field_validator('schedule_type')
    @classmethod
    def known_type(cls, v):
        if v not in ('interval','calendar'):
            raise ValueError('schedule_type 仅支持 interval 或 calendar')
        return v
class PreviewInput(BaseModel):
    timezone: str = Field(max_length=64)
    weekdays: list[int] = Field(default_factory=list)
    local_time: str = Field(max_length=5)
    after: float | None = None
    count: int = Field(default=6, ge=1, le=30)
