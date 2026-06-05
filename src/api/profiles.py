"""Profile CRUD API (v2 monitoring schema).

Scope is carried as query/body fields (process / model / eqpId) rather than a
path token because scopes contain wildcards (``*``) and EARS procs (``@system``)
that are path-hostile. Every write is optimistic-locked on ``governance.version``
(409 on a stale version, 404 on a missing target) and, before persisting, the
*composed effective* profile (this overlay folded with its parent scopes) is
re-validated for reference integrity (422 with field-path errors) — an overlay
alone may legitimately reference a measure defined in a parent scope.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from src.api import deps
from src.db.models import (
    Governance,
    Measure,
    MongoUnavailableError,
    MonitorProfile,
    NotifyChannel,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileVersionConflictError,
    Rule,
    Scope,
    fold_profiles,
    validate_effective,
)

router = APIRouter(prefix="/profiles")


def get_profile_repo(request: Request) -> Any:
    """Resolve the ProfileRepository from ``app.state.repos`` (set by lifespan)."""
    return deps._state(request, "repos").profile_repo


# ----------------------------------------------------------------------
# Request bodies
# ----------------------------------------------------------------------
class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ProfileCreate(_Base):
    scope: Scope
    enabled: bool = True
    measures: list[Measure] = []
    rules: list[Rule] = []
    notify: dict[str, NotifyChannel] = {}


class ProfileReplace(ProfileCreate):
    expected_version: int


class MeasureWrite(_Base):
    scope: Scope
    expected_version: int
    measure: Measure


class RuleWrite(_Base):
    scope: Scope
    expected_version: int
    rule: Rule


class NotifyWrite(_Base):
    scope: Scope
    expected_version: int
    channel: NotifyChannel


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _rank(scope: Scope) -> int:
    if scope.process == "*":
        return 0
    if scope.eqp_model == "*":
        return 1
    if scope.eqp_id == "*":
        return 2
    return 3


def _map_repo_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProfileVersionConflictError):
        return HTTPException(status_code=409, detail="version conflict (stale governance.version)")
    if isinstance(exc, ProfileAlreadyExistsError):
        return HTTPException(status_code=409, detail="profile already exists for scope")
    if isinstance(exc, ProfileNotFoundError):
        return HTTPException(status_code=404, detail="profile not found for scope")
    if isinstance(exc, MongoUnavailableError):
        return HTTPException(status_code=503, detail="database unavailable")
    return HTTPException(status_code=500, detail="internal error")


async def _validate_composed(repo: Any, overlay: MonitorProfile) -> None:
    """Validate the effective profile = overlay folded with its parent scopes.
    Raises HTTP 422 with field-path errors on failure (503 if Mongo is down)."""
    try:
        docs = await repo.collect_scope_docs(
            overlay.scope.process, overlay.scope.eqp_model, overlay.scope.eqp_id
        )
    except MongoUnavailableError as e:
        raise _map_repo_error(e) from e
    parents = [d for d in docs if d.scope != overlay.scope]
    composed = sorted([*parents, overlay], key=lambda p: _rank(p.scope))
    errors = validate_effective(fold_profiles(composed, overlay.scope))
    if errors:
        raise HTTPException(status_code=422, detail=errors)


async def _load_overlay(repo: Any, scope: Scope) -> MonitorProfile:
    try:
        overlay = await repo.find_by_scope(scope)
    except MongoUnavailableError as e:
        raise _map_repo_error(e) from e
    if overlay is None:
        raise HTTPException(status_code=404, detail="profile not found for scope")
    return overlay


async def _commit(repo: Any, overlay: MonitorProfile, expected_version: int) -> dict[str, Any]:
    """Validate then optimistic-locked replace; returns the new version."""
    await _validate_composed(repo, overlay)
    try:
        new_version = await repo.replace_with_version(overlay, expected_version)
    except (ProfileVersionConflictError, ProfileNotFoundError, MongoUnavailableError) as e:
        raise _map_repo_error(e) from e
    return {"scope": overlay.scope.to_mongo(), "version": new_version}


# ----------------------------------------------------------------------
# Read
# ----------------------------------------------------------------------
@router.get("")
async def get_overlay(
    process: str,
    model: str = "*",
    eqpId: str = "*",  # noqa: N803 (matches the EARS/JSON field name)
    repo: Any = Depends(get_profile_repo),
) -> dict[str, Any]:
    """Return the single overlay document stored at this exact scope (404 if none)."""
    overlay = await _load_overlay(repo, Scope(process=process, eqp_model=model, eqp_id=eqpId))
    return overlay.to_mongo()


@router.get("/effective")
async def get_effective(
    process: str,
    model: str = "*",
    eqpId: str = "*",  # noqa: N803
    withProvenance: bool = Query(False),  # noqa: N803
    repo: Any = Depends(get_profile_repo),
) -> dict[str, Any]:
    """Return the cascade-folded effective profile for an equipment (404 if no
    scope matches). With ``withProvenance=1`` each item carries the scope label
    that contributed it (inherited/overridden/local)."""
    try:
        docs = await repo.collect_scope_docs(process, model, eqpId)
    except MongoUnavailableError as e:
        raise _map_repo_error(e) from e
    if not docs:
        raise HTTPException(status_code=404, detail="no profile matches scope")
    target = Scope(process=process, eqp_model=model, eqp_id=eqpId)
    effective = fold_profiles(docs, target)
    out = effective.to_mongo()
    if withProvenance:
        out["provenance"] = _provenance(docs)
    return out


def _provenance(docs: list[MonitorProfile]) -> dict[str, dict[str, str]]:
    prov: dict[str, dict[str, str]] = {"measures": {}, "rules": {}, "notify": {}}
    for d in sorted(docs, key=lambda p: _rank(p.scope)):  # base→specific; later wins
        label = f"{d.scope.process}/{d.scope.eqp_model}/{d.scope.eqp_id}"
        for m in d.measures:
            prov["measures"][m.id] = label
        for r in d.rules:
            prov["rules"][r.id] = label
        for name in d.notify:
            prov["notify"][name] = label
    return prov


# ----------------------------------------------------------------------
# Whole-overlay create / replace / delete
# ----------------------------------------------------------------------
@router.post("", status_code=201)
async def create_overlay(body: ProfileCreate, repo: Any = Depends(get_profile_repo)) -> dict[str, Any]:
    overlay = MonitorProfile(
        scope=body.scope, enabled=body.enabled, measures=body.measures,
        rules=body.rules, notify=body.notify,
    )
    await _validate_composed(repo, overlay)
    try:
        profile_id = await repo.create(overlay)
    except (ProfileAlreadyExistsError, MongoUnavailableError) as e:
        raise _map_repo_error(e) from e
    return {"id": profile_id, "scope": overlay.scope.to_mongo(), "version": 1}


@router.put("")
async def replace_overlay(body: ProfileReplace, repo: Any = Depends(get_profile_repo)) -> dict[str, Any]:
    overlay = MonitorProfile(
        scope=body.scope, enabled=body.enabled, measures=body.measures,
        rules=body.rules, notify=body.notify,
        governance=Governance(version=body.expected_version),
    )
    return await _commit(repo, overlay, body.expected_version)


@router.delete("")
async def delete_overlay(
    process: str,
    version: int,
    model: str = "*",
    eqpId: str = "*",  # noqa: N803
    repo: Any = Depends(get_profile_repo),
) -> dict[str, Any]:
    scope = Scope(process=process, eqp_model=model, eqp_id=eqpId)
    try:
        await repo.delete_by_scope(scope, expected_version=version)
    except (ProfileVersionConflictError, ProfileNotFoundError, MongoUnavailableError) as e:
        raise _map_repo_error(e) from e
    return {"deleted": scope.to_mongo()}


# ----------------------------------------------------------------------
# Item-level CRUD (read-modify-write the overlay, validate, version-locked)
# ----------------------------------------------------------------------
@router.post("/measures")
async def add_measure(body: MeasureWrite, repo: Any = Depends(get_profile_repo)) -> dict[str, Any]:
    overlay = await _load_overlay(repo, body.scope)
    if any(m.id == body.measure.id for m in overlay.measures):
        raise HTTPException(status_code=409, detail=f"measure '{body.measure.id}' already exists")
    overlay.measures.append(body.measure)
    return await _commit(repo, overlay, body.expected_version)


@router.patch("/measures/{measure_id}")
async def update_measure(
    measure_id: str, body: MeasureWrite, repo: Any = Depends(get_profile_repo)
) -> dict[str, Any]:
    overlay = await _load_overlay(repo, body.scope)
    idx = next((i for i, m in enumerate(overlay.measures) if m.id == measure_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"measure '{measure_id}' not found")
    overlay.measures[idx] = body.measure
    return await _commit(repo, overlay, body.expected_version)


@router.delete("/measures/{measure_id}")
async def delete_measure(
    measure_id: str,
    process: str,
    version: int,
    model: str = "*",
    eqpId: str = "*",  # noqa: N803
    repo: Any = Depends(get_profile_repo),
) -> dict[str, Any]:
    scope = Scope(process=process, eqp_model=model, eqp_id=eqpId)
    overlay = await _load_overlay(repo, scope)
    kept = [m for m in overlay.measures if m.id != measure_id]
    if len(kept) == len(overlay.measures):
        raise HTTPException(status_code=404, detail=f"measure '{measure_id}' not found")
    overlay.measures = kept
    return await _commit(repo, overlay, version)


@router.post("/rules")
async def add_rule(body: RuleWrite, repo: Any = Depends(get_profile_repo)) -> dict[str, Any]:
    overlay = await _load_overlay(repo, body.scope)
    if any(r.id == body.rule.id for r in overlay.rules):
        raise HTTPException(status_code=409, detail=f"rule '{body.rule.id}' already exists")
    overlay.rules.append(body.rule)
    return await _commit(repo, overlay, body.expected_version)


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str, body: RuleWrite, repo: Any = Depends(get_profile_repo)
) -> dict[str, Any]:
    overlay = await _load_overlay(repo, body.scope)
    idx = next((i for i, r in enumerate(overlay.rules) if r.id == rule_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"rule '{rule_id}' not found")
    overlay.rules[idx] = body.rule
    return await _commit(repo, overlay, body.expected_version)


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    process: str,
    version: int,
    model: str = "*",
    eqpId: str = "*",  # noqa: N803
    repo: Any = Depends(get_profile_repo),
) -> dict[str, Any]:
    scope = Scope(process=process, eqp_model=model, eqp_id=eqpId)
    overlay = await _load_overlay(repo, scope)
    kept = [r for r in overlay.rules if r.id != rule_id]
    if len(kept) == len(overlay.rules):
        raise HTTPException(status_code=404, detail=f"rule '{rule_id}' not found")
    overlay.rules = kept
    return await _commit(repo, overlay, version)


@router.patch("/notify/{name}")
async def patch_notify(
    name: str, body: NotifyWrite, repo: Any = Depends(get_profile_repo)
) -> dict[str, Any]:
    overlay = await _load_overlay(repo, body.scope)
    overlay.notify[name] = body.channel
    return await _commit(repo, overlay, body.expected_version)
