# 阶段一 动手与验收

## 先理解数据怎样流动

网页提交 source_id → API 创建 Job → Redis 保存消息 → Worker 读取来源 → SafeHTTP 校验并获取公开材料 → 适配器返回 RawItem → 数据库保存 Item、Occurrence、Version → API 返回分页资料 → 网页显示实际获取范围。

Job 表保存“这次同步是否成功”，IngestionRun 保存“第几次尝试、获取和更新数量”。Redis 不是资料库，也不代替 PostgreSQL 中的任务状态。

三种内容对象的区别：Item 是内容身份；Occurrence 是在哪个来源发现它、原始链接是什么；Version 是当时实际获取到的快照。不能只用一个 URL 集合去重，否则重要更新会被忽略。

## 任务一 读出第一批真实资料

输入：运行中的 Docker Desktop、根目录 `.env`、三个公开来源配置。
操作：按 README 启动服务、seed，然后在网页同步一个可访问 RSS。
输出：资料列表、原始链接、抓取时间、任务成功/失败状态。
需要理解：HTTP 请求/JSON、同步 API 与后台任务、容器端口和服务名。
验收：打开原文核对标题；未知发布时间显示未知；Black Hat 403 显示失败，不写成“成功获取零条”。

## 任务二 找到数据库中的版本

输入：`backend/tests/test_ingestion_integration.py` 的重复/更新测试。
操作：运行 `uv run pytest tests/test_ingestion_integration.py -q`，阅读 `test_repeat_and_update_preserve_old_version`。
输出：同一身份的第一次快照、重复采集、更新后两版本的断言。
需要理解：SQLAlchemy Session、事务、唯一约束、哈希与身份的区别。
验收：解释为什么 fetched_at 不进入内容指纹；为什么论文/代码不能仅凭向量接近就删一条；修改测试文本后观察版本结果，不修改正式资料库来做单元测试。

## 任务三 复跑你之前的向量距离计算

输入：`[1,2,3]` 和 `[1,2,4]`。
操作：`uv run pytest tests/test_ingestion_integration.py -k pgvector_distance -v`。
输出：L2 距离为 1 的测试通过。
需要理解：欧氏距离、pgvector 类型、`<->` 运算符。
验收：自己算出两个向量只有第三维相差 1；能解释“数据库能计算向量距离”不等于“已实现文本 Embedding、语义搜索或推荐”。

## 任务四 看一次受控失败

输入：`backend/tests/test_connectors.py` 的私网地址和重定向测试。
操作：运行 `uv run pytest tests/test_connectors.py -q`，跟踪 `resolve_public_host` 与 `PublicTransport`。
输出：私网、混合 DNS、Fake-IP、重定向到回环地址都被拒绝；令牌不会随跨站重定向转发。
需要理解：DNS、TLS 的 Host/SNI、SSRF、测试中的 MockTransport。
验收：说清为什么只校验最初 URL 不够；为什么已校验 IP 后再次按域名连接仍可能有重绑定风险。

## 下一项学习任务

先设计长期画像 JSON 和独立固定栏目规则，再接一个可替换的 Embedding 提供者。输入真实资料和你自己的学习方向，输出可解释的检索结果；先不训练模型。实施前确定模型运行方式与费用/硬件边界。
