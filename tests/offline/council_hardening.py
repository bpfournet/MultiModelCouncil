"""Bounded extensions to the Council smoke suite; actual SDKs, synthetic HTTP only."""

import asyncio
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from weakref import WeakValueDictionary

import httpx
import httpx2

from application.council import workflow as council, run_archive as archives, context_sources, recovery
from application.council.devui import CouncilDevServer


class Fixtures:
    def __init__(self):
        self.calls, self.faults, self.holds = [], {}, {}
        self.fragment = 4096
        self.output = "Ordinary answer 🧪.\n" + "useful output " * 20
        self.stream_pause = None
        self.stream_started = asyncio.Event()
        self.stage_signal = {s: asyncio.Event() for s in council.STAGES}
        self.advance_time = None
        self.reasoning = False

    def transports(self):
        return {p:(httpx if p == "google" else httpx2).MockTransport(self.handler) for p in council.LIVE_KEYS}

    async def handler(self, request):
        body = json.loads(request.content)
        provider = {"api.openai.com":"openai", "api.anthropic.com":"anthropic", "generativelanguage.googleapis.com":"google"}[request.url.host]
        stage = next(s for s,t in council.STAGE_INSTRUCTIONS.items() if t in json.dumps(body))
        self.calls.append((stage, provider, body))
        self.stage_signal[stage].set()
        hold = self.holds.get((stage,provider))
        if hold: await hold.wait()
        fault = self.faults.get((stage,provider))
        http = httpx if provider == "google" else httpx2
        if fault and fault.startswith("http"):
            return http.Response(int(fault[4:]), json={"error":{"type":"synthetic_error", "message":"Useful failure. Authorization: Bearer OFFLINE_SECRET_VALUE"}},
                                 headers={"request-id":"req_error", "x-request-id":"req_error"})
        if fault == "transport": raise http.ReadError("Useful transport cause; Authorization: Bearer OFFLINE_SECRET_VALUE")
        if fault == "timeout": await asyncio.sleep(10)
        if fault == "malformed": return http.Response(200, json={"invalid":True,"thinking":"HIDDEN_THOUGHT_MUST_NOT_BE_SAVED"})
        text = self.output + f"\n{stage}/{provider}"
        if fault in ("refusal", "refusal_details"): text = ""
        model = body.get("model", request.url.path.split("/")[-1].split(":")[0])
        if provider == "openai":
            payload = dict(id="resp_synthetic", object="response", created_at=0, model=model, status="completed",
                output=[dict(id="msg_synthetic", type="message", status="completed", role="assistant", content=[
                    dict(type="output_text", text=text, annotations=[])])],
                usage=dict(input_tokens=10, output_tokens=20, total_tokens=30,
                           input_tokens_details=dict(cached_tokens=3), output_tokens_details=dict(reasoning_tokens=7)))
            if fault in ("refusal", "refusal_details"):
                payload["output"][0]["content"] = [dict(type="refusal",refusal="Synthetic explicit refusal")]
            if fault == "length": payload.update(status="incomplete",incomplete_details=dict(reason="max_output_tokens"))
        elif provider == "anthropic":
            payload = dict(id="msg_synthetic", type="message", role="assistant", model=model, content=[dict(type="text",text=text)],
                stop_reason="refusal" if fault in ("refusal","refusal_details") else "max_tokens" if fault == "length" else "end_turn",
                stop_sequence=None, usage=dict(input_tokens=10,output_tokens=20,cache_creation_input_tokens=4,cache_read_input_tokens=3))
            if fault == "refusal_details": payload["stop_details"] = dict(type="refusal",category="cyber",explanation="Synthetic supplied explanation. Authorization: Bearer OFFLINE_SECRET_VALUE")
            if self.reasoning: payload["content"].insert(0,dict(type="thinking",thinking="HIDDEN_THOUGHT_MUST_NOT_BE_SAVED",signature="synthetic-signature"))
        else:
            payload = dict(responseId="gemini_synthetic", modelVersion=model,
                candidates=[dict(content=dict(role="model",parts=[dict(text=text)]),index=0,
                                 finishReason="MAX_TOKENS" if fault == "length" else "SAFETY" if fault in ("refusal","refusal_details") else "STOP",
                                 finishMessage="Synthetic finish explanation" if fault else None)],
                usageMetadata=dict(promptTokenCount=10,candidatesTokenCount=20,totalTokenCount=37,thoughtsTokenCount=7,cachedContentTokenCount=3))
        headers = {"request-id":"req_anthropic", "x-request-id":"req_openai", "x-goog-request-id":"req_google"}
        if body.get("stream"):
            assert provider == "anthropic"
            events = [dict(type="message_start",message={**payload,"content":[],"stop_reason":None,
                        "usage":{**payload["usage"],"output_tokens":0}})]
            index = 1 if self.reasoning else 0
            if self.reasoning:
                events += [dict(type="content_block_start",index=0,content_block=dict(type="thinking",thinking="",signature="")),
                           dict(type="content_block_delta",index=0,delta=dict(type="thinking_delta",thinking="HIDDEN_THOUGHT_MUST_NOT_BE_SAVED")),
                           dict(type="content_block_stop",index=0)]
            events.append(dict(type="content_block_start",index=index,content_block=dict(type="text",text="")))
            events.extend(dict(type="content_block_delta",index=index,delta=dict(type="text_delta",text=text[i:i+self.fragment]))
                          for i in range(0,len(text),self.fragment))
            events += [dict(type="content_block_stop",index=index),
                       dict(type="message_delta",delta=dict(stop_reason=payload["stop_reason"],stop_sequence=None,
                            stop_details=payload.get("stop_details")),usage=dict(output_tokens=20)),dict(type="message_stop")]
            if fault == "missing_stop": events.pop()
            if fault == "stream_error": events = events[:3]
            owner = self
            class Stream(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    for event in events:
                        if event["type"] == "message_start" and owner.advance_time: owner.advance_time()
                        yield ("event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n").encode()
                        if event["type"] == "content_block_delta":
                            owner.stream_started.set()
                            if owner.stream_pause: await owner.stream_pause.wait()
                    if fault == "stream_error": raise httpx2.ReadError("Stream broke; Authorization: Bearer OFFLINE_SECRET_VALUE")
            return httpx2.Response(200,stream=Stream(),headers={**headers,"content-type":"text/event-stream"})
        if fault == "body_error":
            class Broken(http.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"content":[{"type":"text","text":"UNEXPOSED_TEXT'
                    raise http.ReadError("Body broke; Authorization: Bearer OFFLINE_SECRET_VALUE")
            return http.Response(200, stream=Broken(), headers=headers)
        return http.Response(200,json=payload,headers=headers)


def verify_recording(record):
    """Fixed synthetic schema contract, independent of private data and Git history."""
    expected = dict(run_started=1, implementation_loaded=1, context_prepared=1,
        stage_started=3, provider_preparing=9, provider_dispatched=9,
        provider_headers=9, provider_completed=9, provider_accepted=9,
        stage_accepted=3, run_completed=1)
    assert len(record.records) == 55 and Counter(r["event"] for r in record.records) == Counter(expected)
    started = next(r for r in record.records if r["event"] == "run_started")
    assert started["profile"]["name"] == "Testing Council"
    identity = next(r for r in record.records if r["event"] == "implementation_loaded")["identity"]
    assert all(identity[k] for k in ("sources","profile_sha256","template_sha256","loaded_workflow_code_sha256"))
    for row in record.records:
        if row["event"].startswith("provider_"):
            assert {"attempt","attempt_id","requested_model"}.issubset(row), row["event"]
        if row["event"] == "provider_completed":
            assert {"response_id","provider_request_id","native_usage",
                    "raw_stop_reason","native_refusal","stop_details","ordinary_blocks"}.issubset(row)
        if row["event"] == "provider_headers": assert row["request_ids"]
        if row["event"] == "provider_accepted": assert "response_sha256" in row
    completed = [r for r in record.records if r["event"] == "provider_completed"]
    assert all(r["provider_request_id"] == "req_anthropic" for r in completed if r["provider"] == "anthropic")
    bodies = recovery.response_texts(recovery.read_run(record.run_id))
    transcript = record.paths[0].read_text(encoding="utf-8")
    assert transcript.startswith("# MultiModelCouncil Transcript\n") and "**Council:** Testing Council" in transcript
    assert len(bodies) == 9 and all(body in transcript for body in bodies.values())
    assert record.records[-1]["new_results_accepted"] == 9
    print("Synthetic recorder: PASS; 55-event successful Testing structure, nine accepted bodies, generic identity, native metadata and Anthropic request IDs")


def verify_synthetic_legacy(record):
    """Exercise exact legacy framing using only this test's owned synthetic pair."""
    originals = {p:p.read_bytes() for p in record.paths}
    expected = recovery.response_texts(recovery.read_run(record.run_id))
    before = recovery.reconstruct(record.run_id)
    rows = deepcopy(record.records)
    rows = [r for r in rows if r["event"] != "implementation_loaded"]
    rows[0]["schema_version"] = 1
    for row in rows:
        if row["event"] == "provider_completed": row.pop("ordinary_blocks",None)
    def save():
        record.paths[1].write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8",newline="\n")
    try:
        record.paths[0].write_text("".join(record._markdown(row,
            [expected[row["stage"],row["provider"]]] if row["event"] == "provider_completed" else None)
            for row in rows),encoding="utf-8",newline="\n")
        save()
        with patch.object(archives,"LIVE_ARCHIVES",WeakValueDictionary()):
            assert recovery.response_texts(recovery.read_run(record.run_id)) == expected
            after = recovery.reconstruct(record.run_id)
            assert after.question == before.question and after.profile == before.profile
            assert after.missing == before.missing and after.references == before.references
            assert after.context_manifest == before.context_manifest
            assert council.prepare_revision_inputs(after.accepted,question=after.question) == council.prepare_revision_inputs(before.accepted,question=before.question)
            completed = next(r for r in rows if r["event"] == "provider_completed")
            for field,value in (("text_block_count",2),("response_text_bytes",completed["response_text_bytes"]+1)):
                original = completed[field]; completed[field] = value; save()
                try: recovery.reconstruct(record.run_id)
                except recovery.RecoveryUnavailable: pass
                else: raise AssertionError("Ambiguous/corrupt legacy evidence accepted")
                completed[field] = original
        print(f"Synthetic legacy: PASS; {len(expected)} exact byte-counted bodies; missing revisions={len(before.missing)}; original question/settings/context/routing; ambiguous blocks and wrong byte counts rejected")
    finally:
        for path,content in originals.items(): path.write_bytes(content)
    assert all(p.read_bytes() == content for p,content in originals.items())


async def verify_hardening(*, sections=("matrix","recovery","stream","storage")):
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parents[2]
    historical_hashes = {p:sha256(p.read_bytes()).hexdigest() for p in (root/"logs").glob("*-Logs/*") if p.is_file()}
    credentials = dict.fromkeys(council.LIVE_KEYS, "OFFLINE_SECRET_VALUE")
    profiles = council.load_council_profiles()
    identity = council.loaded_identity(profiles["testing"])
    with patch.object(Path,"read_bytes",side_effect=AssertionError("Identity reread changed disk source")):
        assert council.loaded_identity(profiles["testing"]) == identity
    assert identity["loaded_workflow_code_sha256"]
    recorded, signals = [], {}
    class ObservedArchive(archives.RunArchive):
        def event(self, event, **fields):
            super().event(event, **fields)
            signals.setdefault((event,fields.get("stage"),fields.get("provider")),asyncio.Event()).set()
    def recorder(*args, **kwargs):
        record = ObservedArchive(*args, **kwargs); recorded.append(record); return record
    async def wait_event(event, stage, provider):
        async with asyncio.timeout(10): await signals.setdefault((event,stage,provider),asyncio.Event()).wait()
    async def run(fixture, key="testing", *, profile=None, module=council):
        config = deepcopy(profile or profiles[key])
        candidate = module.build_council_workflow(config,profile_id=key,credentials=credentials,transports=fixture.transports())
        try: await candidate.run("Original offline question")
        except Exception: pass
        return recorded[-1]
    with TemporaryDirectory(prefix="council-hardening-",dir=Path(__file__).parent) as temporary, \
         patch.object(archives,"LOGS_DIR",Path(temporary)/"archives"), \
         patch.object(context_sources,"CONTEXT_DIR",Path(temporary)/"inputs"), \
         patch.object(council,"RunArchive",side_effect=recorder), patch("os.environ",{}), \
         patch("socket.socket.connect",side_effect=AssertionError("External network forbidden")), \
         patch("socket.getaddrinfo",side_effect=AssertionError("DNS forbidden")):
        inputs = context_sources.CONTEXT_DIR; inputs.mkdir()
        source = inputs/"Reference.md"; source.write_text("ORIGINAL_FROZEN_CONTEXT",encoding="utf-8")
        successful = await run(Fixtures())
        verify_recording(successful)
        verify_synthetic_legacy(successful)
        # Real sibling collection and live projection in all three stages.
        for stage in (council.STAGES if "matrix" in sections else ()):
            fixture = Fixtures(); fixture.faults[stage,"anthropic"] = "refusal"
            release = asyncio.Event()
            fixture.holds.update({(stage,p):release for p in ("openai","google")})
            signals.clear()
            task = asyncio.create_task(run(fixture))
            await wait_event("provider_rejected",stage,"anthropic")
            record = recorded[-1]
            interim = recovery.read_run(record.run_id)
            assert interim.active
            assert any(r["event"] == "provider_completed" and r.get("stage") == stage and r.get("provider") == "anthropic" and r.get("native_refusal") for r in interim.records)
            for provider in ("openai","google"):
                assert any(r["event"] == "provider_dispatched" and r.get("stage") == stage and r.get("provider") == provider for r in interim.records)
                assert not any(r["event"] in ("provider_completed","provider_rejected") and r.get("stage") == stage and r.get("provider") == provider for r in interim.records)
            assert not task.done()
            release.set(); await task
            rejected = [r for r in record.records if r["event"] == "provider_rejected"]
            assert len(rejected) == 1 and rejected[0]["provider"] == "anthropic"
            accepted = [r for r in record.records if r["event"] == "provider_accepted" and r["stage"] == stage]
            assert {r["provider"] for r in accepted} == {"openai","google"}
            assert record.records[-1]["event"] == "run_failed"
            assert all(council.STAGES.index(s) <= council.STAGES.index(stage) for s,_,_ in fixture.calls)
            assert len(fixture.calls) == (council.STAGES.index(stage)+1)*3
            print(f"Isolation {stage}: PASS; siblings remained running after refusal; 2/3 independently accepted; {len(fixture.calls)} dispatches; dependent stage blocked")
        # Each supported provider/mode through native adapter, validation and archive.
        cases = [("testing",p,f) for p in council.LIVE_KEYS for f in
                                    ("refusal","length","http401","http429","http503","transport","malformed","body_error")] + [
                                    ("frontier","anthropic",f) for f in ("refusal","refusal_details","length","stream_error","missing_stop")]
        for key, provider, fault in (cases if "matrix" in sections else ()):
            fixture = Fixtures(); fixture.faults["REVISION",provider] = fault
            record = await run(fixture,key)
            assert record.records[-1]["event"] == "run_failed", (key,provider,fault)
            rejected = next(r for r in record.records if r["event"] == "provider_rejected" and r["provider"] == provider)
            assert len(fixture.calls) == 9 and len({(s,p) for s,p,_ in fixture.calls}) == 9
            saved = record.paths[1].read_text(encoding="utf-8")
            assert "OFFLINE_SECRET_VALUE" not in saved
            assert "HIDDEN_THOUGHT_MUST_NOT_BE_SAVED" not in saved
            assert any(r["event"] == "provider_headers" and r["provider"] == provider for r in record.records) or fault == "transport"
            if provider == "anthropic" and fault.startswith("refusal"):
                assert rejected["raw_stop_reason"] == "refusal" and rejected["refusal"] is False
                completed = next(r for r in record.records if r["event"] == "provider_completed" and r["stage"] == "REVISION" and r["provider"] == provider)
                assert completed["native_refusal"] is True and completed["refusal_text_tagged"] is False
                if key == "frontier":
                    assert completed["stream_response_id"] == "msg_synthetic"
                    assert completed["provider_request_id"] == "req_anthropic"
                    assert completed["stream_native_usage"]["output_tokens"] == 20
                if fault == "refusal_details": assert "Synthetic supplied explanation" in completed["stop_details"]["explanation"]
                else: assert completed["stop_details"] is None
            if provider == "google" and fault == "refusal":
                completed = next(r for r in record.records if r["event"] == "provider_completed" and r["stage"] == "REVISION" and r["provider"] == provider)
                assert completed["native_refusal"] is False and completed["normalized_content_filter"] is True
            if fault == "body_error": assert "UNEXPOSED_TEXT" not in saved + record.paths[0].read_text(encoding="utf-8")
        if "matrix" in sections: print("Provider failure matrix: PASS; OpenAI/Gemini/Anthropic nonstream plus configured Anthropic stream; refusal semantics/details, limits, 401/429/503, malformed and transport/body failures; nine attempts, no retry")
        if "recovery" in sections: await verify_recovery_and_transport(run, profiles, credentials, recorded, source)
        if "stream" in sections: await verify_stream_durability(run, profiles, recorded, signals, wait_event)
        if "storage" in sections: await verify_storage(run, profiles, recorded, temporary)
    recovery.FROZEN_CONTEXTS.clear()
    assert all(sha256(p.read_bytes()).hexdigest() == digest for p,digest in historical_hashes.items())
    assert not Path(temporary).exists()
    print(f"HARDENING {','.join(sections)}: PASS; disposable archives removed; {len(historical_hashes)} historical files byte-identical; zero live requests")


async def verify_recovery_and_transport(run, profiles, credentials, recorded, source):
    import subprocess
    from uuid import uuid4
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    from zipfile import ZipFile
    zip_path = source.parent/"Reference.zip"
    with ZipFile(zip_path,"w") as archive: archive.writestr("Member.md","ORIGINAL_ZIP_MEMBER")
    fixture = Fixtures(); fixture.faults["REVISION","anthropic"] = "refusal"
    original = await run(fixture)
    original_hashes = {p:sha256(p.read_bytes()).hexdigest() for p in original.paths}
    recovery.FROZEN_CONTEXTS.clear()  # Exercise durable reconstruction, not Python objects.
    retry_fixture = Fixtures()
    server = CouncilDevServer(host="127.0.0.1",auth_enabled=True,auth_token="offline-auth",
                             council_credentials=credentials,council_transports=retry_fixture.transports())
    server.set_pending_entities(council.build_registered_councils(credentials=credentials,transports=retry_fixture.transports()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.get_app()),base_url="http://127.0.0.1:8080") as client:
        assert (await client.get("/v1/council/installation")).status_code == 401
        assert (await client.post(f"/v1/council/runs/{original.run_id}/retry-revisions",json={})).status_code == 401
        client.headers["Authorization"] = "Bearer offline-auth"
        assert (await client.get("/v1/council/installation",headers={"Host":"untrusted.example"})).status_code == 400
        with patch.object(context_sources,"load_context",side_effect=AssertionError("Selection must not read context")):
            install = (await client.get("/v1/council/installation")).json()
            assert len(install["profiles"]) == 3 and all(install["credentials"].values())
            shell = await client.get("/")
            import agent_framework_devui
            from application.council.devui_transport import guarded_bundle, INSTALLED_BUNDLE_SHA256, INITIAL_POST_GUARD
            ui = Path(agent_framework_devui.__file__).parent/"ui"
            assert shell.status_code == 200 and shell.content == (ui/"index.html").read_bytes()
            class Links(HTMLParser):
                def __init__(self): super().__init__(); self.urls = []
                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == "script" and attrs.get("type") == "module": self.urls.append(urljoin("/",attrs["src"]))
                    if tag == "link" and attrs.get("rel") == "stylesheet": self.urls.append(urljoin("/",attrs["href"]))
            links = Links(); links.feed(shell.text)
            assert set(links.urls) == {"/assets/index.js","/assets/index.css"}
            bundle = (await client.get("/assets/index.js")).content
            installed = (ui/"assets/index.js").read_bytes()
            assert sha256(installed).hexdigest() == INSTALLED_BUNDLE_SHA256
            assert bundle == guarded_bundle(installed)
            assert bundle.replace(INITIAL_POST_GUARD.encode(),b"",1) == installed
            assert (await client.get("/assets/index.css")).content == (ui/"assets/index.css").read_bytes()
            assert b"maf-council" not in bundle and b"MAFCouncilRetryItem" not in bundle
            assert b'n?.workflow_dump&&(0,T.jsx)(NM' in bundle
            for retired in ("/assets/council.js","/assets/council.css", "/v1/council/runs", f"/v1/council/runs/{original.run_id}"):
                assert (await client.get(retired)).status_code == 404
            assert install["frontend_transport"]["served_bundle_sha256"] == sha256(bundle).hexdigest()
            try: guarded_bundle(installed+b" ")
            except RuntimeError: pass
            else: raise AssertionError("Changed installed bundle was accepted")
            assert not retry_fixture.calls
            readiness = (await client.get(f"/v1/council/runs/{original.run_id}/recovery")).json()
            assert readiness["providers"] == ["anthropic"] and readiness["new_billable_attempts"] == 1
            print("Original DevUI HTTP: PASS; unchanged installed HTML/CSS, original graph/menu code, no custom assets/routes; only nonvisual duplicate-POST guard differs")
        assert (await client.post(f"/v1/council/runs/{original.run_id}/retry-revisions",json={"action_id":"bad","fingerprint":"bad"})).status_code == 400
        token = uuid4().hex
        body = dict(action_id=token,fingerprint=readiness["fingerprint"])
        # A changed current profile and an unrelated new source do not replace
        # original generation settings or expand the old reference envelope.
        server.council_profiles["testing"]["providers"]["anthropic"]["model"] = "changed-current-selection"
        (source.parent/"Unrelated.md").write_text("NEW_UNRELATED_BODY",encoding="utf-8")
        with ZipFile(zip_path,"w") as archive:
            archive.writestr("Member.md","ORIGINAL_ZIP_MEMBER")
            archive.writestr("Later.md","NEW_UNRELATED_ZIP_MEMBER")
        with patch.object(archives,"LIVE_ARCHIVES",WeakValueDictionary()):
            response = await client.post(f"/v1/council/runs/{original.run_id}/retry-revisions",json=body)
        assert response.status_code == 200
        assert [(s,p) for s,p,_ in retry_fixture.calls] == [("REVISION","anthropic")], (response.text,recorded[-1].records[-1])
        wire = retry_fixture.calls[0][2]
        assert wire["model"] == profiles["testing"]["providers"]["anthropic"]["model"]
        assert wire["max_tokens"] == profiles["testing"]["providers"]["anthropic"]["max_tokens"]
        assert wire["output_config"] == profiles["testing"]["providers"]["anthropic"]["output_config"]
        serialized = json.dumps(wire)
        assert "ORIGINAL_FROZEN_CONTEXT" in serialized and "NEW_UNRELATED_BODY" not in serialized
        assert "ORIGINAL_ZIP_MEMBER" in serialized and "NEW_UNRELATED_ZIP_MEMBER" not in serialized
        assert "REVISION/openai" not in serialized and "REVISION/google" not in serialized
        recovered = recorded[-1]
        end = recovered.records[-1]
        assert end["event"] == "run_completed" and end["new_requests_attempted"] == end["new_results_accepted"] == 1
        assert end["restored_responses"] == 8
        saved = recovered.paths[1].read_text(encoding="utf-8")
        assert "INITIAL/openai" not in saved and "CRITIQUE/google" not in saved
        assert all(sha256(p.read_bytes()).hexdigest() == digest for p,digest in original_hashes.items())
        second = await client.post(f"/v1/council/runs/{original.run_id}/retry-revisions",json=body)
        assert second.json()["replayed"] and len(retry_fixture.calls) == 1
        assert not (await client.get(f"/v1/council/runs/{original.run_id}/recovery")).json()["eligible"]
        # A fresh server reconstructs the same selective history and action ID.
        with patch.object(archives,"LIVE_ARCHIVES",WeakValueDictionary()):
            restarted = CouncilDevServer(host="127.0.0.1",auth_enabled=True,auth_token="offline-auth",
                                        council_credentials=credentials,council_transports=retry_fixture.transports())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted.get_app()),base_url="http://127.0.0.1:8080",
                                         headers=client.headers) as other:
                replay = await other.post(f"/v1/council/runs/{original.run_id}/retry-revisions",json=body)
                assert replay.json()["run_id"] == recovered.run_id and len(retry_fixture.calls) == 1
                source_text = recovery.response_texts(recovery.read_run(original.run_id))["INITIAL","openai"]
                link = next(r for r in recovery.read_run(recovered.run_id).records if r["event"] == "recovery_link")
                assert "INITIAL/openai" in source_text and link["reused_response_references"]["INITIAL:openai"]["run_id"] == original.run_id
        print("Recovery 2+1: PASS; authenticated operator API dispatched 1 Anthropic revision, reused 8; original settings/context/routing; replay and fresh-server reconstruction dispatched 0")
        # Three missing, then another deliberate action for the one still missing.
        zero = Fixtures(); zero.faults = {("REVISION",p):"refusal" for p in council.LIVE_KEYS}
        origin_zero = await run(zero)
        verify_synthetic_legacy(origin_zero)
        retry_fixture.calls.clear(); retry_fixture.faults = {("REVISION","anthropic"):"refusal"}
        state = recovery.readiness(origin_zero.run_id); assert state["new_billable_attempts"] == 3
        first = await client.post(f"/v1/council/runs/{origin_zero.run_id}/retry-revisions",json=dict(action_id=uuid4().hex,fingerprint=state["fingerprint"]))
        assert first.status_code == 200 and len(retry_fixture.calls) == 3
        assert {s for s,_,_ in retry_fixture.calls} == {"REVISION"}
        assert recorded[-1].records[-1]["event"] == "run_failed"
        state = recovery.readiness(origin_zero.run_id); assert state["providers"] == ["anthropic"]
        retry_fixture.calls.clear(); retry_fixture.faults.clear()
        await client.post(f"/v1/council/runs/{origin_zero.run_id}/retry-revisions",json=dict(action_id=uuid4().hex,fingerprint=state["fingerprint"]))
        assert [(s,p) for s,p,_ in retry_fixture.calls] == [("REVISION","anthropic")]
        assert recorded[-1].records[-1]["event"] == "run_completed"
        print("Recovery 0+3 / failure / deliberate retry: PASS; new dispatch counts 3 then 1; no INITIAL/CRITIQUE, no repeated accepted revision")
        two = Fixtures(); two.faults = {("REVISION",p):"refusal" for p in ("openai","anthropic")}
        origin_two = await run(two)
        state = recovery.readiness(origin_two.run_id)
        retry_fixture.calls.clear()
        await client.post(f"/v1/council/runs/{origin_two.run_id}/retry-revisions",json=dict(action_id=uuid4().hex,fingerprint=state["fingerprint"]))
        assert {(s,p) for s,p,_ in retry_fixture.calls} == {("REVISION","openai"),("REVISION","anthropic")}
        assert len(retry_fixture.calls) == 2 and recorded[-1].records[-1]["event"] == "run_completed"
        print("Recovery 1+2: PASS; exactly 2 missing revisions requested; 7 accepted responses reused")
        # Duplicated while active: same action retrieves identity; other action denied.
        another = Fixtures(); another.faults["REVISION","anthropic"] = "refusal"
        active_source = await run(another)
        retry_fixture.calls.clear()
        release = asyncio.Event(); retry_fixture.holds["REVISION","anthropic"] = release
        retry_fixture.stage_signal["REVISION"].clear()
        state = recovery.readiness(active_source.run_id); active_body = dict(action_id=uuid4().hex,fingerprint=state["fingerprint"])
        task = asyncio.create_task(client.post(f"/v1/council/runs/{active_source.run_id}/retry-revisions",json=active_body))
        try:
            async with asyncio.timeout(10): await retry_fixture.stage_signal["REVISION"].wait()
            duplicate = await client.post(f"/v1/council/runs/{active_source.run_id}/retry-revisions",json=active_body)
            assert duplicate.json()["replayed"] and duplicate.json()["active"]
            competing = await client.post(f"/v1/council/runs/{active_source.run_id}/retry-revisions",json=dict(active_body,action_id=uuid4().hex))
            assert competing.status_code == 409 and len(retry_fixture.calls) == 1
            running_id = duplicate.json()["run_id"]
            live = recovery.read_run(running_id)
            assert live.active and any(r["event"] == "provider_dispatched" and r.get("stage") == "REVISION" and r.get("provider") == "anthropic" for r in live.records)
            assert (await client.post(f"/v1/council/runs/{running_id}/stop-recovery")).status_code == 200
        finally: release.set()
        try: await task
        except (asyncio.CancelledError,AssertionError): pass  # Canceled ASGI stream may lack its final HTTP frame.
        stopped = recovery.read_run(running_id)
        assert not stopped.active and stopped.records[-1]["event"] == "run_failed"
        assert any(r.get("cancellation_initiator") == "human_stop" for r in stopped.records)
        retry_fixture.holds.clear()
        # Changed/missing required context fails before any new dispatch. Fresh
        # full runs remain allowed and intentionally load the current folder.
        changed = Fixtures(); changed.faults["REVISION","anthropic"] = "refusal"
        change_source = await run(changed)
        recovery.FROZEN_CONTEXTS.clear(); retry_fixture.calls.clear()
        original_bytes = source.read_bytes(); source.write_text("CHANGED_REQUIRED_CONTENT",encoding="utf-8")
        for missing in (False,True):
            if missing: source.unlink()
            state = recovery.readiness(change_source.run_id)
            await client.post(f"/v1/council/runs/{change_source.run_id}/retry-revisions",json=dict(action_id=uuid4().hex,fingerprint=state["fingerprint"]))
            assert not retry_fixture.calls and recorded[-1].records[-1]["event"] == "run_failed"
            assert "Required context" in json.dumps(recorded[-1].records[-1]["causes"])
        source.write_bytes(original_bytes)
        print("Recovery guards: PASS; active duplicate/competing clicks dispatched 0 extra; changed/missing required context dispatched 0; original archives unchanged")
        # Execute the actual served nonvisual transport in the existing Node harness.
        node = subprocess.run(["node",str(Path(__file__).with_name("devui_transport.test.mjs"))],
                              input=json.dumps(dict(stream_method="async*streamOpenAIResponse"+
                                  bundle.decode().split("async*streamOpenAIResponse",1)[1].split("async*streamAgentExecutionOpenAI",1)[0])),text=True,capture_output=True,timeout=20)
        assert node.returncode == 0, node.stdout + node.stderr
        print(node.stdout.strip())
        syntax = subprocess.run(["node","--check","--input-type=module"],input=bundle,capture_output=True,timeout=10)
        assert syntax.returncode == 0, syntax.stderr.decode()


async def verify_stream_durability(run, profiles, recorded, signals, wait_event):
    from application.council.attempts import DEVUI_SCOPE
    from contextlib import redirect_stderr
    from io import StringIO
    fixture = Fixtures(); fixture.fragment = 5; fixture.stream_pause = asyncio.Event()
    scope = {"initiator":"human_stop"}
    token = DEVUI_SCOPE.set(scope)
    with patch.object(archives.os,"fsync",wraps=archives.os.fsync) as synced:
        task = asyncio.create_task(run(fixture,"frontier"))
        try:
            async with asyncio.timeout(10): await fixture.stream_started.wait()
            record = recorded[-1]
            assert synced.call_count > 0
            saved = record.paths[1].read_text(encoding="utf-8")
            assert '"event": "provider_text"' in saved and '"text_delta": "Ordin"' in saved
            assert not any(r["event"] == "provider_completed" and r["provider"] == "anthropic" for r in record.records)
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            else: raise AssertionError("Stop cancellation was swallowed")
        finally:
            fixture.stream_pause.set(); DEVUI_SCOPE.reset(token)
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
    rejected = next(r for r in record.records if r["event"] == "provider_rejected" and r["provider"] == "anthropic")
    assert rejected["classification"] == "cancelled" and rejected["cancellation_initiator"] == "human_stop"
    assert rejected["stream_native_usage"]["input_tokens"] == 10 and rejected["usage_status"] == "partial_or_unavailable"
    assert recovery.response_texts(recovery.read_run(record.run_id))["INITIAL","anthropic"] == "Ordin"
    # Truncate a disposable copy of this fixture's final diagnostic tail. Reader
    # retains complete records and marks unfinished attempts unknown, without calls.
    rows = record.paths[1].read_bytes().splitlines(keepends=True)
    last_text = max(i for i,line in enumerate(rows) if b'"event": "provider_text"' in line)
    record.paths[1].write_bytes(b"".join(rows[:last_text+1]) + b'{"event":"run_completed"')
    count = len(fixture.calls)
    with patch.object(archives,"LIVE_ARCHIVES",WeakValueDictionary()):
        interrupted = recovery.read_run(record.run_id)
        assert interrupted.interrupted_tail and not interrupted.active
        assert not any(r["event"] in ("run_completed","run_failed") for r in interrupted.records)
        assert recovery.response_texts(interrupted)["INITIAL","anthropic"] == "Ordin"
    assert len(fixture.calls) == count
    fixture = Fixtures(); fixture.fragment = 5; fixture.faults["REVISION","anthropic"] = "stream_error"
    broken = await run(fixture,"frontier")
    rejection = next(r for r in broken.records if r["event"] == "provider_rejected" and r["stage"] == "REVISION")
    assert rejection["classification"] == "network_failure" and rejection["stream_response_id"] == "msg_synthetic"
    assert recovery.response_texts(recovery.read_run(broken.run_id))["REVISION","anthropic"] == "Ordin"
    assert "Stream broke" in json.dumps(rejection["causes"]) and "OFFLINE_SECRET_VALUE" not in json.dumps(broken.records)
    print("Stream durability: PASS; text fsynced before completion; partial text/IDs/usage survive error and Human Stop; truncated final record stays interrupted/unknown with zero redispatch")
    reasoning = Fixtures(); reasoning.reasoning = True
    hidden = await run(reasoning,"frontier")
    assert hidden.records[-1]["event"] == "run_completed"
    assert "HIDDEN_THOUGHT_MUST_NOT_BE_SAVED" not in "".join(p.read_text(encoding="utf-8") for p in hidden.paths)
    # Distinct individual request timeout and shared stopping conditions.
    fixture = Fixtures(); fixture.faults["INITIAL","openai"] = "timeout"
    short = deepcopy(profiles["testing"])
    short["execution"].update(request_seconds=3,stage_seconds=6,council_seconds=20)
    timed = await run(fixture,profile=short)
    assert sum(r["event"] == "provider_accepted" for r in timed.records) == 2
    assert next(r for r in timed.records if r["event"] == "provider_rejected")["classification"] == "provider_timeout"
    original_payload = council.LiveParticipant.make_payload
    async def slow_prepare(self, request, question):
        if self.id == "openai": await asyncio.sleep(4)
        return await original_payload(self,request,question)
    short["execution"].update(request_seconds=3,stage_seconds=3.1,council_seconds=10)
    with patch.object(council.LiveParticipant,"make_payload",slow_prepare):
        stage_expired = await run(Fixtures(),profile=short)
    assert any(r.get("cancellation_initiator") == "stage_deadline" for r in stage_expired.records)
    real_stage = council.run_stage
    async def expire_run(stage, *args, **kwargs):
        kwargs["accepted"]["deadline"] = asyncio.get_running_loop().time()+.2
        return await real_stage(stage,*args,**kwargs)
    fixture = Fixtures(); release = asyncio.Event()
    fixture.holds = {("INITIAL",p):release for p in council.LIVE_KEYS}
    with patch.object(council,"run_stage",expire_run):
        run_expired = await run(fixture)
    assert any(r.get("cancellation_initiator") == "run_deadline" for r in run_expired.records)
    assert not any(r["event"] == "run_completed" for r in run_expired.records)
    print("Deadlines: PASS; individual timeout preserves 2 siblings; stage deadline/run budget cancel pending work distinctly; cancellation propagated and tasks joined")
    # SDK cleanup cannot replace success or an initiating HTTP failure.
    close = httpx2.AsyncClient.aclose
    async def failing_close(self):
        await close(self)
        raise RuntimeError("Useful cleanup issue; Authorization: Bearer OFFLINE_SECRET_VALUE")
    for failure in (False,True):
        fixture = Fixtures()
        if failure: fixture.faults["REVISION","openai"] = "http503"
        with patch.object(httpx2.AsyncClient,"aclose",failing_close): result = await run(fixture)
        assert result.records[-1]["event"] == ("run_failed" if failure else "run_completed")
        assert any(r["event"] == "cleanup_warning" for r in result.records)
        if failure: assert result.records[-1]["classification"] == "provider_http_error"
        assert "OFFLINE_SECRET_VALUE" not in json.dumps(result.records)
    # A fully validated response survives Stop arriving during SDK cleanup.
    fixture = Fixtures(); reached_cleanup = asyncio.Event(); release_cleanup = asyncio.Event()
    async def hold_completed_cleanup(self):
        latest = recorded[-1]
        if any(r["event"] == "provider_accepted" and r.get("stage") == "REVISION" and r.get("provider") == "openai" for r in latest.records):
            reached_cleanup.set(); await release_cleanup.wait()
        await close(self)
    with patch.object(httpx2.AsyncClient,"aclose",hold_completed_cleanup):
        task = asyncio.create_task(run(fixture))
        try:
            async with asyncio.timeout(15): await reached_cleanup.wait()
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            else: raise AssertionError("Cleanup swallowed Stop")
        finally:
            release_cleanup.set()
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
    retained = recorded[-1]
    assert any(r["event"] == "provider_accepted" and r.get("provider") == "openai" and r.get("stage") == "REVISION" for r in retained.records)
    assert not any(r["event"] == "provider_rejected" and r.get("provider") == "openai" and r.get("stage") == "REVISION" for r in retained.records)
    assert "openai" not in recovery.reconstruct(retained.run_id).missing
    corrupt = recovery.read_run(retained.run_id)
    altered = next(r for r in corrupt.records if r["event"] == "provider_completed" and r.get("ordinary_blocks"))
    altered["ordinary_blocks"][0] += "CORRUPTED_BODY"
    try: recovery.response_texts(corrupt)
    except recovery.RecoveryUnavailable as exc: assert "body verification failed" in str(exc)
    else: raise AssertionError("Accepted body corruption was silently reused")
    # Simulate a failing JSONL destination; a surviving transcript and live view
    # show the archive warning without changing provider acceptance.
    original_append, attempts = archives.RunArchive._append, []
    def failed_append(path, content):
        if path.name.endswith("-Diagnostics.jsonl") and '"event": "provider_completed"' in content:
            attempts.append(path); raise PermissionError("Must not print this; OFFLINE_SECRET_VALUE")
        original_append(path,content)
    terminal = StringIO()
    with patch.object(archives.RunArchive,"_append",staticmethod(failed_append)), redirect_stderr(terminal):
        result = await run(Fixtures())
    assert result.records[-1]["event"] == "run_completed" and result.records[-1]["new_results_accepted"] == 9
    assert len(attempts) == 1 and result.notice()
    assert any(r["event"] == "archive_error" for r in recovery.read_run(result.run_id).records)
    assert "ARCHIVE ERROR" in result.paths[0].read_text(encoding="utf-8") + terminal.getvalue()
    assert "OFFLINE_SECRET_VALUE" not in terminal.getvalue()
    print("Recorder/cleanup failures: PASS; original provider outcome preserved; destination disabled once; archive incomplete visible in surviving transcript/live state/terminal; causes redacted")
    # Preparation failure before transport dispatch still yields durable evidence.
    fixture = Fixtures()
    with patch.object(context_sources,"load_context",side_effect=OSError("Preparation failed")):
        prepared = await run(fixture)
    assert not fixture.calls and prepared.records[-1]["event"] == "run_failed"
    # Empty reference handling through every real registration profile.
    empty = context_sources.CONTEXT_DIR/"empty"; empty.mkdir()
    with patch.object(context_sources,"CONTEXT_DIR",empty):
        for key in council.PROFILE_IDS:
            fixture = Fixtures(); result = await run(fixture,key)
            assert len(fixture.calls) == 9 and result.records[-1]["event"] == "run_completed"
            assert next(r for r in result.records if r["event"] == "context_prepared")["metadata"]["sources"] == []
    empty.rmdir()
    print("Preparation/empty context: PASS; pre-dispatch failure made 0 requests; all three profiles completed 9 requests with empty context")


async def verify_storage(run, profiles, recorded, temporary):
    from application.council import attempts
    def size(record): return sum(p.stat().st_size for p in record.paths)
    measured = []
    for fragment, elapsed in ((1,0),(512,3600),(4096,0)):
        fixture = Fixtures(); fixture.output = "Fixed ordinary output.\n" + "y"*5000; fixture.fragment = fragment
        clock = [0.0]
        fixture.advance_time = lambda: clock.__setitem__(0,clock[0]+elapsed)
        with patch.object(attempts,"perf_counter",side_effect=lambda:clock[0]):
            record = await run(fixture,"frontier")
        count = sum(r["event"] == "provider_text" for r in record.records)
        assert count <= 9 and len(record.records) < 120
        assert record.records[-1]["event"] == "run_completed"
        assert all(r["native_usage"]["output_tokens"] == 20 for r in record.records if r["event"] == "provider_completed" and r["provider"] == "anthropic")
        measured.append(size(record))
        print(f"Storage fragmentation={fragment}, simulated stream delay={elapsed}s: {size(record)} bytes; {count} ordinary-text batches; {len(record.records)} meaningful records")
    assert max(measured) < min(measured)*1.35
