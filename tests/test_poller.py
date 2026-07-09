"""Tests for poller recovery + change detection (review fixes #1, #5)."""

from __future__ import annotations


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_reset_inflight_recovers_crashed_rows(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import reset_inflight

    init_db()
    with session_scope() as s:
        s.add(PlaudFile(id="dl", status=FileStatus.downloading))
        s.add(PlaudFile(id="pr", status=FileStatus.processing, audio_path="/x"))
        s.add(PlaudFile(id="ok", status=FileStatus.done))

    n = reset_inflight()
    assert n == 2
    with session_scope() as s:
        assert s.get(PlaudFile, "dl").status == FileStatus.discovered
        assert s.get(PlaudFile, "pr").status == FileStatus.downloaded
        assert s.get(PlaudFile, "ok").status == FileStatus.done  # untouched


def test_sync_redownloads_when_md5_changes(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()
    with session_scope() as s:
        s.add(
            PlaudFile(
                id="f1", status=FileStatus.done, audio_path="/old.mp3",
                file_md5="OLD", version=1, version_ms=1,
            )
        )

    class FakeClient:
        def iter_files(self, include_trash=False):
            # same version, but the audio md5 changed -> must force re-download
            yield PlaudFileDTO(id="f1", file_md5="NEW", version=1, version_ms=1)

    new, changed = sync_file_list(FakeClient(), get_settings())
    assert (new, changed) == (0, 1)
    with session_scope() as s:
        assert s.get(PlaudFile, "f1").status == FileStatus.discovered  # not "downloaded"
