"""Core pipeline tests: storage dedup, ingest, metadata, search, auth, fixity.

These run against a throwaway data directory so they never touch a real
repository. Run with:  python -m pytest -q
"""
import csv
import io
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ["KEEPSTACK_DATA_DIR"] = tempfile.mkdtemp(prefix="keepstack-test-")
os.environ["KEEPSTACK_AI_ENABLED"] = "false"

from PIL import Image  # noqa: E402

from keepstack import ai, ingest, ocr, search, standards, storage, thumbnails, vision  # noqa: E402
from keepstack.auth import (hash_password, make_token, role_at_least,  # noqa: E402
                         verify_password, verify_token)
from keepstack.db import get_conn, init_db  # noqa: E402


def _img_bytes(color=(90, 130, 240), size=(400, 300)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _xmp_img_bytes():
    xmp = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
           xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
    <rdf:Description>
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">XMP Harbor Archive</rdf:li></rdf:Alt></dc:title>
      <dc:description><rdf:Alt><rdf:li xml:lang="x-default">NeedleMuseum catalog caption</rdf:li></rdf:Alt></dc:description>
      <dc:creator><rdf:Seq><rdf:li>Ada Archivist</rdf:li></rdf:Seq></dc:creator>
      <dc:subject><rdf:Bag><rdf:li>estuaries</rdf:li><rdf:li>tidal maps</rdf:li></rdf:Bag></dc:subject>
      <dc:publisher>NeedleMuseum Press</dc:publisher>
      <dc:date>2026-07-02</dc:date>
      <dc:rights>Creative Commons Test License</dc:rights>
      <photoshop:Credit>Archive Credit Line</photoshop:Credit>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    buf = io.BytesIO()
    Image.new("RGB", (180, 120), (20, 90, 130)).save(buf, format="JPEG", xmp=xmp.encode("utf-8"))
    return buf.getvalue()


def _split_img_bytes():
    buf = io.BytesIO()
    img = Image.new("RGB", (100, 60), (20, 40, 80))
    for x in range(100):
        for y in range(60):
            img.putpixel((x, y), (220, 30, 30) if x < 50 else (30, 180, 80))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def setup_module(_):
    init_db()


def test_password_hashing():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_token_roundtrip():
    tok = make_token(42, "admin")
    payload = verify_token(tok)
    assert payload["sub"] == 42 and payload["role"] == "admin"
    assert verify_token(tok + "x") is None


def test_role_hierarchy():
    assert role_at_least("admin", "viewer")
    assert role_at_least("editor", "contributor")
    assert not role_at_least("viewer", "editor")


def test_ingest_creates_asset_with_dimensions_and_thumb():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes()), "skyline.jpg")
    assert a["media_type"] == "image"
    assert a["width"] == 400 and a["height"] == 300
    assert a["sha256"] and storage.blob_path(a["storage_key"]).exists()
    assert storage.thumb_path(a["thumb_key"]).exists()


def test_content_addressable_dedup():
    data = _img_bytes(color=(10, 200, 120), size=(256, 256))
    first = ingest.ingest_stream(io.BytesIO(data), "dup_a.jpg")
    second = ingest.ingest_stream(io.BytesIO(data), "dup_b.jpg")
    assert second.get("duplicate") is True
    assert first["sha256"] == second["sha256"]


def test_keyword_search_finds_by_title():
    ingest.ingest_stream(io.BytesIO(_img_bytes(color=(200, 50, 90), size=(420, 260))),
                         "unique_harbor_photo.jpg", title="Unique Harbor Photo")
    res = search.search(q="harbor")
    assert res["total"] >= 1
    assert any("harbor" in (i["title"] or "").lower() for i in res["items"])


