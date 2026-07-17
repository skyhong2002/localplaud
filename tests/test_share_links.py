"""Public read-only recording share links."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest


@pytest.fixture
def clients(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'share.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "share-test-password")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "share-test-session-secret")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_COOKIE_SECURE", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)

    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    authenticated = TestClient(app, base_url="http://localplaud.test")
    login = authenticated.post(
        "/login", data={"password": "share-test-password"}, follow_redirects=False
    )
    assert login.status_code == 303
    public = TestClient(app, base_url="http://localplaud.test")
    return authenticated, public, tmp_path


def _seed_recording(tmp_path, *, file_id="recording", trash=False):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        Speaker,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    audio = tmp_path / f"{file_id}.mp3"
    audio.write_bytes(bytes(range(100)))
    with session_scope() as session:
        recording = PlaudFile(
            id=file_id,
            filename="Cloud title",
            local_title="Local weekly sync",
            status=FileStatus.done,
            duration_ms=65_000,
            start_time_ms=1_783_582_737_000,
            audio_path=str(audio),
            is_trash=trash,
        )
        session.add(recording)
        session.flush()
        transcript = Transcript(
            file_id=file_id,
            provider="faster-whisper",
            source="local",
            text="raw local transcript",
            segments=[
                {
                    "text": "raw local transcript",
                    "start": 1.0,
                    "end": 3.0,
                    "speaker": "SPEAKER_00",
                }
            ],
            has_speakers=True,
        )
        session.add(transcript)
        session.flush()
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=transcript.id,
                revision=1,
                source="local",
                text="corrected canonical line",
                segments=[
                    {
                        "text": "corrected canonical line",
                        "start": 1.0,
                        "end": 3.0,
                        "speaker": "SPEAKER_00",
                    }
                ],
                has_speakers=True,
            )
        )
        session.add(Speaker(file_id=file_id, key="SPEAKER_00", display_name="Alice Chen"))
        session.add(
            Summary(
                file_id=file_id,
                template="meeting",
                title="Meeting notes",
                content_md="Local generated note",
                source="local",
                input_transcript_source="local",
            )
        )
        session.add(
            Summary(
                file_id=file_id,
                template="cloud-import",
                title="Plaud note",
                content_md="paid cloud note must stay private",
                source="cloud",
            )
        )
        session.add(
            Summary(
                file_id=file_id,
                template="cloud-derived-local",
                title="Mislabelled note",
                content_md="cloud-derived local note must stay hidden",
                source="local",
                input_transcript_source="cloud",
            )
        )
        session.add(
            Summary(
                file_id=file_id,
                template="mind_map",
                title="Old map",
                content_md="stale map must stay hidden",
                source="local",
                input_transcript_source="local",
            )
        )
        session.add(
            StageRun(
                file_id=file_id,
                stage=StageName.mind_map,
                status=StageStatus.completed,
                detail={"stale": True},
            )
        )
    return audio.read_bytes()


def _create_link(client, file_id="recording") -> dict:
    response = client.post(f"/api/files/{file_id}/share-link")
    assert response.status_code == 200
    return response.json()


def _share_path(link: dict) -> str:
    return urlsplit(link["url"]).path


def test_create_and_get_active_share_link(clients):
    authenticated, public, tmp_path = clients
    _seed_recording(tmp_path)

    created = _create_link(authenticated)
    token = _share_path(created).removeprefix("/share/")
    assert created["active"] is True
    assert len(token) >= 40
    assert created["url"] == f"http://localplaud.test/share/{token}"
    assert created["created_at"] and created["last_used_at"] is None

    fetched = authenticated.get("/api/files/recording/share-link")
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert _create_link(authenticated) == created
    assert public.get("/api/files/recording/share-link").status_code == 401


def test_public_page_is_local_read_only_and_noindex(clients):
    authenticated, public, tmp_path = clients
    _seed_recording(tmp_path)
    path = _share_path(_create_link(authenticated))

    page = public.get(path)
    assert page.status_code == 200
    assert page.headers["X-Robots-Tag"] == "noindex"
    assert '<meta name="robots" content="noindex">' in page.text
    assert "Local weekly sync" in page.text
    assert "corrected canonical line" in page.text
    assert "raw local transcript" not in page.text
    assert "Alice Chen" in page.text
    assert "Local generated note" in page.text
    assert "paid cloud note must stay private" not in page.text
    assert "cloud-derived local note must stay hidden" not in page.text
    assert "stale map must stay hidden" not in page.text
    assert "Ask localplaud" not in page.text


def test_public_audio_supports_ranges_and_dies_on_revoke(clients):
    authenticated, public, tmp_path = clients
    audio = _seed_recording(tmp_path)
    path = _share_path(_create_link(authenticated))

    ranged = public.get(f"{path}/audio", headers={"Range": "bytes=10-19"})
    assert ranged.status_code == 206
    assert ranged.content == audio[10:20]
    assert ranged.headers["Content-Range"] == "bytes 10-19/100"
    assert ranged.headers["X-Robots-Tag"] == "noindex"

    assert authenticated.delete("/api/files/recording/share-link").status_code == 200
    assert authenticated.delete("/api/files/recording/share-link").status_code == 200
    assert public.get(f"{path}/audio").status_code == 404


def test_revoke_and_recreate_never_revives_old_token(clients):
    authenticated, public, tmp_path = clients
    _seed_recording(tmp_path)
    first = _create_link(authenticated)
    first_path = _share_path(first)

    assert authenticated.delete("/api/files/recording/share-link").json() == {"ok": True}
    assert public.get(first_path).status_code == 404
    assert public.get(f"{first_path}/audio").status_code == 404
    assert authenticated.get("/api/files/recording/share-link").json()["active"] is False

    second = _create_link(authenticated)
    assert second["url"] != first["url"]
    assert public.get(_share_path(second)).status_code == 200
    assert public.get(first_path).status_code == 404


def test_unknown_and_trash_recordings_are_not_shareable(clients):
    authenticated, public, tmp_path = clients
    _seed_recording(tmp_path, file_id="trashed", trash=True)

    unknown = public.get("/share/not-a-real-token")
    assert unknown.status_code == 404
    assert unknown.headers["X-Robots-Tag"] == "noindex"
    assert public.get("/share/not-a-real-token/audio").status_code == 404
    assert authenticated.post("/api/files/missing/share-link").status_code == 404
    assert authenticated.post("/api/files/trashed/share-link").status_code == 404

    _seed_recording(tmp_path, file_id="later-trashed")
    path = _share_path(_create_link(authenticated, "later-trashed"))
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.get(PlaudFile, "later-trashed").is_trash = True
    assert public.get(path).status_code == 404
    assert public.get(f"{path}/audio").status_code == 404


def test_cloud_only_transcript_never_appears_on_public_share(clients):
    authenticated, public, tmp_path = clients
    from localplaud.db.models import FileStatus, PlaudFile, Summary, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording = PlaudFile(id="cloud-only", filename="Imported", status=FileStatus.done)
        recording.transcripts.append(
            Transcript(
                provider="plaud",
                source="cloud",
                text="paid cloud transcript must stay private",
                segments=[
                    {
                        "text": "paid cloud transcript must stay private",
                        "start": 0.0,
                        "end": 1.0,
                    }
                ],
            )
        )
        recording.summaries.append(
            Summary(
                template="plaud",
                source="cloud",
                content_md="paid cloud summary must stay private",
            )
        )
        session.add(recording)

    page = public.get(_share_path(_create_link(authenticated, "cloud-only")))
    assert page.status_code == 200
    assert "Transcript is not available yet." in page.text
    assert "paid cloud transcript must stay private" not in page.text
    assert "paid cloud summary must stay private" not in page.text


def test_page_view_updates_last_used_at(clients):
    authenticated, public, tmp_path = clients
    _seed_recording(tmp_path)
    created = _create_link(authenticated)
    assert created["last_used_at"] is None

    assert public.get(_share_path(created)).status_code == 200
    used = authenticated.get("/api/files/recording/share-link").json()["last_used_at"]
    assert used is not None
