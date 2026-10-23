"""Shared context-enabled, manually initiated real Council for native DevUI.

Coordinator metadata and routing are anonymous; model prose may identify its author.
This is not a sandbox. Delivery references do not establish semantic provenance or
model acknowledgment. Live execution requires a manual, authenticated DevUI Run.
"""

# --- 1. Imports and shared request contracts ---

import sys

if sys.flags.optimize:
    raise RuntimeError("Run without Python optimization so validation stays enabled")

import asyncio
import json
import math
import os
from pathlib import Path
import re
import secrets
from hashlib import sha256
import marshal
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from application.council.run_archive import RunArchive, USAGE_FIELDS
from application.council import context_sources
from application.council import run_archive as archive_module
from application.council import attempts
from application.council.attempts import AttemptCapture, native_metadata

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
LOADED_CODE_SHA256 = sha256(marshal.dumps(sys._getframe().f_code)).hexdigest()


class SharedIntegrityError(RuntimeError):
    """A shared request destination/accounting invariant requires sibling cancellation."""


class CouncilDeadlineError(SharedIntegrityError):
    """The remaining run budget expired before a request could be dispatched."""


def loaded_identity(profile):
    sources = {"workflow.py": SOURCE_SHA256, "run_archive.py": archive_module.SOURCE_SHA256,
               "context_sources.py": context_sources.SOURCE_SHA256, "attempts.py": attempts.SOURCE_SHA256}
    for name in ("recovery", "devui", "devui_transport"):
        module = sys.modules.get("application.council."+name)
        if module and getattr(module,"SOURCE_SHA256",None): sources[name+".py"] = module.SOURCE_SHA256
    ui = sys.modules.get("application.council.devui")
    return dict(sources=sources, loaded_workflow_code_sha256=LOADED_CODE_SHA256,
                frontend_transport=getattr(ui,"LOADED_FRONTEND_TRANSPORT",None),
                profile_sha256=sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
                template_sha256=sha256(json.dumps(prompt_templates(), sort_keys=True).encode()).hexdigest())

# Disable OpenTelemetry and framework usage telemetry before framework imports.
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["AGENT_FRAMEWORK_USER_AGENT_DISABLED"] = "true"
os.environ["ENABLE_INSTRUMENTATION"] = "false"
os.environ["ENABLE_SENSITIVE_DATA"] = "false"

from agent_framework import (
    AgentResponse,
    BaseAgent,
    Message,
    WorkflowInvocationKwargs,
    WorkflowBuilder,
    WorkflowContext,
    executor,
    WorkflowRunResult,
    WorkflowRunState,
    WorkflowEvent,
)
from agent_framework.orchestrations import ConcurrentBuilder

STAGES = ("INITIAL", "CRITIQUE", "REVISION")
PROFILE_IDS = ("testing", "conversation", "frontier")
LIVE_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GEMINI_API_KEY"}
PEER_FIELDS = {"label", "proposal", "proposal_id", "critique_id", "delivery_id"}
FEEDBACK_FIELDS = {"proposal_id", "critique_id", "delivery_id", "critique", "reviewed", "own_label", "kind"}
STAGE_INSTRUCTIONS = {
    "INITIAL": "Analyze the user's research question independently. Provide your best concise conclusion, "
               "supporting reasoning, important assumptions, and uncertainties. Do not claim to have verified "
               "information you have not actually verified.",
    "CRITIQUE": "Examine the two anonymous peer proposals. Identify material mistakes, unsupported assumptions, "
                "contradictions, or important overlooked alternatives. Do not manufacture objections. If a proposal "
                "has no meaningful problem, explicitly say so. Do not attempt to identify the authors.",
    "REVISION": "Reconsider your own original proposal using the combined reviews of two proposals. Each review "
                "includes its local label mapping and identifies your own proposal; other comments may concern "
                "the other reviewed proposal. Correct "
                "mistakes where justified, reject unsupported objections, and preserve defensible disagreements. "
                "Give your final position and briefly explain material changes.",
}
SYSTEM_SUFFIX = "\nPeer labels are anonymous coordinator metadata; ordinary Markdown and code are welcome."


def prompt_templates():
    return {stage:text+SYSTEM_SUFFIX for stage,text in STAGE_INSTRUCTIONS.items()}


def check_bytes(actual, limit, boundary):
    if actual > limit:
        error = AssertionError(f"{boundary} byte limit: actual={actual}; configured={limit} UTF-8 bytes")
        error.boundary, error.actual_bytes, error.limit_bytes = boundary, actual, limit
        raise error


def bounded_text(text, limit, *, boundary="response"):
    assert isinstance(text, str) and text.strip(), "Expected nonempty text"
    check_bytes(len(text.encode("utf-8")), limit, boundary)
    return text


# --- 2. Blind input preparation and validation ---

def revision_position(text):
    """Optional explicit declaration only; never infer a position from the argument."""
    declarations = {value.lower() for value in re.findall(r"(?im)^[ \t]*Position:[ \t]*(revise|preserve)[ \t]*\r?$", text)}
    return next(iter(declarations)).lower() if len(declarations) == 1 else "unspecified"


def parse_stage_input(text: str, stage: str, fields: set[str], *, question) -> dict:
    """Enforce an exact anonymous input envelope for each later stage."""
    assert isinstance(text, str), f"{stage} input must be serialized text"
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"Malformed {stage} input") from exc
    assert isinstance(payload, dict) and set(payload) == {"stage", "question"} | fields, (
        f"Unexpected {stage} input fields; no extra history or identity metadata allowed"
    )
    assert payload["stage"] == stage and payload["question"] == question, (
        f"{stage} stage or original question changed"
    )
    return payload


def validate_critique_input(
    text: str, own_proposal: str, *, question, origins=None, reviewer=None
) -> dict:
    """Accept only two anonymous INITIAL bodies and explicit opaque references."""
    payload = parse_stage_input(text, "CRITIQUE", {"peers"}, question=question)
    peers = payload["peers"]
    assert isinstance(peers, list) and len(peers) == 2, "Expected exactly two peer proposals"
    for peer in peers:
        assert isinstance(peer, dict) and set(peer) == PEER_FIELDS, (
            "Unexpected peer fields; only anonymous proposal data and references allowed"
        )
        assert isinstance(peer["proposal"], str), "Peer proposal must be plain INITIAL text"
        assert peer["proposal_id"] in {"P1", "P2", "P3"}, "Invalid proposal identifier"
        assert peer["critique_id"] in {f"C{i}" for i in range(1, 7)}, "Invalid critique identifier"
        assert peer["delivery_id"] == "O" + peer["critique_id"][1:], "Invalid delivery identifier"
    for field in ("proposal_id", "critique_id", "delivery_id"):
        assert len({peer[field] for peer in peers}) == 2, "Duplicate or ambiguous critique references"
    assert [peer["label"] for peer in peers] == ["Proposal A", "Proposal B"], (
        "Invalid anonymous proposal labels"
    )
    proposals = [peer["proposal"] for peer in peers]
    assert origins is not None and reviewer in LIVE_KEYS, "Accepted origins and reviewer are required"
    expected = {pid: body for name, pid, body in origins if name != reviewer}
    assert len(expected) == 2 and {p["proposal_id"]: p["proposal"] for p in peers} == expected, (
        "Peers must contain only the other accepted INITIAL proposals; no own proposal or history"
    )
    return payload


