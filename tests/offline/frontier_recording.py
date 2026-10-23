"""Targeted Frontier recorder qualification: real installed SDKs, synthetic HTTP only.

Reuses the hardening fixtures. Does not rerun the completed broad regression suite.
All new archives/context are disposable; sockets and DNS are forbidden.
"""

import asyncio
from hashlib import sha256
from importlib.metadata import version
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from council_hardening import Fixtures
from application.council import workflow as council, run_archive as archives, context_sources, recovery
import httpx2


QUESTION = "Original offline Frontier qualification question"
PARTIAL = "Ordinary partial output 🧪.\nStill unfinished."
DETAILS = dict(type="refusal", category="cyber", explanation="Synthetic provider-supplied refusal explanation.")


class FrontierFixture(Fixtures):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.refusal = case.startswith("refusal") or case == "error_after_refusal"
        self.details = DETAILS if self.refusal and case != "refusal_no_details" else None
        self.text = "" if case in ("refusal_details", "refusal_no_details") else PARTIAL

    async def handler(self, request):
        body = json.loads(request.content)
        if request.url.host != "api.anthropic.com" or council.STAGE_INSTRUCTIONS["REVISION"] not in json.dumps(body):
            return await super().handler(request)
        self.calls.append(("REVISION", "anthropic", body))
        assert body["stream"] is True
        events = [dict(type="message_start", message=dict(id="msg_frontier_qualification", type="message",
            role="assistant", model=body["model"], content=[], stop_reason=None, stop_sequence=None,
            stop_details=None, usage=dict(input_tokens=313, output_tokens=1,
                cache_creation_input_tokens=4, cache_read_input_tokens=3)))]
        if self.text:
            events += [dict(type="content_block_start", index=0, content_block=dict(type="text", text="")),
                dict(type="content_block_delta", index=0, delta=dict(type="text_delta", text=self.text[:9])),
                dict(type="content_block_delta", index=0, delta=dict(type="text_delta", text=self.text[9:])),
                dict(type="content_block_stop", index=0)]
        if self.case not in ("error_partial", "eof_partial", "missing_delta", "sse_error"):
            events.append(dict(type="message_delta", delta=dict(stop_reason="refusal" if self.refusal else "end_turn",
                stop_sequence=None, stop_details=self.details), usage=dict(output_tokens=17)))
        if self.case not in ("error_partial", "error_after_refusal", "eof_partial", "eof_after_delta", "sse_error"):
            events.append(dict(type="message_stop"))
        if self.case == "sse_error":
            events.append(dict(type="error", error=dict(type="overloaded_error", message="Synthetic streaming overload")))
        case = self.case

        class Stream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                for event in events:
                    yield ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                if case in ("error_partial", "error_after_refusal"):
                    raise httpx2.ReadError("Synthetic interrupted Frontier stream")

        return httpx2.Response(200, stream=Stream(), headers={"content-type":"text/event-stream",
                                                           "request-id":"req_frontier_qualification"})


def rows_for(rows, event):
    return [r for r in rows if r["event"] == event and r.get("stage") == "REVISION" and r.get("provider") == "anthropic"]


