"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tag, status, dates)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from itertools import chain, groupby
from typing import Optional

from . import ai
from .db import get_conn

# 主排序字段相同时，用唯一的 ID 固定顺序，避免跨页重复或遗漏。
SORTS = {
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def _normalize_tags(names: Iterable[str]) -> set[str]:
    # 沿用保存标签时的去空格和转小写规则，集合会自动去重。
    normalized = {name.strip().lower() for name in names}
    normalized.discard("")
    return normalized


def _keyword_query(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Iterable[str] = (),
    exclude_tags: Iterable[str] = (),
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
) -> tuple[str, list, str]:
    # 列表和导出共用筛选参数与排序，结果只在是否分页上有区别。
    where = ["a.status = ?"]
    params: list = [status]
    join = ""

    match = _fts_query(q)
    if match:
        join += " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH ?")
        params.append(match)
    elif q.strip():
        # 纯标点没有可搜索的词，直接返回零匹配，避免执行空的 MATCH。
        where.append("0 = 1")
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    included = _normalize_tags(include_tags)
    if tag:
        # 旧的单标签参数也作为一个必选标签，继续沿用原有的小写匹配。
        included.add(tag.lower())
    excluded = _normalize_tags(exclude_tags)

    # 条件用 AND 连接，因此包含标签必须全部存在，排除标签必须全部不存在。
    for operator, names in (("EXISTS", included), ("NOT EXISTS", excluded)):
        for name in sorted(names):
            where.append(
                f"{operator} (SELECT 1 FROM asset_tags at "
                "JOIN tags t ON t.id = at.tag_id "
                "WHERE at.asset_id = a.id AND t.name = ?)"
            )
            params.append(name)
    if collection_id:
        # 通过关联表判断素材是否属于指定集合，每个素材最多返回一行。
        where.append(
            "EXISTS (SELECT 1 FROM collection_assets ca "
            "WHERE ca.asset_id = a.id AND ca.collection_id = ?)"
        )
        params.append(collection_id)

    where_sql = " AND ".join(where)
    # 关键词用于匹配，结果顺序由用户选择的排序决定。
    order = SORTS.get(sort, SORTS["newest"])
    return f"FROM assets a{join} WHERE {where_sql}", params, order


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Iterable[str] = (),
    exclude_tags: Iterable[str] = (),
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    from_where, params, order = _keyword_query(
        q=q,
        media_type=media_type,
        tag=tag,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
        sort=sort,
    )
    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) {from_where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT a.* {from_where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )

    return {
        "total": total,
        "items": [_card(dict(row)) for row in rows],
        "facets": _facets(status),
        "mode": mode,
    }


CSV_FIELDS = ("uuid", "title", "description", "filename", "media_type", "tags", "created_at")


def export_csv(**filters) -> bytes:
    from_where, params, order = _keyword_query(**filters)
    # 先选出全部匹配素材，再关联它们的所有标签。排序让同一素材的标签行相邻。
    rows = get_conn().execute(
        f"""
        WITH matched AS (
            SELECT a.id, a.uuid, a.title, a.description, a.filename,
                   a.media_type, a.created_at, a.size
            {from_where}
        )
        SELECT a.*, t.name AS tag_name
        FROM matched a
        LEFT JOIN asset_tags at ON at.asset_id = a.id
        LEFT JOIN tags t ON t.id = at.tag_id
        ORDER BY {order}, t.name ASC
        """,
        params,
    )

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    for _, group in groupby(rows, key=lambda row: row["id"]):
        asset = next(group)
        # 每个素材只写一行，标签列用 JSON 数组保留标签名里的逗号和引号。
        tags = [
            row["tag_name"] for row in chain((asset,), group)
            if row["tag_name"] is not None
        ]
        writer.writerow((
            asset["uuid"], asset["title"], asset["description"], asset["filename"],
            asset["media_type"], json.dumps(tags, ensure_ascii=False), asset["created_at"],
        ))

    # 以带 BOM 的 UTF-8 导出，便于表格软件识别中文。
    return output.getvalue().encode("utf-8-sig")


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