def prepare_critique_inputs(
    initial_messages: list[Message], *, question, response_bytes
) -> tuple[tuple, dict[str, str], tuple]:
    # 2.1 Validate the whole set before creating any CRITIQUE input; freeze strings, not Messages.
    accepted_messages = validate_messages(initial_messages, question=question, response_bytes=response_bytes)
    names = LIVE_KEYS
    proposal_ids = ["P1", "P2", "P3"]
    critique_ids = [f"C{i}" for i in range(1, 7)]
    secrets.SystemRandom().shuffle(proposal_ids)
    secrets.SystemRandom().shuffle(critique_ids)
    accepted = tuple(
        (name, proposal_ids.pop(), json.loads(accepted_messages[name].text)["response"])
        for name in names
    )
    # 2.2 Identity keys stay in coordinator metadata. Labels are local to each request.
    inputs = {}
    provenance = []
    for reviewer, _, own_proposal in accepted:
        peers = [(pid, proposal) for name, pid, proposal in accepted if name != reviewer]
        secrets.SystemRandom().shuffle(peers)
        peer_payloads = []
        for label, (pid, proposal) in zip(("Proposal A", "Proposal B"), peers, strict=True):
            cid = critique_ids.pop()
            oid = "O" + cid[1:]
            peer_payloads.append(dict(label=label, proposal=proposal, proposal_id=pid,
                                      critique_id=cid, delivery_id=oid))
            provenance.append((cid, reviewer, pid, oid))
        text = json.dumps({
            "stage": "CRITIQUE",
            "question": question,
            "peers": peer_payloads,
        }, sort_keys=True)
        validate_critique_input(text, own_proposal, question=question,
                                origins=accepted, reviewer=reviewer)
        inputs[reviewer] = text
    return accepted, inputs, tuple(provenance)


def validate_revision_input(text: str, own_proposal: str, *, question) -> dict:
    payload = parse_stage_input(text, "REVISION", {"origin", "feedback"}, question=question)
    origin = payload["origin"]
    assert isinstance(origin, dict) and set(origin) == {"proposal_id", "proposal"}, "Invalid origin"
    assert origin["proposal"] == own_proposal, "Originating proposal changed"
    assert origin["proposal_id"] in {"P1", "P2", "P3"}, "Invalid origin identifier"
    feedback = payload["feedback"]
    assert isinstance(feedback, list) and len(feedback) == 2, "Expected two relevant critiques"
    for item in feedback:
        assert isinstance(item, dict) and set(item) == FEEDBACK_FIELDS, "Invalid feedback fields"
        assert item["proposal_id"] == origin["proposal_id"], "Critique routed to wrong proposal"
        assert item["critique_id"] in {f"C{i}" for i in range(1, 7)}, "Invalid critique identifier"
        assert item["delivery_id"] == "O" + item["critique_id"][1:], "Invalid delivery identifier"
        assert isinstance(item["critique"], str) and item["critique"], "Missing critique content"
        assert item["kind"] == "combined_unsegmented_review", "Invalid feedback kind"
        reviewed = item["reviewed"]
        assert isinstance(reviewed, list) and len(reviewed) == 2 and all(
            isinstance(row, dict) and set(row) == {"label", "proposal_id"} for row in reviewed
        ), "Invalid reviewed-label mapping"
        assert [row["label"] for row in reviewed] == ["Proposal A", "Proposal B"] and len({
            row["proposal_id"] for row in reviewed}) == 2 and all(
            row["proposal_id"] in {"P1", "P2", "P3"} for row in reviewed), "Invalid reviewed-label mapping"
        assert {"label": item["own_label"], "proposal_id": origin["proposal_id"]} in reviewed, (
            "Wrong own-proposal label in combined review"
        )
    for field in ("critique_id", "delivery_id"):
        assert len({item[field] for item in feedback}) == 2, "Duplicate or ambiguous critique references"
    return payload


def prepare_revision_inputs(accepted: dict, *, question) -> dict[str, str]:
    """Route frozen accepted reviews using coordinator identifiers, never proposal text."""
    assert all(stage in accepted for stage in STAGES[:2]), "Stage prerequisites not accepted"
    proposals, routes = accepted["proposals"], accepted["provenance"]
    names = LIVE_KEYS
    owners = {pid: name for name, pid, _ in proposals}
    assert len(owners) == 3 and len(set(owners.values())) == 3, "Ambiguous originating proposals"
    assert len(routes) == 6 and all(
        len({row[index] for row in routes}) == 6 for index in (0, 3)
    ), "Duplicate or ambiguous critique references"
    assert {(reviewer, pid) for _, reviewer, pid, _ in routes} == {
        (reviewer, pid) for reviewer in names for pid, owner in owners.items()
        if reviewer != owner
    }, "Invalid reviewer or target provenance"
    registry = {row[0]: row for row in routes}
    critique_inputs = dict(accepted["critique_inputs"])
    messages = [Message("assistant", [text], author_name=name) for name, text in accepted["CRITIQUE"]]
    critiques = validate_messages(messages, "CRITIQUE", critique_inputs, question=question,
                                  origins=proposals, response_bytes=accepted["execution"]["response_bytes"])
    feedback = {name: [] for name in names}
    seen = set()
    for reviewer, message in critiques.items():
        payload = json.loads(message.text)
        reviewed = [{key: row[key] for key in ("label", "proposal_id")} for row in payload["reviews"]]
        for review in payload["reviews"]:
            cid, pid, oid = (review[key] for key in ("critique_id", "proposal_id", "delivery_id"))
            assert cid not in seen and registry.get(cid) == (cid, reviewer, pid, oid), (
                "Critique reviewer/target provenance mismatch"
            )
            seen.add(cid)
            feedback[owners[pid]].append(dict(
                proposal_id=pid, critique_id=cid, delivery_id=oid,
                critique=payload["response"], reviewed=reviewed, own_label=review["label"],
                kind="combined_unsegmented_review"))
    assert seen == set(registry), "Missing required critique"
    inputs = {}
    initial = dict(accepted["INITIAL"])
    for name, pid, proposal in proposals:
        assert json.loads(initial[name])["response"] == proposal, "Frozen INITIAL proposal changed"
        text = json.dumps({
            "stage": "REVISION", "question": question,
            "origin": {"proposal_id": pid, "proposal": proposal},
            "feedback": sorted(feedback[name], key=lambda item: item["critique_id"]),
        }, sort_keys=True)
        validate_revision_input(text, proposal, question=question)
        inputs[name] = text
    return inputs


# --- 3. Shared participant execution and bounded provider calls ---

