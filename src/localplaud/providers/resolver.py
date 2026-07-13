"""Pure deterministic execution-profile resolution and validation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .contracts import Capability, ProviderStage


class ResolutionError(ValueError):
    pass


def _merge(base: dict[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class ResolvedProfile:
    snapshot: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.snapshot)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def resolve_profile(
    layers: Sequence[Mapping[str, Any] | None],
    capabilities: Mapping[tuple[str, str], Capability | Mapping[str, Any]],
    connection_details: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResolvedProfile:
    """Merge system -> rule/folder -> template -> recording layers."""
    merged: dict[str, Any] = {"policy": {}, "stages": {}}
    applied: list[str] = []
    provenance: list[dict[str, Any]] = []
    for layer in layers:
        if not layer:
            continue
        merged["policy"] = _merge(merged["policy"], layer.get("policy"))
        merged["stages"] = _merge(merged["stages"], layer.get("stages"))
        if layer.get("key"):
            applied.append(str(layer["key"]))
            provenance.append(
                dict(layer.get("provenance") or {"kind": "partial", "key": layer["key"]})
            )

    no_egress = bool(merged["policy"].get("no_egress"))

    def validate_selection(stage: ProviderStage, selection: dict, label: str) -> None:
        key = (str(selection.get("connection")), str(selection.get("model")))
        raw = capabilities.get(key)
        if raw is None:
            raise ResolutionError(f"unknown provider/model for {label}: {key[0]}/{key[1]}")
        capability = raw if isinstance(raw, Capability) else Capability.model_validate(raw)
        if capability.for_stage(stage) is None:
            raise ResolutionError(f"model {key[1]} does not support stage {stage.value}")
        if no_egress and capability.data_egress:
            raise ResolutionError(f"no-egress profile cannot use {key[0]}/{key[1]}")
        details = (connection_details or {}).get(key[0])
        if details:
            selection.update(details)
        selection["execution_target"] = capability.execution_target
        selection["data_egress"] = capability.data_egress

    for stage_name, selection in merged["stages"].items():
        try:
            stage = ProviderStage(stage_name)
        except ValueError as exc:
            raise ResolutionError(f"unknown stage: {stage_name}") from exc
        validate_selection(stage, selection, stage.value)

    fallback = merged["policy"].get("fallback_policy") or {}
    fallback_stages = fallback.get("stages") or {}
    if not isinstance(fallback_stages, Mapping):
        raise ResolutionError("fallback_policy.stages must be an object")
    for stage_name, candidates in fallback_stages.items():
        try:
            stage = ProviderStage(stage_name)
        except ValueError as exc:
            raise ResolutionError(f"unknown fallback stage: {stage_name}") from exc
        if stage_name not in merged["stages"]:
            raise ResolutionError(f"fallback stage has no primary selection: {stage_name}")
        if not isinstance(candidates, list | tuple) or len(candidates) > 5:
            raise ResolutionError(f"fallbacks for {stage_name} must be a list of at most 5")
        seen = {
            (
                merged["stages"][stage_name].get("connection"),
                merged["stages"][stage_name].get("model"),
            )
        }
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ResolutionError(f"fallback {stage_name}[{index}] must be an object")
            key = (candidate.get("connection"), candidate.get("model"))
            if key in seen:
                raise ResolutionError(f"duplicate fallback for {stage_name}: {key[0]}/{key[1]}")
            seen.add(key)
            validate_selection(stage, candidate, f"{stage_name} fallback {index + 1}")

    merged["schema"] = "localplaud-resolved-profile/v2"
    merged["layers"] = applied
    merged["layer_provenance"] = provenance
    return ResolvedProfile(_freeze(merged))
