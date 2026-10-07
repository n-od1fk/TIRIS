import hashlib
import re


def passages(title: str, body: str) -> list[dict]:
    """Bounded character baseline, not a token-aware/semantic RAG chunker."""
    body = re.sub(r"\s+", " ", body).strip()
    full_title = re.sub(r"\s+", " ", title).strip()
    title = full_title[:200]
    chunks = []
    for ordinal in range(8):
        start = ordinal * 500
        end = min(start + 600, len(body))
        passage = f"Title: {title}\n{body[start:end]}".strip()
        chunks.append(
            {
                "ordinal": ordinal,
                "passage": passage,
                "text_hash": hashlib.sha256(passage.encode()).hexdigest(),
                "evidence": {
                    "start_char": start,
                    "end_char": end,
                    "body_chars": len(body),
                    "title_truncated": len(full_title) > 200,
                    "body_truncated": len(body) > 4100,
                },
            }
        )
        if end == len(body):
            break
    return chunks
