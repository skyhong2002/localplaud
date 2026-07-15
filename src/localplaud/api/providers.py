"""Provider, model, and execution-profile management APIs."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..db.session import session_scope
from ..providers.service import (
    check_connection_health,
    check_model_health,
    clear_recording_override,
    create_profile_version,
    delete_connection,
    delete_model,
    delete_profile,
    install_hardware_recommendation,
    list_connections,
    list_models,
    list_profiles,
    preview_resolution,
    resolve_recording_profile,
    save_connection,
    save_model,
    select_folder_profile,
    select_recording_override,
)
from ..remote.registry import check_worker, delete_worker, list_workers, save_worker

router = APIRouter(prefix="/api/providers", tags=["providers"])


class PreviewRequest(BaseModel):
    rule_or_folder: dict | None = None
    template: dict | None = None
    recording: dict | None = None


class OverrideRequest(BaseModel):
    profile_id: int
    stages: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)


class ProfileSelectionRequest(BaseModel):
    profile_id: int | None = None


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    provider_type: str
    execution_target: str = "local"
    data_egress: bool = False
    secret_ref: str | None = None
    config: dict = Field(default_factory=dict)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: int
    model_key: str
    display_name: str | None = None
    capabilities: dict = Field(default_factory=dict)
    enabled: bool = True


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    version: int | None = None
    is_system_default: bool = False
    privacy_policy: str = "allow-egress"
    no_egress: bool = False
    cost_ceiling: float | None = None
    fallback_policy: dict = Field(default_factory=dict)
    stages: dict = Field(default_factory=dict)


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    base_url: str
    token_env: str = "LOCALPLAUD_WORKER_TOKEN"
    timeout: float = 120
    job_timeout: float = 3600
    enabled: bool = True


class RecommendationInstallRequest(BaseModel):
    make_default: bool = False


@router.get("/hardware-recommendations")
def hardware_profile_recommendations():
    from ..providers.hardware import hardware_recommendations

    return hardware_recommendations()


@router.post("/hardware-recommendations/{recommendation_key}/install", status_code=201)
def install_recommendation(
    recommendation_key: str, body: RecommendationInstallRequest
):
    with session_scope() as session:
        try:
            return install_hardware_recommendation(
                session, recommendation_key, make_default=body.make_default
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/connections")
def connections():
    with session_scope() as session:
        return {"connections": list_connections(session)}


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionRequest):
    with session_scope() as session:
        try:
            return save_connection(session, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/connections/{connection_id}")
def update_connection(connection_id: int, body: ConnectionRequest):
    with session_scope() as session:
        try:
            return save_connection(session, body.model_dump(), connection_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/connections/{connection_id}/health")
def connection_health(connection_id: int):
    with session_scope() as session:
        try:
            return check_connection_health(session, connection_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/connections/{connection_id}", status_code=204)
def remove_connection(connection_id: int):
    with session_scope() as session:
        try:
            delete_connection(session, connection_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/models")
def models():
    with session_scope() as session:
        return {"models": list_models(session)}


@router.post("/models", status_code=201)
def create_model(body: ModelRequest):
    with session_scope() as session:
        try:
            return save_model(session, body.model_dump(exclude_none=True))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/models/{model_id}")
def update_model(model_id: int, body: ModelRequest):
    with session_scope() as session:
        try:
            return save_model(session, body.model_dump(exclude_none=True), model_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/models/{model_id}/health")
def model_health(model_id: int):
    with session_scope() as session:
        try:
            return check_model_health(session, model_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/models/{model_id}", status_code=204)
def remove_model(model_id: int):
    with session_scope() as session:
        try:
            delete_model(session, model_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/profiles")
def profiles():
    with session_scope() as session:
        return {"profiles": list_profiles(session)}


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileRequest):
    with session_scope() as session:
        try:
            return create_profile_version(session, body.model_dump(exclude_none=True))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/profiles/{profile_id}", status_code=204)
def remove_profile(profile_id: int):
    with session_scope() as session:
        try:
            delete_profile(session, profile_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/resolve")
def resolve(body: PreviewRequest):
    with session_scope() as session:
        try:
            result = preview_resolution(
                session, body.rule_or_folder, body.template, body.recording
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"resolved": result.to_dict()}


@router.put("/recordings/{file_id}/override")
def recording_override(file_id: str, body: OverrideRequest):
    with session_scope() as session:
        try:
            return select_recording_override(
                session, file_id, body.profile_id, stages=body.stages, policy=body.policy
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/recordings/{file_id}/override")
def remove_recording_override(file_id: str):
    with session_scope() as session:
        try:
            return clear_recording_override(session, file_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/recordings/{file_id}/resolution")
def recording_resolution(file_id: str, template_key: str | None = None):
    with session_scope() as session:
        from ..db.models import PlaudFile

        if session.get(PlaudFile, file_id) is None:
            raise HTTPException(status_code=404, detail="recording not found")
        try:
            return {"resolved": resolve_recording_profile(
                session, file_id, template_key=template_key
            ).to_dict()}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/folders/{folder_id}/profile")
def folder_profile(folder_id: int, body: ProfileSelectionRequest):
    with session_scope() as session:
        try:
            return select_folder_profile(session, folder_id, body.profile_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workers")
def workers():
    with session_scope() as session:
        return {"workers": list_workers(session)}


@router.post("/workers", status_code=201)
def create_worker(body: WorkerRequest):
    with session_scope() as session:
        return save_worker(session, body.model_dump())


@router.put("/workers/{worker_id}")
def update_worker(worker_id: int, body: WorkerRequest):
    with session_scope() as session:
        try:
            return save_worker(session, body.model_dump(), worker_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workers/{worker_id}/health")
def worker_health(worker_id: int):
    with session_scope() as session:
        try:
            return check_worker(session, worker_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/workers/{worker_id}", status_code=204)
def remove_worker(worker_id: int):
    with session_scope() as session:
        try:
            delete_worker(session, worker_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