class LiveParticipant(BaseAgent):
    """Fresh stage input, one real provider request, and coordinator-owned attribution."""

    def __init__(self, name, events, stage, *, question, origins, context, accepted, recorder=None):
        super().__init__(id=name, name=name)
        assert stage in STAGES, "Unsupported stage"
        self._events, self._stage, self._question = events, stage, question
        self._origins, self._recorder = origins, recorder
        self._context, self._accepted = context, accepted

    async def run(
        self,
        messages: list[Message] | None = None,
        *,
        stream: bool = False,
        session=None,
        function_invocation_kwargs=None,
        client_kwargs=None,
    ) -> AgentResponse:
        assert not stream, "Council participants return one complete stage response"
        assert isinstance(messages, list) and len(messages) == 1, (
            f"{self.id}: expected only the original user message"
        )
        message = messages[0]
        assert isinstance(message, Message) and message.role == "user", (
            f"{self.id}: unexpected message type or role"
        )
        assert len(message.contents) == 1 and message.contents[0].type == "text", (
            f"{self.id}: unexpected input content"
        )
        assert message.text == self._question, f"{self.id}: original prompt changed"
        assert not function_invocation_kwargs, "Unexpected tool arguments"
        if self._stage == "INITIAL":
            assert not client_kwargs, "INITIAL received unexpected peer context"
            request = None
        else:
            key = self._stage.lower() + "_input"
            assert isinstance(client_kwargs, dict) and set(client_kwargs) == {key}, (
                "Expected only this participant's stage input, without routing metadata"
            )
            validator = validate_critique_input if self._stage == "CRITIQUE" else validate_revision_input
            own = next(body for name, _, body in self._origins if name == self.id)
            request = validator(
                client_kwargs[key], own, question=self._question,
                **({"origins": self._origins, "reviewer": self.id} if self._stage == "CRITIQUE" else {}),
            )
        self._events.append(("start", self.id))
        self._archive_request = request
        if self._recorder:
            self._recorder.provider("provider_preparing", self._stage, self.id, request=request)
        try:
            payload = await self.make_payload(request, message.text)
        except BaseException as exc:
            already_accepted = self.id in self._accepted.get("individual", {}).get(self._stage, {})
            if already_accepted:
                # Validation/acceptance was fsynced before resource cleanup. A
                # Stop during cleanup must propagate without erasing that result.
                if self._recorder:
                    self._recorder.provider("cleanup_warning",self._stage,self.id,error=exc)
                    self._recorder.provider("accepted_result_local_control_interrupted",self._stage,self.id,
                        request=request, cancellation_initiator=(getattr(self._recorder,"cancellation_scope",None) or {}).get("initiator","stop_or_client_disconnect_or_shutdown"),
                        remote_outcome="complete response received; remote billing unverified")
                raise
            if self._recorder:
                self._recorder.provider("provider_rejected", self._stage, self.id, request=request, error=exc,
                    cancellation_initiator=(self._context.get("cancellation_initiator") or (
                        "run_deadline" if asyncio.get_running_loop().time() >= self._accepted["deadline"] else
                        "stage_deadline" if asyncio.get_running_loop().time() >= self._context.get("stage_deadline", float("inf")) else
                        (getattr(self._recorder, "cancellation_scope", None) or {}).get("initiator", "stop_or_client_disconnect_or_shutdown"))) if isinstance(exc, asyncio.CancelledError) else None,
                    remote_outcome="unknown" if isinstance(exc, asyncio.CancelledError) else None,
                    **(self._capture.summary() if isinstance(exc, asyncio.CancelledError) and hasattr(self, "_capture") else {}))
            if not isinstance(exc, Exception) or isinstance(exc, SharedIntegrityError): raise
            # Native ConcurrentBuilder still owns scheduling/aggregation. An ordinary
            # request failure is a collected outcome until all independent siblings
            # finish; it must not escape into its cancel-siblings-on-error helper.
            self._accepted.setdefault("failures", {}).setdefault(self._stage, {})[self.id] = attempts.sanitize(
                str(exc), self._context["credentials"].values())
            return AgentResponse(agent_id=self.id, messages=[Message("assistant", [json.dumps(
                dict(participant=self.id, stage=self._stage, failed=True))], author_name=self.id)])
        response = AgentResponse(
            agent_id=self.id,
            messages=[Message(
                "assistant", [json.dumps(payload, sort_keys=True)], author_name=self.id
            )],
        )
        self._events.append(("finish", self.id))
        self.accept_payload(payload)
        return response

    def accept_payload(self, payload):
        text = json.dumps(payload,sort_keys=True)
        outputs = self._accepted.setdefault("individual",{}).setdefault(self._stage,{})
        if self.id in outputs:
            assert outputs[self.id] == text, "Accepted response changed"
            return
        outputs[self.id] = text
        if self._recorder:
            self._recorder.provider("provider_accepted",self._stage,self.id,request=self._archive_request,
                output_status="accepted",position=payload.get("position"),response_sha256=sha256(payload["response"].encode()).hexdigest())

    async def call_model(self, messages):
        import httpx as http
        if self.id != "google":
            import httpx2 as http
        settings = self._context["profile"]["providers"][self.id]
        key = self._context["credentials"][self.id]
        execution = self._context["execution"]
        request_seconds = execution["request_seconds"]
        max_tokens = settings["max_tokens"]
        stream_response = settings.get("stream", False)
        hosts = {"openai": "api.openai.com", "anthropic": "api.anthropic.com",
                 "google": "generativelanguage.googleapis.com"}
        sent = False
        capture = AttemptCapture(self._recorder, self._stage, self.id, self._archive_request, self._context["credentials"].values())
        self._capture = capture

        async def before_request(request):
            nonlocal sent
            tag = self._stage + ":" + self.id
            if (request.method != "POST" or request.url.scheme != "https" or request.url.host != hosts[self.id]
                    or sent or tag in self._accepted["dispatched"] or len(self._accepted["dispatched"]) >= 9):
                self._context["cancellation_initiator"] = "shared_integrity_failure"
                raise SharedIntegrityError("Unexpected destination, method, or duplicate provider request blocked")
            if asyncio.get_running_loop().time() >= self._accepted["deadline"]:
                self._context["cancellation_initiator"] = "run_deadline"
                raise CouncilDeadlineError("Council deadline exceeded before dispatch")
            self._accepted["dispatched"].append(tag)
            sent = True
            if self._recorder:
                reference = self._context.get("reference_context")
                self._recorder.provider("provider_dispatched", self._stage, self.id,
                                        request=self._archive_request, execution="live",
                                        input_text_bytes=sum(len(m.text.encode("utf-8")) for m in messages),
                                        requested_output_tokens=max_tokens,
                                        **(dict(context_source_ids=self._context["context_source_ids"],
                                                context_bytes=len(reference.text.encode("utf-8")))
                                           if reference is not None else {}))

        async def after_response(response):
            capture.request_id = response.headers.get({"openai":"x-request-id", "anthropic":"request-id", "google":"x-goog-request-id"}[self.id])
            capture.emit("provider_headers", observation="HTTP response headers received; not inference acceptance",
                         http_status=response.status_code,
                         response_headers_ms=capture.ms(),
                         request_ids={name: response.headers[name] for name in
                                      ("x-request-id", "request-id", "x-goog-request-id") if name in response.headers})

        transport = self._context.get("transports", {}).get(self.id)
        sdk = None
        stream_complete = True
        failure = "provider_or_transport_error"
        http_client = http.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                       timeout=request_seconds,
                                       event_hooks={"request": [before_request], "response": [after_response]})
        try:
            options = {key: value for key, value in settings.items() if key != "stream"}
            if self.id == "openai":
                from openai import AsyncOpenAI
                from agent_framework_openai import RawOpenAIChatClient
                sdk = AsyncOpenAI(api_key=key, admin_api_key="", webhook_secret="",
                                  base_url="https://api.openai.com/v1", max_retries=0,
                                  timeout=request_seconds, http_client=http_client)
                client = RawOpenAIChatClient(model=settings["model"], api_key=key, async_client=sdk)
                options.update(store=False, background=False)
            elif self.id == "anthropic":
                from anthropic import AsyncAnthropic
                from agent_framework_anthropic import RawAnthropicClient
                os.environ["ANTHROPIC_CONFIG_DIR"] = os.devnull
                sdk = AsyncAnthropic(api_key=key, webhook_key="", max_retries=0, open_telemetry=False,
                                     base_url="https://api.anthropic.com", timeout=request_seconds,
                                     http_client=http_client)
                client = RawAnthropicClient(model=settings["model"], api_key=key, anthropic_client=sdk)
            else:
                from google import genai
                from google.genai import types
                from agent_framework_gemini import RawGeminiChatClient
                sdk = genai.Client(api_key=key, vertexai=False, enterprise=False, http_options=types.HttpOptions(
                    base_url="https://generativelanguage.googleapis.com", api_version="v1beta",
                    timeout=max(1, round(request_seconds * 1000)),
                    retry_options=types.HttpRetryOptions(attempts=1), httpx_async_client=http_client,
                ))
                client = RawGeminiChatClient(model=settings["model"], api_key=key, client=sdk)
                options.update(automatic_function_calling={"disable": True}, automatic_continuation=False)
            async with asyncio.timeout(request_seconds):
                if stream_response and self.id == "anthropic":
                    # Reuse the installed request builder and SDK Message accumulator. The
                    # framework's update stream drops message_stop and maps pause_turn to stop.
                    # Retain the native terminal event and stop reason for the existing guards.
                    stream_complete = False
                    async with sdk.beta.messages.stream(**client._prepare_options(messages, options)) as events:
                        capture.request_id = events.request_id or capture.request_id
                        capture.emit("provider_receiving", observation="SDK stream opened",
                                     provider_request_id=events.request_id, http_status=events.response.status_code)
                        async for event in events:
                            capture.stream_event(event)
                            if event.type == "message_stop": stream_complete = True
                        message = await events.get_final_message()
                    response = client._process_message(message, options)
                elif stream_response:
                    response = await client.get_response(messages, stream=True, options=options).get_final_response()
                else:
                    response = await client.get_response(messages, stream=False, options=options)
            contents = [c for msg in response.messages for c in msg.contents]
            # ChatResponse.text strips outer whitespace. Preserve its message/block separators
            # while retaining every SDK-exposed ordinary text block, including whitespace.
            ordinary_text = "\n".join(msg.text for msg in response.messages)
            raw = response.raw_representation
            blocked = getattr(getattr(raw, "prompt_feedback", None), "block_reason", None)
            refusal = any(c.additional_properties.get("model_output_kind") == "refusal"
                          or c.additional_properties.get("core", {}).get("model_output_kind") == "refusal"
                          for c in contents)
            try:
                if self._recorder:
                    ordinary = [c.text for c in contents if c.type == "text"]
                    candidates = getattr(raw, "candidates", None) or []
                    provider_finish = getattr(candidates[0], "finish_reason", None) if candidates else None
                    self._recorder.provider("provider_completed", self._stage, self.id,
                        request=self._archive_request, text=ordinary if not stream_response else None, execution="live",
                        text_recorded_incrementally=stream_response,
                        text_representation="ordered SDK ordinary text blocks; transcript separators are presentation",
                        text_block_count=len(ordinary), combined_text_bytes=len(ordinary_text.encode("utf-8")),
                        response_id=response.response_id or getattr(raw, "response_id", None),
                        response_model=response.model, provider_request_id=getattr(raw, "_request_id", None) or capture.request_id,
                        finish_reason=response.finish_reason, raw_stop_reason=getattr(raw, "stop_reason", None),
                        raw_status=getattr(raw, "status", None), continuation_present=response.continuation_token is not None,
                        provider_finish_reason=getattr(provider_finish, "name", provider_finish),
                        refusal=refusal, refusal_text_tagged=refusal,
                        native_refusal=getattr(raw, "stop_reason", None) == "refusal" or refusal,
                        normalized_content_filter=response.finish_reason == "content_filter",
                        safety_stop=getattr(blocked, "name", blocked),
                        requested_output_tokens=max_tokens,
                        response_text_bytes=sum(len(part.encode("utf-8")) for part in ordinary),
                        usage={key: (response.usage_details or {}).get(key) for key in USAGE_FIELDS},
                        usage_source="provider_reported", usage_details=dict(response.usage_details or {}),
                        **native_metadata(raw, self.id, self._context["credentials"].values()),
                        **(capture.summary() if stream_response else dict(
                            first_stream_activity_ms=None, first_ordinary_text_ms=capture.ms(),
                            partial_text_availability="nonstreaming; no ordinary text exposed before complete response",
                            usage_status="final")))
            except Exception as exc:
                self._recorder.recording_error("response metadata", exc)
            failure = "unexpected_content_or_tool_call"
            assert contents and all(c.type in ("text", "text_reasoning") for c in contents), "Tool/nontext output rejected"
            failure = "unexpected_message_role"
            assert all(msg.role == "assistant" for msg in response.messages), "Unexpected output role"
            failure = "refusal_or_safety_block"
            assert not any(c.additional_properties.get("model_output_kind") == "refusal"
                           or c.additional_properties.get("core", {}).get("model_output_kind") == "refusal"
                           for c in contents), "Provider refusal"
            raw = response.raw_representation
            blocked = getattr(getattr(raw, "prompt_feedback", None), "block_reason", None)
            assert getattr(blocked, "name", blocked) in (None, "BLOCK_REASON_UNSPECIFIED", "BLOCKED_REASON_UNSPECIFIED"), "Safety block"
            failure = "incomplete_or_refused_output"
            assert stream_complete, "Incomplete provider event stream"
            assert response.finish_reason == "stop" and response.continuation_token is None, "Incomplete/refused output"
            assert getattr(raw, "status", "completed") == "completed", "Incomplete provider response"
            assert getattr(raw, "stop_reason", "end_turn") in ("end_turn", "stop_sequence"), "Incomplete/refused output"
            failure = "empty_or_oversized_output"
            text = bounded_text(ordinary_text, execution["response_bytes"], boundary=f"{self._stage}/{self.id} response")
            self._accepted["usage"].append(dict(
                stage=self._stage, provider=self.id, model=response.model,
                response_id=response.response_id or getattr(raw, "response_id", None),
                usage=dict(response.usage_details) if response.usage_details is not None else None,
            ))
            self.accept_payload(self.payload_from_text(self._archive_request,self._question,text))
            return text
        except Exception as exc:
            # SDK messages/headers may contain secrets. Never forward their contents or chain.
            if self._recorder:
                category = ("output_token_exhaustion" if failure == "incomplete_or_refused_output"
                            and response.finish_reason == "length" else failure)
                category = self._context.get("cancellation_initiator") or category
                self._recorder.provider("provider_rejected", self._stage, self.id,
                                        request=self._archive_request, error=exc, classification=category,
                                        local_validation_reason=failure,
                                        cancellation_initiator=self._context.get("cancellation_initiator"),
                                        **(capture.summary() if stream_response else {}))
            if failure == "incomplete_or_refused_output":
                completion = {
                    "finish_reason": response.finish_reason,
                    "raw_stop_reason": getattr(raw, "stop_reason", None),
                    "raw_status": getattr(raw, "status", None),
                    "continuation_present": response.continuation_token is not None,
                    "output_tokens": (response.usage_details or {}).get("output_token_count"),
                }
                failure += " (" + ", ".join(
                    f"{name}={value if isinstance(value, (str, int, bool)) else 'unavailable'}"
                    for name, value in completion.items()
                ) + ")"
            cause = getattr(exc, "inner_exception", None) or exc.__cause__ or exc
            seen = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, SharedIntegrityError):
                    if isinstance(cause, CouncilDeadlineError):
                        raise CouncilDeadlineError(f"{self._stage}/{self.id}: run_deadline; Council incomplete") from None
                    self._context["cancellation_initiator"] = "shared_integrity_failure"
                    raise SharedIntegrityError(f"{self._stage}/{self.id}: provider_or_transport_error; shared integrity failure") from None
                cause = getattr(cause, "inner_exception", None) or cause.__cause__
            cause = getattr(exc, "inner_exception", None) or exc.__cause__ or exc
            if isinstance(getattr(exc, "actual_bytes", None), int):
                failure += f" ({exc.boundary}: actual={exc.actual_bytes}; configured={exc.limit_bytes} UTF-8 bytes)"
            if isinstance(exc, TimeoutError) or "Timeout" in type(cause).__name__:
                failure = "request_timeout; remote billing may still occur"
            raise RuntimeError(f"{self._stage}/{self.id}: {failure}; Council incomplete; no retry") from None
        finally:
            capture.flush()
            # Close each resource once; ordinary cleanup failure cannot replace the request outcome.
            cleanup = ([(lambda: sdk.aio.aclose(), True), (lambda: sdk.close(), False)] if self.id == "google" else
                       [(lambda: sdk.close(), True)]) if sdk is not None else []
            for close, asynchronous in [*cleanup, (lambda: http_client.aclose(), True)]:
                try:
                    if asynchronous: await close()
                    else: close()
                except Exception as exc:
                    if self._recorder:
                        self._recorder.provider("cleanup_warning", self._stage, self.id, error=exc)

    async def make_payload(self, request, question):
        instruction = self._context.get("templates", prompt_templates())[self._stage]
        messages = [Message("system", [instruction]), Message("user", [question])]
        if request is not None:
            messages.append(Message("user", [json.dumps(request, ensure_ascii=False, sort_keys=True)]))
        reference_text = self._context["reference_context"].text if "reference_context" in self._context else None
        if reference_text is not None:
            messages.append(Message("user", [reference_text]))
        request_bytes = sum(len(msg.text.encode("utf-8")) for msg in messages)
        check_bytes(request_bytes, self._context["execution"]["request_text_bytes"], f"{self._stage}/{self.id} combined request")
        text = await self.call_model(messages)
        return self.payload_from_text(request,question,text)

    def payload_from_text(self, request, question, text):
        payload = dict(participant=self.id, stage=self._stage, prompt=question, response=text)
        if self._stage == "CRITIQUE":
            payload["reviews"] = [{key: value for key, value in peer.items() if key != "proposal"}
                                  for peer in request["peers"]]
        elif self._stage == "REVISION":
            payload.update(position=revision_position(text), origin_proposal_id=request["origin"]["proposal_id"],
                           feedback_references=[{k: item[k] for k in ("critique_id", "delivery_id", "proposal_id")}
                                                for item in request["feedback"]])
        return payload


