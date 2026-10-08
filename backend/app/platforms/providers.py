import re
from urllib.parse import urlencode, urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.connectors.adapters import parse_date
from app.connectors.contracts import RawItem, SourceError
from app.connectors.feeds import parse_feed
from app.connectors.http import canonicalize_url
from app.platforms.contracts import Document, Material, Page, page_number
from app.platforms.web import allowed_page, extract_html, get_page, get_pdf, pdf_text


class WebPlatform:
    kind, label = "web", "网页 / 博客"
    operations = ("list",)

    def query(self, source_url, request, http):
        target = allowed_page(request.subject or source_url, source_url)
        soup = BeautifulSoup(get_page(target, http).content, "html.parser")
        items = {}
        for link in soup.select("a[href]"):
            title = link.get_text(" ", strip=True)
            if len(title) < 5:
                continue
            try:
                url = allowed_page(urljoin(target, link["href"]), source_url)
            except SourceError:
                continue
            if url != target:
                items[url] = RawItem(
                    url,
                    url,
                    title,
                    "article",
                    metadata={"evidence_scope": "页面链接，尚未读取正文"},
                )
        return slice_page(list(items.values()), request, "指定页面同站链接，不自动全站遍历")

    def detail(self, source_url, request, http):
        target = allowed_page(request.url, source_url)
        return extract_html(target, get_page(target, http))


def slice_page(items, request, scope):
    page = page_number(request.cursor)
    offset = (page - 1) * request.limit
    return Page(
        items[offset : offset + request.limit],
        str(page + 1) if offset + request.limit < len(items) else None,
        scope,
    )


class RSSPlatform:
    kind, label = "rss", "RSS / 博客"
    operations = ("list",)

    def query(self, source_url, request, http):
        result = parse_feed(http.get(source_url).content, source_url, 500)
        return slice_page(result.items, request, "订阅当前提供的条目，不是全部历史")

    def detail(self, source_url, request, http):
        result = parse_feed(http.get(source_url).content, source_url, 500)
        target = canonicalize_url(request.url)
        entry = next((row for row in result.items if canonicalize_url(row.url) == target), None)
        if entry is None:
            raise SourceError("详情不在当前 RSS 中，历史或跨站文章请单独添加网页来源")
        document = extract_html(target, get_page(target, http))
        document.item.published_at = document.item.published_at or entry.published_at
        document.item.author = document.item.author or entry.author
        document.item.metadata["feed_url"] = source_url
        return document


class BlackHatPlatform(WebPlatform):
    kind, label = "blackhat", "Black Hat 平台"
    operations = ("list",)

    def query(self, source_url, request, http):
        target = allowed_page(request.subject or source_url, source_url, blackhat=True)
        soup = BeautifulSoup(get_page(target, http).content, "html.parser")
        items = {}
        for link in soup.select("a[href]"):
            title = link.get_text(" ", strip=True)
            href = urljoin(target, link["href"])
            try:
                url = allowed_page(href, source_url, blackhat=True)
            except SourceError:
                continue
            if title and re.search(r"briefings|arsenal|archives|materials", url, re.I):
                fragment = urlsplit(href).fragment
                reference = url + ("#" + fragment if fragment else "")
                if reference != target:
                    items[reference] = RawItem(
                        reference,
                        reference,
                        title,
                        "conference_index",
                        metadata={"evidence_scope": "公开活动或演讲链接"},
                    )
        if not items:
            raise SourceError("未识别到 Black Hat 条目，可能需要更新专用解析器")
        return slice_page(
            list(items.values()),
            request,
            "subject 可指定活动页；调用方决定下一步读取哪个活动或演讲",
        )

    def detail(self, source_url, request, http):
        target = allowed_page(request.url, source_url, blackhat=True)
        response = get_page(target, http)
        fragment = urlsplit(request.url).fragment
        if fragment:
            soup = BeautifulSoup(response.content, "html.parser")
            node = soup.find(id=fragment)
            if node is None:
                raise SourceError("未找到演讲锚点，没有把整场活动页面当作演讲全文")
            container = node if node.name in {"article", "section", "div"} else node.parent
            scoped = httpx.Response(
                200, content=str(container).encode(), headers={"content-type": "text/html"}
            )
            doc = extract_html(target, scoped, blackhat=True)
            doc.materials = [Material("original.html", "text/html", target, response.content)]
        else:
            doc = extract_html(target, response, blackhat=True)
        doc.item.url, doc.item.external_id = request.url, request.url
        doc.item.metadata["session_description"] = doc.item.clean_text
        doc.status, doc.reason = "partial", "演讲页面描述不是材料全文，需要读取公开 PDF"
        if request.download_attachments:
            texts = []
            for attachment in doc.attachments[:3]:
                if not urlsplit(attachment["url"]).path.lower().endswith(".pdf"):
                    continue
                try:
                    data = get_pdf(attachment["url"], http)
                    body, sections, error = pdf_text(data)
                    doc.materials.append(
                        Material(
                            f"material-{len(doc.materials)}.pdf",
                            "application/pdf",
                            attachment["url"],
                            data,
                        )
                    )
                    attachment["downloaded"] = True
                    attachment["text_status"] = "partial" if error else "full"
                    if error:
                        attachment["error"] = error
                    if body:
                        texts.append(body)
                        doc.sections.extend(sections)
                except SourceError as exc:
                    attachment["error"] = str(exc)
            if texts:
                doc.item.clean_text = "\n\n".join(texts)
            pdfs = [a for a in doc.attachments if urlsplit(a["url"]).path.lower().endswith(".pdf")]
            if pdfs and all(a.get("text_status") == "full" for a in pdfs):
                doc.status, doc.reason = "full", ""
        return doc


