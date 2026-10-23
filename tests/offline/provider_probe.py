"""One bounded provider probe; offline self-test by default. Live use costs money."""

# --- 1. Imports and fixed probe contract ---

import argparse
import asyncio
import json
import logging
import math
import os
import sys

os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["AGENT_FRAMEWORK_USER_AGENT_DISABLED"] = "true"
os.environ["ENABLE_INSTRUMENTATION"] = "false"
os.environ["ENABLE_SENSITIVE_DATA"] = "false"

from agent_framework import Message

PROVIDERS = {
    "openai": ("OPENAI_API_KEY", "api.openai.com"),
    "anthropic": ("ANTHROPIC_API_KEY", "api.anthropic.com"),
    "gemini": ("GEMINI_API_KEY", "generativelanguage.googleapis.com"),
}
PROMPT = "Reply with exactly PROBE_OK. Do not use tools."


# --- 2. Arguments, dispatch budget, and response acceptance ---

def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Authorize one model request")
    mode.add_argument("--self-test", action="store_true", help="Run offline checks only")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--model", help="Explicit provider model identifier; no default")
    parser.add_argument("--prompt", default=PROMPT, help="Request text; defaults to the fixed offline probe")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if bool(args.provider) != bool(args.model) or (args.live and not args.provider):
        parser.error("provide both --provider and --model; both are required with --live")
    if args.model and (len(args.model) > 200 or any(c.isspace() or not c.isprintable() for c in args.model)):
        parser.error("model must be a nonempty identifier without whitespace, at most 200 characters")
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    if not 1 <= args.max_tokens <= 256:
        parser.error("max-tokens must be between 1 and 256")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("timeout must be finite, greater than zero, and at most 30 seconds")
    return args


def claim_request(result):
    if result["request_attempts"] >= 1:
        raise RuntimeError("Second request blocked")
    result["request_attempts"] += 1


def response_failure(response, expected_text="PROBE_OK"):
    raw = response.raw_representation
    contents = [content for message in response.messages for content in message.contents]
    blocked = getattr(getattr(raw, "prompt_feedback", None), "block_reason", None)
    blocked = getattr(blocked, "name", blocked)
    if blocked not in (None, "BLOCKED_REASON_UNSPECIFIED", "BLOCK_REASON_UNSPECIFIED"):
        return "refusal_or_safety_block"
    if response.finish_reason == "content_filter" or any(
        content.additional_properties.get("model_output_kind") == "refusal"
        or content.additional_properties.get("core", {}).get("model_output_kind") == "refusal"
        for content in contents
    ):
        return "refusal_or_safety_block"
    if not contents or any(content.type not in ("text", "text_reasoning") for content in contents):
        return "unexpected_content_or_tool_call"
    if response.finish_reason != "stop" or response.continuation_token is not None:
        return "incomplete_output"
    if getattr(raw, "status", "completed") != "completed" or getattr(
        raw, "stop_reason", "end_turn"
    ) not in ("end_turn", "stop_sequence"):
        return "incomplete_output"
    if any(message.role != "assistant" for message in response.messages):
        return "unexpected_message_role"
    if not response.text.strip() or (expected_text is not None and response.text.strip() != expected_text):
        return "unexpected_or_empty_reply"
    return None


# --- 3. Official provider clients; one inference call, no agent/tool loop ---