# --- 4. Shared stage acceptance checks ---

def validate_messages(
    messages: list[Message], stage="INITIAL", stage_inputs=None, *, question,
    origins=None, response_bytes
) -> dict[str, Message]:
    """Reject missing, duplicate, malformed, or incorrectly attributed outputs."""
    assert isinstance(messages, list) and len(messages) == 3, "Expected three outputs"
    by_participant: dict[str, Message] = {}
    names = LIVE_KEYS
    for message in messages:
        assert isinstance(message, Message) and message.role == "assistant", (
            "Malformed participant message"
        )
        identity = message.author_name
        assert identity in names, f"Unknown participant identity: {identity!r}"
        assert identity not in by_participant, f"Duplicate participant identity: {identity}"
        assert len(message.contents) == 1 and message.contents[0].type == "text", (
            f"Malformed content from {identity}"
        )
        try:
            payload = json.loads(message.text)
        except ValueError as exc:
            raise AssertionError("Malformed structured output") from exc
        assert isinstance(payload, dict), "Malformed structured output"
        expected = {"participant": identity, "stage": stage, "prompt": question}
        if stage == "INITIAL":
            proposal = payload
            assert isinstance(proposal, dict) and isinstance(proposal.get("response"), str), (
                "Malformed INITIAL proposal"
            )
            expected["response"] = bounded_text(proposal["response"], response_bytes)
        elif stage == "CRITIQUE":
            assert stage_inputs is not None, "Invalid output stage"
            request = validate_critique_input(
                stage_inputs[identity],
                next(body for name, _, body in origins if name == identity),
                question=question, origins=origins, reviewer=identity,
            )
            expected["response"] = bounded_text(payload.get("response"), response_bytes)
            expected["reviews"] = [{key: value for key, value in peer.items() if key != "proposal"}
                                   for peer in request["peers"]]
        else:
            assert stage == "REVISION" and stage_inputs is not None, "Invalid output stage"
            own = next(body for name, _, body in origins if name == identity)
            request = validate_revision_input(stage_inputs[identity], own, question=question)
            response = bounded_text(payload.get("response"), response_bytes)
            expected.update(position=revision_position(response), response=response)
            expected["origin_proposal_id"] = request["origin"]["proposal_id"]
            expected["feedback_references"] = [
                {key: item[key] for key in ("critique_id", "delivery_id", "proposal_id")}
                for item in request["feedback"]
            ]
            revision = payload
            assert isinstance(revision, dict) and revision.get("participant") == identity, (
                f"Malformed, changed, or misattributed output: {identity}"
            )
            assert revision.get("feedback_references") == expected["feedback_references"], (
                "Revision has missing, duplicate, or unrelated feedback references"
            )
        expected = json.dumps(expected, sort_keys=True)
        assert message.text == expected, f"Malformed, changed, or misattributed output: {identity}"
        by_participant[identity] = message
    assert set(by_participant) == set(names), "Incomplete participant identities"
    assert len({message.text for message in messages}) == 3, "Outputs are not distinct"
    return by_participant


