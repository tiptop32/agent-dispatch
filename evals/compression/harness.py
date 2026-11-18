"""Evaluate frozen synthetic contexts through production selector/lifecycle boundaries.

Offline never opens a network transport or invokes a coding executor. Live semantic
uses current Jev settings. Paired coding uses the existing local daemon; labels and
the exact independent checker never enter its request or fixture repository.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from statistics import median
from typing import Annotated, Any, Literal
from unittest.mock import patch

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agent_dispatch.config import Settings, load_settings
from agent_dispatch.dispatch import context_compression as selector
from agent_dispatch.dispatch.context_blocks import ContextSpan, assemble_context, split_context
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.dispatch.followup import FollowupError
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import (
    FINAL_STATUSES,
    Availability,
    DispatchRequest,
    ExecutionResult,
    FollowupRequest,
    RouteDecision,
    TaskRecord,
)
from agent_dispatch.routing.jev import JevRouter
from agent_dispatch.routing.preflight import external_router_allowed
from agent_dispatch.telemetry.storage import Storage

CORPUS = Path(__file__).with_name("cases.jsonl")
CORPUS_SHA256 = (  # Frozen public corpus digest, not a credential.
    "f61fc184f2071540b881baf1a643ffb72f7e4ed673080b20f32976827ee1a66d"  # pragma: allowlist secret
)
TRUSTED_ROOT = Path("/tmp/plan-dispatch/jev-context-compression")
MOCK_URL = "https://compression-eval.invalid/decisions"
Text = Annotated[str, Field(min_length=1)]
NonnegativeInt = Annotated[int, Field(ge=0)]
TransportResponse = dict[str, Any] | Literal["block_until_cancel", "block_until_deadline"]
CompressionCode = Literal[
    "compression_unsupported",
    "compression_privacy",
    "compression_limit",
    "compression_provenance",
    "compression_response",
    "compression_http",
    "compression_transport",
    "compression_deadline",
    "compression_cancelled",
    "compression_budget",
    "compression_budget_unknown",
    "compression_storage",
    "compression_interrupted",
]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CaseRequest(StrictRecord):
    task: Text
    context: str
    files: list[Text]
    constraints: list[Text]
    success_criteria: list[Text]
    context_mode: Literal["prompt", "prompt+summary", "full"]
    context_compression: Literal["jev"]
    prod: bool = False
    kind: Literal["task", "review"] = "task"


class Alternative(StrictRecord):
    block_ids: Annotated[list[Text], Field(min_length=1)]
    fragments: Annotated[list[Text], Field(min_length=1)]


class Fact(StrictRecord):
    id: Text
    description: Text
    alternatives: Annotated[list[Alternative], Field(min_length=1)]


class RecordedStep(StrictRecord):
    candidate_id: Text
    retained_ids_before: list[Text]
    response: dict[str, Any]


class Fixture(StrictRecord):
    files: dict[Text, str]
    editable_paths: Annotated[list[Text], Field(min_length=1)]
    runtime: Literal["python>=3.11"]
    setup_argv: list[str]


class Assertion(StrictRecord):
    language: Literal["python"]
    source: Text
    argv: list[Text]
    timeout_seconds: Literal[5]
    visibility: Literal["withheld_from_executor"]
    success_exit_code: Literal[0]


class BaseCase(StrictRecord):
    schema_version: Literal[1]
    id: Text
    kind: str
    split: str
    tags: list[Text]
    request: CaseRequest


class SemanticCase(BaseCase):
    kind: Literal["semantic"]
    split: Literal["calibration", "holdout"]
    required_facts: list[Fact]
    recorded_steps: list[RecordedStep]
    expected_kept_ids: list[Text]
    expected_error: None


class CodingCase(SemanticCase):
    kind: Literal["coding"]
    fixture: Fixture
    assertion: Assertion


class Ancestor(StrictRecord):
    edge: Literal["parent_task_id", "escalated_from", "followup_of"]
    prod: bool = False
    perimeter: Literal["prod", "answered", "unknown"] | None = None


class DaemonSuffix(StrictRecord):
    origin: Literal["daemon_handoff", "daemon_review"]
    text: Text
    source_task_id: Text


class ControlSetup(StrictRecord):
    trusted_cwd: bool | None = None
    persisted_ancestor: Ancestor | None = None
    reload_storage: bool = False
    corporate_perimeter: bool = False
    trusted_root: str | None = None
    cwd_symlink: str | None = None
    symlink_target: str | None = None
    allow_unknown_projects: bool = False
    router: Literal["static", "jev"] = "static"
    routing_response_perimeter: Literal["answered"] | None = None
    responses: list[TransportResponse] = Field(default_factory=list)
    cancel_on_step_started: NonnegativeInt | None = None
    deadline_seconds: Annotated[float, Field(gt=0)] | None = None
    daily_cost_limit_usd: Annotated[float, Field(gt=0)] | None = None
    max_chain_cost_usd: Annotated[float, Field(gt=0)] | None = None
    http_status: Annotated[int, Field(ge=100, le=599)] = 200
    raw_response_body: str | None = None
    jev_settings_retries: NonnegativeInt = 0
    daemon_suffix: DaemonSuffix | None = None
    prior_chain_executor_task: Text | None = None
    assigned_but_unstarted_child: bool = False


class ControlExpected(StrictRecord):
    error_code: CompressionCode | None
    phase: Literal["before_routing", "after_routing", "selection", "completed"]
    router_calls: NonnegativeInt
    compression_http_calls: NonnegativeInt
    executor_started: bool
    effective_context_published: bool
    completed_steps: NonnegativeInt
    cooldown_written: bool
    escalation_created: bool
    followup_code: Literal["no_executor_run", "task_in_progress", "worktree_not_integrated"] | None
    known_compression_cost_usd: Annotated[float, Field(ge=0)] | None = None
    cost_complete: bool | None = None
    ledger_rows: NonnegativeInt | None = None
    task_status: Literal["cancelled", "failed", "completed"] | None = None
    current_http_cancelled: bool | None = None
    forbidden_metadata_fragments: list[Text] = Field(default_factory=list)
    expected_context: str | None = None
    expected_candidate_ids: list[Text] | None = None
    followup_selected_task_id: str | None = None


class ControlCase(BaseCase):
    kind: Literal["control"]
    split: Literal["offline"]
    setup: ControlSetup
    expected: ControlExpected


Case = Annotated[SemanticCase | CodingCase | ControlCase, Field(discriminator="kind")]
CASE_ADAPTER = TypeAdapter(Case)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"Nonstandard JSON number: {value}")


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def relative_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or PureWindowsPath(name).drive
        or any(part.casefold() in ("", ".", "..", ".git") for part in name.split("/"))
        or path.as_posix() != name
    ):
        raise ValueError(f"Unsafe fixture path: {name!r}")
    return name


def validate_case(value: Any) -> SemanticCase | CodingCase | ControlCase:
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    case = CASE_ADAPTER.validate_python(value)
    # Literal[1]/Literal[0] must not accept Python's bool == int equivalence.
    if type(value.get("schema_version")) is not int:
        raise ValueError("schema_version must be an integer")
    match = re.fullmatch(r"(sem|code|control)-(\d{2})-[a-z0-9-]+", case.id)
    if match is None:
        raise ValueError("Invalid case ID")
    prefix, number = match[1], int(match[2])
    expected_prefix = {"semantic": "sem", "coding": "code", "control": "control"}[case.kind]
    maximum = 30 if case.kind == "semantic" else 20
    boundary = 10 if case.kind == "semantic" else 6
    expected_split = (
        "offline" if case.kind == "control" else "calibration" if number <= boundary else "holdout"
    )
    if prefix != expected_prefix or not 1 <= number <= maximum or case.split != expected_split:
        raise ValueError("Invalid ID/kind/frozen split")
    unique(case.tags, "tags")
    for name in case.request.files:
        relative_path(name)
    DispatchRequest(**case.request.model_dump(), cwd="/tmp")
    if isinstance(case, ControlCase):
        setup = case.setup
        paths = (setup.trusted_root, setup.cwd_symlink, setup.symlink_target)
        if any(paths) and not all(paths):
            raise ValueError("Symlink control requires all sandbox paths")
        for path in paths:
            if path is not None:
                if not path.startswith("{sandbox}/"):
                    raise ValueError("Control path must stay in sandbox")
                relative_path(path.removeprefix("{sandbox}/"))
        if setup.cancel_on_step_started is not None:
            index = setup.cancel_on_step_started
            if index >= len(setup.responses) or setup.responses[index] != "block_until_cancel":
                raise ValueError("Cancellation must address its blocking transport step")
        if setup.assigned_but_unstarted_child != bool(setup.prior_chain_executor_task):
            raise ValueError("Never-run child requires its prior chain task")
        return case
    blocks = split_context(case.request.context)
    ids = [block.id for block in blocks]
    unique(case.expected_kept_ids, "kept IDs")
    if case.expected_kept_ids != [key for key in ids if key in case.expected_kept_ids]:
        raise ValueError("Unknown or unordered kept IDs")
    if [step.candidate_id for step in case.recorded_steps] != ids:
        raise ValueError("Recorded steps must cover every source candidate in order")
    for step in case.recorded_steps:
        unique(step.retained_ids_before, "retained IDs")
        if step.candidate_id not in step.retained_ids_before or step.retained_ids_before != [
            key for key in ids if key in step.retained_ids_before
        ]:
            raise ValueError("Invalid recorded retained references")
    unique([fact.id for fact in case.required_facts], "fact IDs")
    slices = {b.id: case.request.context[b.start : b.end] for b in blocks}
    for fact in case.required_facts:
        for alternative in fact.alternatives:
            unique(alternative.block_ids, "fact block references")
            if not set(alternative.block_ids) <= slices.keys():
                raise ValueError("Unknown fact block reference")
            if not all(
                any(fragment in slices[key] for key in alternative.block_ids)
                for fragment in alternative.fragments
            ):
                raise ValueError("Required fragment absent from referenced source blocks")
    if isinstance(case, CodingCase):
        fixture = case.fixture
        if not fixture.files or fixture.setup_argv:
            raise ValueError("Frozen fixture must have files and no setup commands")
        for name in fixture.files:
            relative_path(name)
        unique(
            [unicodedata.normalize("NFD", name).casefold() for name in fixture.files],
            "materialization paths",
        )
        unique(fixture.editable_paths, "editable paths")
        if not set(fixture.editable_paths) <= fixture.files.keys():
            raise ValueError("Editable path absent from fixture")
        for name in fixture.editable_paths:
            relative_path(name)
        names = list(fixture.files)
        if any(a != b and b.startswith(a + "/") for a in names for b in names):
            raise ValueError("Conflicting fixture materialization paths")
        if set(case.request.files) != set(fixture.editable_paths):
            raise ValueError("Request file scope differs from editable paths")
        if case.assertion.argv != ["{python}", "-I", "{checker_path}", "{repo_path}"]:
            raise ValueError("Checker argv must match the exact isolated Python contract")
        raw_assertion = value["assertion"]
        if any(
            type(raw_assertion[key]) is not int for key in ("timeout_seconds", "success_exit_code")
        ):
            raise ValueError("Checker numeric fields must be integers")
        compile(case.assertion.source, "<withheld-checker>", "exec")
        for source in fixture.files.values():
            compile(source, "<fixture>", "exec")
    return case


def load_corpus(path: Path = CORPUS) -> list[SemanticCase | CodingCase | ControlCase]:
    data = path.read_bytes()
    if not data.endswith(b"\n") or b"\r\n" in data:
        raise ValueError("Corpus must be LF-terminated UTF-8 JSONL")
    cases = []
    for line, text in enumerate(data.decode("utf-8", errors="strict").splitlines(), 1):
        try:
            cases.append(validate_case(strict_json(text)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Corpus line {line}: {exc}") from exc
    unique([case.id for case in cases], "case IDs")
    for kind, count in (("semantic", 30), ("coding", 20), ("control", 20)):
        numbers = sorted(int(case.id.split("-")[1]) for case in cases if case.kind == kind)
        if numbers != list(range(1, count + 1)):
            raise ValueError(f"Frozen {kind} ID coverage must be 1..{count}")
    if hashlib.sha256(data).hexdigest() != CORPUS_SHA256:
        raise ValueError("Corpus differs from frozen revision 3 SHA-256")
    return cases


def facts_missing(case: SemanticCase, kept_ids: list[str], effective: str) -> list[str]:
    blocks = split_context(case.request.context)
    slices = {b.id: case.request.context[b.start : b.end] for b in blocks}
    kept = set(kept_ids)
    return [
        fact.id
        for fact in case.required_facts
        if not any(
            set(alt.block_ids) <= kept
            and all(
                fragment in effective and any(fragment in slices[key] for key in alt.block_ids)
                for fragment in alt.fragments
            )
            for alt in fact.alternatives
        )
    ]


def offline_settings(root: Path, **updates: Any) -> Settings:
    return Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "offline-fixture"},  # pragma: allowlist secret
            "server": {"data_dir": str(root / "data")},
            "router": {
                "external": {"trusted_roots": [str(root)]},
                "jev": {"base_url": MOCK_URL, "retries": 0},
            },
            "routing": {"fallback_executor": "eval", "corporate_perimeter": False},
            "execution": {"workspace_mode": "in_place", "review_fix_rounds": 0},
            "executors": {
                name: {"adapter": "omp", "model": "offline", "corporate": True}
                for name in ("eval", "eval-next")
            },
            "escalation": {"eval": ["eval-next"]},
            **updates,
        }
    )


async def select_case(
    case: SemanticCase,
    settings: Settings,
    root: Path,
    *,
    live: bool = False,
) -> dict[str, Any]:
    req = DispatchRequest(**case.request.model_dump(), cwd=str(root))
    blocks = split_context(req.context)
    transitions = []
    outcomes = []
    pending = []
    reasons = []

    async def before(start: selector.StepStart, acknowledge: Callable[[], None]) -> None:
        pending.append(start)
        acknowledge()

    async def after(outcome: selector.StepOutcome) -> None:
        outcomes.append(outcome)

    def respond(request: httpx.Request) -> httpx.Response:
        index = len(transitions)
        if index >= len(case.recorded_steps):
            raise AssertionError("Unexpected extra selector HTTP call")
        body = strict_json(request.content.decode("utf-8"))
        state = body["state"]
        step = case.recorded_steps[index]
        expected_blocks = [
            {"id": b.id, "origin": "user", "text": req.context[b.start : b.end]}
            for b in blocks
            if b.id in step.retained_ids_before
        ]
        expected_state = {
            "task": req.task,
            "files": req.files,
            "constraints": req.constraints,
            "success_criteria": req.success_criteria,
            "blocks": expected_blocks,
            "candidate_id": step.candidate_id,
        }
        if state != expected_state:
            raise AssertionError(f"Current retained state mismatch at step {index}")
        if body["questions"] != {"delete_safe": selector.QUESTION}:
            raise AssertionError("Not the production single-question protocol")
        if not pending or pending[-1].index != index:
            raise AssertionError("HTTP preceded acknowledged step")
        transitions.append(state)
        return httpx.Response(200, json=step.response)

    # Live tracing observes the real serialized request, never an alternate selector.
    async def observe(request: httpx.Request) -> None:
        transitions.append(strict_json(request.content.decode("utf-8"))["state"])

    transport = httpx.AsyncHTTPTransport(retries=0) if live else httpx.MockTransport(respond)
    started = time.monotonic()
    metrics = None
    effective = None
    kept = []
    try:
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [observe]} if live else {},
        ) as client:
            result = await selector.compress_context(
                selector.CompressionInput(case.id, req, blocks, None, None),
                settings=settings,
                client=client,
                before_step=before,
                after_step=after,
            )
        effective = result.effective_context
        kept = list(result.kept_ids)
        metrics = result.metrics
        if not live and kept != case.expected_kept_ids:
            reasons.append("Final retained IDs differ from recorded labels")
        if not live and len(transitions) != len(case.recorded_steps):
            reasons.append("Recorded steps were not all consumed")
        expected = "".join(req.context[b.start : b.end] for b in blocks if b.id in kept)
        if effective != expected:
            reasons.append("Final output is not the exact retained source slices")
        missing = facts_missing(case, kept, effective)
        if missing:
            reasons.append(f"Required facts lost: {missing}")
        if live and (
            not metrics["usage"]["cost_complete"]
            or metrics["usage"]["input_tokens"] is None
            or metrics["usage"]["output_tokens"] is None
            or (outcomes and metrics["is_byok"] is not False)
        ):
            reasons.append("Incomplete usage or unknown BYOK provider bill")
    except selector.CompressionError as exc:
        metrics = exc.metrics
        reasons.append(exc.code)
    except (AssertionError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "id": case.id,
        "kind": case.kind,
        "split": case.split,
        "ok": not reasons,
        "reasons": reasons,
        "required_facts_missing": facts_missing(case, kept, effective or ""),
        "kept_ids": kept,
        "effective_context": effective,
        "input_bytes": len(req.context.encode("utf-8")),
        "output_bytes": len(effective.encode("utf-8")) if effective is not None else None,
        "wall_ms": (time.monotonic() - started) * 1000,
        "transitions": transitions,
        "steps": [asdict(outcome) for outcome in outcomes],
        "metrics": metrics,
    }


@dataclass
class OfflineAdapter:
    name: str
    calls: list[Any] = field(default_factory=list)

    async def check(self) -> Availability:
        return Availability(available=True, version="offline", checked_at=datetime.now(UTC))

    async def execute(self, ctx: Any) -> ExecutionResult:
        self.calls.append(ctx)
        return ExecutionResult(
            status="completed", executor=self.name, model="offline", summary="offline boundary"
        )


class StaticRouter:
    name = "static"

    async def decide(self, req: DispatchRequest, candidates: Any) -> RouteDecision:
        return RouteDecision(executor="eval", confidence=1, scores={"eval": 1}, router="static")


def ancestor_record(task_id: str, req: DispatchRequest, *, ran: bool = False) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_task_id=None,
        escalated_from=None,
        root_agent="cli",
        source_agent="cli",
        hop=0,
        request=req,
        status="failed" if ran else "completed",
        decision=RouteDecision(executor="eval", confidence=1, scores={"eval": 1}, router="static"),
        result=ExecutionResult(
            status="failed" if ran else "completed",
            executor="eval",
            model="offline",
            summary="persisted fixture ancestor",
            meta={"executor_started": ran},
        ),
        log_path="/dev/null",
        created_at=now,
        started_at=now if ran else None,
        finished_at=now,
    )


async def control_case(case: ControlCase, root: Path) -> dict[str, Any]:
    """Run declarative scenarios through real admission, ledger, cancel and follow-up."""
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(initialize_repository, root)
    setup = case.setup
    req = DispatchRequest(**case.request.model_dump(), cwd=str(root), source_agent="cli")
    if setup.router == "static":
        req.executor = "eval"
    settings = offline_settings(root)
    settings.routing.corporate_perimeter = setup.corporate_perimeter
    settings.routing.daily_cost_limit_usd = setup.daily_cost_limit_usd
    settings.routing.max_chain_cost_usd = setup.max_chain_cost_usd
    settings.router.jev.retries = setup.jev_settings_retries
    if setup.trusted_cwd is False:
        settings.router.external.trusted_roots = []
    if setup.trusted_root is not None:
        trusted = root / setup.trusted_root.removeprefix("{sandbox}/")
        target = root / setup.symlink_target.removeprefix("{sandbox}/")
        link = root / setup.cwd_symlink.removeprefix("{sandbox}/")
        await asyncio.to_thread(trusted.mkdir, parents=True)
        await asyncio.to_thread(target.mkdir, parents=True)
        await asyncio.to_thread(link.symlink_to, target, target_is_directory=True)
        await asyncio.to_thread(initialize_repository, target)
        req.cwd = str(link)
        settings.router.external.trusted_roots = [str(trusted)]
    settings.router.external.allow_unknown_projects = setup.allow_unknown_projects
    storage = Storage(root / "control.sqlite")
    await storage.open()
    adapters = {name: OfflineAdapter(name) for name in settings.executors}
    router_calls = 0
    requests = []
    entered = asyncio.Event()
    cancelled = False
    reasons = []
    dispatcher = None
    started = time.monotonic()

    async def route_response(request: httpx.Request) -> httpx.Response:
        nonlocal router_calls
        router_calls += 1
        body = strict_json(request.content.decode())
        if "corporate_data" not in body["questions"]:
            raise AssertionError("Perimeter scenario did not ask the actual routing question")
        tiers = list(body["questions"]["capability"]["criteria"])
        return httpx.Response(
            200,
            json={
                "answers": {
                    "capability": {
                        "type": "choice",
                        "choice": tiers[0],
                        "confidence": 1.0,
                        "probabilities": {tier: float(tier == tiers[0]) for tier in tiers},
                    },
                    "corporate_data": {"type": "noul", "noul": 1.0},
                },
                "usage": {"cost": 0.0},
            },
        )

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        index = len(requests)
        state = strict_json(request.content.decode())["state"]
        requests.append(state)
        action = setup.responses[index] if index < len(setup.responses) else None
        if isinstance(action, str):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
        if setup.http_status != 200:
            return httpx.Response(setup.http_status, text=setup.raw_response_body or "")
        if not isinstance(action, dict):
            raise AssertionError("Unsupported/unrecorded control transport step")
        return httpx.Response(200, json=action)

    real_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), follow_redirects=False)

    try:
        ancestor = setup.persisted_ancestor
        if ancestor is not None:
            parent_req = DispatchRequest(
                task="Synthetic ancestor", cwd=str(root), prod=ancestor.prod
            )
            parent = ancestor_record("synthetic-ancestor", parent_req)
            if ancestor.perimeter is not None:
                parent.decision.meta["perimeter"] = ancestor.perimeter
            await storage.insert_task(parent)
            if ancestor.edge != "escalated_from":
                setattr(req, ancestor.edge, parent.task_id)
        if setup.daemon_suffix is not None:
            suffix = setup.daemon_suffix
            parent_req = DispatchRequest(task="Synthetic handoff ancestor", cwd=str(root))
            await storage.insert_task(ancestor_record(suffix.source_task_id, parent_req))
            req.parent_task_id = suffix.source_task_id
            original_length = len(req.context)
            req.context += suffix.text
            req._context_provenance = (
                ContextSpan(0, original_length, "user", None),
                ContextSpan(
                    original_length, len(req.context), suffix.origin, suffix.source_task_id
                ),
            )
        prior = None
        if setup.prior_chain_executor_task is not None:
            parent_req = DispatchRequest(task="Synthetic earlier run", cwd=str(root))
            prior = ancestor_record(setup.prior_chain_executor_task, parent_req, ran=True)
            await storage.insert_task(prior)
            req._context_provenance = (ContextSpan(0, len(req.context), "user", None),)
        if ancestor is not None and ancestor.edge == "escalated_from":
            req._context_provenance = (ContextSpan(0, len(req.context), "user", None),)
        if setup.reload_storage:
            await storage.close()
            await storage.open()
        async with real_client(transport=httpx.MockTransport(route_response)) as routing_client:
            routers = (
                [JevRouter(settings, routing_client)] if setup.router == "jev" else [StaticRouter()]
            )
            # Control 09's route must actually select before compression admission.
            if setup.routing_response_perimeter is not None:
                settings.routing.corporate_perimeter = True
            dispatcher = Dispatcher(
                settings, storage, adapters, AvailabilityCache(adapters, 60), routers
            )
            with ExitStack() as stack:
                stack.enter_context(
                    patch("agent_dispatch.dispatch.dispatcher.httpx.AsyncClient", client_factory)
                )
                if setup.deadline_seconds is not None:
                    stack.enter_context(
                        patch.object(selector, "STEP_TIMEOUT_SECONDS", setup.deadline_seconds)
                    )
                escalated_from = prior.task_id if prior else None
                if ancestor is not None and ancestor.edge == "escalated_from":
                    escalated_from = "synthetic-ancestor"
                submitted = await dispatcher.submit(req, escalated_from=escalated_from)
                if prior:
                    prior.result.meta["escalated_to"] = submitted.task_id
                    await storage.update_task(prior)
                if setup.cancel_on_step_started is not None:
                    await asyncio.wait_for(entered.wait(), 5)
                    if len(requests) - 1 != setup.cancel_on_step_started:
                        raise AssertionError("Cancellation entered the wrong step")
                    await dispatcher.cancel(submitted.task_id)
                done = await dispatcher.wait(submitted.task_id, 5)
                if done.status not in FINAL_STATUSES:
                    raise AssertionError("Control did not terminate")
                if done.task_id != submitted.task_id:
                    raise AssertionError("Unexpected normal escalation after compression")
                result = done.result
                metrics = result.meta.get("context_compression", {})
                ledger = await storage.compression_steps(done.task_id)
                events = await storage.list_events(done.task_id)
                task_rows = await storage.list_tasks(limit=100)
                adapter_calls = [ctx for adapter in adapters.values() for ctx in adapter.calls]
                phase = (
                    "completed"
                    if result.meta.get("error_code") is None
                    else "selection"
                    if requests
                    else "after_routing"
                    if router_calls
                    else "before_routing"
                )
                observed = {
                    "error_code": result.meta.get("error_code"),
                    "phase": phase,
                    "router_calls": router_calls,
                    "compression_http_calls": len(requests),
                    "executor_started": bool(adapter_calls),
                    "effective_context_published": any(
                        e["kind"] == "context_compression" and e["payload"]["status"] == "completed"
                        for e in events
                    ),
                    "completed_steps": metrics.get("completed_steps"),
                    "cooldown_written": bool(await storage.active_cooldowns()),
                    "escalation_created": any(t.escalated_from == done.task_id for t in task_rows),
                    "known_compression_cost_usd": metrics.get("usage", {}).get("known_cost"),
                    "cost_complete": metrics.get("usage", {}).get("cost_complete"),
                    "ledger_rows": len(ledger),
                    "task_status": done.status.value,
                    "current_http_cancelled": cancelled,
                    "expected_candidate_ids": [state["candidate_id"] for state in requests],
                }
                if case.expected.expected_context is not None:
                    prompt = adapter_calls[0].prompt if adapter_calls else ""
                    observed["expected_context"] = (
                        prompt.partition("# Context\n")[2]
                        .partition("## Relevant files\n")[0]
                        .removesuffix("\n")
                    )
                    # All selectable caller text must be absent, not merely the suffix present.
                    for block in split_context(case.request.context):
                        text = case.request.context[block.start : block.end]
                        if adapter_calls and text in adapter_calls[0].prompt:
                            reasons.append("Deleted caller text leaked into executor prompt")
                followup_code = None
                selected_task_id = None
                try:
                    child = await dispatcher.followup(
                        prior.task_id if prior else done.task_id,
                        FollowupRequest(
                            message="Continue the synthetic run", allow_escalation=False
                        ),
                    )
                    selected_task_id = child.request.followup_of
                    await dispatcher.wait(child.task_id, 5)
                except FollowupError as exc:
                    followup_code = exc.code
                observed.update(
                    followup_code=followup_code, followup_selected_task_id=selected_task_id
                )
                expected = case.expected.model_dump(exclude_unset=True)
                fragments = expected.pop("forbidden_metadata_fragments", [])
                for key, value in expected.items():
                    actual = observed.get(key)
                    matches = (
                        math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-12)
                        if key == "known_compression_cost_usd" and isinstance(actual, (float, int))
                        else actual == value
                    )
                    if not matches:
                        reasons.append(f"{key}: expected {value!r}, observed {actual!r}")
                safe_metadata = json.dumps(
                    {"result": result.model_dump(mode="json"), "ledger": ledger, "events": events}
                )
                for fragment in fragments:
                    if fragment in safe_metadata:
                        reasons.append("Forbidden provider metadata leaked")
                stored = await storage.get_task(done.task_id)
                if stored.request.context != req.context:
                    reasons.append("Persisted original context was mutated")
                # Verify durable aggregation, not a sum of its telemetry mirrors.
                known = math.fsum(row["cost_usd"] for row in ledger if row["cost_usd"] is not None)
                if not math.isclose(await storage.tree_cost([done.task_id]), known):
                    reasons.append("Storage tree accounting differs from selector ledger")
                return {
                    "id": case.id,
                    "kind": "control",
                    "split": "offline",
                    "ok": not reasons,
                    "reasons": reasons,
                    "observed": observed,
                    "ledger": ledger,
                    "wall_ms": (time.monotonic() - started) * 1000,
                }
    finally:
        if dispatcher is not None:
            await dispatcher.shutdown()
        await storage.close()


def clean_env() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def initialize_repository(repo: Path, files: tuple[str, ...] = ()) -> None:
    commands = [["init", "-q", "-b", "main"]]
    if files:
        commands.append(["add", "--", *files])
    commands.append(
        [
            "-c",
            "user.email=eval@example.com",
            "-c",
            "user.name=eval",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ]
    )
    for args in commands:
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            env=clean_env(),
            capture_output=True,
            check=True,
            timeout=10,
        )


def materialize(case: CodingCase, repo: Path, *, git: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=False)
    for name, source in case.fixture.files.items():
        relative_path(name)
        destination = repo / name
        if not destination.resolve().is_relative_to(repo.resolve()):
            raise ValueError("Fixture path escapes repository")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8", newline="")
    if git:
        initialize_repository(repo, tuple(case.fixture.files))


def check_candidate(case: CodingCase, repo: Path, checker_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    checker_dir.mkdir(parents=True, exist_ok=True)
    if checker_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("Withheld checker must be outside the executor repository")
    checker = checker_dir / "checker.py"
    checker.write_text(case.assertion.source, encoding="utf-8", newline="")
    substitutions = {
        "python": sys.executable,
        "checker_path": str(checker.resolve()),
        "repo_path": str(repo.resolve()),
    }
    argv = [arg.format(**substitutions) for arg in case.assertion.argv]
    # -I ignores PYTHONDONTWRITEBYTECODE; the checker must not create fixture files.
    # Keep the frozen assertion argv unchanged and constrain its interpreter here.
    argv.insert(1, "-B")
    try:
        result = subprocess.run(
            argv,
            cwd=checker_dir,
            env=clean_env(),
            capture_output=True,
            timeout=case.assertion.timeout_seconds,
        )
        return {
            "executed": True,
            "ok": result.returncode == case.assertion.success_exit_code,
            "exit_code": result.returncode,
            "timed_out": False,
            "wall_ms": (time.monotonic() - started) * 1000,
            "output_tail": (result.stdout + result.stderr).decode("utf-8", errors="replace")[
                -2000:
            ],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "executed": isinstance(exc, subprocess.TimeoutExpired),
            "ok": False,
            "exit_code": None,
            "timed_out": isinstance(exc, subprocess.TimeoutExpired),
            "error": type(exc).__name__,
            "wall_ms": (time.monotonic() - started) * 1000,
        }


# Independent reference implementations prove all frozen checkers are reachable.
# They are never materialized in a paid fixture, request, or executor context.
REFERENCES = {
    1: "def stable_unique(values):\n    return list(dict.fromkeys(values))\n",
    2: "def lookup(mapping, key):\n    return mapping[key] if key in mapping else 'MISSING'\n",
    3: "def eligible(age):\n    return type(age) is int and 18 <= age <= 65\n",
    4: (
        "import re\nfrom urllib.parse import unquote_to_bytes\n"
        "def decode_component(value):\n"
        "    if re.search(r'%(?![a-fA-F0-9]{2})', value):\n        raise ValueError\n"
        "    return unquote_to_bytes(value).decode('utf-8')\n"
    ),
    5: (
        "from decimal import Decimal, ROUND_HALF_UP\n"
        "def cents(value):\n"
        "    return int((Decimal(value) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))\n"
    ),
    6: (
        "def resolve(name, env, config):\n"
        "    return env[name] if name in env else config.get(name)\n"
    ),
    7: "def requires_auth(path):\n    return path not in ('/health', '/ready')\n",
    8: (
        "def prune_none(mapping):\n"
        "    return {key: value for key, value in mapping.items() if value is not None}\n"
    ),
    9: "import csv\ndef parse_row(line):\n    return next(csv.reader([line]))\n",
    10: "def ranked(rows):\n    return sorted(rows, key=lambda row: -row['score'])\n",
    11: (
        "def utf8_prefix(text, limit):\n"
        "    if limit < 0:\n        raise ValueError\n"
        "    size = 0\n    end = 0\n"
        "    for char in text:\n"
        "        size += len(char.encode('utf-8'))\n"
        "        if size > limit:\n            break\n"
        "        end += 1\n"
        "    return text[:end]\n"
    ),
    12: (
        "def merge_intervals(intervals):\n"
        "    result = []\n"
        "    for start, end in sorted(intervals):\n"
        "        if start == end:\n            continue\n"
        "        if result and start < result[-1][1]:\n"
        "            result[-1] = (result[-1][0], max(result[-1][1], end))\n"
        "        else:\n            result.append((start, end))\n"
        "    return result\n"
    ),
    13: (
        "def retryable(method, status, ambiguous):\n"
        "    return method.upper() in ('GET', 'HEAD') and status in (429, 502, 503) "
        "and ambiguous is not True\n"
    ),
    14: (
        "def counts(words):\n    result = {}\n"
        "    for word in words:\n        key = word.casefold()\n"
        "        result[key] = result.get(key, 0) + 1\n    return result\n"
    ),
    15: (
        "def patch(base, changes):\n    result = dict(base)\n"
        "    for key, value in changes.items():\n"
        "        if value is None:\n            result.pop(key, None)\n"
        "        else:\n            result[key] = value\n    return result\n"
    ),
    16: (
        "def parse_pairs(items):\n    result = {}\n"
        "    for item in items:\n        key, sep, value = item.partition('=')\n"
        "        if not sep or not key:\n            raise ValueError\n"
        "        result[key] = value\n    return result\n"
    ),
    17: (
        "def chunks(values, size):\n"
        "    if type(size) is not int or size <= 0:\n        raise ValueError\n"
        "    return [list(values[i:i+size]) for i in range(0, len(values), size)]\n"
    ),
    18: (
        "def redact(mapping):\n"
        "    return {key: '[REDACTED]' if key.lower() in ('token', 'password') else value\n"
        "            for key, value in mapping.items()}\n"
    ),
    19: (
        "import math\ndef finite_mean(values):\n"
        "    numbers = [v for v in values if type(v) in (int, float) and math.isfinite(v)]\n"
        "    return sum(numbers) / len(numbers) if numbers else None\n"
    ),
    20: (
        "def escape_text(value):\n"
        "    return ''.join({'&': '&amp;', '<': '&lt;', '>': '&gt;'}.get(c, c) for c in value)\n"
    ),
}


def fixture_proof(case: CodingCase, root: Path) -> dict[str, Any]:
    stub = root / "stub"
    reference = root / "reference"
    materialize(case, stub, git=False)
    materialize(case, reference, git=False)
    red = check_candidate(case, stub, root / "red-check")
    (reference / "solution.py").write_text(REFERENCES[int(case.id.split("-")[1])])
    green = check_candidate(case, reference, root / "green-check")
    return {
        "ok": red["executed"] and red["exit_code"] not in (None, 0) and green["ok"],
        "stub": red,
        "reference": green,
    }


async def run_offline(cases: list[Any], root: Path) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        directory = root / case.id
        await asyncio.to_thread(directory.mkdir, parents=True)
        try:
            if isinstance(case, ControlCase):
                row = await control_case(case, directory)
            else:
                row = await select_case(case, offline_settings(directory), directory)
                if isinstance(case, CodingCase):
                    proof = await asyncio.to_thread(
                        fixture_proof, case, directory / "fixture-proof"
                    )
                    row["fixture_proof"] = proof
                    if not proof["ok"]:
                        row["ok"] = False
                        row["reasons"].append("Independent fixture red/green proof failed")
        except Exception as exc:
            row = {
                "id": case.id,
                "kind": case.kind,
                "split": case.split,
                "ok": False,
                "reasons": [f"Harness failure: {type(exc).__name__}: {exc}"],
            }
        rows.append(row)
    return rows


def live_settings() -> Settings:
    settings = load_settings()
    if not TRUSTED_ROOT.is_dir():
        raise ValueError("Approved synthetic root does not exist")
    probe = DispatchRequest(task="Synthetic eval preflight", cwd=str(TRUSTED_ROOT))
    if not external_router_allowed(probe, settings):
        raise ValueError("Configured Jev preflight denied the approved synthetic root")
    if not settings.secret(settings.router.jev.api_key_env):
        raise ValueError("Configured Jev credential is unavailable")
    return settings


def settings_fingerprint(settings: Settings) -> str:
    # Never serialize credentials into a report or fingerprint input.
    payload = settings.model_dump(mode="json", exclude={"secrets"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def run_semantic(
    cases: list[Any],
    settings: Settings,
    *,
    split: Literal["calibration", "holdout", "all"] = "all",
    rows: list[dict[str, Any]] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    if split not in ("calibration", "holdout", "all"):
        raise ValueError("Unknown live semantic split")
    if rows is None:
        rows = []
    fingerprint = settings_fingerprint(settings)
    for case in cases:
        if case.kind != "semantic" or (split != "all" and case.split != split):
            continue
        if settings_fingerprint(load_settings()) != fingerprint:
            raise ValueError("Configured model/settings changed during live evaluation")
        rows.append(await select_case(case, settings, TRUSTED_ROOT, live=True))
        if checkpoint is not None:
            checkpoint()
        if rows[-1]["metrics"] is None or rows[-1]["metrics"]["status"] != "completed":
            raise RuntimeError("Live selector refused; stopping without retry or fallback")
    return rows


def finite_nonnegative(value: Any, *, integer: bool = False) -> int | float | None:
    if integer:
        return value if type(value) is int and value >= 0 else None
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def measurement(values: list[Any], *, integer: bool = False) -> dict[str, Any]:
    numbers = [finite_nonnegative(value, integer=integer) for value in values]
    known = [value for value in numbers if value is not None]
    try:
        total = sum(known) if integer else math.fsum(known)
    except OverflowError:
        total = None
    complete = len(known) == len(numbers) and total is not None
    return {
        "known_total": total,
        "total": total if complete else None,
        "known_components": len(known),
        "unknown_components": len(numbers) - len(known),
        "complete": complete,
    }


def chain_snapshot(db_path: Path, root_id: str) -> dict[str, list[dict[str, Any]]]:
    """Read a consistent durable lineage union without opening/migrating daemon storage."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        # UNION, not UNION ALL: overlapping nesting/escalation/follow-up edges count once.
        tasks = connection.execute(
            "WITH RECURSIVE lineage(task_id) AS (SELECT task_id FROM tasks WHERE task_id=? "
            "UNION SELECT t.task_id FROM tasks t JOIN lineage l ON "
            "t.parent_task_id=l.task_id OR t.escalated_from=l.task_id OR "
            "json_extract(t.request_json, '$.followup_of')=l.task_id) "
            "SELECT t.* FROM tasks t JOIN lineage l USING(task_id)",
            (root_id,),
        ).fetchall()
        if not tasks:
            raise ValueError("Submitted task is absent from configured daemon database")
        ids = [row["task_id"] for row in tasks]
        slots = ",".join("?" for _ in ids)
        snapshot = {"tasks": [dict(row) for row in tasks]}
        for table in ("routing_decisions", "compression_steps", "events"):
            snapshot[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE task_id IN ({slots})", ids
                )
            ]
        return snapshot