async def run_probe(args, key, *, transport=None):
    if not args.live:
        raise ValueError("Live opt-in required before client construction")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("An explicit process credential is required; discovery is disabled")
    result = dict(status="FAIL", provider=args.provider, requested_model=args.model,
                  request_attempts=0, request_ids={}, response_id=None, usage=None)

    async def before_request(request):
        if (request.method != "POST" or request.url.scheme != "https"
                or request.url.host != PROVIDERS[args.provider][1]):
            raise RuntimeError("Unexpected request destination or method blocked")
        claim_request(result)

    async def after_response(response):
        for name in ("x-request-id", "request-id", "x-goog-request-id"):
            if name in response.headers:
                result["request_ids"][name] = response.headers[name]

    import httpx as http
    if args.provider != "gemini":
        import httpx2 as http
    # These hooks bound SDK traffic; inference still uses official framework clients.
    http_client = http.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=args.timeout,
        event_hooks={"request": [before_request], "response": [after_response]},
    )
    sdk = None
    try:
        options = {"model": args.model, "max_tokens": args.max_tokens}
        if args.provider == "openai":
            from openai import AsyncOpenAI
            from agent_framework_openai import RawOpenAIChatClient
            sdk = AsyncOpenAI(api_key=key, admin_api_key="", webhook_secret="",
                              base_url="https://api.openai.com/v1", max_retries=0,
                              timeout=args.timeout, http_client=http_client)
            client = RawOpenAIChatClient(model=args.model, api_key=key, async_client=sdk)
            options.update(store=False, background=False)
        elif args.provider == "anthropic":
            from anthropic import AsyncAnthropic
            from agent_framework_anthropic import RawAnthropicClient
            # Prevent SDK shadow-warning checks from searching saved user profiles.
            # This setting exists only in this standalone process; no config is written.
            os.environ["ANTHROPIC_CONFIG_DIR"] = os.devnull
            sdk = AsyncAnthropic(api_key=key, webhook_key="", max_retries=0,
                                 base_url="https://api.anthropic.com", open_telemetry=False,
                                 timeout=args.timeout, http_client=http_client)
            client = RawAnthropicClient(model=args.model, api_key=key, anthropic_client=sdk)
        else:
            from google import genai
            from google.genai import types
            from agent_framework_gemini import RawGeminiChatClient
            sdk = genai.Client(api_key=key, vertexai=False, enterprise=False, http_options=types.HttpOptions(
                base_url="https://generativelanguage.googleapis.com", api_version="v1beta",
                timeout=math.ceil(args.timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=1), httpx_async_client=http_client,
            ))
            client = RawGeminiChatClient(model=args.model, api_key=key, client=sdk)
            options["automatic_function_calling"] = {"disable": True}
        async with asyncio.timeout(args.timeout):
            response = await client.get_response([Message("user", [args.prompt])], stream=False, options=options)
        result.update(
            served_model=response.model, finish_reason=response.finish_reason,
            response_id=response.response_id or getattr(response.raw_representation, "response_id", None),
            usage=dict(response.usage_details) if response.usage_details is not None else None,
            text=response.text, failure=response_failure(response, "PROBE_OK" if args.prompt == PROMPT else None),
        )
        if result["failure"] is None:
            result["status"] = "PASS"
    except TimeoutError:
        result["failure"] = "deadline_exceeded; remote billing may still occur"
    except Exception as exc:
        # Never print SDK exception messages, raw headers, or credential values.
        cause = getattr(exc, "inner_exception", None) or exc.__cause__ or exc
        failure = ("request_timeout; remote billing may still occur" if "Timeout" in type(cause).__name__
                   else "provider_or_transport_error")
        result.update(failure=failure, error_type=type(cause).__name__,
                      http_status=getattr(cause, "status_code", None) or getattr(cause, "code", None))
    finally:
        cleanup = ([(lambda: sdk.aio.aclose(), True), (lambda: sdk.close(), False)] if args.provider == "gemini" else
                   [(lambda: sdk.close(), True)]) if sdk is not None else []
        for close, asynchronous in [*cleanup, (lambda: http_client.aclose(), True)]:
            try:
                if asynchronous: await close()
                else: close()
            except Exception as exc:
                cause = getattr(exc, "inner_exception", None) or exc.__cause__ or exc
                result.setdefault("cleanup_warnings", []).append(dict(
                    classification="cleanup_warning", exception_type=type(exc).__name__, cause_type=type(cause).__name__))
    return result


# --- 4. Deterministic offline checks with synthetic keys and mock transports ---