def test_collection_search_with_keyword_and_tag():
    matching = ingest.ingest_stream(
        io.BytesIO(_img_bytes(color=(31, 72, 184))),
        "collection_match.jpg", title="Collectionbeacon inside",
    )
    other_member = ingest.ingest_stream(
        io.BytesIO(_img_bytes(color=(88, 164, 38))),
        "collection_other.jpg", title="Other collection member",
    )
    outside = ingest.ingest_stream(
        io.BytesIO(_img_bytes(color=(182, 43, 67))),
        "collection_outside.jpg", title="Collectionbeacon outside",
    )

    with get_conn() as conn:
        collection_id = conn.execute(
            "INSERT INTO collections (uuid, name, created_at) VALUES (?, ?, ?)",
            ("collection-search-test", "Collection search test", matching["created_at"]),
        ).lastrowid
        conn.executemany(
            "INSERT INTO collection_assets (collection_id, asset_id) VALUES (?, ?)",
            [(collection_id, matching["id"]), (collection_id, other_member["id"])],
        )
        tag_id = conn.execute(
            "INSERT INTO tags (name) VALUES (?)", ("collectionproof",),
        ).lastrowid
        conn.executemany(
            "INSERT INTO asset_tags (asset_id, tag_id) VALUES (?, ?)",
            [(matching["id"], tag_id), (outside["id"], tag_id)],
        )

    result = search.search(collection_id=collection_id)
    assert result["total"] == 2
    assert {item["uuid"] for item in result["items"]} == {
        matching["uuid"], other_member["uuid"],
    }

    for filters in (
        {"q": "collectionbeacon"},
        {"tag": "COLLECTIONPROOF"},
        {"q": "collectionbeacon", "tag": "collectionproof", "media_type": "image"},
    ):
        result = search.search(collection_id=collection_id, **filters)
        assert result["total"] == 1
        assert [item["uuid"] for item in result["items"]] == [matching["uuid"]]