def verify_saved(record, fixture, profile):
    # Read the durable files, not only in-memory recorder objects.
    saved = record.paths[1].read_text(encoding="utf-8")
    rows = [json.loads(line) for line in saved.splitlines()]
    assert rows == json.loads(json.dumps(record.records))
    transcript = record.paths[0].read_text(encoding="utf-8")
    assert "OFFLINE_SECRET_VALUE" not in saved + transcript
    outcome = rows[-1]
    assert outcome["event"] == "run_failed" and outcome["stage"] == "REVISION"
    assert outcome["new_requests_attempted"] == 9 and outcome["new_results_accepted"] == 8
    assert outcome["archive_status"] == "no_errors_observed" and outcome["actual_cost_usd"] is None
    assert {r["stage"] for r in rows if r["event"] == "stage_accepted"} == {"INITIAL", "CRITIQUE"}
    assert not rows_for(rows, "provider_accepted")
    assert len(fixture.calls) == len({(s,p) for s,p,_ in fixture.calls}) == 9
    assert all(r["attempt"] == 1 for r in rows if r["event"].startswith("provider_"))
    rejected, = rows_for(rows, "provider_rejected")
    headers, = rows_for(rows, "provider_headers")
    assert headers["http_status"] == 200 and headers["request_ids"]["request-id"] == "req_frontier_qualification"
    opened = next(r for r in rows_for(rows,"provider_receiving") if r.get("observation") == "SDK stream opened")
    assert opened["provider_request_id"] == "req_frontier_qualification"
    assert len({r["attempt_id"] for r in [rejected, headers, opened]}) == 1
    assert rejected["stream_response_id"] == "msg_frontier_qualification"
    assert rejected["stream_response_model"] == profile["providers"]["anthropic"]["model"]
    text = "".join(r["text_delta"] for r in rows_for(rows,"provider_text"))
    assert text == fixture.text
    assert ("**REVISION ordinary text — partial, pending validation" in transcript) == bool(text)
    if text:
        bodies = recovery.response_texts(recovery.read_run(record.run_id))
        assert bodies["REVISION","anthropic"] == fixture.text
    native = rejected["stream_native_usage"]
    assert native["input_tokens"] == 313 and native["cache_creation_input_tokens"] == 4 and native["cache_read_input_tokens"] == 3
    assert native["output_tokens"] == (1 if fixture.case in ("error_partial","eof_partial","missing_delta","sse_error") else 17)
    complete = fixture.case.startswith("refusal")
    assert (rejected["terminal_event_ms"] is not None) == (complete or fixture.case == "missing_delta")
    assert rejected["usage_status"] == ("final" if complete else "partial_or_unavailable"), fixture.case
    assert rejected["stream_stop_reason"] == ("refusal" if fixture.refusal else "end_turn" if fixture.case == "eof_after_delta" else None)
    assert rejected["stream_stop_details"] == fixture.details
    if complete:
        completed, = rows_for(rows,"provider_completed")
        assert completed["native_refusal"] is True and completed["refusal_text_tagged"] is False
        assert completed["raw_stop_reason"] == "refusal" and completed["stop_details"] == fixture.details
        assert completed["response_id"] == "msg_frontier_qualification" and completed["provider_request_id"] == "req_frontier_qualification"
        assert completed["response_text_bytes"] == len(text.encode())
        assert completed["native_usage"] == native and completed["usage"]["output_token_count"] == 17
        assert rejected["local_validation_reason"] == ("incomplete_or_refused_output" if text else "unexpected_content_or_tool_call")
        assert any(c["message"] in ("Incomplete/refused output", "Tool/nontext output rejected") for c in rejected["causes"])
    elif fixture.case.startswith("error"):
        assert rejected["classification"] == "network_failure"
        assert not rows_for(rows,"provider_completed")
        assert "Synthetic interrupted Frontier stream" in json.dumps(rejected["causes"])
    elif fixture.case == "sse_error":
        assert not rows_for(rows,"provider_completed")
        assert rejected["classification"] == "provider_stream_error" and rejected["status_code"] == 200
        assert any(c["provider_error"].get("type") == "overloaded_error" for c in rejected["causes"])
    else:
        assert rejected["classification"] == "incomplete_or_refused_output"
        assert any(c["message"] in ("Incomplete provider event stream", "Incomplete/refused output") for c in rejected["causes"])
    # Independently reconstruct original feedback routing from the saved archive;
    # compare the request actually given to Anthropic, without redispatching it.
    recipe = recovery.reconstruct(record.run_id)
    assert recipe.missing == ["anthropic"] and recipe.question == QUESTION and recipe.profile == profile
    revision_input = council.prepare_revision_inputs(recipe.accepted, question=QUESTION)["anthropic"]
    wire = next(b for s,p,b in fixture.calls if (s,p) == ("REVISION","anthropic"))
    strings = [block["text"] for m in wire["messages"] for block in m["content"] if block["type"] == "text"]
    original_serialization = json.dumps(json.loads(revision_input), ensure_ascii=False, sort_keys=True)
    assert original_serialization in strings and QUESTION in strings and recipe.bundle().text in strings
    assert wire["system"] == recipe.templates["REVISION"]
    identity = next(r["identity"] for r in rows if r["event"] == "implementation_loaded")
    assert identity == council.loaded_identity(profile)
    assert identity["template_sha256"] == sha256(json.dumps(recipe.templates, sort_keys=True).encode()).hexdigest()
    assert identity["profile_sha256"] == sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    assert len(fixture.calls) == 9  # Reconstruction/readiness is read-only, never another request.
    return rejected


