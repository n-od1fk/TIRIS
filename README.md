# TIRIS
Temporal Interest Retrieval Intelligence System

## 当前可用范围

主流程：明确兴趣 → 外部发现 → 临时候选 → 推荐 → Star/收藏 → 长期资料库 → 更新提醒。
采集/推荐不会自动收藏。当前是 RSS/指定仓库更新、可选 GitHub 兴趣搜索和规则/本地向量匹配基线，
不是完整推荐 Agent。侧边栏为今日推荐、资料库、来源和设置，不显示开发任务控制面板。
保存关注方向/学习目标后，资讯流按需补充，Beat 每 15 分钟检查后台补充；不设每日 10 条的总上限。
动态行为画像、相邻探索、RAG、LangGraph、AI 要点摘要、邮件和训练排序模型尚未实现。
见 [实施进度](docs/implementation-progress.md)、[阶段一学习任务](docs/stage-one-learning.md)
与 [画像配置学习任务](docs/stage-two-profile-learning.md)。
当前学习入口：[连续资讯流与本地身份](docs/simple-feed-learning.md)；旧发现控制面板说明保留为历史记录。
独立语义检索页面已移除，底层向量适配用于内部匹配，旧调试接口仍保留。

真实采集验证：Cloudflare RSS 和 GitHub pgvector 仓库成功；Black Hat Archives
在当前网络返回 HTTP 403，页面展示失败原因，不绕过访问限制。

## 推荐启动方式

在项目根目录使用 PowerShell。Docker Desktop 需要运行；已有 `.env` 不要覆盖。
没有 `.env` 时复制 `.env.example`，设置本地开发密码。

```powershell
docker compose up -d --build
docker compose exec api python -m app.cli seed
```

打开 <http://127.0.0.1:5173> 即进入今日推荐。首次在“设置”填写关注方向/学习目标，保存后返回主页。
滚动加载更多，后台分批补充；来源耗尽/失败时退避等待，不用重复或无关内容凑数。
“资料库”包含收藏和更新提醒；“设置”仅提供长期画像、浅色/暗色和本地身份。
侧边栏底部可直接切换身份，切换后重新加载该身份的推荐、收藏、更新提醒和来源，不沿用旧身份页面数据。
“来源”可添加 RSS/Atom 订阅、指定 GitHub 仓库或 Black Hat 公开索引，支持启停、手动同步和失败状态。
添加后点击“同步”，完成后返回推荐页面；只有符合画像的候选才会推荐，采集不会自动收藏。
每个身份独立维护来源；新身份从空来源列表开始，添加、停用和同步不影响其他身份，不自动复制或初始化默认来源。
旧版本给新身份自动添加但从未使用的默认来源会可恢复地隐藏，原身份及已使用/手动配置的来源保留。
任意网站首页不能直接作为 RSS；其他网站需提供 Feed 或新增专用采集器。Black Hat 访问受限时仍可能失败。
没有登录。各本地身份使用独立数据范围，但不是权限边界，只应在本机使用。
API 文档为 <http://127.0.0.1:8000/docs>，健康接口为 `/health` 和 `/health/db`。

### 长期画像与固定栏目

打开“设置”的长期用户画像。可填写背景、基础、关注方向、目标和屏蔽主题。
原有语言偏好与阅读预算在保存时保留；固定栏目和画像历史仍保留 API，不再占用界面。
每次实际修改保留历史快照，并为修改字段记录 `user_explicit` 与时间。
同内容保存不增加版本；旧页面保存返回 409，保留草稿，不自动覆盖新配置。

栏目单独保存名称、关键词、来源、内容类型、目标条数和启停，不被画像编辑改写。
至少有一种筛选条件；最多 20 个栏目，启用目标条数合计暂设上限 10。
固定栏目先分配，再匹配近期/长期兴趣。连续流按北京时间累计栏目每日额度；单批最多 10 条，批次数不限制为每日一次。
栏目关键词组内 OR，与来源/类型 AND；同一内容不重复占位。
屏蔽主题采用字面匹配，优先于栏目，不保证识别所有同义词。
来源停用只停止采集，不删除已有资料或栏目。

配置接口：`GET/PATCH /api/profile`、`GET /api/profile/history`、
`GET/POST /api/fixed-columns`、`PATCH /api/fixed-columns/{id}`。
PATCH 需要 `expected_version`，画像第一次保存为 0；栏目初始版本为 1。
现有服务升级时运行 `docker compose up -d --build`，API 启动时自动迁移，保留数据卷。