def test_asset_search_accepts_repeated_tag_filters():
    from fastapi.testclient import TestClient
    from keepstack.app import app
    from keepstack.config import config

    samples = {
        "match": ("approved", "campaign", "planning, q2"),
        "partial": ("approved",),
        "internal": ("approved", "campaign", "internal"),
        "draft": ("approved", "campaign", "draft"),
        "similar": ("preapproved", "campaign"),
    }
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={
            "username": config.admin_user, "password": config.admin_password,
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        uuids = {}
        for index, (sample, names) in enumerate(samples.items(), start=1):
            upload = client.post(
                "/api/assets", headers=headers, data={"run_ai": "false"},
                files={"files": (
                    f"tagfiltercase-{sample}.jpg",
                    _img_bytes(color=(index * 40, 96, 147)),
                    "image/jpeg",
                )},
            )
            assert upload.status_code == 200
            uuid = upload.json()["created"][0]["uuid"]
            uuids[sample] = uuid
            for name in names:
                response = client.post(
                    f"/api/assets/{uuid}/tags", headers=headers, json={"name": name},
                )
                assert response.status_code == 200

        keyword = ("q", "tagfiltercase")
        filters = [
            keyword,
            ("include_tag", " APPROVED "),
            ("include_tag", "approved"),
            ("include_tag", "campaign"),
            ("exclude_tag", "internal"),
            ("exclude_tag", "draft"),
            ("exclude_tag", "unknown-tag"),
        ]
        for params, expected in (
            (filters, [uuids["match"]]),
            (filters + [("exclude_tag", " APPROVED ")], []),
            (filters + [("include_tag", "unknown-tag")], []),
            ([keyword, ("include_tag", "planning, q2")], [uuids["match"]]),
        ):
            response = client.get("/api/assets", params=params, headers=headers)
            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == len(expected)
            assert [item["uuid"] for item in payload["items"]] == expected


def test_keyword_search_paginates_in_stable_creation_order():
    from fastapi.testclient import TestClient
    from keepstack.app import app
    from keepstack.config import config

    dates = (
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
    )
    assets = []
    for index, created_at in enumerate(dates):
        asset = ingest.ingest_stream(
            io.BytesIO(_img_bytes(color=(24 + index * 50, 138, 64))),
            f"pagesortfixture-{index}.jpg", run_ai=False,
        )
        with get_conn() as conn:
            conn.execute(
                "UPDATE assets SET created_at = ? WHERE id = ?", (created_at, asset["id"]),
            )
        assets.append(asset["uuid"])

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={
            "username": config.admin_user, "password": config.admin_password,
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        # 两个相同时间的素材分别落在两页，验证并列时也不会重漏。
        for sort, expected_order in (
            ("newest", [2, 3, 1, 0]),
            ("oldest", [0, 1, 3, 2]),
        ):
            for offset in (0, 2, 4):
                response = client.get("/api/assets", headers=headers, params={
                    "q": "pagesortfixture", "sort": sort, "limit": 2, "offset": offset,
                })
                assert response.status_code == 200
                data = response.json()
                assert data["total"] == 4
                assert [item["uuid"] for item in data["items"]] == [
                    assets[index] for index in expected_order[offset:offset + 2]
                ]

        for params in (
            {"limit": 0}, {"limit": 201}, {"offset": -1},
            {"offset": "abc"}, {"offset": 2**63},
        ):
            assert client.get("/api/assets", params=params, headers=headers).status_code == 422

        response = client.get("/api/assets", params={"q": ' "!?- '}, headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == 0
        assert response.json()["items"] == []

        response = client.get("/api/assets", params={"q": "pagesortfixture!?"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == 4


def test_csv_export_matches_all_filtered_pages():
    from fastapi.testclient import TestClient
    from keepstack.app import app
    from keepstack.config import config

    special_title = '港口, "夜景"'
    special_description = '第一行, "说明"\n第二行'
    special_tag = '素材, "港口"'
    standard_tags = ("approved", "campaign")
    samples = (
        ("first", "image", "active", True, (*standard_tags, special_tag)),
        ("second", "image", "active", True, standard_tags),
        ("third", "image", "active", True, standard_tags),
        ("internal", "image", "active", True, (*standard_tags, "internal")),
        ("outside", "image", "active", False, standard_tags),
        ("document", "document", "active", True, standard_tags),
        ("deleted", "image", "deleted", True, standard_tags),
        ("partial", "image", "active", True, ("approved",)),
        ("different-keyword", "image", "active", True, standard_tags),
    )
    assets = {}
    for index, (name, _, _, _, _) in enumerate(samples):
        prefix = "unrelated" if name == "different-keyword" else "csvexportfixture"
        assets[name] = ingest.ingest_stream(
            io.BytesIO(_img_bytes(color=(17 + 19 * index, 48, 192))),
            f"{prefix}-{name}.jpg", run_ai=False,
            title=special_title if name == "first" else None,
        )

    fixed_time = "2026-01-02T00:00:00+00:00"
    with get_conn() as conn:
        collection_id = conn.execute(
            "INSERT INTO collections (uuid, name, created_at) VALUES (?, ?, ?)",
            ("csv-export-test", "CSV export test", fixed_time),
        ).lastrowid
        for name, media_type, status, in_collection, tags in samples:
            asset_id = assets[name]["id"]
            conn.execute(
                "UPDATE assets SET media_type = ?, status = ?, created_at = ?, description = ? WHERE id = ?",
                (media_type, status, fixed_time, special_description if name == "first" else None, asset_id),
            )
            if in_collection:
                conn.execute(
                    "INSERT INTO collection_assets (collection_id, asset_id) VALUES (?, ?)",
                    (collection_id, asset_id),
                )
            for tag in tags:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                conn.execute(
                    "INSERT INTO asset_tags (asset_id, tag_id) SELECT ?, id FROM tags WHERE name = ?",
                    (asset_id, tag),
                )

    fields = ["uuid", "title", "description", "filename", "media_type", "tags", "created_at"]
    filters = [
        ("q", "csvexportfixture"), ("collection_id", str(collection_id)),
        ("media_type", "image"), ("include_tag", "APPROVED"),
        ("include_tag", "campaign"), ("exclude_tag", "internal"),
    ]
    with TestClient(app) as client:
        assert client.get("/api/assets/export.csv").status_code == 401
        login = client.post("/api/auth/login", json={
            "username": config.admin_user, "password": config.admin_password,
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        # 列表与导出都拒绝非 active 状态，避免相同参数查出不同范围。
        for endpoint in ("/api/assets", "/api/assets/export.csv"):
            response = client.get(endpoint, headers=headers, params=[*filters, ("status", "deleted")])
            assert response.status_code == 422
            assert response.json()["detail"][0]["loc"] == ["query", "status"]

        names = ["first", "second", "third"]
        for sort, ordered, status_params in (
            ("oldest", names, [("status", "active")]),
            ("newest", list(reversed(names)), []),
        ):
            params = [*filters, ("sort", sort), *status_params]
            listed_uuids = []
            for offset in (0, 2):
                response = client.get("/api/assets", headers=headers, params=[
                    *params, ("limit", "2"), ("offset", str(offset)),
                ])
                assert response.status_code == 200
                page = response.json()
                assert page["total"] == 3
                listed_uuids.extend(item["uuid"] for item in page["items"])

            # 即使请求带着第二页参数，CSV 也应包含全部匹配素材。
            response = client.get("/api/assets/export.csv", headers=headers, params=[
                *params, ("limit", "2"), ("offset", "2"),
            ])
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/csv")
            assert response.headers["content-disposition"] == 'attachment; filename="keepstack-assets.csv"'
            reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline=""))
            rows = list(reader)
            assert reader.fieldnames == fields
            assert [row["uuid"] for row in rows] == listed_uuids == [assets[name]["uuid"] for name in ordered]
            first = next(row for row in rows if row["uuid"] == assets["first"]["uuid"])
            assert first["title"] == special_title
            assert first["description"] == special_description
            assert first["filename"] == assets["first"]["filename"]
            assert first["media_type"] == "image"
            assert first["created_at"] == fixed_time
            assert json.loads(first["tags"]) == ["approved", "campaign", special_tag]

        empty = client.get("/api/assets/export.csv", headers=headers, params={"q": "missingcsvexportfixture"})
        assert empty.status_code == 200
        reader = csv.reader(io.StringIO(empty.content.decode("utf-8-sig"), newline=""))
        assert list(reader) == [fields]


def test_asset_edits_update_search_and_csv():
    from fastapi.testclient import TestClient
    from keepstack.app import app
    from keepstack.config import config

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={
            "username": config.admin_user, "password": config.admin_password,
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        upload = client.post(
            "/api/assets", headers=headers, data={"run_ai": "false"},
            files={"files": ("asset-edit-fixture.jpg", _img_bytes(color=(213, 157, 48)), "image/jpeg")},
        )
        assert upload.status_code == 200
        uuid = upload.json()["created"][0]["uuid"]
        asset_path = f"/api/assets/{uuid}"

        def assert_matches(expected_uuids, **filters):
            listing = client.get("/api/assets", headers=headers, params=filters)
            export = client.get("/api/assets/export.csv", headers=headers, params=filters)
            assert listing.status_code == export.status_code == 200
            data = listing.json()
            rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig"), newline="")))
            assert data["total"] == len(expected_uuids)
            assert [item["uuid"] for item in data["items"]] == expected_uuids
            assert [row["uuid"] for row in rows] == expected_uuids
            return rows

        # 搜索词只写在对应字段中，避免文件名或其他字段继续命中旧词。
        first_edit = client.patch(asset_path, headers=headers, json={
            "title": "harboronly", "description": "oldcaptiononly",
        })
        assert first_edit.status_code == 200
        assert_matches([uuid], q="harboronly")
        assert_matches([uuid], q="oldcaptiononly")

        next_edit = client.patch(asset_path, headers=headers, json={
            "title": "alpineonly", "description": "camelliaonly",
        })
        assert next_edit.status_code == 200
        assert_matches([], q="harboronly")
        assert_matches([], q="oldcaptiononly")
        for word in ("alpineonly", "camelliaonly"):
            row, = assert_matches([uuid], q=word)
            assert row["title"] == "alpineonly"
            assert row["description"] == "camelliaonly"

        added = client.post(asset_path + "/tags", headers=headers, json={"name": " REVIEWREADY "})
        assert added.status_code == 200
        # 标签变化要同时反映在全文搜索和精确标签筛选中。
        for filters in ({"q": "reviewready"}, {"include_tag": "reviewready"}):
            row, = assert_matches([uuid], **filters)
            assert "reviewready" in json.loads(row["tags"])

        removed = client.delete(asset_path + "/tags/reviewready", headers=headers)
        assert removed.status_code == 200
        assert_matches([], q="reviewready")
        assert_matches([], include_tag="reviewready")
        row, = assert_matches([uuid], q="alpineonly")
        assert "reviewready" not in json.loads(row["tags"])


def test_semantic_search_returns_ranked_results():
    ingest.ingest_stream(io.BytesIO(_img_bytes(color=(40, 150, 90), size=(330, 500))),
                         "mountain_trail.jpg", title="Mountain trail in the alpine forest")
    res = search.search(q="alpine forest trail", mode="semantic")
    assert res["mode"] == "semantic"
    assert res["total"] >= 1
    # scores should be present and sorted descending
    scores = [i.get("score", 0) for i in res["items"]]
    assert scores == sorted(scores, reverse=True)


def test_embedding_similarity_is_symmetric_and_bounded():
    v = ai.embed("a civic poster about public transit")
    assert 0.99 <= ai.cosine(v, v) <= 1.01
    other = ai.embed("a watershed environmental survey diagram")
    assert -1.01 <= ai.cosine(v, other) <= 1.01


def test_fixity_detects_intact_and_corruption():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(size=(120, 120))), "fixity.jpg")
    assert storage.verify_fixity(a["storage_key"], a["sha256"]) is True
    # corrupt the blob and confirm the check fails
    storage.blob_path(a["storage_key"]).write_bytes(b"tampered")
    assert storage.verify_fixity(a["storage_key"], a["sha256"]) is False


def test_gc_removes_blob_after_asset_delete():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(5, 20, 250), size=(111, 111))), "gc-delete.jpg")
    path = storage.blob_path(a["storage_key"])
    assert path.exists()

    conn = get_conn()
    conn.execute("UPDATE assets SET status = 'deleted' WHERE id = ?", (a["id"],))
    conn.commit()

    result = storage.collect_orphan_blobs()

    assert result["removed"] >= 1
    assert not path.exists()


def test_gc_keeps_active_asset_versions():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(250, 20, 5), size=(112, 112))), "gc-version.jpg")
    old_key = a["storage_key"]
    _new_sha, new_key, new_size = storage.store_bytes(b"replacement version", "txt")

    conn = get_conn()
    conn.execute(
        "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (a["id"], 1, a["sha256"], old_key, a["size"], "previous current", "2026-07-01T00:00:00Z"),
    )
    conn.execute(
        "UPDATE assets SET storage_key = ?, size = ? WHERE id = ?",
        (new_key, new_size, a["id"]),
    )
    conn.commit()

    result = storage.collect_orphan_blobs()

    assert old_key not in {b["key"] for b in result["blobs"]}
    assert storage.blob_path(old_key).exists()
    assert storage.blob_path(new_key).exists()


