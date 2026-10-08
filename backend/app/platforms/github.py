import re
from urllib.parse import quote, urlencode, urlsplit

from app.config import get_settings
from app.connectors.adapters import parse_date
from app.connectors.contracts import RawItem, SourceError
from app.connectors.http import canonicalize_url
from app.platforms.contracts import Document, Material, Page, PlatformQuery, page_number


class GitHubPlatform:
    kind, label = "github", "GitHub 平台"
    operations = ("list", "search", "stars", "releases")

    def __init__(self, token=None):
        self._token = token

    @property
    def token(self):
        return get_settings().github_token if self._token is None else self._token

    def with_credentials(self, values):
        return GitHubPlatform(token=values.get("token", ""))

    def connection_info(self, url):
        parsed = urlsplit(url)
        if parsed.hostname not in {"github.com", "www.github.com"} or parsed.path != "/":
            return None
        return {
            "kind": self.kind,
            "title": "GitHub 平台 API",
            "url": "https://github.com/",
            "access_type": "platform_api",
            "description": "连接 GitHub 官方 API，可浏览和查询不同仓库、"
            "读取 README/Releases，不绑定单个仓库。",
            "credential_fields": [
                {
                    "name": "token",
                    "label": "GitHub Token",
                    "required": False,
                    "help": "填写 GitHub Personal Access Token；公开读取可留空。"
                    "权限不足、失效或限流会显示失败。",
                }
            ],
            "credential_help_url": "https://github.com/settings/personal-access-tokens",
        }

    def verify_connection(self, url, http):
        if self.token:
            identity = self.request("/user", http).json()
            if not isinstance(identity, dict) or not isinstance(identity.get("login"), str):
                raise SourceError("GitHub 未返回有效的 Token 验证结果")
        return self.query(url, PlatformQuery(operation="list", limit=3), http)

    def request(self, path, http, params=None, *, raw=False):
        settings = get_settings()
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.github_api_version,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        return http.get(url, headers)

    def repository(self, url):
        parsed = urlsplit(canonicalize_url(url))
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.hostname != "github.com"
            or len(parts) < 2
            or not all(re.fullmatch(r"[\w.-]+", p) for p in parts[:2])
        ):
            raise SourceError("请使用 GitHub 仓库或 Release 原文链接")
        return parts[0], parts[1], parts[2:]

    def repo_item(self, row):
        return RawItem(
            f"repo:{row['id']}",
            row["html_url"],
            row["full_name"],
            "repository",
            clean_text=row.get("description") or "",
            author=row["owner"]["login"],
            published_at=parse_date(row.get("created_at")),
            metadata={
                "topics": row.get("topics", []),
                "updated_at": row.get("updated_at"),
                "evidence_scope": "列表元数据；需调用详情读取 README",
            },
        )

    def release_item(self, row):
        return RawItem(
            f"release:{row['id']}",
            row["html_url"],
            row.get("name") or row["tag_name"],
            "release",
            clean_text=row.get("body") or "",
            author=(row.get("author") or {}).get("login"),
            published_at=parse_date(row.get("published_at")),
            metadata={
                "tag": row["tag_name"],
                "repository": "https://github.com/"
                + "/".join(urlsplit(row["html_url"]).path.strip("/").split("/")[:2]),
                "updated_at": row.get("updated_at"),
                "evidence_scope": "GitHub Release 原始说明",
            },
        )

    def query(self, source_url, request, http):
        public_stream = request.operation == "list" and not request.subject
        if public_stream:
            if request.cursor and (not request.cursor.isdigit() or len(request.cursor) > 20):
                raise SourceError("公开仓库 ID 游标无效")
            page = 1
        else:
            page = page_number(request.cursor)
        params = {"per_page": request.limit, "page": page}
        if request.operation == "search":
            if not request.query.strip():
                raise SourceError("请提供明确 query；现在不自动生成兴趣关键词")
            params["q"] = request.query
            response = self.request("/search/repositories", http, params)
            data = response.json()
            if data.get("incomplete_results"):
                raise SourceError("GitHub 搜索结果不完整，请缩小查询范围")
            rows = data["items"]
        elif request.operation == "releases":
            owner, repo, _ = self.repository(request.subject or source_url)
            response = self.request(f"/repos/{owner}/{repo}/releases", http, params)
            rows = [r for r in response.json() if not r.get("draft")]
        elif request.operation == "stars":
            if request.subject:
                self.username(request.subject)
                path = f"/users/{request.subject}/starred"
            else:
                if not self.token:
                    raise SourceError(
                        "读取本人 Star 需要 Token，请在来源页面编辑凭证"
                        "（旧连接也兼容 GITHUB_TOKEN）"
                    )
                path = "/user/starred"
            response = self.request(path, http, params)
            rows = response.json()
        elif request.subject:
            self.username(request.subject)
            response = self.request(f"/users/{request.subject}/repos", http, params)
            rows = response.json()
        else:
            response = self.request(
                "/repositories", http, {"since": request.cursor or "0", "per_page": request.limit}
            )
            rows = response.json()
        if not isinstance(rows, list):
            raise SourceError("GitHub 返回列表格式异常")
        # Do not lose rows if this endpoint ignores per_page (public stream returns 100).
        selected = rows[: request.limit]
        mapper = self.release_item if request.operation == "releases" else self.repo_item
        next_cursor = str(selected[-1]["id"]) if public_stream and selected else None
        if not public_stream and 'rel="next"' in response.headers.get("link", ""):
            next_cursor = str(page + 1)
        return Page(
            [mapper(row) for row in selected],
            next_cursor,
            "平台级 API 调用，不限制预先配置的仓库；受接口权限与额度约束",
        )

    def username(self, name):
        if not re.fullmatch(r"[\w-]{1,100}", name):
            raise SourceError("GitHub 用户名无效")

    def detail(self, source_url, request, http):
        owner, repo, tail = self.repository(request.url)
        if tail:
            if len(tail) < 3 or tail[:2] != ["releases", "tag"]:
                raise SourceError("当前支持仓库 README 和 Release；不是全部代码文件")
            tag = quote("/".join(tail[2:]), safe="")
            response = self.request(f"/repos/{owner}/{repo}/releases/tags/{tag}", http)
            row = response.json()
            item = self.release_item(row)
            materials = [
                Material(
                    "release.json", "application/json", str(response.request.url), response.content
                ),
                Material("release.md", "text/markdown", item.url, item.clean_text.encode("utf-8")),
            ]
            attachments = [
                {
                    "url": a["browser_download_url"],
                    "label": a["name"],
                    "size": a.get("size"),
                    "downloaded": False,
                }
                for a in row.get("assets", [])
            ]
            return Document(
                item,
                "full" if item.clean_text else "partial",
                "" if item.clean_text else "Release 没有正文",
                materials,
                attachments,
            )
        response = self.request(f"/repos/{owner}/{repo}", http)
        item = self.repo_item(response.json())
        materials = [
            Material(
                "repository.json", "application/json", str(response.request.url), response.content
            )
        ]
        try:
            readme = self.request(f"/repos/{owner}/{repo}/readme", http, raw=True)
        except SourceError as exc:
            if "HTTP 404" not in str(exc):
                raise
            return Document(item, "partial", "仓库没有可读取的 README", materials)
        item.clean_text = readme.content.decode("utf-8", errors="strict")
        item.metadata["evidence_scope"] = "完整 README；不是整个仓库全部文件"
        materials.append(Material("README.md", "text/markdown", item.url, readme.content))
        return Document(
            item,
            "full" if item.clean_text.strip() else "partial",
            "" if item.clean_text.strip() else "README 为空",
            materials,
            sections=[{"type": "markdown", "text": item.clean_text}],
        )
