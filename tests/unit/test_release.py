"""cicd/release.py logic with a fake S3 (no AWS). Runs in the pipeline's Test stage."""
from __future__ import annotations
import io
import json

import pytest
from botocore.exceptions import ClientError

from cicd import release

A, B, C = "a" * 40, "b" * 40, "c" * 40


def err(code):
    return ClientError({"Error": {"Code": code}}, "op")


class FakeS3:
    def __init__(self, objects=None, conflicts=0):
        self.objects = dict(objects or {})
        self.etag = 0
        self.conflicts = conflicts

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise err("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": f'"{self.etag}"'}

    def put_object(self, Bucket, Key, Body, ContentType, IfMatch=None, IfNoneMatch=None):
        if self.conflicts:
            self.conflicts -= 1
            raise err("PreconditionFailed")
        if IfNoneMatch == "*" and Key in self.objects or IfMatch and IfMatch != f'"{self.etag}"':
            raise err("PreconditionFailed")
        self.objects[Key], self.etag = Body, self.etag + 1

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise err("404")


@pytest.fixture
def fake(monkeypatch):
    s3 = FakeS3({f"releases/{r}/release.json": b"{}" for r in (A, B, "p5-baseline")})
    monkeypatch.setattr(release, "s3", lambda: s3)
    monkeypatch.setattr(release.time, "sleep", lambda s: None)
    return s3


def history(s3):
    return json.loads(s3.objects[release.HISTORY_KEY])


def test_record_creates_then_appends(fake):
    release.record(A, "deployed")
    release.record(A, "verified")
    (entry,) = history(fake)["releases"]
    assert entry["status"] == "verified" and [e["status"] for e in entry["events"]] == ["deployed", "verified"]


def test_history_write_retries_on_etag_conflict(fake):
    fake.conflicts = 2
    release.record(A, "deployed")
    assert history(fake)["releases"][0]["release_id"] == A


def test_known_good_is_newest_verified_existing_and_not_excluded(fake):
    for rid in ("p5-baseline", A, B, C):          # C has no files in S3
        release.record(rid, "verified")
    release.record(B, "failed")
    assert release.known_good(exclude=None) == A      # C missing, B failed
    assert release.known_good(exclude=A) == "p5-baseline"


def test_known_good_none_when_nothing_verified(fake):
    release.record(A, "failed")
    assert release.known_good(exclude=None) is None


@pytest.mark.parametrize("bad", ["", "abc", "../etc", A[:39], A.upper(), "p5-baseline/x"])
def test_release_id_validation(bad):
    with pytest.raises(ValueError):
        release.check_release_id(bad)


def test_switch_release_refuses_incomplete_release(fake):
    with pytest.raises(ValueError, match="missing or incomplete"):
        release.switch_release(C)


def test_build_stamps_every_page_and_writes_release_json(tmp_path):
    out = release.build(A, out=tmp_path / "site")
    rel = json.loads((out / "release.json").read_text())
    assert rel["release_id"] == rel["commit"] == A
    for page in out.rglob("*.html"):
        assert f"Release <code>{A[:7]}</code>" in page.read_text()