def validate_stage(
    result, events, stage="INITIAL", stage_inputs=None, *, question,
    origins=None, response_bytes
) -> dict[str, Message]:
    # Attribute every invocation and require complete aggregation; accept observed scheduling.
    names = LIVE_KEYS
    assert len(events) == 6, "Missing or repeated participant execution"
    for phase in ("start", "finish"):
        observed = [name for event, name in events if event == phase]
        assert len(observed) == 3 and set(observed) == set(names), (
            f"Missing or repeated participant execution: {phase}"
        )
    for name in names:
        assert events.index(("start", name)) < events.index(("finish", name)), "Participant finished before its own start"
    assert isinstance(result, WorkflowRunResult), "Unexpected workflow result type"
    assert result.get_final_state() == WorkflowRunState.IDLE, "Workflow did not complete"
    assert not result.get_request_info_events(), "Unexpected external input request"

    individual = result.get_intermediate_outputs()
    assert len(individual) == 3, "Missing or extra individual participant responses"
    seen: set[str] = set()
    for response in individual:
        assert isinstance(response, AgentResponse), "Malformed individual response"
        assert response.agent_id in names, "Incorrect response agent identity"
        assert response.agent_id not in seen, "Duplicate response agent identity"
        assert len(response.messages) == 1, "Expected one message per participant"
        assert response.messages[0].author_name == response.agent_id, (
            "Response agent identity and message author disagree"
        )
        seen.add(response.agent_id)
    originals = validate_messages(
        [response.messages[0] for response in individual], stage, stage_inputs, question=question,
        origins=origins, response_bytes=response_bytes
    )

    outputs = result.get_outputs()
    assert len(outputs) == 1 and isinstance(outputs[0], AgentResponse), (
        "Expected one complete aggregated AgentResponse"
    )
    assert [event.executor_id for event in result if event.type == "output"] == ["aggregator"], (
        "Final output did not come from the framework aggregator"
    )
    aggregated = validate_messages(outputs[0].messages, stage, stage_inputs, question=question,
                                   origins=origins, response_bytes=response_bytes)
    assert {name: msg.to_dict() for name, msg in aggregated.items()} == {
        name: msg.to_dict() for name, msg in originals.items()
    }, "Aggregation changed participant output"
    return aggregated


# --- 5. Actual concurrent orchestration ---