async def qualify():
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parents[2]
    history = {p:sha256(p.read_bytes()).hexdigest() for p in (root/"logs").glob("*-Logs/*") if p.is_file()}
    profile = council.load_council_profiles()["frontier"]
    credentials = dict.fromkeys(council.LIVE_KEYS, "OFFLINE_SECRET_VALUE")
    print("Installed contract:", ", ".join(f"{p}={version(p)}" for p in ("anthropic","agent-framework-anthropic","agent-framework-core")))
    recorded, rejected_signal, failures = [], asyncio.Event(), []

    class ObservedArchive(archives.RunArchive):
        def event(self, event, **fields):
            super().event(event, **fields)
            if event == "provider_rejected" and (fields.get("stage"),fields.get("provider")) == ("REVISION","anthropic"):
                rejected_signal.set()

    def recorder(*args, **kwargs):
        record = ObservedArchive(*args, **kwargs)
        recorded.append(record)
        return record

    async def run(fixture):
        candidate = council.build_council_workflow(profile,profile_id="frontier",credentials=credentials,transports=fixture.transports())
        try:
            async with asyncio.timeout(20):
                await candidate.run(QUESTION)
        except Exception:
            pass  # Assert the durable outcome and every expected dispatch below.
        return recorded[-1]

    cases = ("refusal_details", "refusal_no_details", "refusal_partial", "error_partial",
             "error_after_refusal", "eof_partial", "eof_after_delta", "missing_delta", "sse_error")
    with TemporaryDirectory(prefix="frontier-recording-", dir=Path(__file__).parent) as temporary, \
         patch.object(archives,"LOGS_DIR",Path(temporary)/"archives"), \
         patch.object(context_sources,"CONTEXT_DIR",Path(temporary)/"inputs"), \
         patch.object(council,"RunArchive",side_effect=recorder), patch("os.environ",{}), \
         patch("socket.socket.connect",side_effect=AssertionError("Network forbidden")), \
         patch("socket.getaddrinfo",side_effect=AssertionError("DNS forbidden")):
        context_sources.CONTEXT_DIR.mkdir()
        (context_sources.CONTEXT_DIR/"Reference.md").write_text("FROZEN_QUALIFICATION_CONTEXT",encoding="utf-8")
        for case in cases:
            fixture = FrontierFixture(case)
            record = await run(fixture)
            try:
                rejection = verify_saved(record, fixture, profile)
                print(f"{case}: PASS; SDK -> native capture -> fsynced JSONL/transcript; "
                      f"classification={rejection['classification']}; usage={rejection['usage_status']}; 9 unique requests, 8 accepted")
            except AssertionError as exc:
                import traceback
                failures.append(case)
                print(f"{case}: FAIL; {exc}")
                traceback.print_exc()
        # Streamed Frontier refusal occurs before either independent REVISION
        # sibling is released. Inspect the durable files while work is active.
        fixture = FrontierFixture("refusal_partial")
        release = asyncio.Event()
        fixture.holds.update({("REVISION",p):release for p in ("openai","google")})
        rejected_signal.clear()
        task = asyncio.create_task(run(fixture))
        try:
            async with asyncio.timeout(20): await rejected_signal.wait()
            record = recorded[-1]
            interim = [json.loads(line) for line in record.paths[1].read_text(encoding="utf-8").splitlines()]
            assert rows_for(interim,"provider_rejected") and record.active and not task.done()
            for provider in ("openai","google"):
                assert any(r["event"] == "provider_dispatched" and r.get("stage") == "REVISION" and r.get("provider") == provider for r in interim)
                assert not any(r["event"] in ("provider_accepted","provider_rejected","provider_completed") and r.get("stage") == "REVISION" and r.get("provider") == provider for r in interim)
        finally:
            release.set()
            await task
        verify_saved(record,fixture,profile)
        assert {r["provider"] for r in record.records if r["event"] == "provider_accepted" and r.get("stage") == "REVISION"} == {"openai","google"}
        print("Frontier REVISION sibling isolation: PASS; Anthropic rejected while 2 siblings active; both later accepted/preserved; stage failed; no retry/redispatch")
    recovery.FROZEN_CONTEXTS.clear()
    assert all(sha256(p.read_bytes()).hexdigest() == digest for p,digest in history.items())
    assert not Path(temporary).exists()
    print(f"Historical evidence: {len(history)} files byte-identical; temporary archives removed; zero network/provider calls")
    assert not failures, f"Qualification failures: {failures}"


if __name__ == "__main__":
    asyncio.run(qualify())
