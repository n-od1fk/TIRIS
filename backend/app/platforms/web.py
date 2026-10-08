import io
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from app.config import get_settings
from app.connectors.adapters import parse_date
from app.connectors.contracts import RawItem, SourceError
from app.connectors.http import canonicalize_url
from app.platforms.contracts import Document, Material


def allowed_page(url, source_url, *, blackhat=False):
    normalized = canonicalize_url(url)
    allowed = {"blackhat.com", "www.blackhat.com"} if blackhat else {urlsplit(source_url).hostname}
    if urlsplit(normalized).hostname not in allowed:
        raise SourceError("详情地址不属于此平台连接，请添加对应来源")
    return normalized


def get_page(url, http):
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    cache = getattr(http, "robots_cache", None)
    if cache is None:
        cache = http.robots_cache = {}
    if robots_url not in cache:
        parser = RobotFileParser()
        try:
            response = http.get(robots_url)
            parser.parse(response.text.splitlines())
        except SourceError as exc:
            if "HTTP 404" not in str(exc):
                raise SourceError("无法确认网页采集规则，未继续访问正文") from None
            parser.parse([])
        cache[robots_url] = parser
    if not cache[robots_url].can_fetch("TIRIS", url):
        raise SourceError("网站 robots.txt 不允许此页面采集")
    response = http.get(url)
    if "html" not in response.headers.get("content-type", "text/html").lower():
        raise SourceError("详情不是 HTML 页面")
    return response


def extract_html(url, response, *, blackhat=False):
    import trafilatura

    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.find("h1") or soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else url
    body = (
        trafilatura.extract(
            response.content,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_recall=True,
        )
        or ""
    )
    blocked = soup.select('[class*="paywall"], [id*="paywall"], [class*="captcha"]')
    complete = len(body.strip()) >= 100 and not blocked
    attachments = {}
    for link in soup.select("a[href]"):
        href = urljoin(url, link["href"])
        if not urlsplit(href).path.lower().endswith((".pdf", ".zip", ".ppt", ".pptx", ".txt")):
            continue
        try:
            href = canonicalize_url(href)
        except SourceError:
            continue
        attachments[href] = {
            "url": href,
            "label": link.get_text(" ", strip=True),
            "downloaded": False,
        }
    author = soup.select_one('meta[name="author"]')
    published = soup.select_one('meta[property="article:published_time"], meta[name="date"]')
    item = RawItem(
        url,
        url,
        title,
        "conference_talk" if blackhat else "article",
        clean_text=body,
        author=author.get("content") if author else None,
        published_at=parse_date(published.get("content")) if published else None,
        metadata={
            "evidence_scope": "原始 HTML 正文提取；不是 AI 摘要",
            "completeness_check": "heuristic_not_human_verified",
        },
    )
    return Document(
        item,
        "full" if complete else "partial",
        "" if complete else "正文不足或存在访问提示，不能作为完整正文",
        [Material("original.html", "text/html", url, response.content)],
        list(attachments.values()),
        [{"type": "paragraph", "text": p} for p in body.splitlines() if p.strip()],
    )


def _extract_pdf(data, max_pages):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return "", [], "PDF 已加密"
        if len(reader.pages) > max_pages:
            return "", [], "PDF 超过页数预算，未把截断内容当全文"
        sections = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            sections.append({"type": "page", "page": number, "text": text})
            if sum(len(s["text"]) for s in sections) > 2_000_000:
                return "", [], "PDF 提取文字超过预算，已保留原文件"
        body = "\n\n".join(s["text"] for s in sections)
        error = (
            ""
            if sections and all(s["text"].strip() for s in sections) and len(body) >= 100
            else ("PDF 有空白或扫描页面，未进行 OCR；不能视为完整正文")
        )
        return body, sections, error
    except Exception:
        return "", [], "PDF 文本解析失败，已保留原文件"


def _pdf_worker(data, max_pages, pipe):
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (1_000_000_000, 1_000_000_000))
            resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        except ImportError:
            pass  # Windows still has the parent-enforced wall-clock deadline.
        pipe.send(_extract_pdf(data, max_pages))
    finally:
        pipe.close()


def pdf_text(data):
    """Parse untrusted PDFs out of process; preserve originals on timeout/crash."""
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_worker, args=(data, get_settings().pdf_max_pages, send))
    try:
        process.start()
        send.close()
        if receive.poll(25):
            return receive.recv()
        return "", [], "PDF 解析超时，已保留原文件"
    except (EOFError, OSError):
        return "", [], "PDF 解析进程失败，已保留原文件"
    finally:
        receive.close()
        send.close()
        if process.pid:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)


def get_pdf(url, http):
    response = http.get(url, max_bytes=get_settings().material_max_bytes)
    if not response.content.startswith(b"%PDF-"):
        raise SourceError("材料地址未返回 PDF 文件")
    return response.content