async def run_stage(stage, stage_inputs=None, *, accepted, question, live_context, recorder=None, providers=None):
    if recorder:
        recorder.event("stage_started", stage=stage, required_inputs_present=(
            stage == "INITIAL" and stage_inputs is None or
            isinstance(stage_inputs, dict) and set(stage_inputs) == set(LIVE_KEYS)),
            prerequisites_accepted=stage in STAGES and isinstance(accepted, dict) and all(
                prior in accepted for prior in STAGES[:STAGES.index(stage)]))
    assert stage in STAGES and isinstance(accepted, dict), "Invalid coordinator stage state"
    assert all(previous in accepted for previous in STAGES[:STAGES.index(stage)]), "Stage prerequisites not accepted"
    assert stage not in accepted, "Stage already accepted"
    names = tuple(LIVE_KEYS) if providers is None else tuple(providers)
    assert names and len(set(names)) == len(names) and set(names) <= set(LIVE_KEYS), "Invalid participant selection"
    assert providers is None or stage == "REVISION", "Selective execution is revision-only"
    origins = accepted.get("proposals")
    invocation = None
    if stage != "INITIAL":
        assert stage_inputs is not None and set(stage_inputs) == set(LIVE_KEYS), "Incomplete stage input routing"
        expected_inputs = (dict(accepted["critique_inputs"]) if stage == "CRITIQUE" else
                           prepare_revision_inputs(accepted, question=question))
        assert stage_inputs == expected_inputs, "Stage inputs disagree with accepted provenance"
        validator = validate_critique_input if stage == "CRITIQUE" else validate_revision_input
        for name, text in stage_inputs.items():
            own = next(body for owner, _, body in origins if owner == name)
            validator(text, own, question=question,
                      **({"origins": origins, "reviewer": name} if stage == "CRITIQUE" else {}))
        invocation = WorkflowInvocationKwargs(executor_kwargs={
            name: {stage.lower() + "_input": stage_inputs[name]} for name in names})
    else:
        assert stage_inputs is None, "Invalid INITIAL context"
    assert live_context is not None, "Provider execution context is required"
    remaining = accepted["deadline"] - asyncio.get_running_loop().time()
    assert remaining > 0, "Overall Council deadline exceeded"
    events = []
    participants = [LiveParticipant(name, events, stage, question=question, origins=origins,
                                    context=live_context, accepted=accepted, recorder=recorder) for name in names]
    live_context["stage_deadline"] = asyncio.get_running_loop().time() + min(live_context["execution"]["stage_seconds"], remaining)
    async with asyncio.timeout(min(live_context["execution"]["stage_seconds"], remaining)):
        # Installed ConcurrentBuilder requires a fan-out of at least two targets.
        # A one-missing-revision action uses MAF's ordinary single AgentExecutor
        # graph; no placeholder participants or repeated accepted requests.
        graph = (WorkflowBuilder(start_executor=participants[0]).build() if len(participants) == 1 else
                 ConcurrentBuilder(participants=participants, intermediate_output_from="all").build())
        result = await graph.run(question, include_status_events=True, client_kwargs=invocation)
    failures = accepted.get("failures", {}).get(stage, {})
    if failures:
        if recorder:
            recorder.event("stage_incomplete", stage=stage, accepted_providers=list(accepted.get("individual", {}).get(stage, {})),
                           failed_providers=list(failures), dependent_stage_blocked=True)
        raise RuntimeError("; ".join(failures.values()))
    if providers is None:
        aggregated = validate_stage(result, events, stage, stage_inputs, question=question,
                                    origins=origins, response_bytes=accepted["execution"]["response_bytes"])
    else:
        assert result.get_final_state() == WorkflowRunState.IDLE and not result.get_request_info_events()
        originals = result.get_outputs() if len(names) == 1 else result.get_intermediate_outputs()
        assert len(originals) == len(names) and {r.agent_id for r in originals} == set(names)
        assert all(r.messages[0].text == accepted["individual"][stage][r.agent_id] for r in originals)
        aggregated = validate_messages([Message("assistant", [text], author_name=name) for name, text in
                                       accepted["individual"][stage].items()], stage, stage_inputs,
                                       question=question, origins=origins, response_bytes=accepted["execution"]["response_bytes"])
    accepted[stage] = tuple((name, aggregated[name].text) for name in LIVE_KEYS)
    return result, events


# --- 6. Native three-stage workflow and readable accepted results ---

def stage_report(stage: str, accepted: dict, events: list) -> str:
    """Render accepted data for the coordinator UI, never as participant context."""
    overlap = [phase for phase, _ in events[:3]] == ["start"] * 3
    lines = [f"## {stage} — ACCEPTED",
             (f"Recovery: {sum(phase == 'start' for phase,_ in events)} newly executed participant(s); remaining results reused." if len(events) != 6 else
              "Observed concurrency: all three participants started before any finished." if overlap else
              "Observed scheduling: a participant finished before all participants had started."),
             "Execution: " + " → ".join(f"{phase}:{name}" for phase, name in events)]
    for name, text in accepted[stage]:
        payload = json.loads(text)
        lines.append(f"### {name}")
        if stage == "CRITIQUE":
            lines.append("Combined, unsegmented review of two proposals; coordinator delivery references:")
            for review in payload["reviews"]:
                lines.append(f"- {review['label']} / {review['proposal_id']} — "
                             f"{review['critique_id']} / {review['delivery_id']}")
            lines.append(payload["response"])
        else:
            if stage == "REVISION":
                lines.append(f"Position: {payload['position']}; origin: {payload['origin_proposal_id']}.")
            lines.append(payload["response"])
            for citation in payload.get("feedback_references", []):
                lines.append(f"- Feedback supplied (coordinator reference) "
                             f"{citation['critique_id']} / {citation['delivery_id']} "
                             f"targeting {citation['proposal_id']}.")
    return "\n\n".join(lines)


async def council_initial(question: str, ctx: WorkflowContext[dict, str], *, live_context, recorder=None) -> None:
    bounded_text(question, live_context["execution"]["question_bytes"], boundary="INITIAL question")
    missing = [LIVE_KEYS[name] for name in LIVE_KEYS
               if not isinstance(live_context["credentials"].get(name), str) or not live_context["credentials"][name].strip()]
    if missing:
        raise RuntimeError("Missing launch-terminal environment variables: " + ", ".join(missing))
    accepted = dict(dispatched=[], usage=[], execution=dict(live_context["execution"]),
                    deadline=asyncio.get_running_loop().time() + live_context["execution"]["council_seconds"],
                    council_name=live_context["profile"]["name"],
                    context_manifest=json.loads(live_context["reference_context"].metadata_json))
    result, events = await run_stage("INITIAL", accepted=accepted, question=question,
                                    live_context=live_context, recorder=recorder)
    frozen_initial = accepted["INITIAL"]
    proposals, inputs, provenance = prepare_critique_inputs(
        result.get_outputs()[0].messages, question=question, response_bytes=accepted["execution"]["response_bytes"]
    )
    accepted.update(proposals=proposals, provenance=provenance, critique_inputs=tuple(inputs.items()))
    if recorder:
        recorder.event("stage_accepted", stage="INITIAL", parallel_success=True,
                       results=[dict(provider=name, proposal_id=pid) for name, pid, _ in proposals])
    report = stage_report("INITIAL", accepted, events)
    await ctx.yield_output(report)
    await ctx.send_message(dict(question=question, accepted=accepted, critique_inputs=inputs,
                                initial_frozen=frozen_initial, sections=[report],
                                archive_run_id=recorder.run_id if recorder else None))


