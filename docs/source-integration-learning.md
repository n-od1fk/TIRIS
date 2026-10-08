# 统一来源接入与空身份

> 2026-10-09 更新：当前添加页面改为输入 URL、自动发现、预览、复验后保存，详见 [来源发现与验证](source-onboarding.md)。下面的类型选择/手动 POST 为历史接口和学习记录，不是当前添加流程。

## 为什么切换后曾看起来来源没变

旧 `create_identity` 自动为每个新身份插入 Cloudflare、pgvector 和 Black Hat 三条公开模板。
它们的数据库 ID 和所属身份不同，但名称、地址一样，因此不符合“每个学习空间自己选来源”的要求。
现在新身份只有身份标签，不创建画像、内容、收藏或来源。切换会重建页面并取消旧身份请求。

迁移 `b0198ed41008` 增加 `Source.archived`，只隐藏精确匹配旧模板、与身份同事务创建、
没有任务/采集/版本/来源关联/固定栏目引用的非默认身份来源。不删除配置，不改原身份，不隐藏已使用的来源。
隐藏来源不会列表展示或后台采集；用户主动添加同类型同地址可恢复原 UUID。
这与“停用来源”不同：停用是保留可见配置、只停止采集。历史资料与收藏始终保留。

## 同一条接入路径

1. `GET /api/source-types` 从 `connectors/registry.py` 返回可添加类型、名称、提示和地址示例；前端不再独立维护平台选项。
2. `POST /api/sources` 统一接收 `{name, kind, url}`，用同一登记表校验并规范化地址，保存当前 `X-TIRIS-User` 的来源；重复仍返回 409。
3. `POST /api/sources/sync` 统一接收 `{source_id}`，检查归属、归档和启停状态，提交 Celery 任务。不能指定别的身份来源。
4. Worker 和兴趣发现共用 `fetch_source(kind, url, cursor, http)`，按登记表选择 `SourceConnector.fetch`。
5. 每个平台的 `reader(url, cursor, http) -> FetchResult` 返回统一 `RawItem`。继续由原 `store_items` 处理规范化、精确去重、不可变版本与更新提醒。

`SourceConnector` 是一个简单的数据类，不是新框架。它集中描述、地址校验和 reader 函数；
`adapters.py` 保留现有 RSS、GitHub 和 Black Hat 的实际读取实现。新增平台实现 reader 并登记，
无需新增一套 API/任务/数据库或修改前端下拉列表；现有来源类型数据库约束不再写死四个平台。

内部 `github_search` 同样经过统一采集入口，但不出现在可添加类型中；仍要求独立明确授权。
统一接口不代表统一解析所有网页：博客应提供 Feed，GitHub 应提供具体仓库，Black Hat 仅公开索引且仍可能 403。

## 可以自己完成的小任务

- 地址校验：输入错误平台域名，输出 422，且不启动网络请求。学习 URL 解析、模型校验和错误边界；验收 `test_unified_dispatch_rejects_invalid_inputs_before_network`。
- 新采集器：输入一个测试 reader，输出一条 `RawItem`；登记后用同一 API 添加/同步，候选增加一条，长期库仍为零。学习依赖注入、可调用类型和标准数据契约；验收 `test_new_connector_uses_same_add_sync_and_ingestion_pipeline`。这是模拟流程验证，不代表新平台真实采集成功。
- 隔离：输入两组身份、不同来源和一个迟到响应，输出只属于当前身份的列表；切回保留原来源。学习请求头范围、取消请求和组件生命周期；验收前端 `sources.spec.ts`。
- 兼容：输入旧自动模板、已用来源和手动添加来源，输出只归档未使用自动模板；再主动添加恢复原 ID。学习保守迁移与可恢复操作；验收 `test_archive_only_unused_automatic_templates_and_explicit_add_restores`。

后端：在 `backend` 运行 `.venv\Scripts\pytest.exe tests/test_source_registry.py -v`。
前端：在 `frontend` 运行 `npm test`。实际检查 `node scripts/live-smoke.mjs`，会拦截个人推荐补充，不替用户添加来源或启动采集。
真实 reader 必须复用传入的安全 HTTP 客户端并遵守大小、超时和条数预算，不能另起无约束的网络客户端。
