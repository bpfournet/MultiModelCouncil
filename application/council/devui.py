"""Original DevUI presentation with backend cancellation evidence and recovery API."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from agent_framework_devui._server import DevServer

from . import recovery
from .devui_transport import guarded_bundle, INSTALLED_BUNDLE_SHA256
from .workflow import build_revision_recovery, load_council_profiles, loaded_identity, LIVE_KEYS
from .attempts import DEVUI_SCOPE

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
LOADED_FRONTEND_TRANSPORT = None


class CouncilDevServer(DevServer):
    def __init__(self, *, council_credentials=None, council_transports=None, **kwargs):
        super().__init__(**kwargs)
        self.council_profiles = load_council_profiles()
        self.council_credentials = dict(council_credentials or {})
        self.council_transports = council_transports or {}
        self.recovery_actions = {}
        self.request_scopes = {}
        ui = Path(__import__('agent_framework_devui').__file__).parent / "ui"
        self.devui_bundle = guarded_bundle((ui / "assets/index.js").read_bytes())
        global LOADED_FRONTEND_TRANSPORT
        LOADED_FRONTEND_TRANSPORT = dict(installed_bundle_sha256=INSTALLED_BUNDLE_SHA256,
            served_bundle_sha256=sha256(self.devui_bundle).hexdigest(), initial_post_auto_resend=False,
            known_response_get_reconnect=True)

    def set_pending_entities(self, entities):
        snapshots = {getattr(w,"council_profile_id",None):getattr(w,"council_profile",None) for w in entities}
        if set(snapshots) == set(self.council_profiles) and all(snapshots.values()):
            self.council_profiles = json.loads(json.dumps(snapshots))
        super().set_pending_entities(entities)

    def _register_routes(self, app):
        super()._register_routes(app)
        for route in app.routes:
            if getattr(route, "path", None) == "/v1/responses/{response_id}/cancel":
                original_cancel = route.dependant.call
                async def cancel_with_cause(response_id: str):
                    scope = self.request_scopes.get(response_id)
                    if scope is not None: scope["initiator"] = "human_stop"
                    return await original_cancel(response_id)
                route.dependant.call = cancel_with_cause

        @app.get("/v1/council/installation")
        async def installation():
            return dict(profiles=self.council_profiles,
                        credentials={p: isinstance(self.council_credentials.get(p),str) and bool(self.council_credentials[p].strip()) for p in LIVE_KEYS},
                        identity={k: loaded_identity(p) for k,p in self.council_profiles.items()},
                        backend_sha256=SOURCE_SHA256, frontend_transport=LOADED_FRONTEND_TRANSPORT)

        @app.get("/v1/council/runs/{run_id}/recovery")
        async def recovery_readiness(run_id: str):
            # Operator metadata only. Viewing this endpoint never starts recovery.
            return dict(**recovery.readiness(run_id),
                        recovery_active=any(a.get("run_id") == run_id for a in self.recovery_actions.values()))

        @app.post("/v1/council/runs/{run_id}/retry-revisions")
        async def retry_revisions(run_id: str, request: Request):
            try: body = await request.json()
            except ValueError: raise HTTPException(400,"Malformed action JSON") from None
            if not isinstance(body, dict) or set(body) != {"action_id", "fingerprint"}:
                raise HTTPException(400, "Expected action identity and source fingerprint")
            action_id = body["action_id"]
            if not isinstance(action_id, str) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", action_id):
                raise HTTPException(400, "Invalid action identity")
            # A repeated submission retrieves its local execution identity; never
            # dispatch again, including after application restart.
            for action in self.recovery_actions.values():
                if action["action_id"] == action_id:
                    if action["fingerprint"] != body["fingerprint"]: raise HTTPException(409,"Action identity already belongs to another source state")
                    return dict(run_id=action.get("run_id"), replayed=True, active=True)
            for candidate in recovery.run_ids():
                try: rows = recovery.read_run(candidate).records
                except (recovery.RecoveryUnavailable, OSError): continue
                for row in rows:
                    if row["event"] == "recovery_link" and row.get("action_id") == action_id:
                        if row.get("source_fingerprint") != body["fingerprint"]: raise HTTPException(409,"Action identity already belongs to another source state")
                        return dict(run_id=candidate, replayed=True)
            state = recovery.readiness(run_id)
            if not state["eligible"]: raise HTTPException(409, state["reason"])
            if state["fingerprint"] != body["fingerprint"]:
                raise HTTPException(409, "Source recovery state changed; review the current missing revisions")
            recipe = recovery.reconstruct(state["source_run_id"])
            root = recipe.root_run_id
            if root in self.recovery_actions: raise HTTPException(409, "Revision recovery is already active for this source")
            action = dict(action_id=action_id, fingerprint=body["fingerprint"], run_id=None, task=None)
            self.recovery_actions[root] = action

            async def events():
                action["task"] = asyncio.current_task()
                scope = {"initiator": "client_disconnect_or_server_request_cancel"}
                action["scope"] = scope
                token = DEVUI_SCOPE.set(scope)
                def on_archive(record): action["run_id"] = record.run_id
                try:
                    workflow = build_revision_recovery(recipe, action_id=action_id,
                        credentials=self.council_credentials, transports=self.council_transports, on_archive=on_archive)
                    async for event in workflow.run(recipe.source_run_id, stream=True):
                        # Progress/text are obtained from the same attempt records.
                        # This stream keeps request lifetime/cancellation attached.
                        yield "data: " + json.dumps(dict(run_id=action["run_id"], event=event.type)) + "\n\n"
                    yield "data: " + json.dumps(dict(run_id=action["run_id"], event="execution_finished")) + "\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield "data: " + json.dumps(dict(run_id=action["run_id"], event="execution_incomplete")) + "\n\n"
                finally:
                    if self.recovery_actions.get(root) is action: self.recovery_actions.pop(root)
                    DEVUI_SCOPE.reset(token)
            return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

        @app.post("/v1/council/runs/{run_id}/stop-recovery")
        async def stop_recovery(run_id: str):
            for action in self.recovery_actions.values():
                if action.get("run_id") == run_id and action.get("task"):
                    action["scope"]["initiator"] = "human_stop"
                    action["task"].cancel()
                    return dict(status="local cancellation requested", remote_outcome="unknown")
            raise HTTPException(409, "No locally active recovery request for this run")

    async def _stream_with_cancellation(self, executor, request, response_id):
        # Inherit through the installed stream wrapper and native MAF child tasks.
        # Human Stop is observable at its existing endpoint. Other request-task
        # cancellation is deliberately not mislabeled as provider confirmation.
        scope = {"response_id": response_id, "initiator": "client_disconnect_or_server_request_cancel"}
        self.request_scopes[response_id] = scope
        token = DEVUI_SCOPE.set(scope)
        try:
            async for chunk in super()._stream_with_cancellation(executor, request, response_id): yield chunk
        finally:
            self.request_scopes.pop(response_id, None)
            DEVUI_SCOPE.reset(token)

    def _mount_ui(self, app):
        # All HTML, CSS, graph, colors, controls and menus come from the original
        # installed DevUI. Only its nonvisual lost-response transport is guarded.
        @app.get("/assets/index.js")
        async def bundle():
            return Response(self.devui_bundle, media_type="text/javascript", headers={"Cache-Control": "no-store"})

        super()._mount_ui(app)


def serve_councils(*, entities, entities_dir=None, host="127.0.0.1", port=8080, auto_open=False,
                   instrumentation_enabled=False, auth_enabled=True, auth_token=None, council_credentials=None):
    if host != "127.0.0.1" or port != 8080 or auto_open or instrumentation_enabled or not auth_enabled or not auth_token:
        raise ValueError("Preserve the authenticated loopback Council launch contract")
    import uvicorn
    server = CouncilDevServer(entities_dir=entities_dir, host=host, port=port, auth_enabled=True,
                             auth_token=auth_token, council_credentials=council_credentials)
    server.set_pending_entities(entities)
    uvicorn.run(server.get_app(), host=host, port=port, log_level="info")