def test_dublin_core_mapping():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(120, 90, 210), size=(360, 360))),
                             "dc_test.jpg", title="DC Test")
    dc = standards.dublin_core(a)
    assert dc["title"] == "DC Test"
    assert dc["type"] == "image"
    assert dc["identifier"] == a["uuid"]


def test_xmp_fields_seed_catalog_and_search():
    a = ingest.ingest_stream(io.BytesIO(_xmp_img_bytes()), "xmp_harbor.jpg")

    assert a["title"] == "XMP Harbor Archive"
    assert a["description"] == "NeedleMuseum catalog caption"
    assert a["dc_creator"] == "Ada Archivist"
    assert a["dc_subject"] == "estuaries, tidal maps"
    assert a["dc_publisher"] == "NeedleMuseum Press"
    assert a["dc_date"] == "2026-07-02"
    assert a["rights_statement"] == "Creative Commons Test License"
    assert a["credit"] == "Archive Credit Line"

    dc = standards.dublin_core(a)
    assert dc["publisher"] == "NeedleMuseum Press"

    meta = get_conn().execute("SELECT xmp_json FROM asset_metadata WHERE asset_id = ?", (a["id"],)).fetchone()
    xmp = json.loads(meta["xmp_json"])
    assert xmp["fields"]["dc:publisher"] == "NeedleMuseum Press"

    res = search.search(q="NeedleMuseum")
    assert any(i["uuid"] == a["uuid"] for i in res["items"])