async def council_critique(state: dict, ctx: WorkflowContext[dict, str], *, live_context=None, recorder=None) -> None:
    accepted, question = state["accepted"], state["question"]
    _, events = await run_stage("CRITIQUE", state["critique_inputs"],
                                accepted=accepted, question=question, live_context=live_context, recorder=recorder)
    assert accepted["INITIAL"] == state["initial_frozen"], "Frozen INITIAL data changed"
    state["critique_frozen"] = accepted["CRITIQUE"]
    state["revision_inputs"] = prepare_revision_inputs(accepted, question=question)
    if recorder:
        recorder.event("stage_accepted", stage="CRITIQUE", parallel_success=True,
                       results=[dict(provider=name, reviews=[{key: row[key] for key in (
                           "label", "proposal_id", "critique_id", "delivery_id")}
                           for row in json.loads(text)["reviews"]]) for name, text in accepted["CRITIQUE"]])
    report = stage_report("CRITIQUE", accepted, events)
    state["sections"].append(report)
    await ctx.yield_output(report)
    await ctx.send_message(state)


async def council_revision(state: dict, ctx: WorkflowContext[dict, str], *, live_context=None, recorder=None) -> None:
    accepted, question = state["accepted"], state["question"]
    _, events = await run_stage("REVISION", state["revision_inputs"],
                                accepted=accepted, question=question, live_context=live_context, recorder=recorder)
    assert accepted["INITIAL"] == state["initial_frozen"], "Frozen INITIAL data changed"
    assert accepted["CRITIQUE"] == state["critique_frozen"], "Frozen CRITIQUE data changed"
    state["sections"].append(stage_report("REVISION", accepted, events))
    assert len(accepted["dispatched"]) == len(set(accepted["dispatched"])) == 9, "Incomplete nine-request Council"
    state["sections"].append("Provider usage (null means unknown):\n" + json.dumps(accepted["usage"], indent=2))
    qualification = (f"Nine provider results accepted across stages. This execution: "
                     f"{len(accepted['dispatched']) - len(accepted.get('restored_dispatched', ()))} newly attempted requests; "
                     f"{len(accepted['usage']) - len(accepted.get('restored_returned', ()))} newly returned usable responses; "
                     f"{len(accepted.get('restored_returned', ()))} restored accepted responses. "
                     "No tools or web verification were used. Feedback references verify delivery and routing, "
                     "not model acknowledgment or semantic provenance. No consensus was forced.")
    final_report = "\n\n".join([
        f"# {accepted['council_name']} — LIVE — billable", f"Research question: {question}", qualification,
        context_sources.context_report(accepted["context_manifest"]), *state["sections"],
        "COMPLETE: INITIAL → CRITIQUE → REVISION. All stages accepted.",
    ])
    if recorder:
        recorder.event("stage_accepted", stage="REVISION", parallel_success=True,
                       results=[{key: json.loads(text)[key] for key in (
                           "participant", "position", "origin_proposal_id", "feedback_references")}
                           for _, text in accepted["REVISION"]])
        recorder.event("run_completed", status="accepted", outcome="COMPLETE: INITIAL → CRITIQUE → REVISION. All stages accepted.",
                       qualification=qualification)
    await ctx.yield_output(final_report + ("\n\n" + recorder.notice() if recorder and recorder.notice() else ""))


def validate_profile(profile):
    """Validate editable settings structurally; account/model support is decided by the provider."""
    if (not isinstance(profile, dict) or set(profile) != {"name", "description", "providers", "execution"}
            or not all(isinstance(profile[k], str) and profile[k].strip() for k in ("name", "description"))
            or not isinstance(profile["providers"], dict) or set(profile["providers"]) != set(LIVE_KEYS)):
        raise ValueError("Invalid Council profile name or providers")
    execution = profile["execution"]
    fields = {"request_seconds", "stage_seconds", "council_seconds", "question_bytes", "response_bytes", "request_text_bytes"}
    if (not isinstance(execution, dict) or set(execution) != fields
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in execution.values())
            or any(type(execution[k]) is not int for k in fields if k.endswith("_bytes"))
            or execution["stage_seconds"] <= execution["request_seconds"]
            or execution["council_seconds"] < 3 * execution["stage_seconds"]):
        raise ValueError("Invalid Council execution limits or inconsistent request/stage/Council deadlines")
    for settings in profile["providers"].values():
        if (not isinstance(settings, dict) or not isinstance(settings.get("model"), str) or not settings["model"].strip()
                or type(settings.get("max_tokens")) is not int or settings["max_tokens"] <= 0
                or type(settings.get("stream", False)) is not bool):
            raise ValueError("Invalid provider model, output-token allowance or stream setting")
    if execution["response_bytes"] < 8 * max(p["max_tokens"] for p in profile["providers"].values()):
        raise ValueError("Response byte allowance conflicts with configured output-token allowance")


def load_council_profiles() -> dict:
    """Read nonsecret requested settings; no provider clients or source-context reads."""
    path = Path(__file__).resolve().parents[2] / "configuration" / "council" / "profiles.json"
    profiles = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_IDS):
        raise ValueError("Expected Testing, Conversation, and Frontier profiles")
    for profile in profiles.values(): validate_profile(profile)
    return profiles


def build_council_workflow(profile, *, profile_id, credentials=None, transports=None):
    """One native graph and one automatic context source for every real Council tier."""
    if profile_id not in PROFILE_IDS:
        raise ValueError("Supply a configured stable Council profile selection")
    validate_profile(profile)
    name = profile["name"]
    description = profile["description"] + " LIVE — billable. Uses read-only context/DumpingGround at Run time."
    context = dict(profile=json.loads(json.dumps(profile)), execution=dict(profile["execution"]),
                   credentials={key: (credentials or {}).get(key) if credentials is not None
                                else os.environ.get(env) for key, env in LIVE_KEYS.items()},
                   transports=transports or {})

    archive = None  # Private per-run recorder, never checkpointed into participant/coordinator state.
    run_context = context  # Reference bodies live only in this execution's private closure.

    def new_archive(question, restored=None):
        archive_profile = context["profile"]
        record = RunArchive(question, archive_profile, dict(execution="live", **context["execution"],
            output_tokens={key: p["max_tokens"] for key, p in archive_profile["providers"].items()},
            provider_streaming={key: p.get("stream", False) for key, p in archive_profile["providers"].items()}))
        record.secrets = tuple(context["credentials"].values())
        record.event("implementation_loaded", identity=loaded_identity(archive_profile), templates=prompt_templates(),
                     template_format="full_system_instruction")
        if restored is not None:
            accepted = restored["accepted"]
            previous_deadline = accepted.get("deadline")
            accepted.update(deadline=asyncio.get_running_loop().time() + context["execution"]["council_seconds"],
                            restored_dispatched=tuple(accepted["dispatched"]),
                            restored_returned=tuple(f"{row['stage']}:{row['provider']}" for row in accepted["usage"]))
            record.event("restored_context", source_run_id=restored.get("archive_run_id"),
                         accepted_stages=[s for s in STAGES if s in restored["accepted"]],
                         reported_usage=accepted.get("usage"), previous_deadline=previous_deadline,
                         fresh_deadline=accepted.get("deadline"),
                         restored_requests=accepted.get("restored_dispatched", ()),
                         restored_responses=accepted.get("restored_returned", ()))
            restored["archive_run_id"] = record.run_id
        return record

    def prepare_context(record, restored=None):
        started = asyncio.get_running_loop().time()
        bundle = context_sources.load_context()
        metadata = json.loads(bundle.metadata_json)
        record.event("context_prepared", metadata=metadata,
                     status="prepared_not_yet_dispatched",
                     preparation_ms=round((asyncio.get_running_loop().time() - started) * 1000, 3))
        if restored is not None and metadata["sources"] != restored["accepted"].get("context_manifest", {}).get("sources"):
            del bundle
            raise RuntimeError("Original checkpoint context is unavailable: source identities/hashes changed; no provider request sent")
        return {**context, "reference_context": bundle,
                "context_source_ids": tuple(source["source_id"] for source in metadata["sources"])}

    def release_context():
        nonlocal run_context
        if run_context is not context:
            if archive and archive.closed and "reference_context" in run_context:
                from application.council.recovery import remember_context
                if any(r["event"] == "stage_accepted" and r.get("stage") == "CRITIQUE"
                       for r in getattr(archive, "records", ())):
                    remember_context(archive.run_id, run_context["reference_context"])
            run_context.pop("reference_context", None)
        run_context = context

    async def initial(question: str, ctx: WorkflowContext[dict, str]) -> None:
        nonlocal archive, run_context
        archive = new_archive(question)
        try:
            bounded_text(question, context["execution"]["question_bytes"], boundary="INITIAL question")
            run_context = prepare_context(archive)
            await council_initial(question, ctx, live_context=run_context, recorder=archive)
        except BaseException as exc:
            archive.fail("INITIAL", exc)
            raise
        finally:
            if archive.closed: release_context()
            if archive.notice(): await ctx.add_event(WorkflowEvent.warning(archive.notice()))

    async def critique(state: dict, ctx: WorkflowContext[dict, str]) -> None:
        nonlocal archive, run_context
        restored = archive is None or archive.closed or state.get("archive_run_id") != archive.run_id
        if restored:
            archive = new_archive(state["question"], state)
        try:
            if restored: run_context = prepare_context(archive, state)
            await council_critique(state, ctx, live_context=run_context, recorder=archive)
        except BaseException as exc:
            if archive: archive.fail("CRITIQUE", exc)
            raise
        finally:
            if archive and archive.closed: release_context()
            if archive and archive.notice(): await ctx.add_event(WorkflowEvent.warning(archive.notice()))

    async def revision(state: dict, ctx: WorkflowContext[dict, str]) -> None:
        nonlocal archive, run_context
        restored = archive is None or archive.closed or state.get("archive_run_id") != archive.run_id
        if restored:
            archive = new_archive(state["question"], state)
        try:
            if restored: run_context = prepare_context(archive, state)
            await council_revision(state, ctx, live_context=run_context, recorder=archive)
        except BaseException as exc:
            if archive: archive.fail("REVISION", exc)
            raise
        finally:
            if archive and archive.closed: release_context()
            if archive and archive.notice(): await ctx.add_event(WorkflowEvent.warning(archive.notice()))

    handlers = (initial, critique, revision)
    # Fresh executor instances isolate workflow locks/state while sharing the same functions.
    initial, critique, revision = [executor(func, id=stage) for func, stage in zip(handlers, STAGES, strict=True)]
    workflow = (WorkflowBuilder(
        name=name,
        description=description,
        start_executor=initial,
        output_from=[revision],
        intermediate_output_from=[initial, critique],
    ).add_edge(initial, critique)
      .add_edge(critique, revision).build())
    workflow.council_profile_id = profile_id
    workflow.council_profile = json.loads(json.dumps(context["profile"]))
    return workflow