async def self_test():
    import contextlib
    import io
    from unittest.mock import patch
    import httpx
    import httpx2

    count = 0
    bad_arguments = (
        ["--provider", "unknown"], ["--provider", "openai"], ["--model", "offline-model"],
        ["--provider", "openai", "--model", " "], ["--max-tokens", "0"],
        ["--max-tokens", "257"], ["--timeout", "0"], ["--timeout", "31"],
        ["--timeout", "nan"], ["--timeout", "inf"], ["--retry", "1"],
    )
    for argv in bad_arguments:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                arguments(argv)
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError(f"Invalid arguments accepted: {argv}")
        count += 1
    budget = {"request_attempts": 0}
    claim_request(budget)
    try:
        claim_request(budget)
    except RuntimeError:
        assert budget["request_attempts"] == 1
    else:
        raise AssertionError("Second request allowed")
    count += 1

    # Replace the environment object, without inspecting or copying real credentials.
    with patch("os.environ", {}), patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
         patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
        for provider in PROVIDERS:
            args = arguments(["--provider", provider, "--model", "offline-model"])
            assert not args.live and args.max_tokens == 64 and args.timeout == 30
            with patch(__name__ + ".run_probe") as dispatch, contextlib.redirect_stdout(io.StringIO()):
                assert main(["--provider", provider, "--model", "offline-model"]) == 0
                dispatch.assert_not_called()
            try:
                await run_probe(args, "offline-placeholder")
            except ValueError:
                pass
            else:
                raise AssertionError("Missing opt-in accepted")
            count += 1
            # Exercise the real integrations and SDKs with fake HTTP only; never CLI --live.
            args.live = True
            http = httpx if provider == "gemini" else httpx2
            faults = ("success", "missing_usage", "refusal", "incomplete", "tool", "empty",
                      "server_error", "transport_timeout", "timeout")
            for fault in faults + (("prompt_block",) if provider == "gemini" else ()):
                sent = []
                async def handler(request):
                    sent.append(request)
                    body = json.loads(request.content)
                    assert body["model"] == "offline-model" if provider != "gemini" else "offline-model" in request.url.path
                    assert not body.get("tools") and not body.get("stream")
                    if fault == "timeout":
                        await asyncio.sleep(1)
                    if fault == "cancel":
                        raise asyncio.CancelledError()
                    if fault == "transport_timeout":
                        raise http.ReadTimeout("offline timeout", request=request)
                    if fault == "server_error":
                        return http.Response(503, json={"error": {"message": "offline error"}})
                    if provider == "openai":
                        payload = dict(id="resp_offline", object="response", created_at=0, model="offline-model",
                            status="completed", output=[dict(id="msg_offline", type="message", status="completed",
                            role="assistant", content=[dict(type="output_text", text="PROBE_OK", annotations=[])])],
                            usage=dict(input_tokens=5, output_tokens=2, total_tokens=7))
                        assert body["max_output_tokens"] == 64 and body["store"] is False
                        if fault == "refusal":
                            payload["output"][0]["content"] = [dict(type="refusal", refusal="Declined")]
                        elif fault == "incomplete":
                            payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
                        elif fault == "tool":
                            payload["output"] = [dict(type="function_call", id="fc_offline", call_id="call_offline",
                                                       name="forbidden_tool", arguments="{}")]
                        elif fault == "empty":
                            payload["output"][0]["content"][0]["text"] = ""
                    elif provider == "anthropic":
                        payload = dict(id="msg_offline", type="message", role="assistant", model="offline-model",
                            content=[dict(type="text", text="PROBE_OK")], stop_reason="end_turn", stop_sequence=None,
                            usage=dict(input_tokens=5, output_tokens=2))
                        assert body["max_tokens"] == 64
                        if fault in ("refusal", "incomplete"):
                            payload["stop_reason"] = "refusal" if fault == "refusal" else "max_tokens"
                        elif fault == "tool":
                            payload.update(stop_reason="tool_use", content=[dict(type="tool_use", id="tool_offline",
                                                                                name="forbidden_tool", input={})])
                        elif fault == "empty":
                            payload["content"][0]["text"] = ""
                    else:
                        payload = dict(responseId="gemini_offline", modelVersion="offline-model",
                            candidates=[dict(content=dict(role="model", parts=[dict(text="PROBE_OK")]),
                                             finishReason="STOP", index=0)],
                            usageMetadata=dict(promptTokenCount=5, candidatesTokenCount=2, totalTokenCount=7))
                        assert body["generationConfig"]["maxOutputTokens"] == 64
                        if fault in ("refusal", "incomplete"):
                            payload["candidates"][0]["finishReason"] = "SAFETY" if fault == "refusal" else "MAX_TOKENS"
                        elif fault == "tool":
                            payload["candidates"][0]["content"]["parts"] = [dict(functionCall=dict(name="forbidden_tool", args={}))]
                        elif fault == "empty":
                            payload["candidates"][0]["content"]["parts"][0]["text"] = ""
                        elif fault == "prompt_block":
                            payload.update(candidates=[], promptFeedback={"blockReason": "SAFETY"})
                    if fault == "missing_usage":
                        payload.pop("usageMetadata" if provider == "gemini" else "usage")
                    return http.Response(200, json=payload, headers={"x-request-id": "offline-request"})
                args.timeout = 0.2 if fault == "timeout" else 30
                result = await run_probe(args, "offline-placeholder", transport=http.MockTransport(handler))
                assert len(sent) == result["request_attempts"] == 1, result
                assert (result["status"] == "PASS") == (fault in ("success", "missing_usage")), result
                expected_failure = {"refusal": "refusal_or_safety_block", "prompt_block": "refusal_or_safety_block",
                                    "incomplete": "incomplete_output",
                                    "tool": "unexpected_content_or_tool_call", "empty": "unexpected_or_empty_reply",
                                    "server_error": "provider_or_transport_error"}.get(fault)
                if expected_failure:
                    assert result["failure"] == expected_failure, result
                if fault == "server_error":
                    assert result["http_status"] == 503, result
                if fault in ("timeout", "transport_timeout"):
                    assert "timeout" in result["failure"] or "deadline_exceeded" in result["failure"], result
                if fault == "missing_usage":
                    assert result["usage"] is None, result
                elif fault == "success":
                    assert result["usage"]["input_token_count"] == 5 and result["usage"]["output_token_count"] == 2
                    assert result["request_ids"]["x-request-id"] == "offline-request" and result["response_id"]
                count += 1
            if provider == "openai":
                from openai import AsyncOpenAI as SDKClient
                sdk_close = "close"
            elif provider == "anthropic":
                from anthropic import AsyncAnthropic as SDKClient
                sdk_close = "close"
            else:
                from google.genai.client import AsyncClient as SDKClient
                sdk_close = "aclose"
            for layer, fault in (("SDK", "success"), ("HTTP", "success"), ("SDK", "server_error"), ("HTTP", "cancel")):
                sent, closed = [], []
                target, attribute = (SDKClient, sdk_close) if layer == "SDK" else (http.AsyncClient, "aclose")
                close = getattr(target, attribute)
                async def failed_close(self):
                    await close(self)
                    closed.append(True)
                    raise RuntimeError("synthetic-cleanup-secret")
                with patch.object(target, attribute, failed_close):
                    try: result = await run_probe(args, "offline-placeholder", transport=http.MockTransport(handler))
                    except asyncio.CancelledError: assert fault == "cancel" and closed
                    else:
                        assert fault != "cancel" and (result["status"] == "PASS") == (fault == "success")
                        assert result["cleanup_warnings"] and "synthetic-cleanup-secret" not in json.dumps(result)
                        if fault == "server_error": assert result["http_status"] == 503
                assert len(sent) == 1
                count += 1
            print(f"OFFLINE {provider}: PASS; real SDK/framework mapping, one attempt, refusal/incomplete/error/timeout rejection")
    print(f"PASS: {count} offline checks; zero live model requests; no real credentials read")


# --- 5. Safe command-line entry point ---

def main(argv=None):
    args = arguments(argv)
    logging.disable(logging.CRITICAL)
    if not args.live:
        if args.self_test or args.provider is None:
            asyncio.run(self_test())
        else:
            print(f"OFFLINE VALIDATION PASS: {args.provider}; no request dispatched; --live is required")
        return 0
    key = os.environ.get(PROVIDERS[args.provider][0])
    if not key or not key.strip():
        print(f"FAIL: missing process environment variable {PROVIDERS[args.provider][0]}", file=sys.stderr)
        return 1
    result = asyncio.run(run_probe(args, key))
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