def arxiv_id(value):
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        if parsed.hostname not in {"arxiv.org", "export.arxiv.org"}:
            raise SourceError("论文链接必须属于 arxiv.org")
        value = re.sub(r"^/(abs|pdf)/", "", parsed.path).removesuffix(".pdf")
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?", value):
        raise SourceError("arXiv 论文 ID 无效")
    return value


class ArxivPlatform:
    kind, label = "arxiv", "arXiv 平台"
    operations = ("list", "search")

    def entries(self, params, http):
        # Respect arXiv's requested per-domain interval in addition to SafeHTTP limits.
        http.last_request = getattr(http, "last_request", {})
        import time

        last = http.last_request.get("export.arxiv.org", 0)
        if time.monotonic() - last < 3:
            time.sleep(3 - (time.monotonic() - last))
        response = http.get("https://export.arxiv.org/api/query?" + urlencode(params))
        parsed = feedparser.parse(response.content)
        if not parsed.get("version") or parsed.bozo:
            raise SourceError("arXiv 未返回有效 Atom 数据")
        if any("api/errors" in e.get("id", "") for e in parsed.entries):
            raise SourceError("arXiv 拒绝此查询，请检查语法或论文 ID")
        return response, parsed

    def item(self, entry):
        identifier = arxiv_id(entry.id)
        stable_id = re.sub(r"v\d+$", "", identifier)
        return RawItem(
            stable_id,
            "https://arxiv.org/abs/" + stable_id,
            " ".join(entry.title.split()),
            "paper",
            clean_text=entry.get("summary", ""),
            acquisition_level="abstract_only",
            author=", ".join(a.name for a in entry.get("authors", [])),
            published_at=parse_date(entry.get("published")),
            metadata={
                "version": identifier,
                "updated_at": entry.get("updated"),
                "categories": [t.term for t in entry.get("tags", [])],
                "pdf_url": "https://arxiv.org/pdf/" + identifier,
                "evidence_scope": "官方论文元数据与摘要，不是全文",
            },
        )

    def query(self, source_url, request, http):
        page = page_number(request.cursor)
        _, parsed = self.entries(
            {
                "search_query": request.query.strip() or "cat:cs.*",
                "start": (page - 1) * request.limit,
                "max_results": request.limit,
                "sortBy": "lastUpdatedDate",
                "sortOrder": "descending",
            },
            http,
        )
        items = [self.item(e) for e in parsed.entries]
        total = int(parsed.feed.get("opensearch_totalresults", len(items)))
        return Page(
            items,
            str(page + 1) if page * request.limit < total else None,
            "调用方指定查询；默认计算机领域更新，不自动推断兴趣",
        )

    def detail(self, source_url, request, http):
        response, parsed = self.entries({"id_list": arxiv_id(request.url)}, http)
        if not parsed.entries:
            raise SourceError("arXiv 未找到此论文")
        item = self.item(parsed.entries[0])
        pdf_url = item.metadata["pdf_url"]
        data = get_pdf(pdf_url, http)
        body, sections, error = pdf_text(data)
        item.metadata["source_abstract"] = item.clean_text
        item.metadata["evidence_scope"] = "原始 PDF 逐页提取，不含 OCR 或公式理解"
        item.metadata["completeness_check"] = "all_pages_text_not_human_verified"
        item.clean_text = body
        return Document(
            item,
            "partial" if error else "full",
            error,
            [
                Material(
                    "metadata.atom",
                    "application/atom+xml",
                    str(response.request.url),
                    response.content,
                ),
                Material("paper.pdf", "application/pdf", pdf_url, data),
            ],
            [{"url": pdf_url, "label": "原始 PDF", "downloaded": True}],
            sections,
        )
