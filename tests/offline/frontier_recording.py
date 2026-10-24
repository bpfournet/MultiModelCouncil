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
LONG_OUTPUT = "".join(f"## Synthetic observation {i}\n\nPreserve ordinary text: café 🧪.\n```text\nrow={i}\n```\n\n" for i in range(700))
HIDDEN = "HIDDEN_THOUGHT_MUST_NOT_BE_SAVED"
SIGNATURE = "SYNTHETIC_SIGNATURE_MUST_NOT_BE_SAVED"
DETAILS = dict(type="refusal", category="cyber", explanation="Synthetic provider-supplied refusal explanation.")


class FrontierFixture(Fixtures):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.output += "\n## Proposal A\nKeep this whole review.\n## Proposal B\n```text\nUnicode café 🧪\n```"
        self.refusal = case.startswith("refusal") or case == "error_after_refusal"
        self.details = DETAILS if self.refusal and case != "refusal_no_details" else None
        self.text = "" if case in ("refusal_zero_tokens", "refusal_details", "refusal_no_details") else PARTIAL
        self.start_tokens = 0 if case == "refusal_zero_tokens" else 1
        self.output_tokens = 0 if case == "refusal_zero_tokens" else 17
        if case == "success_substantial":
            self.text, self.output_tokens, self.reasoning = LONG_OUTPUT, 2048, True

    async def handler(self, request):
        body = json.loads(request.content)
        if request.url.host != "api.anthropic.com" or council.STAGE_INSTRUCTIONS["REVISION"] not in json.dumps(body):
            return await super().handler(request)
        self.calls.append(("REVISION", "anthropic", body))
        assert body["stream"] is True
        events = [dict(type="message_start", message=dict(id="msg_frontier_qualification", type="message",
            role="assistant", model=body["model"], content=[], stop_reason=None, stop_sequence=None,
            stop_details=None, usage=dict(input_tokens=313, output_tokens=self.start_tokens,
                cache_creation_input_tokens=4, cache_read_input_tokens=3)))]
        index = 1 if self.reasoning else 0
        if self.reasoning:
            events += [dict(type="content_block_start", index=0, content_block=dict(type="thinking", thinking="", signature="")),
                dict(type="content_block_delta", index=0, delta=dict(type="thinking_delta", thinking=HIDDEN)),
                dict(type="content_block_delta", index=0, delta=dict(type="signature_delta", signature=SIGNATURE)),
                dict(type="content_block_stop", index=0)]
        if self.text:
            events.append(dict(type="content_block_start", index=index, content_block=dict(type="text", text="")))
            events.extend(dict(type="content_block_delta", index=index, delta=dict(type="text_delta", text=self.text[i:i+257]))
                          for i in range(0, len(self.text), 257))
            events.append(dict(type="content_block_stop", index=index))
        final_usage = dict(output_tokens=self.output_tokens)
        if self.reasoning:
            final_usage["output_tokens_details"] = dict(thinking_tokens=1536)
        if self.case not in ("error_partial", "eof_partial", "missing_delta", "sse_error"):
            events.append(dict(type="message_delta", delta=dict(stop_reason="refusal" if self.refusal else "end_turn",
                stop_sequence=None, stop_details=self.details), usage=final_usage))
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
    assert all(value not in saved + transcript for value in ("OFFLINE_SECRET_VALUE", HIDDEN, SIGNATURE))
    if fixture.case == "success_substantial":
        return verify_success(record, rows, fixture, profile)
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
    assert native["output_tokens"] == (1 if fixture.case in ("error_partial","eof_partial","missing_delta","sse_error") else fixture.output_tokens)
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
        assert completed["native_usage"] == native and completed["usage"]["output_token_count"] == fixture.output_tokens
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
    verify_reconstruction(record, rows, fixture, profile)
    return rejected