def build_revision_recovery(recipe, *, action_id, credentials, transports=None, on_archive=None):
    """An unregistered native MAF execution, explicitly requested by an operator."""
    from copy import deepcopy
    profile = deepcopy(recipe.profile)
    validate_profile(profile)
    missing = tuple(recipe.missing)
    if not missing: raise ValueError("All revisions are already accepted")

    async def retry(source: str, ctx: WorkflowContext[dict, str]) -> None:
        if source != recipe.source_run_id: raise ValueError("Recovery source changed")
        record = RunArchive(recipe.question, profile, dict(execution="live", **profile["execution"],
            output_tokens={p:s["max_tokens"] for p,s in profile["providers"].items()},
            provider_streaming={p:s.get("stream", False) for p,s in profile["providers"].items()}))
        record.secrets = tuple(credentials.values())
        if on_archive: on_archive(record)
        accepted = deepcopy(recipe.accepted)
        restored = tuple(recipe.references)
        accepted.update(dispatched=list(restored), restored_dispatched=restored,
                        restored_returned=restored, failures={}, usage=[dict(
                            stage=tag.split(":")[0], provider=tag.split(":")[1], source=ref)
                            for tag,ref in recipe.references.items()],
                        deadline=asyncio.get_running_loop().time() + profile["execution"]["council_seconds"])
        record.event("implementation_loaded", identity=loaded_identity(profile), templates=recipe.templates,
                     template_format="full_system_instruction")
        record.event("recovery_link", source_run_id=recipe.source_run_id, root_run_id=recipe.root_run_id,
                     action_id=action_id, source_fingerprint=recipe.fingerprint,
                     requested_providers=missing, reused_response_references=recipe.references,
                     restored_requests=restored, restored_responses=restored,
                     fresh_budget_seconds=profile["execution"], inference="new billable requests; no remote retrieval")
        try:
            bundle = recipe.bundle()
            record.event("context_prepared", metadata=recipe.context_manifest,
                         status="original_referenced_contents_verified", recovery=True)
            context = dict(profile=profile, execution=profile["execution"], credentials=credentials,
                           transports=transports or {}, templates=recipe.templates, reference_context=bundle,
                           context_source_ids=tuple(s["source_id"] for s in recipe.context_manifest["sources"]))
            inputs = prepare_revision_inputs(accepted, question=recipe.question)
            _, events = await run_stage("REVISION", inputs, accepted=accepted, question=recipe.question,
                                        live_context=context, recorder=record, providers=missing)
            record.event("stage_accepted", stage="REVISION", parallel_success=True, results=[
                {k: json.loads(text)[k] for k in ("participant", "position", "origin_proposal_id", "feedback_references")}
                for _,text in accepted["REVISION"]])
            qualification = f"{len(missing)} newly attempted revisions; {len(restored)} earlier accepted responses reused by reference."
            record.event("run_completed", status="accepted", outcome="All stages accepted after deliberate revision recovery; original execution unchanged.",
                         qualification=qualification)
            await ctx.yield_output("# Deliberate Revision Recovery\n\n" + qualification + "\n\n" +
                                   stage_report("REVISION", accepted, events) + "\n\n" + record.notice())
        except BaseException as exc:
            record.fail("REVISION", exc)
            raise
        finally:
            if 'bundle' in locals():
                from application.council.recovery import remember_context
                remember_context(record.run_id, bundle)
            if record.notice(): await ctx.add_event(WorkflowEvent.warning(record.notice()))

    task = executor(retry, id="RETRY_REVISIONS")
    return WorkflowBuilder(name="Explicit revision recovery", start_executor=task).build()


# --- 7. Official DevUI in-memory registration ---

def build_registered_councils(*, credentials=None, transports=None):
    profiles = load_council_profiles()
    councils = [build_council_workflow(profiles[key], profile_id=key, credentials=credentials, transports=transports)
                for key in PROFILE_IDS]
    if len({workflow.name for workflow in councils}) != 3:
        raise ValueError("Expected three uniquely named Council registrations")
    return councils


if __name__ == "__main__":
    import logging
    from application.council.devui import serve_councils as serve

    auth_token = os.environ.get("DEVUI_AUTH_TOKEN")
    if not auth_token or not auth_token.strip():
        raise RuntimeError("DEVUI_AUTH_TOKEN is missing. Use Launch-MAF.cmd to load the saved Windows User token.")
    credentials = {name: os.environ.get(env) for name, env in LIVE_KEYS.items()}
    os.environ.pop("OPENAI_API_KEY", None)  # Disable DevUI's unrelated provider proxy.
    logging.basicConfig(level=logging.INFO)
    serve(entities=build_registered_councils(credentials=credentials),
          entities_dir=None,
          host="127.0.0.1", port=8080, auto_open=False,
          instrumentation_enabled=False, auth_enabled=True, auth_token=auth_token,
          council_credentials=credentials)