def test_iiif_info_advertises_level_2():
    info = standards.iiif_info({"uuid": "asset-1", "width": 100, "height": 60})
    assert info["profile"] == "level2"
    assert info["tiles"][0]["width"] == 512


def test_iiif_rendition_crops_and_rotates():
    sha, key, _ = storage.store_bytes(_split_img_bytes(), "jpg")

    data = thumbnails.iiif_rendition(sha, key, "image", "10,20,30,20", "full", "90")
    img = Image.open(io.BytesIO(data))

    assert img.size == (20, 30)


def test_iiif_rendition_supports_percent_region_and_best_fit_size():
    sha, key, _ = storage.store_bytes(_split_img_bytes(), "jpg")

    data = thumbnails.iiif_rendition(sha, key, "image", "pct:0,0,50,100", "!25,25", "0")
    img = Image.open(io.BytesIO(data))

    assert img.size == (21, 25)


def test_oai_identify_is_valid_xml():
    body, status = standards.oai_response({"verb": "Identify"})
    assert status == 200
    assert "<repositoryName>" in body and "OAI-PMH" in body


def test_login_throttle_locks_success_clears_and_expires(monkeypatch):
    from keepstack import auth
    from keepstack.config import config
    monkeypatch.setattr(config, "login_max_attempts", 3)
    monkeypatch.setattr(config, "login_max_attempts_per_ip", 100)  # isolate the pair bucket
    monkeypatch.setattr(config, "login_lockout_seconds", 60)
    auth.reset_login_throttle()
    user, ip = "admin", "203.0.113.9"

    # First two failures do not lock; the third trips it.
    assert auth.note_login_failure(user, ip, now=1000) is False
    assert auth.note_login_failure(user, ip, now=1000) is False
    assert auth.note_login_failure(user, ip, now=1000) is True
    assert auth.login_locked(user, ip, now=1000) > 0
    assert auth.login_locked(user, "198.51.100.4", now=1000) == 0  # a different IP is open

    # A successful login clears an ACTIVE lockout (not just an empty bucket).
    auth.note_login_success(user, ip)
    assert auth.login_locked(user, ip, now=1000) == 0

    # A fresh lockout later expires once the window passes.
    for _ in range(3):
        auth.note_login_failure(user, ip, now=2000)
    assert auth.login_locked(user, ip, now=2000) > 0
    assert auth.login_locked(user, ip, now=2000 + 61) == 0


