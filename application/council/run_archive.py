"""Append-only local research records; no execution, retry, or retention policy."""

from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from itertools import count
import json
import os
from pathlib import Path
import sys
import httpx
import httpx2
from threading import RLock
from time import perf_counter
from hashlib import sha256
from weakref import WeakValueDictionary
from .attempts import exception_evidence, DEVUI_SCOPE

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
LIVE_ARCHIVES = WeakValueDictionary()

LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"
USAGE_FIELDS = ("input_token_count", "output_token_count", "total_token_count",
                "cache_creation_input_token_count", "cache_read_input_token_count",
                "reasoning_output_token_count")


def safe_error(exc, classification=None, secrets=()):
    """Inspect exception types/status only; never save SDK messages, headers, or bodies."""
    cause, seen, status, timeout, boundary = exc, set(), None, None, {}
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if "Timeout" in type(cause).__name__: timeout = type(cause).__name__
        value = getattr(cause, "status_code", None)
        if not isinstance(value, int): value = getattr(cause, "code", None)
        if not isinstance(value, int): value = getattr(getattr(cause, "response", None), "status_code", None)
        if isinstance(value, int):
            status = value
        if isinstance(getattr(cause, "actual_bytes", None), int):
            boundary = {key: getattr(cause, key) for key in ("boundary", "actual_bytes", "limit_bytes")}
        following = getattr(cause, "inner_exception", None) or cause.__cause__
        if following is None:
            break
        cause = following
    name = type(cause).__name__
    if status in (401, 403): classification = "authentication_error"
    elif status == 429: classification = "rate_limit"
    elif timeout: classification = "provider_timeout"
    elif "Cancel" in name: classification = "cancelled"
    elif isinstance(cause, (httpx.TransportError, httpx2.TransportError, httpx.DecodingError, httpx2.DecodingError, ConnectionError)):
        classification = "network_failure"
    # An SDK error event can carry the successful HTTP response that opened its stream.
    elif status is not None:
        classification = "provider_stream_error" if 200 <= status < 300 else "provider_http_error"
    elif classification is None and isinstance(exc, AssertionError):
        # Compare controlled validation messages, but do not retain their potentially unsafe text.
        message = str(exc).casefold()
        classification = next((code for fragment, code in (
            ("misattributed", "incorrect_attribution"),
            ("agent identity", "incorrect_attribution"),
            ("participant identity", "incorrect_attribution"),
            ("provenance", "provenance_validation_error"),
            ("wrong proposal", "provenance_validation_error"),
        ) if fragment in message), "workflow_validation_error")
    return dict(classification=classification or "local_application_exception",
                exception_type=type(exc).__name__, cause_type=name, status_code=status,
                timeout_type=timeout, causes=exception_evidence(exc, secrets), **boundary)


def terminal_warning(message):
    try: print(message, file=sys.stderr, flush=True)
    except Exception: pass  # The native workflow warning still reports the failure.


