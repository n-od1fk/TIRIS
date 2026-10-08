# 来源发现、验证与接入（2026-10-09）

## 使用方式

打开侧边栏“来源”，只需填写网站、栏目、平台资源或订阅源地址，名称可选。
点击“检测接入方式”，查看有效订阅、失败原因、实际采集地址和条目预览。
选择一个或多个已验证订阅，点击“验证并保存所选来源”。只有一个有效候选时会默认勾选；多个候选由用户选择。
保存前重新抓取验证，全部选中项通过后才提交配置，并用这次响应获取首批内容。不另发一次初始同步请求。
内容只进入候选区，符合画像后才可能出现在今日推荐；不会自动收藏或改变推荐策略。

GitHub 平台接入：输入 `https://github.com/` → 检测 → 在出现的“GitHub Token”框填写凭证（公开读取可留空）→ 验证平台连接 → 查看预览 → 保存。
这与输入某个仓库后订阅 Atom 不同：保存的是平台 API 连接，可在“平台工具”选择不同仓库、查询条件或 Star 操作，不绑定一个仓库。
编辑来源时，Token 留空保留原凭证；填写新值替换；勾选移除则切换匿名读取。每次都必须重新验证。
申请入口、密钥备份和安全边界见 [网页填写平台凭证](platform-credentials.md)。

每条来源支持编辑名称、重新检测并修改连接、启用、停用、同步、删除和撤销删除。
修改地址必须重新检测，不能把新地址直接写进已验证配置。正在同步的连接需要等待同步结束再修改地址。
删除是可恢复归档，保留收藏、原文件和历史版本；按身份隔离，不清空数据库。
重新添加同一实际采集地址恢复原连接。旧来源不强行重建，也不把它们标为新验证成功。

## 为什么这样实现