def test_login_throttle_per_ip_catches_username_spray(monkeypatch):
    from keepstack import auth
    from keepstack.config import config
    monkeypatch.setattr(config, "login_max_attempts", 100)  # pair bucket effectively off
    monkeypatch.setattr(config, "login_max_attempts_per_ip", 4)
    monkeypatch.setattr(config, "login_lockout_seconds", 60)
    auth.reset_login_throttle()
    ip = "203.0.113.50"
    # Every username is distinct, so no pair bucket ever trips, but the per-IP
    # bucket does after four sprayed attempts, locking the whole source IP.
    for i in range(4):
        assert auth.login_locked(f"user{i}", ip, now=500) == 0
        auth.note_login_failure(f"user{i}", ip, now=500)
    assert auth.login_locked("someone-else", ip, now=500) > 0


def test_client_ip_ignores_spoofed_xff(monkeypatch):
    from keepstack.app import client_ip
    from keepstack.config import config

    class _Req:
        def __init__(self, peer, xff=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"X-Forwarded-For": xff} if xff else {}

    # No trusted proxies: X-Forwarded-For is ignored, the real peer wins.
    monkeypatch.setattr(config, "trusted_proxies", [])
    assert client_ip(_Req("198.51.100.7", xff="1.2.3.4")) == "198.51.100.7"

    # Behind a trusted proxy: the right-most non-proxy hop is the client.
    monkeypatch.setattr(config, "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(_Req("10.0.0.9", xff="9.9.9.9, 10.0.0.5")) == "9.9.9.9"
    # An untrusted direct peer still cannot spoof its way past.
    assert client_ip(_Req("203.0.113.1", xff="9.9.9.9")) == "203.0.113.1"


def test_iiif_manifest_shape():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(size=(300, 200))),
                             "manifest_test.jpg", title="Manifest Test")
    m = standards.iiif_manifest(a)
    assert m["type"] == "Manifest"
    assert m["@context"].endswith("presentation/3/context.json")
    assert m["label"]["none"] == ["Manifest Test"]

    canvas = m["items"][0]
    assert canvas["type"] == "Canvas"
    assert canvas["width"] == 300 and canvas["height"] == 200

    anno = canvas["items"][0]["items"][0]
    assert anno["motivation"] == "painting"
    assert anno["body"]["id"].endswith("/full/max/0/default.jpg")
    service = anno["body"]["service"][0]
    assert service["type"] == "ImageService3"
    assert service["id"].endswith(f"/iiif/3/{a['uuid']}")