def paid_route(value: dict[str, Any]) -> bool:
    if value["router"] in ("static", "override"):
        return False
    reason = value.get("guard_reason", value.get("reason"))
    return not (value["router"] == "fallback" and reason == "single_candidate")


def account_chain(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Count merged executor usage once, reviews as child tasks.

    Merged usage preserves known values even when a contributing attempt is
    unobserved. Repaired runs lack independent initial usage; recovery outcome
    metadata/events lack per-attempt usage. Supervision metadata identifies
    unfinished checks and missing usage. These force unknown full totals without
    adding their known charges again. Inclusive durations overlap wall time.
    """
    tasks = {row["task_id"]: row for row in snapshot["tasks"]}
    routes = {row["id"]: row for row in snapshot["routing_decisions"]}
    steps = {(row["task_id"], row["step_index"]): row for row in snapshot["compression_steps"]}
    recoveries = {
        (row["task_id"], row["kind"])
        for row in snapshot.get("events", [])
        if row["kind"] in ("report_recovery", "verdict_recovery")
    }
    components = []
    executions = []
    reviews = []
    fix_rounds = []
    verification_ms = []
    unknowns = []
    for task_id, task in tasks.items():
        request = json.loads(task["request_json"])
        result = json.loads(task["result_json"]) if task["result_json"] else {}
        meta = result.get("meta", {})
        ran = meta.get("executor_started")
        if ran is None:
            ran = bool(task.get("started_at") and result and not meta.get("guard"))
        if ran:
            usage = result.get("usage") or {}
            component = {
                "id": task_id,
                "kind": "review" if request.get("kind") == "review" else "executor",
                "cost": usage.get("cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "latency_ms": task.get("duration_ms"),
            }
            components.append(component)
            executions.append(component)
            if component["kind"] == "review":
                reviews.append(component)
        if task.get("status") not in {status.value for status in FINAL_STATUSES}:
            unknowns.append(f"Unfinished task {task_id}")
            components.append(unknown_component(f"{task_id}:unfinished", "executor"))
        decision = json.loads(task["decision_json"]) if task.get("decision_json") else None
        if decision and paid_route(decision):
            if not any(row["task_id"] == task_id for row in routes.values()):
                unknowns.append(f"Missing paid routing ledger {task_id}")
                components.append(unknown_component(task_id, "routing"))
        supervision_usage = meta.get("supervision_usage")
        if supervision_usage is not None:
            started = finite_nonnegative(supervision_usage.get("checks_started"), integer=True)
            finished = finite_nonnegative(supervision_usage.get("checks_finished"), integer=True)
            interrupted = (
                started is None
                or finished is None
                or started != finished
                or started > 1
                or supervision_usage.get("inflight_check_cancelled")
                or supervision_usage.get("inflight_check_usage")
            )
            # finish() already merges known supervisor charges into result.usage.
            # Only mark unobserved dimensions; never add the metadata mirror again.
            usage = supervision_usage.get("usage") or {}
            category = "review" if request.get("kind") == "review" else "executor"
            missing = unknown_component(f"{task_id}:supervision", category)
            for dimension, key in (
                ("cost", "cost_usd"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
            ):
                if not interrupted and (
                    started == 0
                    or finite_nonnegative(usage.get(key), integer=dimension != "cost") is not None
                ):
                    missing[dimension] = 0
            if any(missing[key] is None for key in ("cost", "input_tokens", "output_tokens")):
                components.append(missing)
                unknowns.append(f"Unobserved supervision usage {task_id}")
        if meta.get("fix_rounds"):
            # The production merge does not persist the initial attempt's usage.
            # Complete repair usage cannot establish that the initial run was seen.
            components.append(unknown_component(f"{task_id}:initial", "executor"))
            unknowns.append(f"Initial attempt usage unavailable for repaired task {task_id}")
        for index, round_info in enumerate(meta.get("fix_rounds", [])):
            fix_rounds.append({"task_id": task_id, **round_info})
            # Production stores the repair's usage separately; result.usage is
            # already the known sum, including this repair when observed.
            usage = round_info.get("usage") or {}
            missing = unknown_component(f"{task_id}:fix-{index}", "executor")
            for dimension, key in (
                ("cost", "cost_usd"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
            ):
                if finite_nonnegative(usage.get(key), integer=dimension != "cost") is not None:
                    missing[dimension] = 0
            if any(missing[key] is None for key in ("cost", "input_tokens", "output_tokens")):
                components.append(missing)
                unknowns.append(f"Unobserved fix-round usage {task_id}:{index}")
            if "report_recovery" in round_info:
                components.append(unknown_component(f"{task_id}:fix-{index}:report", "executor"))
                unknowns.append(f"Report-recovery attempt usage unavailable {task_id}:{index}")
        # False verdict_recovered alone may mean recovery was skipped. A durable
        # event proves an attempt even when recovery failed; true metadata proves
        # success without relying on the event. Neither carries per-attempt usage.
        for kind, attempted in (
            ("report_recovery", "report_recovery" in meta),
            ("verdict_recovery", meta.get("verdict_recovered") is True),
        ):
            if attempted or (task_id, kind) in recoveries:
                category = "review" if request.get("kind") == "review" else "executor"
                components.append(unknown_component(f"{task_id}:{kind}", category))
                unknowns.append(f"Recovery attempt usage unavailable {task_id}: {kind}")
        verification = result.get("verification")
        if verification is not None:
            for command in verification.get("commands", []):
                verification_ms.append(command.get("duration_ms"))
        if request.get("verify") and (
            not verification
            or len(verification.get("commands", [])) != len(request["verify"])
            or any(
                command.get("timed_out") or command.get("exit_code") != 0
                for command in verification.get("commands", [])
            )
        ):
            unknowns.append(f"Missing/failed daemon verification {task_id}")
        compression = meta.get("context_compression")
        actual = sum(key[0] == task_id for key in steps)
        if compression and compression.get("attempted_steps") != actual:
            unknowns.append(f"Compression ledger/metric mismatch {task_id}")
            components.append(unknown_component(task_id, "compression"))
        if request.get("context_compression") and not compression:
            unknowns.append(f"Missing compression outcome {task_id}")
            components.append(unknown_component(task_id, "compression"))
    for key, row in steps.items():
        components.append(
            {
                "id": f"{key[0]}:{key[1]}",
                "kind": "compression",
                "cost": row.get("cost_usd"),
                "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"),
                "latency_ms": row.get("latency_ms"),
            }
        )
        if row.get("is_byok") not in (False, 0):
            unknowns.append(f"Unknown BYOK provider bill {key[0]}:{key[1]}")
        if row.get("status") == "started":
            unknowns.append(f"Unfinished compression step {key[0]}:{key[1]}")
    for route_id, row in routes.items():
        paid = paid_route(row)
        components.append(
            {
                "id": str(route_id),
                "kind": "routing",
                "cost": row.get("cost_usd") if paid else 0.0,
                # Existing routing ledger has no token columns: explicitly unknown.
                "input_tokens": None if paid else 0,
                "output_tokens": None if paid else 0,
                "latency_ms": row.get("latency_ms"),
            }
        )
        if paid:
            unknowns.append(f"Routing token/BYOK metadata unavailable {route_id}")
    cost = measurement([component["cost"] for component in components])
    input_tokens = measurement(
        [component["input_tokens"] for component in components], integer=True
    )
    output_tokens = measurement(
        [component["output_tokens"] for component in components], integer=True
    )
    complete = cost["complete"] and input_tokens["complete"] and output_tokens["complete"]
    complete = complete and not unknowns
    return {
        "task_ids": sorted(tasks),
        "components": components,
        "cost_usd": cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "full_cost_usd": cost["total"] if complete else None,
        "complete": complete,
        "unknowns": unknowns,
        "executions": len(executions),
        "reviews": len(reviews),
        "escalations": sum(bool(task["escalated_from"]) for task in tasks.values()),
        "escalation_task_ids": [key for key, task in tasks.items() if task["escalated_from"]],
        "fix_rounds": fix_rounds,
        "inclusive_task_latency_ms": measurement(
            [task.get("duration_ms") for task in tasks.values()]
        ),
        "compression_latency_ms": measurement([row.get("latency_ms") for row in steps.values()]),
        "routing_latency_ms": measurement([row.get("latency_ms") for row in routes.values()]),
        "review_inclusive_latency_ms": measurement([row["latency_ms"] for row in reviews]),
        "verification_latency_ms": measurement(verification_ms),
    }


def fixture_violations(case: CodingCase, repo: Path) -> list[str]:
    violations = []
    seen = set()
    for path in repo.rglob("*"):
        relative = path.relative_to(repo).as_posix()
        if relative.split("/")[0] == ".git":
            continue
        if path.is_symlink():
            violations.append(f"Symlink in fixture: {relative}")
            continue
        if path.is_file():
            seen.add(relative)
            if relative not in case.fixture.files:
                violations.append(f"Unexpected file: {relative}")
            elif relative not in case.fixture.editable_paths:
                if path.read_bytes() != case.fixture.files[relative].encode("utf-8"):
                    violations.append(f"Immutable fixture changed: {relative}")
    for relative in case.fixture.files.keys() - seen:
        violations.append(f"Missing fixture file: {relative}")
    return violations


def paired_request(
    case: CodingCase, repo: Path, executor: str, arm: str, timeout: int
) -> DispatchRequest:
    payload = case.request.model_dump()
    payload.pop("context_compression")
    if arm == "treatment":
        payload["context_compression"] = "jev"
    elif arm != "baseline":
        raise ValueError("Unknown paired arm")
    return DispatchRequest(
        **payload,
        cwd=str(repo),
        executor=executor,
        source_agent="cli",
        workspace_mode="in_place",
        decision_log=False,
        allow_escalation=False,
        timeout_seconds=timeout,
    )


def paired_schedule(cases: list[CodingCase]) -> list[tuple[CodingCase, int, str]]:
    schedule = []
    for case in sorted(cases, key=lambda value: value.id):
        number = int(case.id.split("-")[1])
        for repetition in range(3):
            treatment_first = (repetition == 1) != (number % 2 == 0)
            arms = ("treatment", "baseline") if treatment_first else ("baseline", "treatment")
            schedule.extend((case, repetition, arm) for arm in arms)
    return schedule


def coding_verdict(
    result: ExecutionResult | None,
    check: dict[str, Any] | None,
    accounting: dict[str, Any],
    violations: list[str],
    missing_facts: list[str],
) -> list[str]:
    reasons = list(violations)
    if result is None or result.status != "completed" or result.error:
        reasons.append("Executor/dispatch did not complete successfully")
    if (
        not check
        or check.get("executed") is not True
        or check.get("ok") is not True
        or check.get("exit_code") != 0
        or check.get("timed_out") is not False
    ):
        reasons.append("Independent check missing, timed out or failed")
    if not accounting["complete"]:
        reasons.append("Incomplete full-chain usage/cost/check accounting")
    if missing_facts:
        reasons.append(f"Required facts lost: {missing_facts}")
    return reasons


def unknown_component(task_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": f"{task_id}:missing",
        "kind": kind,
        "cost": None,
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": None,
    }


def arm_accounting(rows: list[dict[str, Any]]) -> dict[str, Any]:
    components = {
        (component["kind"], component["id"]): component
        for row in rows
        for component in row.get("accounting", {}).get("components", [])
    }
    missing = sum("components" not in row.get("accounting", {}) for row in rows)
    breakdown = {}
    for kind in ("executor", "compression", "routing", "review"):
        selected = [value for (category, _), value in components.items() if category == kind]
        selected.extend(unknown_component(f"unmeasured-{index}", kind) for index in range(missing))
        breakdown[kind] = {
            "count": len(selected) - missing,
            "unknown_run_components": missing,
            "cost_usd": measurement([value["cost"] for value in selected]),
            "input_tokens": measurement(
                [value["input_tokens"] for value in selected], integer=True
            ),
            "output_tokens": measurement(
                [value["output_tokens"] for value in selected], integer=True
            ),
            "inclusive_latency_ms": measurement([value["latency_ms"] for value in selected]),
        }
    escalations = {
        task_id
        for row in rows
        for task_id in row.get("accounting", {}).get("escalation_task_ids", [])
    }
    return {
        "components": breakdown,
        "known_cost_usd": measurement(
            [row.get("accounting", {}).get("cost_usd", {}).get("known_total") for row in rows]
        ),
        "reviews": breakdown["review"]["count"],
        "escalations": len(escalations),
        "fix_rounds": measurement(
            [
                len(row["accounting"]["fix_rounds"])
                if "fix_rounds" in row.get("accounting", {})
                else None
                for row in rows
            ],
            integer=True,
        ),
        "verification_latency_ms": measurement(
            [
                row.get("accounting", {}).get("verification_latency_ms", {}).get("total")
                for row in rows
            ]
        ),
        "unknown_run_accounting": sum(
            not row.get("accounting", {}).get("complete") for row in rows
        ),
        "latency_note": (
            "Inclusive breakdowns overlap; only measured wall time is end-to-end latency"
        ),
    }


def paired_gate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    arms = {}
    for arm in ("baseline", "treatment"):
        selected = [row for row in rows if row["arm"] == arm]
        costs = measurement([row.get("accounting", {}).get("full_cost_usd") for row in selected])
        arms[arm] = {
            "attempted": len(selected),
            "passed": sum(row["ok"] for row in selected),
            "independent_check_passed": sum(
                bool(row.get("check", {}).get("ok")) for row in selected
            ),
            "full_cost_usd": costs,
            "accounting": arm_accounting(selected),
            "wall_ms": measurement([row.get("wall_ms") for row in selected]),
            "median_context_bytes": median([row["context_bytes"] for row in selected])
            if selected
            else None,
            "splits": {
                split: {
                    "attempted": sum(row["split"] == split for row in selected),
                    "passed": sum(row["split"] == split and row["ok"] for row in selected),
                }
                for split in ("calibration", "holdout")
            },
        }
    indexed = {(row["id"], row["repetition"], row["arm"]): row for row in rows}
    pairs = []
    for case_id, repetition in sorted({(row["id"], row["repetition"]) for row in rows}):
        baseline = indexed.get((case_id, repetition, "baseline"))
        treatment = indexed.get((case_id, repetition, "treatment"))
        if baseline is None or treatment is None:
            continue
        bc = baseline.get("accounting", {}).get("full_cost_usd")
        tc = treatment.get("accounting", {}).get("full_cost_usd")
        pairs.append(
            {
                "id": case_id,
                "repetition": repetition,
                "context_byte_delta": treatment["context_bytes"] - baseline["context_bytes"],
                "wall_ms_delta": treatment["wall_ms"] - baseline["wall_ms"],
                "cost_usd_delta": tc - bc if bc is not None and tc is not None else None,
            }
        )
    lost = [row for row in rows if row["split"] == "holdout" and row.get("missing_facts")]
    complete = (
        len(indexed) == len(rows) == 120
        and len(pairs) == 60
        and all(arms[arm]["attempted"] == 60 for arm in arms)
    )
    # Failed runs remain in both denominators and compression-failure byte medians.
    ok = (
        complete
        and not lost
        and all(row["accounting"]["complete"] for row in rows)
        and arms["treatment"]["passed"] >= arms["baseline"]["passed"]
        and arms["treatment"]["independent_check_passed"]
        >= arms["baseline"]["independent_check_passed"]
        and arms["treatment"]["median_context_bytes"] < arms["baseline"]["median_context_bytes"]
    )
    return {
        "ok": ok,
        "complete_pairs": complete,
        "holdout_fact_loss_runs": len(lost),
        "arms": arms,
        "pairs": pairs,
        "pair_distributions": {
            key: distribution([pair[key] for pair in pairs])
            for key in ("context_byte_delta", "wall_ms_delta", "cost_usd_delta")
        },
        "scope": "Finite synthetic experiment, not statistical noninferiority or financial savings",
    }


def distribution(values: list[float | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None and math.isfinite(value)]
    return {
        "known": len(known),
        "unknown": len(values) - len(known),
        "min": min(known) if known else None,
        "median": median(known) if known else None,
        "max": max(known) if known else None,
    }


async def run_paired(
    cases: list[Any],
    settings: Settings,
    executor: str,
    timeout_seconds: int,
    root: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    configured = settings.enabled_executors().get(executor)
    if configured is None or configured.adapter != "omp" or not configured.model:
        raise ValueError(
            "--executor must name an enabled configured omp executor with a pinned model"
        )
    coding = [case for case in cases if isinstance(case, CodingCase)]
    if len(coding) != 20:
        raise ValueError("Paired run requires all 20 frozen coding cases")
    # Prove checkers before spending, and remove all references before any executor starts.
    with tempfile.TemporaryDirectory(prefix="compression-proof-", dir="/tmp") as proof_root:
        for case in coding:
            proof = await asyncio.to_thread(fixture_proof, case, Path(proof_root) / case.id)
            if not proof["ok"]:
                raise ValueError(f"Independent checker red/green proof failed: {case.id}")
    state = await ensure_daemon(settings)
    fingerprint = settings_fingerprint(settings)
    if rows is None:
        rows = []
    async with DispatchClient(state, timeout=timeout_seconds + 60) as client:
        daemon_identity = await client.health()
        for index, (case, repetition, arm) in enumerate(paired_schedule(coding)):
            if settings_fingerprint(load_settings()) != fingerprint:
                raise ValueError("Configuration changed during paired evaluation")
            if (await client.health()).get("pid") != daemon_identity.get("pid"):
                raise ValueError("Daemon restarted during paired evaluation")
            available = {row["name"]: row for row in await client.executors()}
            actual = available.get(executor, {})
            if (
                actual.get("model") != configured.model
                or actual.get("adapter") != "omp"
                or actual.get("enabled") is not True
            ):
                raise ValueError(
                    "Daemon executor model/config differs from the selected local config"
                )
            repo = root / f"repository-{index:03}"
            await asyncio.to_thread(materialize, case, repo)
            req = paired_request(case, repo, executor, arm, timeout_seconds)
            if arm == "treatment" and not external_router_allowed(req, settings):
                raise ValueError("Configured preflight denied the isolated synthetic repository")
            started = time.monotonic()
            view = None
            dispatch_error = None
            submitted_id = None
            terminated = False
            cancellation_attempted = False
            cancellation_error = None
            try:
                submitted = await client.submit(req)
                submitted_id = submitted.task_id
                view = submitted
                async with asyncio.timeout(timeout_seconds + 60):
                    while True:
                        view = await client.status(submitted_id, wait=min(timeout_seconds, 600))
                        if view.status in FINAL_STATUSES:
                            terminated = True
                            break
            except (
                DaemonUnavailable,
                TimeoutError,
                RuntimeError,
                ValueError,
                httpx.HTTPError,
            ) as exc:
                dispatch_error = type(exc).__name__
                if submitted_id is not None:
                    cancellation_attempted = True
                    try:
                        async with asyncio.timeout(30):
                            view = await client.cancel(submitted_id)
                        terminated = view.status in FINAL_STATUSES
                    except (
                        DaemonUnavailable,
                        TimeoutError,
                        RuntimeError,
                        ValueError,
                        httpx.HTTPError,
                    ) as cancel_exc:
                        cancellation_error = type(cancel_exc).__name__
                        terminated = False
            dispatch_ms = (time.monotonic() - started) * 1000
            violations = (
                await asyncio.to_thread(fixture_violations, case, repo) if terminated else []
            )
            # A checker must not race an executor whose cancellation is unconfirmed.
            if terminated:
                with tempfile.TemporaryDirectory(
                    prefix="compression-check-", dir="/tmp"
                ) as directory:
                    check = await asyncio.to_thread(check_candidate, case, repo, Path(directory))
            else:
                check = {"executed": False, "ok": False, "exit_code": None, "timed_out": False}
            accounting = {"complete": False, "full_cost_usd": None}
            compression = None
            snapshot = {"tasks": [], "routing_decisions": [], "compression_steps": []}
            if submitted_id is not None:
                try:
                    snapshot = await asyncio.to_thread(
                        chain_snapshot, settings.db_path, submitted_id
                    )
                    accounting = account_chain(snapshot)
                    root_task = next(t for t in snapshot["tasks"] if t["task_id"] == submitted_id)
                    if root_task["result_json"]:
                        compression = (
                            json.loads(root_task["result_json"])
                            .get("meta", {})
                            .get("context_compression")
                        )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    accounting["unknowns"] = [f"Accounting unavailable: {type(exc).__name__}"]
            if submitted_id is not None and not terminated:
                accounting["complete"] = False
                accounting["full_cost_usd"] = None
                accounting.setdefault("unknowns", []).append("Executor termination unconfirmed")
            kept = [block.id for block in split_context(case.request.context)]
            context_bytes = len(case.request.context.encode("utf-8"))
            effective = case.request.context
            if arm == "treatment" and compression and compression.get("status") == "completed":
                try:
                    kept = compression["kept_ids"]
                    effective = assemble_context(
                        case.request.context, split_context(case.request.context), frozenset(kept)
                    )
                    context_bytes = len(effective.encode("utf-8"))
                except (KeyError, ValueError, TypeError):
                    violations.append("Invalid retained context metadata")
                    kept = []
            missing = facts_missing(case, kept, effective)
            result = view.result if view is not None else None
            if submitted_id is not None:
                violations.extend(
                    f"Model/executor mismatch in {component['task_id']}"
                    for component in snapshot["tasks"]
                    if component.get("executor") == executor
                    and component.get("status") == "completed"
                    and component.get("model") != configured.model
                )
            reasons = coding_verdict(result, check, accounting, violations, missing)
            if dispatch_error:
                reasons.append(f"Dispatch failed: {dispatch_error}")
            if arm == "treatment" and not (
                compression and compression.get("status") == "completed"
            ):
                reasons.append("Compression did not complete")
            rows.append(
                {
                    "id": case.id,
                    "split": case.split,
                    "repetition": repetition,
                    "arm": arm,
                    "executor": executor,
                    "model": configured.model,
                    "config_sha256": fingerprint,
                    "task_id": submitted_id,
                    "termination_confirmed": terminated,
                    "last_task_status": view.status.value if view is not None else None,
                    "cancellation_attempted": cancellation_attempted,
                    "cancellation_error": cancellation_error,
                    "repo": str(repo),
                    "ok": not reasons,
                    "reasons": reasons,
                    "check": check,
                    "accounting": accounting,
                    "compression": compression,
                    "missing_facts": missing,
                    "context_bytes": context_bytes,
                    "dispatch_wall_ms": dispatch_ms,
                    "wall_ms": (time.monotonic() - started) * 1000,
                }
            )
            if checkpoint is not None:
                checkpoint()
            if dispatch_error:
                raise RuntimeError("Dispatch failed; stopping without retry or fallback")
            if not terminated:
                raise RuntimeError("Executor termination unconfirmed; stopping further paid runs")
            if arm == "treatment" and not (
                compression and compression.get("status") == "completed"
            ):
                raise RuntimeError("Compression refused; stopping without retry or fallback")
    return rows


def report_document(
    lane: str,
    rows: list[dict[str, Any]],
    *,
    split: Literal["calibration", "holdout", "all"] = "all",
) -> dict[str, Any]:
    if split not in ("calibration", "holdout", "all"):
        raise ValueError("Unknown live semantic split")
    if split != "all" and lane != "live-semantic":
        raise ValueError("Split selection is only valid for live semantic evaluation")
    expected = {
        "offline": {"offline": 20, "calibration": 16, "holdout": 34},
        "live-semantic": {
            "offline": 0,
            "calibration": 10 if split != "holdout" else 0,
            "holdout": 20 if split != "calibration" else 0,
        },
        "paired": {"offline": 0, "calibration": 36, "holdout": 84},
    }[lane]
    expected_count = sum(expected.values())
    complete = len(rows) == expected_count and all(
        sum(row["split"] == name for row in rows) == count for name, count in expected.items()
    )
    if lane != "paired":
        complete = complete and len({row["id"] for row in rows}) == expected_count
    report = {
        "corpus_revision": 3,
        "corpus_sha256": CORPUS_SHA256,
        "lane": lane,
        "created_at": datetime.now(UTC).isoformat(),
        "split": split,
        "expected_rows": expected_count,
        "full_gate_expected_rows": 120 if lane == "paired" else 70 if lane == "offline" else 30,
        "complete": complete,
        "scope": "Selected semantic subset only, not the full gate" if split != "all" else lane,
        "threshold": selector.DELETE_SAFE_THRESHOLD,
        "rows": rows,
        "splits": {
            split: {
                "expected": expected[split],
                "attempted": sum(row["split"] == split for row in rows),
                "passed": sum(row["split"] == split and row["ok"] for row in rows),
            }
            for split in ("offline", "calibration", "holdout")
        },
        "ok": complete and all(row["ok"] for row in rows),
    }
    if lane == "paired":
        report["paired"] = paired_gate(rows)
        report["ok"] = complete and report["paired"]["ok"]
    else:
        steps = [step for row in rows for step in row.get("steps", row.get("ledger", []))]
        report["accounting"] = {
            "cost_usd": measurement([step.get("cost_usd") for step in steps]),
            "input_tokens": measurement([step.get("input_tokens") for step in steps], integer=True),
            "output_tokens": measurement(
                [step.get("output_tokens") for step in steps], integer=True
            ),
            "wall_ms": measurement([row.get("wall_ms") for row in rows]),
            "byok_bill_unknown": sum(step.get("is_byok") not in (False, 0) for step in steps),
        }
        report["accounting"]["full_cost_usd"] = (
            report["accounting"]["cost_usd"]["total"]
            if complete
            and not report["accounting"]["byok_bill_unknown"]
            and all(
                report["accounting"][key]["complete"]
                for key in ("cost_usd", "input_tokens", "output_tokens")
            )
            else None
        )
    report["full_gate_passed"] = split == "all" and report["ok"]
    return report


def report_target(path: Path) -> Path:
    target = path.expanduser().resolve()
    if not target.is_relative_to(Path("/tmp").resolve()):
        raise ValueError("Reports must be outside the repo under /tmp")
    return target


def write_report(path: Path, report: dict[str, Any]) -> None:
    target = report_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