def verify_success(record, rows, fixture, profile):
    outcome = rows[-1]
    assert outcome["event"] == "run_completed"
    assert outcome["new_requests_attempted"] == outcome["new_results_accepted"] == 9
    assert outcome["archive_status"] == "no_errors_observed" and outcome["actual_cost_usd"] is None
    assert {r["stage"] for r in rows if r["event"] == "stage_accepted"} == set(council.STAGES)
    assert not any(r["event"] == "provider_rejected" for r in rows)
    completed, = rows_for(rows, "provider_completed")
    accepted, = rows_for(rows, "provider_accepted")
    headers, = rows_for(rows, "provider_headers")
    opened = next(r for r in rows_for(rows, "provider_receiving") if r.get("observation") == "SDK stream opened")
    assert headers["http_status"] == 200 and headers["request_ids"]["request-id"] == "req_frontier_qualification"
    assert {r["attempt_id"] for r in (headers, opened, completed, accepted)} == {completed["attempt_id"]}
    assert completed["response_id"] == completed["stream_response_id"] == "msg_frontier_qualification"
    assert opened["provider_request_id"] == completed["provider_request_id"] == "req_frontier_qualification"
    assert completed["response_model"] == profile["providers"]["anthropic"]["model"]
    assert completed["raw_stop_reason"] == completed["stream_stop_reason"] == "end_turn"
    assert completed["finish_reason"] == "stop" and not completed["native_refusal"] and not completed["normalized_content_filter"]
    assert completed["stop_details"] is None and completed["stream_stop_details"] is None
    assert completed["terminal_event_ms"] is not None and completed["usage_status"] == "final"
    assert completed["first_ordinary_text_ms"] is not None and completed["first_stream_activity_ms"] is not None
    native = completed["native_usage"]
    assert native == completed["stream_native_usage"]
    assert native["input_tokens"] == 313 and native["output_tokens"] == fixture.output_tokens
    assert native["cache_creation_input_tokens"] == 4 and native["cache_read_input_tokens"] == 3
    assert native["output_tokens_details"]["thinking_tokens"] == 1536
    assert completed["usage"]["output_token_count"] == fixture.output_tokens
    text_rows = rows_for(rows, "provider_text")
    text = "".join(r["text_delta"] for r in text_rows)
    assert len(text_rows) > 1 and text == LONG_OUTPUT
    raw = text.encode("utf-8")
    assert len(raw) > 50_000 and completed["response_text_bytes"] == completed["combined_text_bytes"] == len(raw)
    assert accepted["response_sha256"] == sha256(raw).hexdigest()
    # The readable transcript attributes every batch; its labels are not part
    # of the response body. Each original batch must survive byte-for-byte.
    transcript, cursor = record.paths[0].read_bytes(), 0
    for row in text_rows:
        fragment = row["text_delta"].encode("utf-8")
        position = transcript.find(fragment, cursor)
        assert position >= cursor
        cursor = position + len(fragment)
    verify_reconstruction(record, rows, fixture, profile)
    return completed


