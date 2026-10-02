"""Independent chapters: complete coverage, bounded egress, durable isolated retry."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from localplaud.asr.base import Segment, Transcript
from localplaud.config import Settings
from localplaud.db.models import (
    FileStatus,
    PlaudFile,
    StageAttempt,
    StageName,
    StageStatus,
    Summary,
    TranscriptRevision,
)
from localplaud.db.models import (
    Transcript as StoredTranscript,
)
from localplaud.db.session import init_db, session_scope
from localplaud.worker import outline, pipeline


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    import localplaud.config as config
    import localplaud.db.session as db

    settings = Settings(
        _env_file=None,
        store={"database_url": f"sqlite:///{tmp_path / 'outline.db'}"},
        pipeline={"outline": False, "retry_base_seconds": 1},
    )
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="owned", status=FileStatus.done, duration_ms=3_700_000))
        session.flush()
        raw = StoredTranscript(
            file_id="owned",
            source="local",
            provider="owned-asr",
            model="turbo",
            segments=[{"text": "raw original", "start": 0, "end": 1}],
            text="raw original",
        )
        session.add(raw)
        session.flush()
        session.add(
            TranscriptRevision(
                file_id="owned",
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                segments=[
                    {"text": "Corrected introduction", "start": 0, "end": 40},
                    {"text": "Unique final decision", "start": 3600, "end": 3699},
                ],
                text="Corrected introduction\nUnique final decision",
            )
        )
        session.add(
            Summary(
                file_id="owned",
                source="local",
                template="owned-note",
                content_md="User notes survive",
            )
        )
    yield settings
    db.get_engine().dispose()


class Model:
    def __init__(self):
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        if prompt.startswith("These are"):
            return json.dumps({"chapters": [{"start_index": 0, "title": "Full recording"}]})
        ids = re.findall(r"\[(\d+) @", prompt)
        return json.dumps(
            {"chapters": [{"start_segment": int(ids[0]), "title": "Grounded chapter"}]}
        )


def test_llm_reads_every_corrected_segment_and_tail(monkeypatch):
    model = Model()
    monkeypatch.setattr(outline, "build_llm", lambda cfg: model)
    transcript = Transcript(
        segments=[
            Segment(text=f"unique-{i}-" + "台灣" * 40, start=i * 60, end=i * 60 + 50)
            for i in range(70)
        ]
    )
    result = outline.generate_outline(transcript, Settings(pipeline={"summary_chunk_chars": 200}))
    prompts = "\n".join(model.calls)
    for i in range(70):
        assert prompts.count(f"unique-{i}-") == 1
    assert result["detail"]["segments"] == 70
    assert result["chapters"][-1]["end_ms"] == 4_190_000
    assert outline.format_clock(3_660_000) == "1:01:00"
    outline.validate_chapters(result["chapters"], 4_190_000)


@pytest.mark.parametrize(
    "start,end", [(float("nan"), 2), (1, float("inf")), (-1, 2), (2, 1), (1, 1)]
)
def test_unplayable_timing_rejected_before_model(monkeypatch, start, end):
    monkeypatch.setattr(
        outline, "build_llm", lambda cfg: pytest.fail("invalid timing must not reach provider")
    )
    with pytest.raises(ValueError):
        outline.generate_outline(
            Transcript(segments=[Segment(text="speech", start=start, end=end)]), Settings()
        )


def test_out_of_recording_bounds_rejected(monkeypatch):
    monkeypatch.setattr(outline, "build_llm", lambda cfg: pytest.fail("must not contact provider"))
    with pytest.raises(ValueError, match="playable"):
        outline.generate_outline(
            Transcript(segments=[Segment(text="speech", start=0, end=100)]),
            Settings(),
            duration_ms=1000,
        )


def test_explicit_time_slices_has_no_model_and_exact_coverage(monkeypatch):
    monkeypatch.setattr(outline, "build_llm", lambda cfg: pytest.fail("time slices are local"))
    result = outline.time_slice_outline(
        Transcript(
            segments=[
                Segment(text="Beginning topic sentence", start=0, end=50),
                Segment(text="Last topic conclusion", start=3600, end=3700),
            ]
        ),
        duration_ms=3_800_000,
    )
    assert result["method"] == "time_slices"
    assert result["provider"] == "localplaud"
    assert result["chapters"][1]["start_ms"] == 3_600_000
    outline.validate_chapters(result["chapters"], 3_800_000)


def test_outline_only_persists_corrected_lineage_and_preserves_notes(isolated, monkeypatch):
    monkeypatch.setattr(outline, "build_llm", lambda cfg: pytest.fail("no model in time slices"))
    pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.status == FileStatus.done
        assert row.local_transcript.text == "raw original"
        assert row.summaries[0].content_md == "User notes survive"
        artifact = row.outlines[-1]
        assert artifact.input_transcript_revision == 1
        assert artifact.source == artifact.input_transcript_source == "local"
        assert artifact.chapters[-1]["title"] == "Unique final decision"
        assert artifact.resolved_profile_snapshot["policy"]["no_egress"] is True
        assert row.stage_runs[0].status == StageStatus.completed
        assert session.scalar(select(StageAttempt)).provider == "localplaud"
        first_id = artifact.id
    pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert [item.revision for item in row.outlines] == [1, 2]
        assert row.outlines[0].id == first_id


def test_edit_stales_outline_but_keeps_its_history(isolated):
    from localplaud.api.outline import outline_payload
    from localplaud.vocabulary import _mark_derived_stale

    pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert outline_payload(row)["stale"] is False
        _mark_derived_stale(session, "owned")
    with session_scope() as session:
        payload = outline_payload(session.get(PlaudFile, "owned"))
        assert payload["stale"] is True
        assert payload["chapters"]


def test_failed_attempt_retries_same_method_without_touching_other_stages(isolated, monkeypatch):
    original = outline.time_slice_outline
    monkeypatch.setattr(
        outline,
        "time_slice_outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    with pytest.raises(RuntimeError, match="temporary"):
        pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.status == FileStatus.done
        assert row.stage_runs[0].status == StageStatus.failed
        assert row.stage_runs[0].detail["outline_method"] == "time_slices"
        row.stage_runs[0].detail = dict(row.stage_runs[0].detail) | {
            "outline_next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        }
    monkeypatch.setattr(outline, "time_slice_outline", original)
    assert pipeline.process_pending(isolated) == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.status == FileStatus.done
        assert row.stage_runs[0].attempts == 2
        assert row.stage_runs[0].status == StageStatus.completed
        assert len(row.stage_runs) == 1
        assert row.summaries[0].content_md == "User notes survive"


def test_crash_after_queue_is_resumable(isolated):
    token = pipeline.claim_outline_rebuild("owned", method="time_slices")
    pipeline.release_processing_claim("owned", token)
    assert pipeline.process_pending(isolated) == 1
    with session_scope() as session:
        assert session.get(PlaudFile, "owned").outlines[-1].method == "time_slices"


def test_cloud_only_cannot_generate_outline(isolated):
    with session_scope() as session:
        session.add(PlaudFile(id="cloud", status=FileStatus.done))
        session.flush()
        session.add(
            StoredTranscript(
                file_id="cloud",
                provider="plaud",
                source="cloud",
                text="Plaud paid artifact",
                segments=[{"text": "cloud", "start": 0, "end": 1}],
            )
        )
    with pytest.raises(ValueError, match="local transcript"):
        pipeline.process_outline_only("cloud", isolated, method="time_slices")
    with session_scope() as session:
        assert session.get(PlaudFile, "cloud").outlines == []


def test_concurrent_canonical_edit_fences_publish(isolated, monkeypatch):
    original = outline.time_slice_outline

    def edit_during_call(*args, **kwargs):
        result = original(*args, **kwargs)
        with session_scope() as session:
            row = session.get(PlaudFile, "owned")
            session.add(
                TranscriptRevision(
                    file_id="owned",
                    revision=2,
                    source="local",
                    base_transcript_id=row.local_transcript.id,
                    text="new edit",
                    segments=[{"text": "new edit", "start": 0, "end": 1}],
                )
            )
        return result

    monkeypatch.setattr(outline, "time_slice_outline", edit_during_call)
    with pytest.raises(RuntimeError, match="transcript changed"):
        pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        assert session.get(PlaudFile, "owned").outlines == []


def test_cost_reservation_is_checked_before_provider(isolated, monkeypatch):
    from localplaud.providers.usage import CostPolicyError

    monkeypatch.setattr(
        outline, "projected_usage", lambda *args, **kwargs: {"input_chars": 100, "requests": 4}
    )
    monkeypatch.setattr(
        pipeline,
        "_cost_guard",
        lambda *args, **kwargs: (_ for _ in ()).throw(CostPolicyError("budget exhausted")),
    )
    monkeypatch.setattr(
        outline, "generate_outline", lambda *args, **kwargs: pytest.fail("provider egress denied")
    )
    with pytest.raises(CostPolicyError):
        pipeline.process_outline_only("owned", isolated, method="llm")
    with session_scope() as session:
        assert session.get(PlaudFile, "owned").stage_runs[0].status == StageStatus.failed


def test_outline_table_added_idempotently_to_existing_database(isolated):
    from localplaud.db.session import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE outlines"))
    init_db()
    init_db()
    pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.local_transcript.text == "raw original"
        assert row.transcript_revisions[0].text.startswith("Corrected")
        assert len(row.outlines) == 1


def test_api_queue_payload_feedback_and_restart(isolated, monkeypatch):
    from fastapi.testclient import TestClient

    from localplaud.api import outline as api
    from localplaud.api.app import app

    monkeypatch.setattr(api, "_start_worker", lambda *args, **kwargs: None)
    with TestClient(app) as client:
        assert client.get("/api/files/owned/outline").json()["status"] == "missing"
        response = client.post(
            "/api/files/owned/outline/regenerate", json={"method": "time_slices"}
        )
        assert response.status_code == 202
        assert client.get("/api/files/owned/outline").json()["status"] == "pending"
        assert (
            client.post("/api/files/owned/outline/regenerate", json={"method": "llm"}).status_code
            == 409
        )
        with session_scope() as session:
            token = session.get(PlaudFile, "owned").processing_token
        pipeline.release_processing_claim("owned", token)
        assert pipeline.process_pending(isolated) == 1
        payload = client.get("/api/files/owned/outline").json()
        assert payload["status"] == "completed"
        assert payload["provenance"]["transcript_revision"] == 1
        assert payload["generation"]["selection"]["provider_type"]
        assert "configuration" not in payload["generation"]["selection"]
        assert (
            client.post("/api/files/owned/outline/feedback", json={"value": "up"}).json()[
                "feedback"
            ]
            == "up"
        )


def test_no_egress_profile_blocks_cloud_outline_but_allows_time_slices(isolated, monkeypatch):
    from localplaud.db.models import ExecutionProfile, ProviderConnection

    with session_scope() as session:
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.privacy_policy = "local-only"
        profile.no_egress = True
        for connection in session.scalars(select(ProviderConnection)):
            connection.execution_target = "cloud"
            connection.data_egress = True
    monkeypatch.setattr(
        outline, "build_llm", lambda cfg: pytest.fail("no-egress must block before provider build")
    )
    with pytest.raises(ValueError, match="no-egress"):
        pipeline.process_outline_only("owned", isolated, method="llm")
    pipeline.process_outline_only("owned", isolated, method="time_slices")
    with session_scope() as session:
        assert session.get(PlaudFile, "owned").outlines[-1].method == "time_slices"


def test_invalid_llm_output_is_failed_not_silently_time_sliced(isolated, monkeypatch):
    class InvalidModel:
        def complete(self, *args, **kwargs):
            return '{"chapters": [{"start_segment": 99999, "title": "Invented"}]}'

    monkeypatch.setattr(outline, "build_llm", lambda cfg: InvalidModel())
    from localplaud.llm.base import LLMOutputInvalid

    with pytest.raises(LLMOutputInvalid):
        pipeline.process_outline_only("owned", isolated, method="llm")
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.outlines == []
        assert row.status == FileStatus.done
        assert row.stage_runs[0].status == StageStatus.failed


def test_automatic_outline_resumes_without_regenerating_valid_artifact(isolated, monkeypatch):
    isolated.pipeline.outline = True
    isolated.pipeline.outline_method = "time_slices"
    isolated.pipeline.summarize = isolated.pipeline.mind_map = isolated.pipeline.index = (
        isolated.pipeline.polish
    ) = False
    calls = []
    original = outline.time_slice_outline

    def generate(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(outline, "time_slice_outline", generate)
    pipeline.process_derived_artifacts("owned", isolated)
    pipeline.process_derived_artifacts("owned", isolated)
    assert len(calls) == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert len(row.outlines) == 1
        assert row.status == FileStatus.done
        assert row.outlines[0].input_transcript_revision == 1


@pytest.mark.parametrize("ceiling,allowed", [(0.1, False), (1.0, True)])
def test_real_shared_cost_ledger_reserves_outline_attempt(isolated, monkeypatch, ceiling, allowed):
    from localplaud.db.models import (
        ExecutionProfile,
        ModelCatalogEntry,
        ProviderConnection,
        ProviderCostReservation,
    )
    from localplaud.providers.usage import CostPolicyError

    with session_scope() as session:
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.cost_ceiling = ceiling
        for connection in session.scalars(select(ProviderConnection)):
            connection.execution_target = "cloud"
            connection.data_egress = True
        for model in session.scalars(select(ModelCatalogEntry)):
            model.capabilities = dict(model.capabilities) | {
                "metadata": {"pricing": {"per_request_usd": 0.1}}
            }
    model = Model()
    monkeypatch.setattr(outline, "build_llm", lambda cfg: model)
    if allowed:
        pipeline.process_outline_only("owned", isolated, method="llm")
        assert len(model.calls) == 1
        with session_scope() as session:
            attempt = session.scalar(
                select(StageAttempt).where(StageAttempt.stage == StageName.outline)
            )
            assert attempt.status == StageStatus.completed
            assert attempt.estimated_cost_usd == pytest.approx(0.1)
            assert attempt.usage["requests"] == 1
            assert list(session.scalars(select(ProviderCostReservation))) == []
    else:
        with pytest.raises(CostPolicyError, match="ceiling"):
            pipeline.process_outline_only("owned", isolated, method="llm")
        assert model.calls == []


def test_rebuild_queue_and_failure_keep_previous_outline_visible(isolated, monkeypatch):
    from localplaud.api.outline import outline_payload

    pipeline.process_outline_only("owned", isolated, method="time_slices")
    token = pipeline.claim_outline_rebuild("owned", method="time_slices")
    with session_scope() as session:
        payload = outline_payload(session.get(PlaudFile, "owned"))
        assert payload["status"] == "pending"
        assert payload["chapters"]
    monkeypatch.setattr(
        outline,
        "time_slice_outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed rebuild")),
    )
    with pytest.raises(RuntimeError, match="failed rebuild"):
        pipeline.process_outline_only("owned", isolated, claim_token=token, method="time_slices")
    with session_scope() as session:
        payload = outline_payload(session.get(PlaudFile, "owned"))
        assert payload["status"] == "failed"
        assert payload["chapters"]
        assert payload["provenance"]["revision"] == 1


@pytest.mark.parametrize("first_failure", [False, True])
def test_outline_retry_survives_crash_after_stage_started(isolated, monkeypatch, first_failure):
    from localplaud.poller.poll import reset_inflight

    original = outline.time_slice_outline
    monkeypatch.setattr(
        outline, "time_slice_outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("first failure")),
    )
    if first_failure:
        with pytest.raises(RuntimeError):
            pipeline.process_outline_only("owned", isolated, method="time_slices")
        with session_scope() as session:
            row = session.get(PlaudFile, "owned")
            row.stage_runs[0].detail = dict(row.stage_runs[0].detail) | {
                "outline_next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            }

    class Interrupted(BaseException):
        pass

    monkeypatch.setattr(
        outline, "time_slice_outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(Interrupted()),
    )
    with pytest.raises(Interrupted):
        pipeline.process_outline_only("owned", isolated, method="time_slices")
    reset_inflight()
    monkeypatch.setattr(outline, "time_slice_outline", original)
    assert pipeline.process_pending(isolated) == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.outlines[-1].method == "time_slices"
        assert row.status == FileStatus.done
        assert len(row.stage_runs) == 1
        assert row.summaries[0].content_md == "User notes survive"


@pytest.mark.parametrize("failure,fallback", [(False, False), (True, False), (True, True)])
def test_remote_outline_isolated_provenance_and_explicit_fallback(isolated, monkeypatch, failure, fallback):
    from types import SimpleNamespace

    from localplaud.remote.client import RemoteWorkerError

    snapshot = {"policy": {"no_egress": False}, "stages": {"mind_map": {
        "connection": "worker:test", "provider_type": "localplaud-worker", "model": "model",
        "execution_target": "remote_worker", "data_egress": True,
    }}}
    if fallback:
        snapshot["policy"]["fallback_policy"] = {"stages": {"mind_map": [{
            "connection": "llm:ollama", "provider_type": "ollama", "model": "local-model",
            "execution_target": "local", "data_egress": False,
        }]}}
    monkeypatch.setattr(pipeline, "resolve_recording_profile", lambda *a, **kw: SimpleNamespace(to_dict=lambda: snapshot))
    calls = []

    def remote(file_id, candidate, stage, inputs, **kwargs):
        calls.append(stage)
        assert stage == "outline"
        assert inputs[0].value["segments"][-1]["text"] == "Unique final decision"
        if failure:
            raise RemoteWorkerError("outline capability unavailable", retryable=True)
        return {"chapters": [{"start_ms": 0, "end_ms": 3_700_000, "title": "Whole"}],
                "method": "llm", "prompt_version": outline.PROMPT_VERSION, "model": "model"}

    monkeypatch.setattr(pipeline, "_run_remote_stage", remote)
    monkeypatch.setattr(outline, "build_llm", lambda cfg: Model())
    if failure and not fallback:
        with pytest.raises(RemoteWorkerError, match="capability unavailable"):
            pipeline.process_outline_only("owned", isolated, method="llm")
    else:
        pipeline.process_outline_only("owned", isolated, method="llm")
    assert calls == ["outline"]
    with session_scope() as session:
        row = session.get(PlaudFile, "owned")
        assert row.status == FileStatus.done
        assert row.summaries[0].content_md == "User notes survive"
        assert all(run.stage == StageName.outline for run in row.stage_runs)
        if failure and not fallback:
            assert row.stage_runs[0].status == StageStatus.failed and not row.outlines
        else:
            artifact = row.outlines[-1]
            assert artifact.input_transcript_revision == 1
            assert artifact.chapters[-1]["end_ms"] == 3_700_000
            assert artifact.resolved_profile_snapshot["stages"]["mind_map"]["execution_target"] == (
                "local" if fallback else "remote_worker")
            if not fallback:
                assert artifact.provider == "remote-worker"