参考 [Miniflux 订阅发现](https://github.com/miniflux/v2/blob/main/internal/reader/subscription/finder.go)、
[格式解析分派](https://github.com/miniflux/v2/blob/main/internal/reader/parser/parser.go) 和
[条目处理](https://github.com/miniflux/v2/blob/main/internal/reader/processor/processor.go)：
发现地址、验证/解析 Feed、处理条目正文是不同职责。借鉴其 URL 发现和格式分派思路，用项目已有 Python 栈实现，未引入 Go 服务或另一套数据库。

复用现有 SafeHTTP、feedparser、BeautifulSoup、来源登记表、入库/去重/版本/收藏和 Celery 同步。
通用网站由标准 Feed 解析器处理，不建立“所有网站的平台名单”。只有特殊 URL 转换需要独立适配器。
网页全文抓取仍是已有平台详情工具的能力，不是检测到 HTML 就自动获得全文。

## 当前支持与边界

| 输入场景 | 自动处理 | 边界 |
| --- | --- | --- |
| RSS、RSS 1.0/RDF、Atom 地址 | 实际下载、格式解析、标准化、预览、保存 | 格式有效的空订阅可以保存，明确显示暂无条目 |
| JSON Feed 1/1.1 地址 | 校验版本与条目，HTML 内容转纯文本；可选标题缺失时标“未提供标题” | 普通 JSON/API 响应不等于 JSON Feed；缺少有效 ID/原文链接或字段类型错误的条目跳过，全无可用条目时拒绝 |
| 网站/栏目页 | 跟随重定向，读取页面声明的 RSS/Atom/JSON Feed，解析 `<base>` 和相对地址，去重并逐项验证 | 不执行 JavaScript，不遍历全站；缺少声明时有限尝试 `feed`、`rss.xml`、`atom.xml`、`feed.json` |
| `https://github.com/` | 识别平台 API，展示 Token 字段；验证凭证和公开列表，预览后保存 | 识别不等于连接成功；权限与额度仍由 GitHub 限制；首次预览仅仓库元数据，不是新闻或 README 全文 |
| GitHub 仓库、Releases/Commits/Tags 页面 | 转换为对应公开 Atom；仓库首页返回三个候选 | 不是整个 GitHub 授权、全站流或全文仓库下载；失败候选不可保存 |
| GitHub 用户/组织地址 | 尝试公开活动 Atom | 是否可用以实际验证为准；不读取私人 Star 或私有活动 |
| YouTube `/channel/UC…`、播放列表 | 转换公开视频 Feed | `@handle` 页面只有声明订阅或 channelId 时可发现；不执行脚本、下载视频或获取转录全文 |
| 无有效 Feed、专有 API、401/403 | 展示失败和需要补充的订阅地址、API 文档、Key/授权说明 | 不绕过访问控制，不把“识别为网页”当成功，不自动理解任意 API |

普通公开订阅不需要填写 Key。GitHub 平台 Token 可以在网页填写，按来源/身份加密保存；不接受任意网站的 Cookie，也不把 GitHub Token 发给普通订阅网站。
旧连接仍兼容 `.env` 中的 `GITHUB_TOKEN`，不改动已有密钥。新连接明确使用网页凭证或匿名模式；移除凭证后不会暗中回退到环境变量。
复杂认证、通用 API 字段映射、更多平台适配器、登录/付费内容、浏览器渲染、全站爬取都未在本阶段实现。
已有网页/PDF 原文件读取工具保留；自动订阅采集只保证 Feed 实际提供的正文或摘要，不保证全文、全部历史或所有附件。
未增加 AI 摘要、兴趣推断、推荐学习或任意平台全文更新任务。

## 数据流与文件

`输入 URL → discover_sources → FeedHint → validate_feed → FeedDocument → RawItem → store_items`

网页/平台产生的 FeedHint 只是提示，不是接入成功。检测结果保留在当前身份的短期草稿中，15 分钟过期。
保存请求仅提交服务端草稿 ID 和候选 ID，不能用客户端伪造地址跳过验证。
批量保存先全部复验，再事务保存全部配置与初始内容；重复或冲突回滚，不留半批来源。

| 文件 | 职责 |
| --- | --- |
| `backend/app/connectors/source_discovery.py` | URL 发现、HTML 链接解析、逐项验证、预览与只读 CLI |
| `backend/app/connectors/feed_adapters.py` | `FeedAdapter.discover(url, document=None) -> list[FeedHint]`；GitHub/YouTube 公开 Feed 转换 |
| `backend/app/connectors/feeds.py` | RSS/Atom/JSON Feed 识别、解析、规范化；不做网络请求 |
| `backend/app/connectors/source_api.py` | 当前身份的发现草稿、复验、事务保存与首次入库 |
| `backend/app/connectors/platform_onboarding.py` | 平台凭证校验、连接验证、加密短期草稿与保存前复验 |
| `backend/app/services/credentials.py` | Fernet 加密、独立密钥文件、来源/身份绑定与失败关闭 |
| `backend/app/platforms/github.py` | GitHub 识别元数据、凭证绑定、验证与统一查询/详情适配器 |
| `frontend/src/SourceOnboarding.tsx` | 统一 URL 表单、候选选择、内容预览、错误提示、编辑 |
| `backend/migrations/versions/d209acde1010_source_discovery.py` | 增量列与草稿表；保留原数据，不重新初始化来源 |
| `backend/migrations/versions/e310acde1011_source_credentials.py` | 增量添加凭证模式与密文列；不迁移或展示环境变量 Token |

来源保存 `name`、`input_url`、`url`（API 同时返回 `fetch_url`）、`access_type`、`enabled`、`validated_at`、`connection_version`。
`kind=rss` 兼容既有通用同步派发，`access_type=rss/atom/json_feed` 表示实际验证格式，不是平台列表。
条目统一为 RawItem：标题、原文 URL、作者、发布时间、正文或摘要、获取范围、来源元数据。
历史仍由 ContentOccurrence/ContentVersion 保留。编辑采集地址后递增连接版本，分隔不同订阅重复使用的外部 ID。
正在运行的旧请求若发现连接版本改变，不保存迟到结果。

## API

- `POST /api/sources/discover`：`{"url":"https://example.com/"}`，返回草稿、候选、验证状态、预览、缺失信息。
- `POST /api/sources/verify-platform`：提交识别草稿 ID、`credentials`、`credential_action=keep/replace/clear`；编辑还需来源 ID 与期望版本。成功返回已验证草稿和预览，不返回凭证。
- `POST /api/sources/connect`：`{"discovery_id":"UUID","selections":[{"candidate_id":"UUID","name":"可选"}],"enabled":true}`。
- 编辑连接在 connect 请求增加 `edit_source_id` 和当前 `expected_version`（连接版本），只选择一个候选。
- `GET /api/sources`：当前身份来源配置；`PATCH /api/sources/{id}`：名称/启停；`DELETE`：归档。
- `POST /api/sources/sync`、`GET /api/jobs`：继续使用既有后台同步与失败显示。

旧 `POST /api/sources` 标为 deprecated，为旧脚本、历史平台工具配置和撤销删除保留。
它仍是旧的手动配置路径，不提供新接入流程的真实验证保证；新页面的添加和连接编辑只走 discover/connect。
`/api/source-types` 仍为旧工具返回能力信息，不再作为新添加表单的平台下拉框。
身份通过 `X-TIRIS-User` 选择；这不是鉴权，服务仍应仅在本机运行。

## 验证和动手任务

自动化：185 项后端测试、20 项浏览器测试通过；Ruff 和前端 TypeScript/生产构建通过。
测试使用隔离 PostgreSQL schema，不删除用户表。新增用例覆盖三种格式、相对/重定向地址、多候选、恶意 XML、无效 JSON、SSRF、失败回滚、身份/过期、地址编辑、删除恢复与 JSON Feed 再同步。
模拟测试证明流程和约束，不代表支持所有真实网站或推荐效果提升。
凭证测试覆盖错误 Token 不降级、验证后保存前复验、加密与篡改、身份隔离、留空保留/替换/移除、版本冲突、过期及请求错误不回显密钥。

真实只读验证（部署容器网络）：Cloudflare 首页发现并验证 RSS；Miniflux GitHub 仓库的 Releases/Commits/Tags 三个 Atom、YouTube 的 Google for Developers 频道 Feed、Daring Fireball JSON Feed 通过，均取得真实条目预览。Black Hat 首页返回 HTTP 403，未接入成功或绕过。
Windows 本机加密 DNS 请求失败，未关闭 TLS 或内网保护；容器使用已有安全 HTTP 能力验证成功。
新增 GitHub 平台 API 匿名只读验证成功，取得 3 条元数据预览；真实个人 Token 未代测。容器密钥目录归非 root 服务用户所有，权限 0700，可写；尚未保存真实凭证时不会创建密钥文件。
实际页面的桌面/手机、来源管理与身份切换 smoke 通过；保留 25 条候选，未替用户添加来源、收藏或调用付费模型。

你可以先完成两个小任务：

1. 只读检查：输入自己选择的公开网站 URL，输出发现结果与预览条数。学习 URL 解析、Feed 格式和“提示/验证”的区别；验收输出中 `verified`、`access_type`、实际 URL 与预期一致。命令如下，不会写数据库：

   ```powershell
   docker compose exec api python -m app.connectors.source_discovery https://blog.cloudflare.com/
   ```

2. 读一个测试：运行下列测试，观察只有 connect 成功后来源/内容才出现、资料库仍为空。学习请求验证、事务和候选/收藏分离；随后自行添加一个失败格式的断言。

   ```powershell
   cd backend
   .venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_feed_onboarding.py
   ```

添加普通提供标准 Feed 的网站不需要改代码。特殊公开订阅新增 FeedAdapter 并注册即可。
专有平台 API 可在平台适配器提供 `connection_info`、`with_credentials`、`verify_connection`，由元数据驱动同一表单；查询和详情仍复用统一接口。
如果平台只有专有 API，则还要实现其认证与字段转换，不能靠 FeedAdapter 或填写一把 Key 自动获得全部能力。

格式依据：[JSON Feed 1.1](https://www.jsonfeed.org/version/1.1/)。