### 连续补充与收藏更新

主界面自动按需提交补充；有关注方向/学习目标的身份每 15 分钟检查一次后台补充，需电脑和 Docker 运行。
空画像不会随机推荐。填好方向再返回主界面即可，无需填写技术参数或手动选运行记录。
GitHub 兴趣检索默认不外发；原有明确授权与匹配配置继续生效，可通过开发 API 调整，不另加设置界面。
旧的每日定时配置 API 兼容保留，与连续补充共用活动任务去重。
每次最多更新 20 个来源，GitHub 最多 3 个查询各 10 条，不翻页追索。
连续补充单批最多检查 200 条候选、推荐 10 条；从累积推荐分页加载，每页最多 50 条，默认 20 条。
同版本已推荐后不在连续补充中重复入队；缓存可以继续供下一批匹配，外部刷新至少间隔 15 分钟。
空/失败批次等待约 15 分钟，不无限请求；本地语义匹配仍受单批预算限制，不保证覆盖全部库存。
修改画像或同步后出现新的未推荐版本，可以提前重新匹配；不会因此取消外部刷新间隔限制。
文章/Release 发布时间已知且超过 30 天时不进入发现候选；未知时间标未知；仓库标为资源而非新闻。
候选 30 天后隐藏，收藏不因候选过期退出长期库；现有资料不自动收藏、不删除。
目前没有物理清理候选正文/向量，不可宣称磁盘缓存有硬上限。
取消收藏返回候选、隐藏提醒，保留历史；不是删除资料。

提醒依据实际标题、文本、作者、获取范围或仓库 topics 的变化，不因抓取/活跃时间变化提醒。
新 Release 可关联收藏仓库；历史 Release 不补发，不监听每个 commit。
RSS 只能检查仍在 Feed 的材料，不保证全文或移出 Feed 的文章更新；论文版本尚未接入。
收藏搜索发现的仓库会添加指定仓库跟踪来源。停用来源后不会自动重新启用。
仅站内提醒，没有邮件或 GitHub Star 授权同步。连续补充刷新来源时也处理收藏更新；仅有收藏的身份可在资料库手动检查。

