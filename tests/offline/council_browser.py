"""Render the installed DevUI in existing Brave, against disposable offline fixtures.

Uses the installed browser's CDP and the existing websockets dependency. No browser
framework, Human profile/backend, provider dispatch, or persistent archive is used.
"""

import asyncio
import argparse
import base64
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn
from websockets.sync.client import connect

from application.council import workflow, context_sources
from application.council.devui import CouncilDevServer


class CDP:
    def __init__(self, url):
        self.connection = connect(url, proxy=None, open_timeout=5, close_timeout=2, max_size=8*1024*1024)
        self.sequence, self.errors = 0, []

    def call(self, method, **params):
        self.sequence += 1
        self.connection.send(json.dumps(dict(id=self.sequence, method=method, params=params)))
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            message = json.loads(self.connection.recv(timeout=max(.01, deadline-time.monotonic())))
            if message.get("method") == "Runtime.exceptionThrown":
                detail = message["params"]["exceptionDetails"]
                self.errors.append(detail.get("exception",{}).get("description",detail.get("text")))
            if message.get("method") == "Log.entryAdded" and message["params"]["entry"]["level"] == "error":
                self.errors.append(message["params"]["entry"]["text"])
            if message.get("id") == self.sequence:
                if "error" in message: raise AssertionError(message["error"])
                return message.get("result", {})
        raise AssertionError("Browser CDP request timed out")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result.get("result",{}).get("value")

    def wait(self, expression, seconds=10):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value: return value
            time.sleep(.1)
        raise AssertionError(f"Browser condition timed out: {expression}\nDOM: {self.snapshot()}\nErrors: {self.errors}")

    def snapshot(self):
        return self.evaluate("""({text:document.body.innerText.slice(0,18000),
          scripts:[...document.scripts].map(s=>s.src),
          buttons:[...document.querySelectorAll('button')].map(b=>({text:b.innerText,title:b.title,disabled:b.disabled})),
          defined:!!customElements.get('maf-council'),hosts:document.querySelectorAll('maf-council').length,
          boxes:document.querySelectorAll('.maf-attempt').length,graphs:document.querySelectorAll('.react-flow').length})""")

    def click(self, text):
        point = self.evaluate("""(()=>{const label="""+json.dumps(text)+""";
          const button=[...document.querySelectorAll('button,[role=menuitem],[role=option]')].find(b=>b.innerText.trim().split('\\n')[0]===label||b.title===label);
          if(!button)throw Error('Missing control: '+label);const r=button.getBoundingClientRect();
          if(button.disabled||!r.width||!r.height)throw Error('Unavailable control: '+label);
          return {x:r.x+r.width/2,y:r.y+r.height/2};})()""")
        self.call("Input.dispatchMouseEvent",type="mousePressed",button="left",clickCount=1,**point)
        self.call("Input.dispatchMouseEvent",type="mouseReleased",button="left",clickCount=1,**point)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot",type=Path)
    options = parser.parse_args()
    logging.disable(logging.CRITICAL)
    browser = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))/"BraveSoftware/Brave-Browser/Application/brave.exe"
    if not browser.is_file(): raise SystemExit("Existing Brave unavailable; no browser was installed")
    profiles = workflow.load_council_profiles()
    state = dict(stage=None, finished=False, posts=0, sessions=[],requests=[])
    token = "offline-browser-auth"
    server = CouncilDevServer(host="127.0.0.1",auth_enabled=True,auth_token=token,council_credentials={})
    server.set_pending_entities(workflow.build_registered_councils(credentials={}))
    app = server.get_app()

    @app.middleware("http")
    async def fixture_only(request, call_next):
        path = request.url.path
        state["requests"].append((request.method,path))
        if path == "/favicon.ico": return JSONResponse(None,status_code=200)
        if path == "/agentframework.svg": return Response('<svg xmlns="http://www.w3.org/2000/svg"/>',media_type="image/svg+xml")
        if path.startswith("/v1/") and request.headers.get("Authorization") != "Bearer "+token:
            return JSONResponse(dict(error="Offline fixture authentication required"),status_code=401)
        if path == "/v1/conversations":
            if request.method == "POST":
                body = await request.json()
                session = dict(id="conv_browser_fixture_"+str(len(state["sessions"])),object="conversation",created_at=1,metadata=body.get("metadata",{}))
                state["sessions"].append(session)
                return JSONResponse(session)
            return JSONResponse(dict(data=state["sessions"],has_more=False))
        if path.startswith("/v1/conversations/"):
            if path.endswith("/items"): return JSONResponse(dict(data=[],has_more=False))
            return JSONResponse(state["sessions"][-1])
        if path == "/v1/responses" and request.method == "POST":
            state.update(stage="INITIAL",finished=False,posts=state["posts"]+1)
            async def stream():
                def event(kind,**body): return "data: "+json.dumps(dict(type=kind,**body))+"\n\n"
                yield event("response.created",response=dict(id="resp_browser_fixture",status="in_progress"))
                previous = None
                while not state["finished"]:
                    stage = state["stage"]
                    if stage != previous:
                        if previous:
                            yield event("response.output_item.done",item=dict(id="exec_"+previous,type="executor_action",executor_id=previous,status="completed",result="Offline "+previous+" stage output"))
                        yield event("response.output_item.added",item=dict(id="exec_"+stage,type="executor_action",executor_id=stage,status="in_progress"))
                        previous = stage
                    await asyncio.sleep(.05)
                item = dict(id="msg_browser_fixture",type="message",role="assistant",status="completed",content=[dict(type="output_text",text="Offline workflow output")])
                yield event("response.output_item.added",item={**item,"content":[]})
                yield event("response.output_text.delta",item_id=item["id"],delta="Offline workflow output")
                yield event("response.output_item.done",item=item)
                yield event("response.output_item.done",item=dict(id="exec_"+previous,type="executor_action",executor_id=previous,status="completed",result="Offline "+previous+" stage output"))
                yield event("response.completed",response=dict(id="resp_browser_fixture",status="completed",output=[item]))
                yield "data: [DONE]\n\n"
            return StreamingResponse(stream(),media_type="text/event-stream")
        if request.method != "GET":
            # Never allow a write to reach a production execution handler.
            return JSONResponse(dict(error="Offline browser fixture: dispatch forbidden"), status_code=405)
        return await call_next(request)

    with TemporaryDirectory(prefix="council-browser-",dir=Path(__file__).parent) as temporary, ExitStack() as stack:
        provider_guard = stack.enter_context(patch.object(workflow.LiveParticipant,"call_model",side_effect=AssertionError("Provider forbidden")))
        context_guard = stack.enter_context(patch.object(context_sources,"load_context",side_effect=AssertionError("Context read forbidden")))
        stack.enter_context(patch("os.environ",{k:v for k,v in os.environ.items() if k not in (*workflow.LIVE_KEYS.values(),"DEVUI_AUTH_TOKEN")}))
        listener = socket.socket(); listener.bind(("127.0.0.1",0))
        port = listener.getsockname()[1]; assert port != 8080
        config = uvicorn.Config(app,host="127.0.0.1",port=port,log_level="critical",access_log=False)
        fixture_server = uvicorn.Server(config)
        thread = threading.Thread(target=fixture_server.run,kwargs=dict(sockets=[listener]),daemon=True)
        thread.start()
        profile_dir = Path(temporary)/"browser"
        process, cdp = None, None
        try:
            deadline = time.monotonic()+10
            while not fixture_server.started and time.monotonic()<deadline: time.sleep(.05)
            assert fixture_server.started, "Disposable fixture server did not start"
            process = subprocess.Popen([str(browser),"--headless=new","--remote-debugging-address=127.0.0.1","--remote-debugging-port=0",
                "--user-data-dir="+str(profile_dir),"--no-first-run","--no-default-browser-check","--disable-background-networking",
                "--disable-component-update","--disable-sync","--disable-extensions","--no-proxy-server",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1","about:blank"],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            active_port = profile_dir/"DevToolsActivePort"
            deadline = time.monotonic()+10
            while not active_port.exists() and time.monotonic()<deadline:
                assert process.poll() is None, "Isolated browser exited"
                time.sleep(.05)
            assert active_port.exists(), "Isolated browser debugging endpoint unavailable"
            debug_port = int(active_port.read_text().splitlines()[0])
            opener = build_opener(ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{debug_port}/json/list",timeout=3) as response: tabs=json.load(response)
            cdp = CDP(next(t["webSocketDebuggerUrl"] for t in tabs if t["type"]=="page"))
            for method in ("Page.enable","Runtime.enable","Log.enable"): cdp.call(method)
            cdp.call("Emulation.setDeviceMetricsOverride",width=1600,height=1100,deviceScaleFactor=1,mobile=False)
            cdp.call("Page.addScriptToEvaluateOnNewDocument",source=f"localStorage.setItem('devui_auth_token',{json.dumps(token)});")
            cdp.call("Page.navigate",url=f"http://127.0.0.1:{port}/")
            cdp.wait("document.querySelectorAll('.react-flow__node').length===3")

            def check_graph():
                actual = cdp.evaluate("""({hosts:document.querySelectorAll('maf-council').length,boxes:document.querySelectorAll('.maf-attempt').length,
                  graphCount:document.querySelectorAll('.react-flow').length,
                  nodes:[...document.querySelectorAll('.react-flow__node')].map(e=>{const r=e.getBoundingClientRect();return {name:e.getAttribute('data-id'),x:r.x,y:r.y,w:r.width,h:r.height}}),
                  customAssets:[...document.scripts].some(e=>e.src.endsWith('/council.js'))})""")
                assert actual["hosts"] == actual["boxes"] == 0 and not actual["customAssets"], actual
                assert actual["graphCount"] == 1 and len(actual["nodes"]) == 3, actual
                nodes = sorted(actual["nodes"],key=lambda n:workflow.STAGES.index(n["name"]))
                assert [n["name"] for n in nodes] == list(workflow.STAGES), actual
                assert all(n["w"]>30 and n["h"]>20 for n in nodes), actual
                assert nodes[0]["y"]<nodes[1]["y"]<nodes[2]["y"], actual

            selected = profiles["testing"]["name"]
            for key,profile in profiles.items():
                state.update(profile=key,stage=None,finished=False)
                if selected != profile["name"]:
                    cdp.click(selected)
                    cdp.wait("!!document.querySelector('[role=menu]')")
                    cdp.click(profile["name"])
                    selected = profile["name"]
                cdp.wait("document.querySelectorAll('.react-flow__node').length===3 && document.body.innerText.includes("+json.dumps(selected)+")")
                cdp.wait("!document.querySelector('[role=menu]')")
                cdp.wait("[...document.querySelectorAll('.react-flow__node')].every(e=>e.getBoundingClientRect().height>20)")
                check_graph()
                cdp.click("Configure & Run")
                cdp.wait("!!document.querySelector('textarea,input[type=text]')")
                cdp.evaluate("document.querySelector('textarea,input[type=text]').focus()")
                cdp.call("Input.insertText",text="Offline browser fixture input")
                cdp.click("Run Workflow")
                cdp.wait("!![...document.querySelectorAll('button')].find(b=>b.title==='Stop workflow execution')")
                for stage in workflow.STAGES:
                    state["stage"] = stage
                    selector = '[data-id="'+stage+'"] .react-flow__handle'
                    cdp.wait("document.querySelector("+json.dumps(selector)+")?.style.backgroundColor==='rgb(100, 63, 178)'")
                    check_graph()
                    assert cdp.evaluate("!![...document.querySelectorAll('button')].find(b=>b.title==='Stop workflow execution'&&!b.disabled)")
                    if key == "testing" and stage == "INITIAL" and options.screenshot:
                        capture=cdp.call("Page.captureScreenshot",format="png")
                        options.screenshot.write_bytes(base64.b64decode(capture["data"]))
                state["finished"] = True
                cdp.wait("!![...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Run Again')")
                cdp.wait("[...document.querySelectorAll('.react-flow__handle')].every(e=>e.style.backgroundColor==='rgb(16, 185, 129)')")
                check_graph()
                cdp.click("Show output")
                cdp.wait("document.body.innerText.includes('Offline INITIAL stage output')")
                cdp.click("Events")
                cdp.wait("document.querySelectorAll('[data-devui-debug-event]').length>0")
                cdp.click("More options")
                cdp.wait("!!document.querySelector('[role=menu]')")
                menu=cdp.evaluate("""(()=>{const trigger=document.querySelector('button[title="More options"]');const menu=document.getElementById(trigger.getAttribute('aria-controls'));
                  return {linked:menu?.getAttribute('role')==='menu',items:[...menu.querySelectorAll('[role=menuitem]')].map(e=>e.innerText.trim())};})()""")
                assert menu["linked"] and "Retry Revisions" not in menu["items"], menu
                assert menu["items"], menu
                cdp.call("Input.dispatchKeyEvent",type="keyDown",key="Escape",windowsVirtualKeyCode=27)
                cdp.call("Input.dispatchKeyEvent",type="keyUp",key="Escape",windowsVirtualKeyCode=27)
                cdp.wait("!document.querySelector('[role=menu]')")
                print(f"Browser {key}: PASS; original three-stage graph visible in idle/running/completed states; native purple/green stage colors; output/events/Stop/Run Again controls intact; no custom panel/assets/menu item")
            assert not cdp.errors, cdp.errors
            assert state["posts"] == 3, state["posts"]
            provider_guard.assert_not_called()
            context_guard.assert_not_called()
            print("BROWSER: PASS; restored original DevUI in real Brave with SPA selection; 3 synthetic SSE executions; zero provider dispatch or context reads")
        finally:
            if cdp:
                try: cdp.call("Browser.close")
                except Exception: pass
                cdp.connection.close()
            if process:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
            fixture_server.should_exit = True
            thread.join(timeout=5)
            listener.close()
            assert not thread.is_alive(), "Disposable test server did not stop"


if __name__ == "__main__":
    main()
