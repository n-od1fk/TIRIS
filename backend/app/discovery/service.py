"""Small bounded discovery baseline, not an LLM Agent or a trained recommender."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, or_, select

from app.config import get_settings
from app.database import SessionLocal
from app.discovery.schemas import DiscoveryData
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    DiscoveryRun,
    DiscoverySettings,
    EmbeddingChunk,
    EmbeddingSpace,
    Favorite,
    FixedColumn,
    Job,
    LongTermProfile,
    Recommendation,
    Source,
)
from app.profiles.api import lock_preferences
from app.retrieval import providers
from app.retrieval.chunks import passages
from app.retrieval.service import latest_versions


def interests(profile, data, columns):
    terms = [(term, "近期明确兴趣", 2) for term in data["recent_topics"]]
    terms += [
        (term, "长期方向/目标", 1)
        for term in profile.get("long_term_directions", []) + profile.get("learning_goals", [])
    ]
    terms += [(term, f"固定栏目：{c['name']}", 1) for c in columns for term in c["keywords"]]
    seen, result = set(), []
    for term, origin, weight in terms:
        key = term.strip().casefold()
        if key and key not in seen:
            result.append({"term": term, "origin": origin, "weight": weight})
            seen.add(key)
    return result[:10]


def enqueue(session, user_id, schedule_key=None, mode="discover", refresh=True, feed=False):
    from app.workers.tasks import discover

    lock_preferences(session, user_id)
    active = session.scalar(
        select(DiscoveryRun).where(
            DiscoveryRun.user_id == user_id, DiscoveryRun.status.in_(["queued", "running"])
        )
    )
    if active:
        return active
    if schedule_key:
        previous = session.scalar(
            select(DiscoveryRun).where(
                DiscoveryRun.user_id == user_id, DiscoveryRun.schedule_key == schedule_key
            )
        )
        if previous:
            return previous
    settings = session.scalar(select(DiscoverySettings).where(DiscoverySettings.user_id == user_id))
    data = DiscoveryData.model_validate(settings.data if settings else {}).model_dump()
    profile = session.scalar(select(LongTermProfile).where(LongTermProfile.user_id == user_id))
    profile_data = profile.data if profile else {}
    columns = [
        {
            "name": c.name,
            "keywords": c.keywords,
            "source_ids": c.source_ids,
            "content_types": c.content_types,
            "items_per_day": c.items_per_day,
        }
        for c in session.scalars(
            select(FixedColumn)
            .where(FixedColumn.user_id == user_id, FixedColumn.enabled.is_(True))
            .order_by(FixedColumn.created_at, FixedColumn.id)
        )
    ]
    terms = interests(profile_data, data, columns)
    if mode == "discover" and not terms and not columns:
        saved = session.scalar(
            select(Favorite.id)
            .where(Favorite.user_id == user_id, Favorite.active.is_(True))
            .limit(1)
        )
        if schedule_key and saved:
            mode = "updates"
        else:
            raise ValueError("请先填写近期兴趣、长期方向/目标或固定栏目，不能随机推荐")
    snapshot = {
        "settings": data,
        "profile_version": profile.version if profile else 0,
        "terms": terms,
        "columns": columns,
        "blocked_topics": profile_data.get("blocked_topics", []),
        "mode": mode,
        "refresh_sources": refresh,
        "feed": feed,
    }
    row = DiscoveryRun(user_id=user_id, schedule_key=schedule_key, snapshot=snapshot)
    session.add(row)
    session.commit()
    try:
        discover.delay(str(row.id))
    except Exception:
        row.status, row.error, row.finished_at = "failed", "发现任务队列不可用", datetime.now(UTC)
        session.commit()
        raise RuntimeError("queue unavailable") from None
    return row


def search_sources(user_id, terms):
    ids = []
    with SessionLocal.begin() as session:
        lock_preferences(session, user_id)
        for entry in terms[:3]:
            # User terms cannot inject GitHub query qualifiers. Deliberately bounded to one page.
            term = " ".join(re.sub(r"[^\w\s.+-]", " ", entry["term"]).split())
            if not term:
                continue
            query = f'"{term}" in:name,description,readme fork:false archived:false'
            url = "https://api.github.com/search/repositories?" + urlencode(
                {"q": query, "sort": "updated", "order": "desc", "per_page": 10}
            )
            source = session.scalar(
                select(Source).where(
                    Source.user_id == user_id, Source.kind == "github_search", Source.url == url
                )
            )
            if source is None:
                source = Source(
                    user_id=user_id,
                    name=f"兴趣搜索：{entry['term']}"[:200],
                    kind="github_search",
                    url=url,
                )
                session.add(source)
                session.flush()
            if source.enabled and not source.archived:
                ids.append(source.id)
    return ids


def refresh_sources(user_id, ids):
    from app.workers.tasks import execute_sync

    reports = []
    for source_id in ids:
        with SessionLocal.begin() as session:
            source = session.scalar(
                select(Source)
                .where(
                    Source.id == source_id,
                    Source.user_id == user_id,
                    Source.enabled.is_(True),
                    Source.archived.is_(False),
                )
                .with_for_update()
            )
            if not source:
                continue
            existing = session.scalar(
                select(Job.id).where(
                    Job.source_id == source_id, Job.status.in_(["queued", "running", "retrying"])
                )
            )
            if existing:
                reports.append(
                    {"source": source.name, "status": "busy", "error": "已有活动同步，未重复采集"}
                )
                continue
            job = Job(user_id=user_id, source_id=source_id)
            session.add(job)
            session.flush()
            job_id, name = job.id, source.name
        # One attempt per discovery source; no sleep or indefinite retries in a daily run.
        retry = execute_sync(job_id)
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if retry:
                job.status, job.finished_at = "failed", datetime.now(UTC)
                job.error = "本次发现遇到临时网络错误或限流，未继续重试；可手动同步"
            reports.append({"source": name, "status": job.status, "error": job.error})
    return reports


def candidate_rows(user_id, feed=False, rule_terms=None, columns=None, blocked=None):
    now = datetime.now(UTC)
    with SessionLocal() as session:
        latest = latest_versions(user_id)
        saved = select(Favorite.item_id).where(
            Favorite.user_id == user_id, Favorite.active.is_(True)
        )
        exposed = (
            select(Recommendation.version_id)
            .join(DiscoveryRun, DiscoveryRun.id == Recommendation.run_id)
            .where(
                Recommendation.user_id == user_id,
                DiscoveryRun.user_id == user_id,
                True if feed else DiscoveryRun.created_at > now - timedelta(days=7),
            )
        )
        query = (
            select(ContentItem, ContentVersion)
            .join(ContentVersion, ContentVersion.item_id == ContentItem.id)
            .join(Source, Source.id == ContentVersion.source_id)
            .where(
                Source.archived.is_(False),
                ContentItem.user_id == user_id,
                ContentVersion.user_id == user_id,
                ContentVersion.id.in_(select(latest.c.id)),
                or_(
                    ContentVersion.metadata_json["content_status"].as_string().is_(None),
                    ContentVersion.metadata_json["content_status"].as_string() == "full",
                ),
                ~ContentItem.id.in_(saved),
                ~ContentVersion.id.in_(exposed),
                ContentItem.candidate_expires_at > now,
                or_(
                    ContentItem.content_type == "repository",
                    ContentVersion.published_at.is_(None),
                    ContentVersion.published_at >= now - timedelta(days=30),
                ),
            )
        )
        if rule_terms is not None:
            text = (
                ContentVersion.title
                + " "
                + ContentVersion.clean_text
                + " "
                + func.coalesce(cast(ContentVersion.metadata_json["topics"], Text), "")
            )
            conditions = [
                text.icontains(t["term"], autoescape=True)
                for t in rule_terms
                if not t["origin"].startswith("固定栏目")
            ]
            for column in columns or []:
                parts = []
                if column["keywords"]:
                    parts.append(
                        or_(*(text.icontains(t, autoescape=True) for t in column["keywords"]))
                    )
                if column["content_types"]:
                    parts.append(ContentItem.content_type.in_(column["content_types"]))
                if column["source_ids"]:
                    source_items = select(ContentOccurrence.item_id).where(
                        ContentOccurrence.user_id == user_id,
                        ContentOccurrence.source_id.in_(
                            [UUID(str(s)) for s in column["source_ids"]]
                        ),
                    )
                    parts.append(ContentItem.id.in_(source_items))
                conditions.append(and_(*parts))
            query = query.where(or_(*conditions) if conditions else False)
            for term in blocked or []:
                query = query.where(~text.icontains(term, autoescape=True))
        rows = session.execute(
            query.order_by(ContentVersion.fetched_at.desc(), ContentItem.id).limit(
                200 if feed else 40
            )
        ).all()
        result = []
        for item, version in rows:
            # Existing repositories are useful resources, not newly published news.
            published = version.published_at
            if (
                item.content_type != "repository"
                and published
                and published < now - timedelta(days=30)
            ):
                continue
            occurrences = session.execute(
                select(ContentOccurrence.source_id, Source.name)
                .join(Source)
                .where(ContentOccurrence.item_id == item.id, ContentOccurrence.user_id == user_id)
            ).all()
            result.append(
                {
                    "item_id": item.id,
                    "version_id": version.id,
                    "title": version.title,
                    "body": version.clean_text,
                    "kind": item.content_type,
                    "metadata": version.metadata_json,
                    "level": version.acquisition_level,
                    "source_ids": [str(r[0]) for r in occurrences],
                    "source": " / ".join(sorted({r[1] for r in occurrences})),
                    "published_at": published,
                    "fetched_at": version.fetched_at,
                }
            )
        return result


def semantic_scores(user_id, rows, terms):
    """Reuse local provider and vector storage; cache one bounded passage per candidate version."""
    if not rows or not terms:
        return {}
    config = get_settings()
    embedder = providers.get_embedder("local", config)
    description = {
        **providers.descriptor("local", config),
        "pipeline": "interest-title200-body600-max1-v1",
    }
    key = providers.space_key(description)
    with SessionLocal.begin() as session:
        lock_preferences(session, user_id)
        space = session.scalar(
            select(EmbeddingSpace).where(
                EmbeddingSpace.user_id == user_id, EmbeddingSpace.key == key
            )
        )
        if space is None:
            space = EmbeddingSpace(
                user_id=user_id, key=key, provider="local", descriptor=description
            )
            session.add(space)
            session.flush()
        space_id = space.id
        cached = {
            chunk.version_id: list(chunk.embedding)
            for chunk in session.scalars(
                select(EmbeddingChunk).where(
                    EmbeddingChunk.space_id == space_id,
                    EmbeddingChunk.user_id == user_id,
                    EmbeddingChunk.version_id.in_([row["version_id"] for row in rows]),
                )
            )
        }
    for row in rows:
        if row["version_id"] in cached:
            continue
        chunk = passages(row["title"], row["body"])[0]
        chunk["evidence"].update(
            body_truncated=chunk["evidence"]["body_chars"] > 600,
            acquisition_level=row["level"],
            evidence_scope=row["metadata"].get("evidence_scope", ""),
        )
        vector = embedder.encode([chunk["passage"]]).vectors[0]
        with SessionLocal.begin() as session:
            session.add(
                EmbeddingChunk(
                    user_id=user_id,
                    space_id=space_id,
                    item_id=row["item_id"],
                    version_id=row["version_id"],
                    **chunk,
                    embedding=vector,
                )
            )
        cached[row["version_id"]] = vector
    queries = embedder.encode([t["term"] for t in terms], query=True).vectors
    result = {}
    for row in rows:
        candidates = [
            (
                sum(float(a) * b for a, b in zip(cached[row["version_id"]], vector, strict=True)),
                term,
            )
            for term, vector in zip(terms, queries, strict=True)
        ]
        result[row["item_id"]] = max(candidates, key=lambda pair: pair[0])
    return result


def rank(rows, terms, columns, blocked, semantic, threshold):
    pool = []
    for row in rows:
        text = (
            row["title"] + " " + row["body"] + " " + " ".join(row["metadata"].get("topics", []))
        ).casefold()
        if any(term.casefold() in text for term in blocked):
            continue
        matches = [
            term
            for term in terms
            if not term["origin"].startswith("固定栏目") and term["term"].casefold() in text
        ]
        sim, semantic_term = semantic.get(row["item_id"], (None, None))
        score = sum(term["weight"] for term in matches)
        if sim is not None and sim >= threshold:
            score += sim
        pool.append(
            {
                **row,
                "text": text,
                "matches": matches,
                "similarity": sim,
                "semantic_term": semantic_term,
                "rank_score": score,
            }
        )
    pool.sort(key=lambda r: (r["rank_score"], r["fetched_at"], str(r["item_id"])), reverse=True)
    chosen, used, gaps = [], set(), []
    for column in columns:
        count = 0
        for row in pool:
            if row["item_id"] in used:
                continue
            if column["keywords"] and not any(
                t.casefold() in row["text"] for t in column["keywords"]
            ):
                continue
            if column["source_ids"] and not set(column["source_ids"]) & set(row["source_ids"]):
                continue
            if column["content_types"] and row["kind"] not in column["content_types"]:
                continue
            chosen.append(
                {
                    **row,
                    "column_name": column["name"],
                    "reason": (
                        f"符合你保留的固定栏目「{column['name']}」；关键词/来源/类型条件已检查"
                    ),
                }
            )
            used.add(row["item_id"])
            count += 1
            if count == column["items_per_day"]:
                break
        if count < column["items_per_day"]:
            gaps.append({"column": column["name"], "missing": column["items_per_day"] - count})
    for row in pool:
        if len(chosen) >= 10:
            break
        if row["item_id"] in used or row["rank_score"] <= 0:
            continue
        if row["matches"]:
            reason = "匹配" + "、".join(f"{t['origin']}「{t['term']}」" for t in row["matches"][:3])
        else:
            reason = (
                f"与{row['semantic_term']['origin']}「{row['semantic_term']['term']}」语义接近；"
                "阈值是待人工验证的基线，不代表相关概率"
            )
        chosen.append({**row, "column_name": None, "reason": reason})
        used.add(row["item_id"])
    return chosen[:10], gaps


def execute(run_id: UUID):
    with SessionLocal.begin() as session:
        run = session.scalar(
            select(DiscoveryRun).where(DiscoveryRun.id == run_id).with_for_update()
        )
        if run is None or run.status != "queued":
            return
        run.status, run.started_at = "running", datetime.now(UTC)
        snapshot, user_id = run.snapshot, run.user_id
    report = {
        "source_results": [],
        "warnings": [],
        "recommendation_count": 0,
        "candidate_limit": 40,
        "column_gaps": [],
        "matching": snapshot["settings"]["matching"],
    }
    try:
        with SessionLocal() as session:
            source_query = select(Source.id).where(
                Source.user_id == user_id,
                Source.enabled.is_(True),
                Source.archived.is_(False),
                Source.kind != "github_search",
                ~Source.kind.in_(["arxiv", "web"]),
                ~((Source.kind == "github") & (Source.url == "https://github.com/")),
            )
            if snapshot["mode"] == "updates":
                saved_items = select(Favorite.item_id).where(
                    Favorite.user_id == user_id, Favorite.active.is_(True)
                )
                linked_sources = select(ContentOccurrence.source_id).where(
                    ContentOccurrence.user_id == user_id, ContentOccurrence.item_id.in_(saved_items)
                )
                saved_urls = select(ContentItem.canonical_url).where(
                    ContentItem.user_id == user_id, ContentItem.id.in_(saved_items)
                )
                source_query = source_query.where(
                    Source.id.in_(linked_sources) | Source.url.in_(saved_urls)
                )
            ids = list(
                session.scalars(
                    source_query.order_by(
                        Source.last_success_at.asc().nullsfirst(), Source.id
                    ).limit(20)
                )
            )
        refresh = snapshot.get("refresh_sources", True)
        if refresh and snapshot["mode"] == "discover" and snapshot["settings"]["github_search"]:
            ids += search_sources(user_id, snapshot["terms"])
        report["source_results"] = (
            refresh_sources(user_id, list(dict.fromkeys(ids))) if refresh else []
        )
        report["refreshed_sources"] = refresh
        if not ids:
            report["warnings"].append(
                "没有已收藏内容的可用跟踪来源"
                if snapshot["mode"] == "updates"
                else "没有可用来源；请添加 RSS/仓库或明确开启 GitHub 兴趣搜索"
            )
        columns = snapshot["columns"]
        if snapshot.get("feed"):
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            with SessionLocal() as session:
                counts = dict(
                    session.execute(
                        select(
                            Recommendation.column_name,
                            func.count(func.distinct(Recommendation.item_id)),
                        )
                        .join(DiscoveryRun, DiscoveryRun.id == Recommendation.run_id)
                        .where(Recommendation.user_id == user_id, DiscoveryRun.created_at >= today)
                        .group_by(Recommendation.column_name)
                    ).all()
                )
            columns = [
                {**c, "items_per_day": c["items_per_day"] - counts.get(c["name"], 0)}
                for c in columns
                if c["items_per_day"] > counts.get(c["name"], 0)
            ]
        rows = (
            candidate_rows(
                user_id,
                feed=snapshot.get("feed", False),
                rule_terms=snapshot["terms"]
                if snapshot.get("feed") and snapshot["settings"]["matching"] == "rules"
                else None,
                columns=columns,
                blocked=snapshot["blocked_topics"],
            )
            if snapshot["mode"] == "discover"
            else []
        )
        report["candidate_limit"] = 200 if snapshot.get("feed") else 40
        semantic = {}
        if rows and snapshot["settings"]["matching"] == "local":
            try:
                semantic = semantic_scores(
                    user_id,
                    rows,
                    [t for t in snapshot["terms"] if not t["origin"].startswith("固定栏目")],
                )
            except providers.EmbeddingError as exc:
                report["warnings"].append(f"本地语义匹配未完成，明确回退规则基线：{exc}")
                report["matching"] = "rules-fallback"
        chosen, gaps = rank(
            rows,
            snapshot["terms"],
            columns,
            snapshot["blocked_topics"],
            semantic,
            snapshot["settings"]["semantic_threshold"],
        )
        if snapshot["mode"] == "updates":
            chosen, gaps = [], []
        report.update(
            recommendation_count=len(chosen), column_gaps=gaps, candidates_checked=len(rows)
        )
        if snapshot["mode"] == "discover" and not chosen:
            report["warnings"].append("没有足够相关的候选；未使用无关资料凑数")
        failed = [r for r in report["source_results"] if r["status"] != "succeeded"]
        with SessionLocal.begin() as session:
            for position, row in enumerate(chosen, 1):
                session.add(
                    Recommendation(
                        user_id=user_id,
                        run_id=run_id,
                        item_id=row["item_id"],
                        version_id=row["version_id"],
                        position=position,
                        reason=row["reason"],
                        column_name=row["column_name"],
                        score={
                            "baseline": report["matching"],
                            "similarity": row["similarity"],
                            "rule_score": row["rank_score"],
                            "source": row["source"],
                            "published_at_unknown": row["published_at"] is None,
                            "resource_not_news": row["kind"] == "repository",
                        },
                    )
                )
            run = session.get(DiscoveryRun, run_id)
            run.report, run.finished_at = report, datetime.now(UTC)
            run.status = "partial" if failed or report["warnings"] or gaps else "succeeded"
    except Exception:
        with SessionLocal.begin() as session:
            run = session.get(DiscoveryRun, run_id)
            run.status, run.error, run.finished_at = (
                "failed",
                "发现处理失败，请检查来源和服务配置；未自动重试",
                datetime.now(UTC),
            )
            run.report = report


def fail_run(run_id: UUID, user_id: UUID):
    with SessionLocal.begin() as session:
        row = session.scalar(
            select(DiscoveryRun)
            .where(DiscoveryRun.id == run_id, DiscoveryRun.user_id == user_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError("发现任务不存在")
        if row.status in {"queued", "running"}:
            row.status, row.finished_at = "failed", datetime.now(UTC)
            row.error = "管理员停止 Worker 后结束中断任务；可手动重试，不保证队列 exactly-once"