@pytest.mark.skipif(not vision.ready(), reason="local vision model not installed")
def test_local_clip_cross_modal_similarity():
    from PIL import ImageDraw, ImageFont
    data_dir = Path(os.environ["KEEPSTACK_DATA_DIR"])

    page = Image.new("RGB", (448, 448), (250, 250, 250))
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        font = ImageFont.load_default(size=28)
    for i in range(12):
        draw.text((20, 20 + i * 36), "Quarterly report on estuary water quality.", font=font, fill=(20, 20, 20))
    page_path = data_dir / "clip_textpage.png"
    page.save(page_path)
    color_path = data_dir / "clip_colorfield.png"
    Image.new("RGB", (448, 448), (215, 30, 30)).save(color_path)

    tvec = vision.embed_text("a page of printed text")
    pvec = vision.embed_image(page_path)
    cvec = vision.embed_image(color_path)

    assert tvec and pvec and cvec
    assert ai.cosine(tvec, pvec) > ai.cosine(tvec, cvec)


@pytest.mark.skipif(not ocr.available(), reason="tesseract binary not installed")
def test_ocr_makes_image_text_searchable():
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (900, 240), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 96)
    except Exception:
        font = ImageFont.load_default(size=96)
    draw.text((40, 60), "QUILLFEATHER ARCHIVE", font=font, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    a = ingest.ingest_stream(io.BytesIO(buf.getvalue()), "ocr_sample.png")

    res = search.search(q="quillfeather")
    assert any(i["uuid"] == a["uuid"] for i in res["items"])


def test_version_endpoint_reports_name_and_version():
    from keepstack.app import version
    payload = version()
    assert payload["name"] == "keepstack"
    assert payload["version"]
