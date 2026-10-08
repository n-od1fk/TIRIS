# 平台数据接入

> 2026-10-09：当前统一添加流程以 [来源发现与验证](source-onboarding.md) 为准。GitHub 根地址已支持网页填写 Token、验证及保存平台 API 连接；其余普通网站优先发现 Feed。本文的平台专用查询/正文/原文件工具仍兼容保留，不再在添加表单选择平台类型。

本阶段采用方案一：在现有 Python 后端内实现可扩展平台连接器。复用安全 HTTP、身份分区、版本与收藏逻辑；用 Trafilatura 提取网页正文，用 pypdf 提取 PDF 文字。不新增 MCP 服务、数据库或登录。

这一步解决“不同平台通过同一组接口调用”，不是完成兴趣推断或自动跨平台推荐。正文提取不是 AI 摘要；将来摘要也必须保留原文链接、依据版本和原文件。

## 如何使用

1. 侧边栏打开“来源”，输入 URL 后检测并验证。GitHub 填写 `https://github.com/` 后可填写 Token，不必指定仓库；普通站点发现 RSS/Atom/JSON Feed。arXiv、网页、Black Hat 的专用工具仍兼容已有连接，但尚未全部接入新的平台 API 添加流程，不能仅填首页就宣称验证成功。
2. 点击该连接的“平台工具”，获取列表或选择平台支持的操作。查询条件由你明确填写，现在不自动生成兴趣关键词。列表查询不保存内容。
3. 选择条目“读取正文与原文件”，或输入原文地址。正文、章节/页码、附件信息、原文链接和原始材料分开返回。详情采集保存为当前身份的临时候选，不自动收藏。
4. 阅读正文、点击“查看原文”或下载原文件；明确点击“收藏到资料库”才进入长期库。资料库的版本阅读也提供原文件下载。

来源可以随时添加、编辑或删除，统一输入地址、名称可选，不提供快捷填入。来源卡片点“删除来源”后确认，即停止后续采集并从当前身份列表移除；可“撤销删除”或以后重新添加同类型、同地址的连接。重新添加复用原 ID/历史，不需要清空数据库。

删除采用可恢复归档，不删除已有收藏、原文件、版本和已生成的推荐；该连接未推荐的候选不再进入后续推荐。排队与执行中的同步任务标记停止，迟到结果不入库；已经发出的网络请求无法撤回。详情请求返回时也会复查来源状态。另一个身份的同名来源不受影响。删除来源不会删除 `.env` 中共享的凭证。

来源、正文、下载、收藏与版本都按 `X-TIRIS-User` 分区。本地身份不是安全认证，不要将无鉴权服务开放到公网。

## 凭证在哪里填

新 GitHub 平台连接：在“来源”页面检测 `https://github.com/`，直接填写 Token，验证后保存；编辑可保留、替换或移除。详细步骤和密钥备份见 [网页填写平台凭证](platform-credentials.md)。

以下环境变量仅兼容旧连接及旧自动兴趣搜索任务，不是新连接的必填步骤：

```dotenv
GITHUB_TOKEN=
```

