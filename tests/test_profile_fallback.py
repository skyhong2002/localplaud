"""Explicit profile fallbacks are validated, visible, and stage scoped."""

from __future__ import annotations

from sqlalchemy import select


def _reset(monkeypatch, tmp_path, *, diarize=False, summarize=True, mind_map=True, index=True):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'fallback.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", str(diarize).lower())
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__SUMMARIZE", str(summarize).lower())
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", str(mind_map).lower())
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", str(index).lower())
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()
    return settings


def _capability(*stages, target="cloud", egress=True):
    from localplaud.providers.contracts import Capability, StageCapabilities

    return Capability(
        execution_target=target,
        data_egress=egress,
        stages=tuple(StageCapabilities(stage=stage) for stage in stages),
    ).model_dump(mode="json")


def _add_candidate(session, key, provider, model_key, stages, *, target="cloud"):
    from localplaud.db.models import ModelCatalogEntry, ProviderConnection

    connection = ProviderConnection(
        key=key,
        name=key,
        provider_type=provider,
        execution_target=target,
        data_egress=target != "local",
    )
    session.add(connection)
    session.flush()
    session.add(
        ModelCatalogEntry(
            connection_id=connection.id,
            model_key=model_key,
            display_name=model_key,
            capabilities=_capability(*stages, target=target, egress=target != "local"),
        )
    )


def _seed_file(session, tmp_path, file_id="fallback"):
    from localplaud.db.models import FileStatus, PlaudFile

    audio = tmp_path / f"{file_id}.wav"
    audio.write_bytes(b"RIFF")
    session.add(
        PlaudFile(
            id=file_id,
            filename="Fallback",
            status=FileStatus.downloaded,
            audio_path=str(audio),
            duration_ms=10_000,
        )
    )


def test_resolver_validates_fallback_capability_egress_and_duplicates():
    import pytest

    from localplaud.providers.contracts import ProviderStage
    from localplaud.providers.resolver import ResolutionError, resolve_profile

    capabilities = {
        ("local", "primary"): _capability(ProviderStage.transcribe, target="local", egress=False),
        ("cloud", "fallback"): _capability(ProviderStage.transcribe),
    }
    layer = {
        "key": "profile",
        "policy": {
            "no_egress": False,
            "fallback_policy": {
                "stages": {
                    "transcribe": [{"connection": "cloud", "model": "fallback", "options": {}}]
                }
            },
        },
        "stages": {"transcribe": {"connection": "local", "model": "primary", "options": {}}},
    }
    resolved = resolve_profile([layer], capabilities).to_dict()
    fallback = resolved["policy"]["fallback_policy"]["stages"]["transcribe"][0]
    assert fallback["execution_target"] == "cloud" and fallback["data_egress"] is True
    layer["policy"]["no_egress"] = True
    with pytest.raises(ResolutionError, match="no-egress"):
        resolve_profile([layer], capabilities)
    layer["policy"]["no_egress"] = False
    layer["policy"]["fallback_policy"]["stages"]["transcribe"][0] = {
        "connection": "local",
        "model": "primary",
    }
    with pytest.raises(ResolutionError, match="duplicate fallback"):
        resolve_profile([layer], capabilities)


def test_pipeline_uses_explicit_fallbacks_for_derived_stages(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import AsrUnavailable, Segment, Transcript
    from localplaud.db.models import ExecutionProfile, StageAttempt, StageName
    from localplaud.db.session import session_scope
    from localplaud.embeddings.base import EmbeddingUnavailable
    from localplaud.llm.base import LLMUnavailable
    from localplaud.providers.contracts import ProviderStage

    with session_scope() as session:
        _seed_file(session, tmp_path)
        _add_candidate(
            session,
            "asr:openai",
            "openai",
            "whisper-1",
            (ProviderStage.transcribe, ProviderStage.align),
        )
        _add_candidate(
            session,
            "llm:openai",
            "openai",
            "gpt-test",
            (ProviderStage.summarize, ProviderStage.mind_map),
        )
        _add_candidate(
            session,
            "embeddings:openai",
            "openai",
            "embed-test",
            (ProviderStage.embed,),
        )
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.privacy_policy = "allow-egress"
        profile.fallback_policy = {
            "stages": {
                "transcribe": [{"connection": "asr:openai", "model": "whisper-1", "options": {}}],
                "summarize": [{"connection": "llm:openai", "model": "gpt-test", "options": {}}],
                "mind_map": [{"connection": "llm:openai", "model": "gpt-test", "options": {}}],
                "embed": [
                    {
                        "connection": "embeddings:openai",
                        "model": "embed-test",
                        "options": {},
                    }
                ],
            }
        }

    def fake_asr(_wav, candidate_settings):
        assert candidate_settings.asr.fallback == []
        if candidate_settings.asr.provider != "openai":
            raise AsrUnavailable("primary ASR unavailable")
        return Transcript(
            segments=[Segment(text="fallback transcript", start=0, end=10)],
            duration=10,
            language="en",
            provider="openai",
            model="whisper-1",
            has_speakers=True,
        )

    def fake_summary(transcript, candidate_settings):
        if candidate_settings.llm.provider != "openai":
            raise LLMUnavailable("primary LLM unavailable")
        return {
            "title": "Fallback",
            "content_md": "# Fallback\n\nReady",
            "provider": "openai",
            "model": "gpt-test",
            "template": candidate_settings.pipeline.summary_template,
        }

    def fake_mind_map(transcript, candidate_settings, summary_md=None):
        if candidate_settings.llm.provider != "openai":
            raise LLMUnavailable("primary mind map unavailable")
        return {
            "template": "mind_map",
            "content_md": "# Fallback\n- Ready",
            "provider": "openai",
            "model": "gpt-test",
            "detail": {},
        }

    def fake_embed(chunks, candidate_settings):
        if candidate_settings.embeddings.provider != "openai":
            raise EmbeddingUnavailable("primary embeddings unavailable")
        return [b"\x00\x00\x80?" for _ in chunks], "embed-test", 1

    monkeypatch.setattr(pipeline.transcribe, "run_asr", fake_asr)
    monkeypatch.setattr(pipeline.summarize, "summarize", fake_summary)
    monkeypatch.setattr(pipeline.mindmap, "generate_mind_map", fake_mind_map)
    monkeypatch.setattr(pipeline.index, "embed_chunks", fake_embed)
    pipeline.process_file("fallback", settings)

    with session_scope() as session:
        attempts = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == "fallback")
                .order_by(StageAttempt.id)
            )
        )
        by_stage = {}
        for attempt in attempts:
            by_stage.setdefault(attempt.stage, []).append(attempt)
        for stage in (
            StageName.transcribe,
            StageName.summarize,
            StageName.mind_map,
            StageName.index,
        ):
            assert [item.status.value for item in by_stage[stage]] == ["failed", "completed"]
            assert by_stage[stage][1].resolved_profile_snapshot["fallback"]["index"] == 1
        assert by_stage[StageName.transcribe][1].provider == "openai"
        assert by_stage[StageName.index][1].provider == "openai"

    from fastapi.testclient import TestClient

    from localplaud.api.app import app

    client = TestClient(app)
    page = client.get("/file/fallback")
    assert page.status_code == 200
    assert "Used fallback #1 · after" in page.text
    assert "fallback 1" in page.text
    usage = client.get("/api/files/fallback/usage").json()
    assert any(
        item["fallback"] and item["fallback"]["index"] == 1
        for item in usage["attempts"]
    )
    settings_page = client.get("/settings")
    assert "Fallback order" in settings_page.text
    assert 'name="fallback-transcribe"' in settings_page.text


