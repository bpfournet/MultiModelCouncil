"""Selective provider evidence. Never serialize a raw response, request, or reasoning body."""

from hashlib import sha256
from contextvars import ContextVar
from pathlib import Path
import re
from time import perf_counter
import traceback

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
DEVUI_SCOPE = ContextVar("maf_devui_request", default=None)


def sanitize(value, secrets=()):
    text = str(value)
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:api[_-]?key|token|cookie|secret)\s*[:=]\s*)[^\s,;\"'}]+",
                  "[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b", "[REDACTED]", text)
    # Query values and URL userinfo are never useful request identity here.
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", text)
    return text[:4000]


def exception_evidence(exc, secrets=()):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        # SDK exception strings can embed the whole response body. Read only a
        # provider error's message/code/type/param, otherwise the ordinary cause.
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else None
        detail = ({k: sanitize(error[k], secrets) for k in ("message", "code", "type", "param")
                   if error.get(k) is not None} if isinstance(error, dict) else {})
        frames = traceback.extract_tb(exc.__traceback__)[-12:]
        origin = frames[-1].filename.replace("\\", "/") if frames else ""
        transport = type(exc).__module__.split(".")[0] in ("httpx", "httpx2", "httpcore", "httpcore2", "ssl", "socket")
        controlled = "/application/council/" in origin and isinstance(exc, (AssertionError, RuntimeError, ValueError))
        validation = "ValidationError" in type(exc).__name__
        # Validation/decoder exception strings can contain their entire input,
        # including native thinking blocks. Preserve locations/types, never input.
        message = sanitize(str(exc), secrets) if (transport or controlled) and not validation and not isinstance(body,(dict,list)) else None
        issues = []
        if validation and callable(getattr(exc,"errors",None)):
            try:
                issues = [dict(type=r.get("type"), location=[sanitize(x,secrets) for x in r.get("loc",())])
                          for r in exc.errors(include_input=False,include_context=False)[:12]]
            except Exception: pass
        chain.append(dict(type=type(exc).__name__, message=message, provider_error=detail, validation_issues=issues,
                          frames=[dict(file=Path(f.filename).name, function=f.name, line=f.lineno) for f in frames]))
        exc = getattr(exc, "inner_exception", None) or exc.__cause__
    return chain


def plain(value):
    """Convert a selected metadata object, never a full response/content object."""
    if value is None: return None
    if hasattr(value, "model_dump"): return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "to_dict"): return value.to_dict()
    if isinstance(value, dict): return {k:plain(v) for k,v in value.items()}
    if isinstance(value, list): return [plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)): return value
    return getattr(value, "name", str(value))


def native_metadata(raw, provider, secrets=()):
    usage = getattr(raw, "usage", None) if provider != "google" else getattr(raw, "usage_metadata", None)
    details = getattr(raw, "stop_details", None)
    stop_details = ({k: sanitize(getattr(details, k), secrets) if getattr(details, k, None) is not None else None
                     for k in ("type", "category", "explanation")} if details is not None else None)
    candidates = getattr(raw, "candidates", None) or []
    candidate = candidates[0] if candidates else None
    incomplete = getattr(raw, "incomplete_details", None)
    error = getattr(raw, "error", None)
    return dict(native_usage=plain(usage), usage_semantics="provider_native; cumulative snapshot, not additive",
                stop_details=stop_details, incomplete_reason=getattr(incomplete, "reason", None),
                native_error=({k: sanitize(getattr(error, k), secrets) for k in ("code", "message")
                               if getattr(error, k, None) is not None} if error else None),
                finish_message=sanitize(candidate.finish_message, secrets)
                    if getattr(candidate, "finish_message", None) else None,
                safety_ratings=plain(getattr(candidate, "safety_ratings", None)))


class AttemptCapture:
    """Batch ordinary text; first text and terminal flushes are fsynced by RunArchive.

    Subsequent activity flushes after 4096 characters, or at least 256 characters
    and one second when another delta arrives. No heartbeat writes; a hard kill
    may lose the buffered tail. Sparse one-character deltas do not become log spam.
    """

    def __init__(self, recorder, stage, provider, request=None, secrets=()):
        self.recorder, self.stage, self.provider, self.request = recorder, stage, provider, request
        self.secrets = tuple(secrets)
        self.started = perf_counter()
        self.first_activity = self.last_activity = self.first_text = self.terminal = None
        self.chunks, self.size, self.flush_clock, self.wrote_text = [], 0, perf_counter(), False
        self.block = None
        self.native_usage = None
        self.response_id = self.response_model = None
        self.request_id = None
        self.stop_reason = self.stop_details = None

    def emit(self, event, **fields):
        if self.recorder:
            self.recorder.provider(event, self.stage, self.provider, request=self.request, **fields)

    def flush(self):
        if self.chunks:
            self.emit("provider_text", text=["".join(self.chunks)], block_index=self.block,
                      output_status="partial_pending_validation")
            self.chunks, self.size, self.flush_clock = [], 0, perf_counter()
            self.wrote_text = True

    def text(self, text, block):
        if not text: return
        if self.block != block: self.flush()
        self.block = block
        if self.first_text is None: self.first_text = self.ms()
        self.chunks.append(text)
        self.size += len(text)
        if not self.wrote_text or self.size >= 4096 or self.size >= 256 and perf_counter() - self.flush_clock >= 1:
            self.flush()

    def ms(self): return round((perf_counter() - self.started) * 1000, 3)

    def stream_event(self, event):
        kind = event.type
        # SDK convenience events repeat ordinary deltas; observe native events only.
        if kind not in ("message_start", "message_delta", "message_stop", "content_block_start",
                        "content_block_delta", "content_block_stop"): return
        self.last_activity = self.ms()
        if self.first_activity is None:
            self.first_activity = self.last_activity
            self.emit("provider_receiving", observation="first_native_stream_event")
        if kind == "message_start":
            self.response_id, self.response_model = event.message.id, event.message.model
            self.native_usage = plain(event.message.usage)
        elif kind == "content_block_start" and event.content_block.type == "text":
            self.text(event.content_block.text, event.index)
        elif kind == "content_block_delta" and event.delta.type == "text_delta":
            self.text(event.delta.text, event.index)
        elif kind == "message_delta":
            # These are cumulative counts; replace supplied fields, never sum.
            self.native_usage = {**(self.native_usage or {}), **(plain(event.usage) or {})}
            self.stop_reason = event.delta.stop_reason
            details = event.delta.stop_details
            self.stop_details = ({k: getattr(details, k, None) for k in ("type", "category", "explanation")}
                                 if details else None)
        elif kind == "message_stop":
            self.terminal = self.ms()
            self.flush()

    def summary(self):
        return dict(first_stream_activity_ms=self.first_activity, last_stream_activity_ms=self.last_activity,
                    first_ordinary_text_ms=self.first_text, terminal_event_ms=self.terminal,
                    stream_response_id=self.response_id, stream_response_model=self.response_model,
                    stream_stop_reason=self.stop_reason, stream_stop_details={k:sanitize(v, self.secrets) if v is not None else None
                        for k,v in self.stop_details.items()} if self.stop_details else None,
                    stream_native_usage=self.native_usage,
                    # message_stop alone cannot make message_start's initial usage final.
                    usage_status="final" if self.terminal is not None and self.stop_reason is not None
                        else "partial_or_unavailable")