模板保持空白，不覆盖已有 `.env`。在 [GitHub Token 设置](https://github.com/settings/tokens) 创建合适的只读凭证；所需权限取决于实际端点与资源。公开查询可以不填，但受匿名额度限制；此实现读取本人 `/user/starred` 时要求 Token。Token 不等于无限访问整个 GitHub，也不会自动获得私有仓库权限。

修改后重建/重建容器使环境变量生效：`docker compose up -d --build`。无需重新创建数据库或删除数据卷。

当前 arXiv、公开 RSS、公开网页和 Black Hat 专用工具不要求 API Key。这不表示所有内容都允许抓取，也不意味着能访问付费、登录后或被禁止访问的资源。新增需认证的平台应由适配器声明字段并实现验证/转换，不能把无用的 Key 输入框当成接入。

## 平台能力与内容范围

| 平台 | 统一查询能力 | 详情正文与原文件 | 当前限制 |
| --- | --- | --- | --- |
| GitHub | 公共仓库 ID 流、指定用户仓库、仓库搜索、Star 列表、指定仓库 Releases，支持分页 | 完整 README + 元数据 JSON；Release 原始说明 Markdown + JSON | 不是整个仓库全部代码、Issues 或所有平台数据；Release 二进制资产当前只提供附件链接 |
| RSS/博客 | 当前 Feed 列表与分页 | 跟随文章链接提取网页正文，保存原始 HTML | 不保证 Feed 全部历史；详情须仍在 Feed 中；历史文章可另建网页来源 |
| 网页 | 指定同站列表页的链接与分页 | 网页正文、段落、原始 HTML、附件链接 | 不自动全站遍历；不执行网页 JavaScript，不绕过登录/付费墙；通用提取可能遗漏代码、表格或动态内容 |
| Black Hat | 公开活动/索引页链接，允许指定活动页 | 原始 HTML、演讲描述与公开 PDF 原文件、逐页提取文本 | 描述不算演讲全文；每次最多尝试前 3 个附件中的 PDF，其余保留链接；当前网络 403，真实正文链路尚未验证 |
| arXiv | 官方 Atom API 查询、最近更新与分页 | 原始 Atom + 版本化 PDF + 全部可解析页文本 | 不含 OCR；正文提取不是公式/图表的完整理解；版本检查目前需显式调用 |

“平台级连接”意味着调用方可以在接口允许的范围内选择资源，而非绑定几个仓库；不等于一次下载全站，或任何平台都支持相同搜索能力。`capabilities` 返回支持的操作，不支持时明确拒绝。

`status=full` 仅表示自动正文提取检查通过，不是人工完整性保证。HTML 采用启发式检查；PDF 检查页数、每页文字与预算。扫描页、加密、解析失败、页面限制、超出预算均标记不完整，不能拿摘要替代全文。原文件保存成功不代表文本提取一定成功。附件字段逐项标识 `downloaded`；只有链接的附件不宣称已保存。

## 统一接口

API 前缀 `/api/platforms`，所有资源操作使用当前身份自己的 `source_id`。

| 接口 | 用途 |
| --- | --- |
| `GET /api/platforms` | 平台能力与凭证配置状态，不返回密钥 |
| `GET /api/platforms/{source_id}/capabilities` | 当前连接支持哪些操作 |
| `POST /api/platforms/{source_id}/query` | 列表/搜索/Star/Releases；只读，不入库 |
| `POST /api/platforms/{source_id}/detail` | 获取正文与原文件，保存候选与版本 |
| `POST /api/platforms/{source_id}/check-update` | 重新采集，比较稳定资源标识下的版本 |
| `GET /api/platforms/documents/{version_id}` | 读取已存正文、结构、附件及下载信息 |
| `GET /api/platforms/materials/{material_id}` | 下载原始字节，带身份分区检查 |

来源管理沿用统一 `/api/sources`：`POST` 添加/重新添加，`DELETE /api/sources/{source_id}` 可恢复删除，`PATCH /api/sources/{source_id}` 启停。删除后不能再通过同步、详情或启停接口使用该连接，需显式重新添加。

例如 GitHub 查询请求体：

```json
{"operation":"search","query":"security language:Python","subject":"","cursor":"","limit":5}
```

随后详情请求体：

```json
{"url":"https://github.com/pgvector/pgvector","download_attachments":false}
```

arXiv 接受论文 abs/pdf 链接或论文 ID；GitHub Releases 操作的 `subject` 填仓库 URL，Star 操作填用户名或留空读取本人。网页/Black Hat 的 `subject` 可指定列表页。分页使用响应中的 `next_cursor`，不要自行假定所有平台都是页码。

统一返回 `RawItem` / `Page` / `Document`。详情包括 `item_id`、`version_id`、`body`、`sections`、`attachments`、`materials`、`original_url`、来源时间、完整性状态与失败原因；`summary=null`、`summary_status=not_generated`。原文件下载响应强制为附件，禁止直接以内联 HTML 执行。

## 扩展一个平台

在 `backend/app/platforms/` 新增类，实现：

```python
class ExamplePlatform:
    kind = "example"
    label = "示例平台"
    operations = ("list", "search")
    default_url = "https://example.com/"

    def query(self, source_url, request, http):
        # 使用传入的 SafeHTTP 请求官方 API/公开页面，转换为 Page。
        ...

    def detail(self, source_url, request, http):
        # 返回 Document，保留实际取得的原始字节，不把节选标为 full。
        ...
```

启动时调用 `register_platform(ExamplePlatform())`（确保 API 与 Worker 都加载注册代码）。它同步登记来源类型和平台工具，创建、类型发现与统一 API 不需要为每个平台另写接口。必要时提供 `hint`、`default_url`、`validate_url`；新增凭证配置和能力目录说明也应同步维护。内置旧采集器为兼容保留；新增平台默认使用平台工具，不会自动获得后台兴趣检索逻辑。

新增平台应先补模拟响应测试：字段缺失、分页、原始字节一致性、空结果与失败区分、非全文状态；再做经过许可的公开真实只读验证。测试通过不等于真实平台访问成功。

## 存储、安全与边界

- 正文使用现有内容/版本表；原文件暂存 PostgreSQL `original_materials` 的二进制字段，记录文件名、类型、SHA-256、大小、原始地址和版本。这样不增加新存储服务；文件较多后再评估文件系统/对象存储。
- 单文件默认最多 20 MB，配置为 `.env` 的 `MATERIAL_MAX_BYTES`；普通 HTTP 响应仍受原有更小预算限制。PDF 默认最多 200 页，配置 `PDF_MAX_PAGES`。PDF 在独立进程解析并有时间限制，Linux 额外限制内存/CPU。
- 网页遵守 robots.txt；无法确认时停止，只有确实 404 才按无规则处理。继续复用 SSRF、DNS、TLS、重定向与超时保护，不绕过 403/验证码。采集只使用既有最小间隔，不承诺已实现跨进程的全局平台配额调度。
- 候选过期后不能再通过下载 API 访问，收藏保留访问；目前没有物理清理机制，磁盘使用没有硬上限。
- 新接口是手动、显式的接入工具。尚未统一接入自动兴趣发现、定时全文刷新或本人 Star 自动同步；旧“同步”仍可能只取得节选/元数据，不能据此宣称全部推荐都有全文。新工具明确标记不完整的材料不进入新候选匹配。

## 本轮验证记录（2026-10-08）

- 模拟与隔离数据库测试覆盖 GitHub/网页/PDF/arXiv/Black Hat 的解析、统一 API、来源扩展、版本去重、原文件下载、身份隔离及不自动收藏。
- 真实只读：Cloudflare RSS 文章正文提取约 1.2 万字符，并取得原始 HTML；没有将测试资料保存进个人库。
- 本机首次 GitHub、arXiv 测试遇到加密 DNS 请求失败；部署后在容器内复测成功：GitHub README 42,227 字符及原始 JSON/Markdown，arXiv 论文 39,524 字符及 2,215,244 字节的 PDF/原始 Atom。两者只做公开读取，不入个人库。
- Black Hat 本轮访问返回 HTTP 403，未绕过；不能将模拟解析测试算作真实材料获取成功。
- 151 项后端测试、14 项浏览器测试、Ruff、前端类型检查/生产构建通过。已部署增量迁移 `c102acde1009`，实际 Alembic 检查无模型差异；桌面/手机实际页面只读 smoke 通过，两个身份来源数量分别为 3/0。
- AI 摘要、后续检索逻辑、语义相关性与推荐效果不在本轮验证范围。

学习小任务：在“来源”新增自己的一个 RSS，取 1 篇文章。输入是 Feed 与文章 URL；输出应包含正文、原文链接、原始 HTML、SHA-256 和状态。对照网页人工检查段落/代码是否遗漏，再下载验证原文件；不完整时记录原因而不是把 `status` 改成 full。涉及知识是 HTTP、API/RSS/HTML 的差别、结构化转换和溯源，不需要先学 Agent。

技术依据：[GitHub Contents API](https://docs.github.com/en/rest/repos/contents)、[arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)、[Trafilatura Python 用法](https://trafilatura.readthedocs.io/en/stable/usage-python.html)、[pypdf 文本提取局限](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。