技术依据：[GitHub 搜索接口](https://docs.github.com/en/rest/search/search#search-repositories)、
[Celery 单实例周期任务](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html)。

接口：`GET/PATCH /api/discovery/settings`、`POST/GET /api/discovery/runs`、
`GET /api/discovery/runs/{id}/recommendations`、`POST /api/discovery/check-updates`、
`PUT /api/items/{id}/favorite`、`GET /api/items?scope=library`、`GET /api/updates`。
主界面：`GET /api/feed`、`POST /api/feed/refill`；身份：`GET/POST /api/identities`、`PATCH /api/identities/{id}`。
统一来源接入：`GET /api/source-types` 获取可添加类型，`POST /api/sources` 添加，`POST /api/sources/sync` 同步。
来源管理：`GET /api/sources`、`PATCH /api/sources/{id}`；任务：`GET /api/jobs`。
前端类型列表、创建地址校验与后台采集共用 `connectors/registry.py`；各平台返回统一 `FetchResult / RawItem`，进入同一溯源/版本/去重流程。
扩展说明与小任务：[统一来源接入](docs/source-integration-learning.md)。统一接口不代表能解析任意网页。
选择身份时发送 `X-TIRIS-User`。这只是本地分区选择，任何能访问服务的人均可切换，不能视为鉴权。

### 内部 Embedding 与开发调试接口

独立搜索页面已移除。发现流程可用规则或本地 Embedding，底层保留两个适配器：

- 本地 `Qwen/Qwen3-Embedding-0.6B`，固定权重 revision，CPU 运行，1024 维。
- 百炼 `text-embedding-v4` API，1024 维；尚未配置 Key/Base URL 时不可调用。

两个模型、端点、版本标签、查询指令和切分策略各自决定独立索引空间；即使维度相同也不能混用。
不同后端不混用向量。内部本地模型不可用时，报告明确标记回退规则，不冒充语义匹配。
内部每个候选版本缓存标题前 200、正文前 600 字符的单片段，与旧调试空间隔离。
浏览器不计算向量；按需提交的后台补充任务在本地匹配模式下才计算。开发调试索引默认每批 3 条、最多 20 条，每条最多 8 片段。
任务重投不会重复执行已结束任务，已完成的资料不会重复索引；费用不能因此称为 exactly-once。

本机已有固定版本权重；Compose 的 `tiris_embedding_models` 卷保存模型，API/Worker 只读挂载。
新电脑可在启动服务后显式准备（仅该维护容器可写模型卷）：

```powershell
docker run --rm --env-file .env --env EMBEDDING_CACHE_DIR=/models --volume tiris_embedding_models:/models --entrypoint python tiris-api -m app.cli prepare-model
```

本机开发使用项目根目录 `.model-cache`，不进入 Git：

```powershell
cd backend
uv sync --frozen --extra local-embedding
uv run --extra local-embedding python -m app.cli prepare-model
```

模型约 1.2GB 权重，另需运行依赖、内存和 CPU；初次加载比热查询慢，本轮未配置 GPU。
Docker 模型卷与本机 `.model-cache` 是两份缓存，不自动同步。此电脑已把下载文件复制到模型卷，
避免依赖失败的 E 盘 bind mount。不要删除模型卷或数据库卷来处理模型错误。

百炼在本地 `.env` 填写，**不要把 Key 发到聊天或提交到 Git**：

```dotenv
BAILIAN_API_KEY=你的百炼密钥
BAILIAN_BASE_URL=你的百炼控制台提供的兼容接口BaseURL
BAILIAN_EMBEDDING_REVISION=service-v1
```

Base URL 必须是控制台所属地域/业务空间的 HTTPS `.../compatible-mode/v1` 地址，不含末尾
`/embeddings`。不能填 DeepSeek Key 或对话模型。接口字段见
[百炼官方文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。
保存后 `docker compose up -d` 更新环境；调试接口显式指定百炼和 `consent_external=true` 才可调用。
当前推荐设置不提供云端选项，每日发现不会调用百炼或生成模型，不会自动计费。
索引仅发送资料片段，查询仅发送查询文本，不发送画像、栏目或行为记录。

`service-v1` 是你维护的服务标签，不是服务商公开的不可变权重 revision。
云端别名可能升级；确认升级后主动增加标签并重建索引。不能承诺 API 权重永久可复现。
接口错误不自动重试；超时或丢失响应也可能已经计费。任务报告 Token 不是完整账单。
中断任务仅在停止 Worker 后可恢复：

```powershell
docker compose stop worker
docker compose exec api python -m app.cli fail-index <job_id> --worker-stopped
docker compose start worker
```

检索接口：`GET /api/retrieval/backends`、`POST /api/retrieval/index`、`POST /api/retrieval/search`。
关键词查询在候选/收藏页面按标题匹配；保留的调试接口按当前版本余弦距离排序，一条资料只返回
最相近的片段。精确检索不等于语义判断正确；相似度不是相关概率。
只有摘要/元数据就只据此索引，不补造全文。长文本采用有上限的字符切分，截断会标注。
启用本地内部匹配并执行发现，会按需建立候选片段向量；旧调试空间仍需显式索引。

2026-10-06 历史调试验证：当时本地真实索引 3 / 21 条、17 个片段，并非当前候选总量或推荐质量指标。百炼适配器和同意检查通过模拟测试，
但未配置真实密钥、未实测云端调用，不把它报告成真实 API 成功结果。

Compose 启动 PostgreSQL、Redis、API、单并发 Worker、单实例 Beat 和页面。
Redis 只作为消息队列，资料、任务和版本保存在 PostgreSQL。
数据库扩展由初始 Alembic 迁移初始化，不再依赖 Windows 初始化目录的挂载。
`infra/postgres/init/001_extensions.sql` 保留为独立数据库的手动初始化参考。

所有端口只绑定 `127.0.0.1`。当前无登录鉴权，不可直接对公网开放。
本地 `.env` 和令牌不进入 Git；公开仓库采集无需配置 GitHub 令牌。

### DNS 与安全读取

默认 `FETCH_DNS_MODE=system`，使用操作系统 DNS。某些 Fake-IP 代理会把公网域名
改写成 `198.18.*`，被 SSRF 检查拒绝。在这种环境可在 `.env` 明确设置：

```dotenv
FETCH_DNS_MODE=doh
```

然后 `docker compose up -d` 使 API/Worker 获取新环境。
DoH 将公开来源域名发给 Cloudflare DNS，固定连接 `1.1.1.1`，保留 TLS 验证，
不发送令牌或内容，不接受私网/保留解析结果，不在失败时静默退回 Fake-IP。
当前仅通过 DoH 查询 A 记录；纯 IPv6 域名不支持。参见
[Cloudflare 官方接口](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/)。
此电脑的 `.env` 已启用 DoH，不修改系统 DNS 或代理设置。

### 查看失败和恢复中断任务

```powershell
docker compose logs --tail 100 worker
docker compose logs --tail 100 api
```

HTTP 429、服务端错误、连接故障最多追加 3 次重试；永久错误直接保存为失败。
Worker 进程意外退出可能留下活动任务；当前没有事务发件箱/租约恢复机制。
确认任务确实中断后，先停止 Worker，再按页面记录中的 job_id 恢复：

```powershell
docker compose stop worker
docker compose exec api python -m app.cli fail-job <job_id> --worker-stopped
docker compose start worker
```

重新点击来源同步。不要用恢复命令结束正常运行的任务。
中断的发现任务同样需停止 Worker，再执行
`docker compose exec api python -m app.cli fail-discovery <run_id> --worker-stopped`，
启动 Worker 后手动重试。没有事务发件箱/租约，不保证 exactly-once。
`docker compose down` 停止服务并保留资料；不要随意使用会删除数据库卷的 `down -v`。

## 本地开发与测试

只用 Docker 运行依赖，后端和前端在本机开发：

```powershell
docker compose stop api worker beat frontend
docker compose up -d db redis
cd backend
uv sync --frozen
uv run alembic upgrade head
uv run python -m app.cli seed
uv run uvicorn app.main:app --host 127.0.0.1 --reload
```

另开 PowerShell，在 `backend` 启动 Windows 兼容的单并发 Worker：

```powershell
uv run celery -A app.workers.tasks:celery_app worker --pool=solo --concurrency=1 --loglevel=INFO
```

再在 `frontend` 启动页面：

```powershell
npm ci
npm run dev
```

若 PowerShell 提示禁止运行 `npm.ps1`，使用 `npm.cmd run dev`（其他 npm 命令同理），
不需要修改系统执行策略。

前端将 `/api` 转发到本机 8000。不要同时占用 Compose 和开发服务器的同一个端口。
调试采集可在 `backend` 用 `uv run python -m app.cli sync` 前台执行相同逻辑；
不要与正常 Worker 重复大量同步，活动任务会被跳过。

```powershell
# backend 目录，PostgreSQL 需要运行
uv run ruff check app migrations tests
uv run pytest -q
uv run alembic check
uv run pytest tests/test_ingestion_integration.py -k pgvector_distance -v
uv run pytest tests/test_profiles_integration.py -v
uv run pytest tests/test_embedding_providers.py tests/test_retrieval_integration.py -v
```

数据库集成测试在随机命名的独立 schema 中执行真实迁移，结束后只清理该测试 schema，
不清空资料库。Mock 数据仅验证流程，不用于报告推荐效果。
无 PostgreSQL 时可用 `uv run pytest -m "not integration"` 运行非数据库测试。

```powershell
# frontend 目录
npm ci
npx playwright install chromium
npm run build
npm test
```

页面测试在 5180 端口运行专用无头浏览器，不读取个人浏览器历史。
运行中的 Compose 已有真实资料时，可在 `frontend` 运行 `node scripts/live-smoke.mjs`
复查实际页面、版本阅读和手机布局；它只读取资料，不生成或修改内容。
GitHub Actions 的配置已写入 `.github/workflows/ci.yml`；本地运行不等于远端 CI 已通过。

## Design references and licensing

TIRIS is MIT licensed. ReadPrism is studied as a design reference, but its
AGPL-3.0 implementation is not copied into this repository. See
[`docs/adr/0001-readprism-reference-policy.md`](docs/adr/0001-readprism-reference-policy.md)
for the accepted policy and rationale.

## 数据库检查

数据库数据在 `tiris_postgres_data` 卷；重建服务不会删除它。
已有数据库的密码必须与 `.env` 保持一致，仅修改 `.env` 不会自动修改卷里的账号密码。
启动并迁移后可验证扩展（若改过用户名/数据库名，请替换下面的 `tiris`）：

```powershell
docker compose ps
docker compose exec db psql -U tiris -d tiris -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"
```
