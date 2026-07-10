"""Deterministic, policy-checked execution-profile resolution."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..db.models import ExecutionProfile, StageName
from .catalog import provider_egress, specs_for_route


class ProfileResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ProfileLayer:
    source: str
    profile: ExecutionProfile | None


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _merge_policy(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(override, dict):
        raise ProfileResolutionError("profile policy must be an object")
    result = _merge(base, override)
    result["no_egress"] = bool(base.get("no_egress") or override.get("no_egress"))
    left, right = base.get("allowed_providers"), override.get("allowed_providers")
    if left is not None and right is not None:
        result["allowed_providers"] = sorted(set(left) & set(right))
    elif left is not None:
        result["allowed_providers"] = list(left)
    for key in ("cost_ceiling", "timeout_ceiling_seconds"):
        values = [value for value in (base.get(key), override.get(key)) if value is not None]
        if values:
            try:
                numbers = [float(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise ProfileResolutionError(f"{key} must be numeric") from exc
            if not all(math.isfinite(value) and value >= 0 for value in numbers):
                raise ProfileResolutionError(f"{key} must be finite and non-negative")
            result[key] = min(numbers)
    return result


def _validate_retry_and_timeout(policy: dict[str, Any], routes: list[dict]) -> None:
    retry = policy.get("retry", {})
    if not isinstance(retry, dict):
        raise ProfileResolutionError("retry policy must be an object")
    retry_values: dict[str, int] = {}
    for key in ("max_attempts", "base_seconds", "max_seconds"):
        if key not in retry:
            continue
        value = retry[key]
        if isinstance(value, bool):
            raise ProfileResolutionError(f"retry.{key} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ProfileResolutionError(f"retry.{key} must be a positive integer") from exc
        if parsed < 1 or parsed != value:
            raise ProfileResolutionError(f"retry.{key} must be a positive integer")
        retry_values[key] = parsed
    if retry_values.get("max_seconds", math.inf) < retry_values.get("base_seconds", 0):
        raise ProfileResolutionError("retry.max_seconds must be >= retry.base_seconds")

    ceiling = policy.get("timeout_ceiling_seconds")
    for index, route in enumerate(routes):
        label = "route" if index == 0 else f"fallback {index}"
        timeout = route.get("timeout_seconds", (route.get("options") or {}).get("timeout_seconds"))
        if timeout is None and ceiling is not None:
            route["timeout_seconds"] = ceiling
            continue
        if timeout is None:
            continue
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ProfileResolutionError(f"{label} timeout_seconds must be numeric") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ProfileResolutionError(f"{label} timeout_seconds must be finite and positive")
        if ceiling is not None and timeout_value > float(ceiling):
            raise ProfileResolutionError(f"{label} exceeds the timeout ceiling")
        route["timeout_seconds"] = timeout_value


def resolve_profile(
    stage: StageName | str,
    *,
    system_default: ExecutionProfile,
    folder_rule: ExecutionProfile | None = None,
    template_default: ExecutionProfile | None = None,
    recording_override: ExecutionProfile | None = None,
) -> dict[str, Any]:
    """Resolve system -> folder/rule -> template -> recording, then validate policy."""
    stage_name = stage.value if isinstance(stage, StageName) else stage
    layers = [
        ProfileLayer("system_default", system_default),
        ProfileLayer("folder_rule", folder_rule),
        ProfileLayer("template_default", template_default),
        ProfileLayer("recording_override", recording_override),
    ]
    resolved: dict[str, Any] = {"stage": stage_name, "route": {}, "policy": {}}
    applied: list[dict[str, Any]] = []
    for layer in layers:
        profile = layer.profile
        if profile is None:
            continue
        if not profile.enabled:
            raise ProfileResolutionError(f"profile {profile.name!r} is disabled")
        if not isinstance(profile.stages, dict):
            raise ProfileResolutionError(f"profile {profile.name!r} stages must be an object")
        route = (profile.stages or {}).get(stage_name)
        if route is not None:
            if not isinstance(route, dict):
                raise ProfileResolutionError(f"route for stage {stage_name!r} must be an object")
            resolved["route"] = _merge(resolved["route"], route)
        resolved["policy"] = _merge_policy(resolved["policy"], profile.policy or {})
        applied.append({"source": layer.source, "id": profile.id, "name": profile.name,
                        "version": profile.version})

    route = resolved["route"]
    if not isinstance(route, dict):
        raise ProfileResolutionError(f"route for stage {stage_name!r} must be an object")
    if not route or not route.get("provider"):
        raise ProfileResolutionError(f"no provider configured for stage {stage_name!r}")
    policy = resolved["policy"]
    provider = route["provider"]
    fallbacks = route.get("fallback", [])
    if not isinstance(fallbacks, list) or any(
        not isinstance(item, dict) or not item.get("provider") for item in fallbacks
    ):
        raise ProfileResolutionError("fallback must be a list of provider route objects")
    allowed = policy.get("allowed_providers")
    if allowed is not None and provider not in allowed:
        raise ProfileResolutionError(f"provider {provider!r} is not allowed by profile policy")
    if allowed is not None:
        disallowed = [item["provider"] for item in fallbacks if item["provider"] not in allowed]
        if disallowed:
            raise ProfileResolutionError(f"fallback providers are not allowed: {disallowed}")
    authoritative_egress = provider_egress(provider, stage_name).value
    route["egress"] = authoritative_egress
    if policy.get("no_egress") and authoritative_egress != "local_only" \
            and not route.get("connection"):
        raise ProfileResolutionError(f"stage {stage_name!r} would violate no-egress policy")
    for fallback in fallbacks:
        fallback["egress"] = provider_egress(fallback["provider"], stage_name).value
    if policy.get("no_egress") and any(
        x["egress"] != "local_only" and not x.get("connection") for x in fallbacks
    ):
        raise ProfileResolutionError("fallback would cross the no-egress boundary")
    _validate_retry_and_timeout(policy, [route, *fallbacks])
    ceiling = policy.get("cost_ceiling")
    quality = policy.get("quality_floor", {})
    if isinstance(quality, dict) and stage_name in quality:
        quality = quality[stage_name]
    if quality and not isinstance(quality, dict):
        raise ProfileResolutionError("quality_floor must be an object")
    for candidate_index, candidate in enumerate([route, *fallbacks]):
        label = "route" if candidate_index == 0 else f"fallback {candidate_index}"
        estimated = candidate.get("estimated_cost")
        if estimated is not None:
            try:
                estimated_value = float(estimated)
            except (TypeError, ValueError) as exc:
                raise ProfileResolutionError(f"{label} estimated_cost must be numeric") from exc
            if not math.isfinite(estimated_value) or estimated_value < 0:
                raise ProfileResolutionError(
                    f"{label} estimated_cost must be finite and non-negative"
                )
            candidate["estimated_cost"] = estimated_value
            if ceiling is not None and estimated_value > float(ceiling):
                raise ProfileResolutionError(f"{label} exceeds the configured cost ceiling")
        if quality:
            specs = specs_for_route(candidate["provider"], stage_name)
            supported = {
                "timestamps": any(spec.timestamps for spec in specs),
                "word_timestamps": any(spec.word_timestamps for spec in specs),
                "speaker_output": any(spec.speaker_output for spec in specs),
            }
            for requirement, required in quality.items():
                if required is True and not supported.get(requirement):
                    raise ProfileResolutionError(
                        f"{label} provider {candidate['provider']!r} does not meet "
                        f"quality floor {requirement!r}"
                    )

    resolved["layers"] = applied
    resolved["profile_version"] = 1
    return resolved
