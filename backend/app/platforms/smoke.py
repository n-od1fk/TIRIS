"""Explicit live network checks. No database writes, imports, Stars or favorites.

Run: python -m app.platforms.smoke --kind github
"""

import argparse
import json

from app.connectors.contracts import SourceError
from app.connectors.http import SafeHTTP
from app.platforms.contracts import DetailRequest, PlatformQuery
from app.platforms.registry import get_platform


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=["github", "rss", "arxiv", "blackhat"])
    args = parser.parse_args()
    provider = get_platform(args.kind)
    http = SafeHTTP()
    try:
        if args.kind == "github":
            page = provider.query(
                "https://github.com/",
                PlatformQuery(operation="search", query="repo:pgvector/pgvector", limit=1),
                http,
            )
            document = provider.detail(
                "https://github.com/",
                DetailRequest(url="https://github.com/pgvector/pgvector"),
                http,
            )
        elif args.kind == "rss":
            source = "https://blog.cloudflare.com/rss/"
            page = provider.query(source, PlatformQuery(limit=1), http)
            if not page.items:
                raise SourceError("真实订阅未返回条目")
            document = provider.detail(source, DetailRequest(url=page.items[0].url), http)
        elif args.kind == "arxiv":
            page = provider.query(
                "https://arxiv.org/",
                PlatformQuery(operation="search", query='ti:"Attention Is All You Need"', limit=1),
                http,
            )
            document = provider.detail("https://arxiv.org/", DetailRequest(url="1706.03762"), http)
        else:
            page = provider.query(
                "https://blackhat.com/html/archives.html", PlatformQuery(limit=1), http
            )
            print(
                json.dumps(
                    {"platform": args.kind, "list_count": len(page.items), "detail_tested": False},
                    ensure_ascii=False,
                )
            )
            return
        print(
            json.dumps(
                {
                    "platform": args.kind,
                    "list_count": len(page.items),
                    "status": document.status,
                    "reason": document.reason,
                    "body_characters": len(document.item.clean_text),
                    "materials": [
                        {"filename": m.filename, "bytes": len(m.data)} for m in document.materials
                    ],
                },
                ensure_ascii=False,
            )
        )
    except SourceError as exc:
        print(
            json.dumps(
                {"platform": args.kind, "status": "failed", "error": str(exc)}, ensure_ascii=False
            )
        )
        raise SystemExit(1) from None
    finally:
        http.close()


if __name__ == "__main__":
    main()