def test_hard_asr_error_does_not_fallback(monkeypatch, tmp_path):
    import pytest

    settings = _reset(monkeypatch, tmp_path, summarize=False, mind_map=False, index=False)
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import AsrError
    from localplaud.db.models import ExecutionProfile, StageAttempt
    from localplaud.db.session import session_scope
    from localplaud.providers.contracts import ProviderStage

    with session_scope() as session:
        _seed_file(session, tmp_path, "hard")
        _add_candidate(
            session,
            "asr:openai",
            "openai",
            "whisper-1",
            (ProviderStage.transcribe, ProviderStage.align),
        )
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.fallback_policy = {
            "stages": {
                "transcribe": [{"connection": "asr:openai", "model": "whisper-1", "options": {}}]
            }
        }
    calls = 0

    def hard_error(*_args):
        nonlocal calls
        calls += 1
        raise AsrError("corrupt audio")

    monkeypatch.setattr(pipeline.transcribe, "run_asr", hard_error)
    with pytest.raises(AsrError, match="corrupt audio"):
        pipeline.process_file("hard", settings)
    assert calls == 1
    with session_scope() as session:
        attempts = list(session.scalars(select(StageAttempt).where(StageAttempt.file_id == "hard")))
        assert len(attempts) == 1 and attempts[0].status == "failed"


def test_diarization_can_fallback_to_remote_worker(monkeypatch, tmp_path):
    settings = _reset(
        monkeypatch,
        tmp_path,
        diarize=True,
        summarize=False,
        mind_map=False,
        index=False,
    )
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import ExecutionProfile, StageAttempt, StageName
    from localplaud.db.session import session_scope
    from localplaud.providers.contracts import ProviderStage
    from localplaud.worker.diarize import DiarizationUnavailable

    with session_scope() as session:
        _seed_file(session, tmp_path, "diarize")
        _add_candidate(
            session,
            "worker:gpu",
            "localplaud-worker",
            "pyannote-worker",
            (ProviderStage.diarize,),
            target="remote_worker",
        )
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.fallback_policy = {
            "stages": {
                "diarize": [
                    {
                        "connection": "worker:gpu",
                        "model": "pyannote-worker",
                        "options": {},
                    }
                ]
            }
        }
    monkeypatch.setattr(
        pipeline.transcribe,
        "run_asr",
        lambda *_args: Transcript(
            segments=[Segment(text="two speakers", start=0, end=10)],
            duration=10,
            provider="fake",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "diarize",
        lambda *_args: (_ for _ in ()).throw(DiarizationUnavailable("local unavailable")),
    )
    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda _file, _snapshot, stage, _inputs, **_kwargs: (
            {
                "segments": [
                    {
                        "text": "two speakers",
                        "start": 0,
                        "end": 10,
                        "speaker": "SPEAKER_00",
                    }
                ],
                "has_speakers": True,
            }
            if stage == "diarize"
            else {}
        ),
    )
    pipeline.process_file("diarize", settings)
    with session_scope() as session:
        attempts = list(
            session.scalars(
                select(StageAttempt)
                .where(
                    StageAttempt.file_id == "diarize",
                    StageAttempt.stage == StageName.diarize,
                )
                .order_by(StageAttempt.attempt)
            )
        )
        assert [item.status.value for item in attempts] == ["failed", "completed"]
        assert attempts[1].provider == "remote-worker"