class RunArchive:
    """One matched pair, serialized synchronous appends, and visible nonfatal I/O errors."""

    def __init__(self, question, profile, configuration):
        self._lock = RLock()
        self._disabled = set()
        self._sequence = 0
        self._providers, self._stages = {}, {}
        self._accepted_count = 0
        self._accepted_keys = set()
        self.records = []
        self.secrets = ()
        self.active = True
        self.cancellation_scope = DEVUI_SCOPE.get()
        self._restored_requests, self._restored_responses = (), ()
        self._clock = 0
        self.error = None
        self.closed = False
        self.paths = ()
        self.profile, self.run_id = {}, "unavailable"
        try:
            self._clock = perf_counter()
            self.profile = json.loads(json.dumps(profile))
            started = datetime.now().astimezone()
            self.run_id = started.strftime("%Y%m%d-%H%M")
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                for number in count(1):
                    self.run_id = started.strftime("%Y%m%d-%H%M") + (f"-{number:02}" if number > 1 else "")
                    folder = LOGS_DIR / f"{self.run_id}-Logs"
                    try:
                        folder.mkdir()  # Reserve the whole run atomically; never reuse an existing folder.
                    except FileExistsError:
                        continue
                    transcript = folder / f"{self.run_id}-Transcript.md"
                    diagnostics = folder / f"{self.run_id}-Diagnostics.jsonl"
                    reserved = []
                    try:
                        for path in (transcript, diagnostics):
                            with path.open("x", encoding="utf-8"):
                                reserved.append(path)
                    except Exception:
                        for path in reserved:
                            path.unlink()  # Only empty reservations created by this invocation.
                        folder.rmdir()
                        raise
                    self.paths = (transcript, diagnostics)
                    break
            except OSError as exc:
                self.error = f"ARCHIVE ERROR: file creation failed ({type(exc).__name__}); archive incomplete."
                terminal_warning(self.error)
            versions = {}
            versions_error = None
            for package in ("agent-framework-core", "agent-framework-orchestrations", "agent-framework-devui",
                            "agent-framework-openai", "agent-framework-anthropic", "agent-framework-gemini",
                            "openai", "anthropic", "google-genai", "httpx", "httpx2"):
                try: versions[package] = version(package)
                except PackageNotFoundError: versions[package] = None
                except Exception as exc:
                    versions[package] = None
                    versions_error = exc
            LIVE_ARCHIVES[self.run_id] = self
            self.event("run_started", schema_version=2, status="incomplete", question=question, profile=profile,
                       start_timestamp_local=started.isoformat(), start_timestamp_utc=started.astimezone(timezone.utc).isoformat(),
                       configuration=configuration, python_version=sys.version.split()[0], versions=versions,
                       application_version=None, devui_session_id=None,
                       devui_response_id=(self.cancellation_scope or {}).get("response_id"),
                       completion_rule="Both files require a terminal recorder-owned Run Outcome and matching run_completed or run_failed diagnostics, with no archive_error. Execution failure can be fully recorded; missing/truncated terminal evidence remains unfinished/unknown.",
                       session_note="Installed DevUI does not forward its conversation ID to this workflow.")
            if versions_error is not None: self.recording_error("package version lookup",versions_error)
        except Exception as exc:
            self.recording_error("initialization", exc)

    @staticmethod
    def _append(path, text):
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

    def notice(self):
        return self.error or ""

    def recording_error(self, operation, exc):
        self.error = f"ARCHIVE ERROR: {operation} failed ({type(exc).__name__}); archive incomplete."
        terminal_warning(self.error)
        self.event("archive_error", status="incomplete", operation=operation, exception_type=type(exc).__name__)

    def event(self, event, *, text=None, **fields):
        if event in ("run_completed", "run_failed"):
            self.closed = True
            self.active = False
        try:
            self._record(event, text=text, **fields)
        except Exception as exc:
            # A diagnostic serialization/formatting failure must not change scientific acceptance.
            self.error = f"ARCHIVE ERROR: record failed ({type(exc).__name__}); archive incomplete."
            terminal_warning(self.error)
            if event != "archive_error":
                try: self._record("archive_error", status="incomplete", exception_type=type(exc).__name__)
                except Exception: pass

    def _markdown(self, record, text):
        """Render selected readable labels; technical fields belong only in JSONL."""
        def name(provider):
            return {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google"}.get(
                provider or "", provider or "Unavailable")

        def feedback(rows):
            return ", ".join(f"{row['critique_id']}/{row['delivery_id']} ({row['proposal_id']})"
                             for row in rows)

        event, stage = record["event"], record.get("stage")
        provider = name(record.get("provider"))
        bodies = "\n\n".join(text or [])  # No stripping, fencing, or rewriting ordinary content.
        if event == "run_started":
            return (f"# MultiModelCouncil Transcript\n\n**Run:** {self.run_id}\n\n"
                    f"**Started:** {record['start_timestamp_local']}\n\n"
                    f"**Started (UTC):** {record['start_timestamp_utc']}\n\n"
                    f"**Council:** {record['profile']['name']}\n\n"
                    "The final Run Outcome section records completion. If it is absent, this run is unfinished.\n\n"
                    "## Original Question\n\n" + record["question"] + "\n\n")
        if event == "stage_started":
            return f"\n## {stage}\n\n"
        if event == "context_prepared":
            from .context_sources import context_report
            return "\n" + context_report(record["metadata"]) + "\n\n"
        if event == "provider_completed":
            if record.get("text_recorded_incrementally"):
                return f"\n\n**{provider} ({stage}): response received; pending validation.**\n\n"
            lines = [f"\n### {provider} — {record.get('model') or 'model unavailable'}",
                     "**Response:** Available — pending stage acceptance."]
            references = record.get("references")
            if stage == "CRITIQUE" and references:
                lines.append("**Review:** Combined, unsegmented response about both proposals; references are coordinator deliveries.")
                lines.append("**Reviewed:** " + ", ".join(
                    f"{row['label']} ({row['proposal_id']}; {row['critique_id']}/{row['delivery_id']})"
                    for row in references))
            elif stage == "REVISION" and references:
                lines.extend([f"**Origin:** {references['origin_proposal_id']}",
                              "**Feedback:** " + feedback(references["feedback"])])
            return "\n\n".join(lines) + "\n\n" + bodies + "\n\n"
        if event == "provider_text":
            # Every batch is attributed even when concurrent responses interleave.
            return (f"\n### {provider} — {record.get('model') or 'model unavailable'}\n\n"
                    f"**{stage} ordinary text — partial, pending validation; block {record['block_index']}.**\n\n"
                    + "".join(text or []) + "\n")
        if event == "provider_accepted":
            return f"\n**{provider} ({stage}): INDIVIDUALLY ACCEPTED — stage barrier still applies.**\n\n"
        if event == "recovery_link":
            return (f"\n## Deliberate Revision Recovery\n\nSource run: {record['source_run_id']}. "
                    f"New billable revision attempts: {len(record['requested_providers'])}. "
                    "Earlier accepted responses are referenced, not copied; original outcome is unchanged.\n\n")
        if event == "cleanup_warning":
            return f"\n**Cleanup warning — {provider} ({stage}).** Request outcome unchanged; see diagnostics.\n\n"
        if event == "provider_rejected":
            reason = record["classification"].replace("_", " ")
            return (f"\n**INCOMPLETE / REJECTED provider response — {provider} ({stage}).**\n\n"
                    f"**Reason:** {reason}. Any available response text is preserved above.\n\n")
        if event == "restored_context":
            return ("\n## Previously Accepted Context\n\n"
                    f"**Source run:** {record.get('source_run_id') or 'Unavailable'}\n\n"
                    "**Accepted stages:** " + ", ".join(record["accepted_stages"]) + "\n\n"
                    "Earlier accepted work remains in the source archive; these are not newly dispatched responses.\n\n"
                    + bodies + "\n\n")
        if event == "stage_accepted":
            lines = [f"\n**{stage}: ACCEPTED**", "Accepted attribution and references:"]
            for row in record["results"]:
                if stage == "INITIAL":
                    lines.append(f"- {name(row['provider'])} — **Proposal:** {row['proposal_id']}")
                elif stage == "CRITIQUE":
                    lines.append(f"- {name(row['provider'])} — " + "; ".join(
                        f"{review['label']} ({review['proposal_id']}): {review['critique_id']}/{review['delivery_id']}"
                        for review in row["reviews"]))
                else:
                    lines.append(f"- {name(row['participant'])} — **Origin:** {row['origin_proposal_id']}; "
                                 f"**Position:** {row['position'].capitalize()}; **Feedback:** {feedback(row['feedback_references'])}")
            return "\n\n".join(lines) + "\n\n"
        if event == "stage_failed":
            return f"\n**{stage}: REJECTED** — {provider}; {record['classification'].replace('_', ' ')}.\n\n"
        if event == "run_completed":
            return ("\n## Run Outcome\n\n**COMPLETE — ALL THREE STAGES ACCEPTED**\n\n"
                    + record["outcome"] + "\n\n" + record["qualification"] + "\n\n"
                    + ("**Archive:** INCOMPLETE — archive writing failed; see diagnostics and terminal output.\n\n"
                       if record["archive_status"] == "incomplete" else ""))
        if event == "run_failed":
            return ("\n## Run Outcome\n\n**INCOMPLETE — COUNCIL FAILED**\n\n"
                    f"**Stage:** {stage}\n\n**Provider:** {provider}\n\n"
                    f"**Failure:** {record['classification'].replace('_', ' ')}. See diagnostics for details.\n\n")
        if event == "archive_error":
            return "\n**ARCHIVE ERROR — archive incomplete.** See diagnostics and terminal output.\n\n"
        return ""

    def _record(self, event, *, text=None, **fields):
        with self._lock:
            self._sequence += 1
            now = datetime.now().astimezone()
            record = dict(run_id=self.run_id, sequence=self._sequence, event=event,
                          timestamp_local=now.isoformat(), timestamp_utc=now.astimezone(timezone.utc).isoformat(), **fields)
            stage = fields.get("stage")
            if event in ("restored_context", "recovery_link"):
                self._restored_requests = tuple(fields.get("restored_requests", ()))
                self._restored_responses = tuple(fields.get("restored_responses", ()))
            if event == "provider_accepted": self._accepted_keys.add((stage, fields["provider"]))
            if event == "stage_accepted":
                self._accepted_keys.update((stage, r.get("provider", r.get("participant"))) for r in fields["results"]
                    if f"{stage}:{r.get('provider', r.get('participant'))}" not in self._restored_responses)
            self._accepted_count = len(self._accepted_keys)
            if event == "stage_started": self._stages[stage] = perf_counter()
            if event in ("stage_accepted", "stage_failed"):
                record["elapsed_ms"] = round((perf_counter() - self._stages.get(stage, self._clock)) * 1000, 3)
                record["providers_dispatched"] = [p for (s, p), v in self._providers.items() if s == stage and v.get("dispatched")]
                record["providers_completed"] = [p for (s, p), v in self._providers.items() if s == stage and v.get("completed")]
            if event in ("run_completed", "run_failed"):
                record["elapsed_ms"] = round((perf_counter() - self._clock) * 1000, 3)
                record["archive_status"] = "incomplete" if self.error else "no_errors_observed"
                record["actual_cost_usd"] = None
                record.update(new_requests_attempted=sum(bool(v.get("dispatched")) for v in self._providers.values()),
                              new_responses_returned=sum(bool(v.get("completed")) for v in self._providers.values()),
                              new_results_accepted=self._accepted_count,
                              restored_requests=len(self._restored_requests), restored_responses=len(self._restored_responses))
                record["reported_usage"] = [dict(stage=s, provider=p, usage=v.get("usage"))
                                             for (s, p), v in self._providers.items() if v.get("completed")]
            if event == "provider_text": record["text_delta"] = "".join(text or [])
            if event == "provider_completed" and text is not None:
                record["ordinary_blocks"] = text
            self.records.append(record)
            details = json.dumps(record, ensure_ascii=False, allow_nan=False)
            markdown = self._markdown(record, text)
            errors = []
            if self.paths:
                for path, content in ((self.paths[1], details + "\n"), (self.paths[0], markdown)):
                    if not content or path in self._disabled: continue
                    try: self._append(path, content)
                    except (OSError, UnicodeError) as exc:
                        self._disabled.add(path)  # Do not retry this failing destination.
                        self.error = f"ARCHIVE ERROR: {path.name} write failed ({type(exc).__name__}); archive incomplete."
                        terminal_warning(self.error)
                        errors.append(dict(file=path.name, exception_type=type(exc).__name__))
            if errors:
                self.event("archive_error", status="incomplete", failures=errors)

    def provider(self, event, stage, provider, *, request=None, text=None, error=None, **fields):
        try:
            with self._lock:
                if event == "cleanup_warning":
                    self.event(event, stage=stage, provider=provider, classification="cleanup_warning", cleanup_error=safe_error(error, secrets=self.secrets))
                    return  # Never overwrite an earlier request failure with cleanup metadata.
                current = self._providers.setdefault((stage, provider), {})
                if event == "provider_rejected" and current.get("rejected"): return
                if event == "provider_dispatched":
                    current.update(dispatched=True, clock=perf_counter())
                if event == "provider_completed": current["completed"] = True
                if event == "provider_rejected":
                    current["rejected"] = True
                    fields = {**{key: current.get(key) for key in (
                        "finish_reason", "raw_stop_reason", "raw_status", "continuation_present",
                        "provider_finish_reason", "refusal", "safety_stop", "usage", "usage_source",
                        "requested_output_tokens", "response_id", "response_model", "provider_request_id", "response_text_bytes")}, **fields}
                    fields.update(safe_error(error, fields.pop("classification", None), self.secrets))
                current.update(fields)
                requested_model = self.profile.get("providers", {}).get(provider, {}).get("model")
                model = current.get("response_model") or requested_model
                references = None
                if request is not None:
                    references = ([{k: peer[k] for k in ("label", "proposal_id", "critique_id", "delivery_id")}
                                   for peer in request["peers"]] if stage == "CRITIQUE" else
                                  dict(origin_proposal_id=request["origin"]["proposal_id"], feedback=[
                                      {k: item[k] for k in ("proposal_id", "critique_id", "delivery_id")}
                                      for item in request["feedback"]]))
                elapsed = round((perf_counter() - current["clock"]) * 1000, 3) if "clock" in current else None
                self.event(event, stage=stage, provider=provider, attempt=1,
                           attempt_id=f"{self.run_id}:{stage}:{provider}:1",
                           model=model, requested_model=requested_model, text=text, references=references,
                           elapsed_ms=elapsed, stage_accepted=False,
                           status="received_pending_validation" if event == "provider_completed" else
                                  "accepted_waiting_for_peers" if event == "provider_accepted" else
                                  "receiving" if event in ("provider_receiving", "provider_headers", "provider_text") else
                                  "rejected" if event == "provider_rejected" else "dispatched",
                           other_providers_completed=[p for (s, p), v in self._providers.items()
                                                      if s == stage and p != provider and v.get("completed")], **fields)
        except Exception as exc:
            self.recording_error("provider metadata", exc)

    def fail(self, stage, exc):
        self.closed = True
        self.active = False
        try:
            rejected = [(p, v) for (s, p), v in self._providers.items()
                        if s == stage and v.get("rejected") and v.get("classification") != "cancelled"]
            details = safe_error(exc, secrets=self.secrets)
            if rejected:
                details = {k: rejected[0][1].get(k) for k in ("classification", "exception_type", "cause_type", "status_code", "timeout_type", "causes")}
            elif isinstance(exc, TimeoutError):
                initiators = {v.get("cancellation_initiator") for (s,_),v in self._providers.items() if s == stage}
                details["classification"] = "run_deadline" if "run_deadline" in initiators else "stage_deadline"
                details["cancellation_initiator"] = details["classification"]
            elif isinstance(exc, BaseException) and "Cancel" in type(exc).__name__:
                details["cancellation_initiator"] = (self.cancellation_scope or {}).get("initiator", "stop_or_client_disconnect_or_shutdown")
            self.event("stage_failed", stage=stage, provider=rejected[0][0] if rejected else None,
                       status="rejected", parallel_success=False, **details)
            self.event("run_failed", stage=stage, provider=rejected[0][0] if rejected else None,
                       status="incomplete", **details)
        except Exception as exc:
            self.recording_error("outcome metadata", exc)
