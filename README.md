# 播出简报异常编排

异常摘要与定时分发的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18219；后端文档 http://localhost:19219/docs；数据库宿主端口 17719。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=bulletin-ewma；FRONTEND_PORT=18219；BACKEND_PORT=19219；DB_PORT=17719；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。

### 日历分发计划（按接收地钟点）
除原有固定秒数周期外，可为异地值班组建立日历计划：**接收地时区 + 星期 + 当地钟点**。规则在 `backend/src/services/calendar_schedule.py`（纯计算，无数据库依赖）：

- 每个当地日期独立换算 UTC，不做秒数累加，因此不随夏令时漂移。
- 夏令时跳时：当地钟点不存在时顺延到当天首个有效时刻（如纽约 02:30 → 03:00）。
- 夏令时重复钟点：同一钟点出现两次时只发送较早一次。
- 每个应发场次落 `report_occurrences`，状态为 `pending`（待发送）→ `claimed`（发送中）→ `archived`（已归档）。
- 停机跨过多个应发时点：恢复后只补最近一场，其余标为 `missed` 并记录补发替代场次与原因；更早的场次不会重复发布。
- 并发/重试：认领用条件 UPDATE，两个调度进程只有一个能归档同一场次；中断恢复把超时认领退回待发送；归档是终态，重试不重复归档。
- 改期：负责人改期成功后旧规则下未发布场次标为 `superseded`，不再发布；已归档记录保留原定当地时刻、时区和计划版本。
- 页面「分发档案」可预览后续场次、查看每个计划的场次流转与补发/错过依据。原固定秒数计划继续可用。

接口：`POST /reports/preview`（不落库预览）、`GET /reports/{id}`（含 `upcoming`/`occurrences`/`history`）、`POST /reports/{id}/reschedule`。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1020/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