def verify_reconstruction(record, rows, fixture, profile):
    # Force an on-disk read and reload the original source manifest; a live
    # recorder/context cache must not conceal body, attribution or order loss.
    with patch.object(archives, "LIVE_ARCHIVES", {}), patch.object(recovery, "FROZEN_CONTEXTS", {}):
        data = recovery.read_run(record.run_id)
        assert data.records == rows and not data.interrupted_tail and not data.active
        bodies = recovery.response_texts(data)
        recipe = recovery.reconstruct(record.run_id)
        assert recipe.missing == ([] if fixture.case == "success_substantial" else ["anthropic"])
        assert recipe.question == QUESTION and recipe.profile == profile
        manifest = next(r["metadata"] for r in rows if r["event"] == "context_prepared")
        bundle = recipe.bundle()
        assert recipe.context_manifest == manifest == json.loads(bundle.metadata_json)
        assert {source["source_id"] for source in manifest["sources"]} == {"Reference.md", "Assumptions.txt"}
        for source in manifest["sources"]:
            raw = (context_sources.CONTEXT_DIR / source["relative_path"]).read_bytes()
            assert source["sha256"] == sha256(raw).hexdigest() and source["byte_size"] == len(raw)
        revisions = council.prepare_revision_inputs(recipe.accepted, question=QUESTION)
        wires = {(s,p):b for s,p,b in fixture.calls}
        for provider in council.LIVE_KEYS:
            # Fixtures emit whole, distinct bodies, including Markdown/Unicode.
            for stage in council.STAGES[:2]:
                assert bodies[stage,provider] == fixture.output + f"\n{stage}/{provider}"
                assert recipe.references[stage + ":" + provider]["sha256"] == sha256(bodies[stage,provider].encode()).hexdigest()
            wire = wires["REVISION",provider]
            if provider == "anthropic":
                canonical = json.dumps(json.loads(revisions[provider]), ensure_ascii=False, sort_keys=True)
                assert wire["system"] == recipe.templates["REVISION"]
                assert wire["messages"] == [dict(role="user", content=[dict(type="text", text=value)])
                                            for value in (QUESTION, canonical, bundle.text)]
            request = json.loads(revisions[provider])
            assert request["origin"]["proposal"] == bodies["INITIAL",provider]
            pid = request["origin"]["proposal_id"]
            expected = []
            for reviewer in council.LIVE_KEYS:
                if reviewer == provider: continue
                # Independently use the ORIGINAL delivered CRITIQUE request's
                # anonymous mapping, not reconstructed coordinator provenance.
                critique = wires["CRITIQUE",reviewer]
                if reviewer == "anthropic":
                    payload = json.loads(critique["messages"][1]["content"][0]["text"])
                elif reviewer == "openai":
                    user_messages = [m for m in critique["input"] if m["role"] == "user"]
                    payload = json.loads(user_messages[1]["content"][0]["text"])
                else:
                    payload = json.loads(critique["contents"][1]["parts"][0]["text"])
                peer, = [p for p in payload["peers"] if p["proposal_id"] == pid]
                expected.append(dict(proposal_id=pid, critique_id=peer["critique_id"], delivery_id=peer["delivery_id"],
                    critique=bodies["CRITIQUE",reviewer], own_label=peer["label"], kind="combined_unsegmented_review",
                    reviewed=[{k:p[k] for k in ("label", "proposal_id")} for p in payload["peers"]]))
            assert request["feedback"] == sorted(expected, key=lambda item: item["critique_id"])
        text = bodies.get(("REVISION", "anthropic"), "")
        assert text.encode() == fixture.text.encode()
    identity = next(r["identity"] for r in rows if r["event"] == "implementation_loaded")
    assert identity == council.loaded_identity(profile)
    assert identity["template_sha256"] == sha256(json.dumps(recipe.templates, sort_keys=True).encode()).hexdigest()
    assert identity["profile_sha256"] == sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    assert len(fixture.calls) == len(wires) == 9
    assert sum(r["event"] == "provider_dispatched" for r in rows) == 9
    assert all(r["attempt"] == 1 for r in rows if r["event"].startswith("provider_"))


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

    cases = ("refusal_zero_tokens", "refusal_details", "refusal_no_details", "refusal_partial", "error_partial",
             "error_after_refusal", "eof_partial", "eof_after_delta", "missing_delta", "sse_error", "success_substantial")
    with TemporaryDirectory(prefix="frontier-recording-", dir=Path(__file__).parent) as temporary, \
         patch.object(archives,"LOGS_DIR",Path(temporary)/"archives"), \
         patch.object(context_sources,"CONTEXT_DIR",Path(temporary)/"inputs"), \
         patch.object(council,"RunArchive",side_effect=recorder), patch("os.environ",{}), \
         patch("socket.socket.connect",side_effect=AssertionError("Network forbidden")), \
         patch("socket.getaddrinfo",side_effect=AssertionError("DNS forbidden")):
        context_sources.CONTEXT_DIR.mkdir()
        (context_sources.CONTEXT_DIR/"Reference.md").write_text("FROZEN_QUALIFICATION_CONTEXT",encoding="utf-8")
        (context_sources.CONTEXT_DIR/"Assumptions.txt").write_text("Synthetic assumptions: café 🧪.", encoding="utf-16")
        for case in cases:
            fixture = FrontierFixture(case)
            record = await run(fixture)
            try:
                rejection = verify_saved(record, fixture, profile)
                print(f"{case}: PASS; SDK -> native capture -> fsynced JSONL/transcript; "
                      f"usage={rejection['usage_status']}; output_tokens={rejection['stream_native_usage']['output_tokens']}; "
                      f"ordinary_bytes={len(fixture.text.encode())}; exact reconstruction; 9 unique requests")
            except AssertionError as exc:
                import traceback
                failures.append(case)
                print(f"{case}: FAIL; {exc}")
                traceback.print_exc()
        # Streamed Frontier refusal occurs before either independent REVISION
        # sibling is released. Inspect the durable files while work is active.
        fixture = FrontierFixture("refusal_zero_tokens")
        release = asyncio.Event()
        fixture.holds.update({("REVISION",p):release for p in ("openai","google")})
        rejected_signal.clear()
        task = asyncio.create_task(run(fixture))
        try:
            async with asyncio.timeout(20): await rejected_signal.wait()
            record = recorded[-1]
            interim = [json.loads(line) for line in record.paths[1].read_text(encoding="utf-8").splitlines()]
            rejected, = rows_for(interim,"provider_rejected")
            completed, = rows_for(interim,"provider_completed")
            assert record.active and not task.done()
            assert rejected["usage_status"] == "final" and rejected["stream_native_usage"]["output_tokens"] == 0
            assert completed["response_text_bytes"] == 0 and completed["stop_details"] == DETAILS
            for provider in ("openai","google"):
                assert any(r["event"] == "provider_dispatched" and r.get("stage") == "REVISION" and r.get("provider") == provider for r in interim)
                assert not any(r["event"] in ("provider_accepted","provider_rejected","provider_completed") and r.get("stage") == "REVISION" and r.get("provider") == provider for r in interim)
        finally:
            release.set()
            await task
        verify_saved(record,fixture,profile)
        assert {r["provider"] for r in record.records if r["event"] == "provider_accepted" and r.get("stage") == "REVISION"} == {"openai","google"}
        print("Frontier REVISION sibling isolation: PASS; zero-token Anthropic refusal rejected while 2 siblings active; both later accepted/preserved; stage failed; no retry/redispatch")
    recovery.FROZEN_CONTEXTS.clear()
    assert all(sha256(p.read_bytes()).hexdigest() == digest for p,digest in history.items())
    assert not Path(temporary).exists()
    print(f"Historical evidence: {len(history)} files byte-identical; temporary archives removed; zero network/provider calls")
    assert not failures, f"Qualification failures: {failures}"


if __name__ == "__main__":
    asyncio.run(qualify())
