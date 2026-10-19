#!/usr/bin/env python3
"""Translate the Anthropic Messages stream used by Claude Code to OpenAI Chat.

The configured provider exposes OpenAI-compatible endpoints only.  This process
is intended to run on the sandbox host, outside task containers; it never logs
request bodies or credentials.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit


def text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "input_text") and isinstance(item.get("text"), str):
                    pieces.append(item["text"])
                elif isinstance(item.get("text"), str):
                    pieces.append(item["text"])
        return "".join(pieces)
    return "" if value is None else str(value)


def json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return text_of(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return text_of(value)
    return "\n".join(
        item.get("text", "")
        for item in value
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def anthropic_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for source in payload.get("messages") or []:
        if not isinstance(source, dict):
            continue
        role = source.get("role")
        content = source.get("content")
        if isinstance(content, str):
            messages.append({"role": role if role in ("user", "assistant") else "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            pieces: list[str] = []
            calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    pieces.append(text_of(block.get("text")))
                elif block.get("type") == "tool_use":
                    args = block.get("input", {})
                    calls.append({
                        "id": str(block.get("id") or "call_" + uuid.uuid4().hex),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or "tool"),
                            "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
                        },
                    })
            item: dict[str, Any] = {"role": "assistant", "content": "".join(pieces) or None}
            if calls:
                item["tool_calls"] = calls
            if item["content"] is not None or calls:
                messages.append(item)
            continue
        # A Claude user turn may contain both ordinary text and tool_result
        # blocks.  Chat Completions requires a separate role=tool message for
        # each result, preserving the order of the source blocks.
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if text_parts:
                    messages.append({"role": "user", "content": "".join(text_parts)})
                    text_parts = []
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": json_text(block.get("content", "")),
                })
            elif block.get("type") == "text":
                text_parts.append(text_of(block.get("text")))
            elif block.get("type") in ("image", "input_image"):
                # The benchmark currently uses text tools.  Keep a readable
                # marker if a future task sends an image block.
                text_parts.append("[image]")
        if text_parts:
            messages.append({"role": "user", "content": "".join(text_parts)})
    return messages


def openai_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return result


def upstream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    effort = ((payload.get("output_config") or {}).get("effort")
              if isinstance(payload.get("output_config"), dict) else None)
    if effort == "xhigh":
        effort = "max"
    result: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": anthropic_messages(payload),
        "max_tokens": payload.get("max_tokens", 32768),
        "stream": bool(payload.get("stream", True)),
        "tool_choice": "auto",
    }
    tools = openai_tools(payload)
    if tools:
        result["tools"] = tools
        result["parallel_tool_calls"] = True
    if effort in ("low", "high", "max"):
        result["reasoning_effort"] = effort
    return result


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    @property
    def bridge(self) -> "BridgeServer":
        return self.server  # type: ignore[return-value]

    def _error(self, status: int, message: str) -> None:
        raw = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        # Recent Claude Code versions probe /v1/models for model discovery when
        # a third-party base URL is used.  The actual model request is still
        # sent through Messages and translated below.
        if self.path in ("/v1/models", "/models"):
            raw = json.dumps({"object": "list", "data": [{"id": "glm-5.3", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            return
        self._error(404, "Not found")

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            # Claude Code may probe token counts before a turn.  The upstream
            # OpenAI-compatible service has no Anthropic count_tokens route;
            # returning a conservative positive count keeps that probe from
            # being treated as a provider/model failure.
            raw = json.dumps({"input_tokens": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            return
        if route not in ("/v1/messages", "/messages"):
            self._error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            self.stream(payload)
        except urllib.error.HTTPError as error:
            # Preserve the provider's status without exposing its body.
            self._error(error.code, "upstream provider rejected request")
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._error(400, str(error))
        except Exception:
            self._error(502, "upstream transport failed")

    def stream(self, payload: dict[str, Any]) -> None:
        body = json.dumps(upstream_payload(payload), ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.bridge.upstream,
            data=body,
            headers={
                "Authorization": "Bearer " + self.bridge.key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        response = urllib.request.urlopen(request, timeout=self.bridge.timeout)
        if not payload.get("stream", True):
            self.non_stream(response, payload)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        identifier = "msg_" + uuid.uuid4().hex
        model = str(payload.get("model") or "")
        self.emit("message_start", {"type": "message_start", "message": {
            "id": identifier, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }})
        blocks: dict[int, dict[str, Any]] = {}
        next_index = 0
        finish = None
        usage: dict[str, Any] = {}
        truncated = False
        saw_choice = False
        non_sse = bytearray()

        def process_choice(choice: Any) -> None:
            """Translate one Chat Completions choice into Anthropic events."""
            nonlocal next_index, finish, saw_choice
            if not isinstance(choice, dict):
                return
            saw_choice = True
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                # A few compatible gateways return a complete message even
                # when stream=true.  Treat it as a final delta so Claude can
                # still consume the response.
                delta = choice.get("message") or {}
            if not isinstance(delta, dict):
                return
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                index = next((i for i, block in blocks.items() if block.get("kind") == "thinking"), None)
                if index is None:
                    index = next_index; next_index += 1
                    blocks[index] = {"kind": "thinking"}
                    self.emit("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "thinking", "thinking": ""}})
                self.emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "thinking_delta", "thinking": reasoning}})
            content = delta.get("content")
            if isinstance(content, str) and content:
                index = next((i for i, block in blocks.items() if block.get("kind") == "text"), None)
                if index is None:
                    index = next_index; next_index += 1
                    blocks[index] = {"kind": "text"}
                    self.emit("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "text", "text": ""}})
                self.emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "text_delta", "text": content}})
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                try:
                    index = int(call.get("index", 0))
                except (TypeError, ValueError):
                    index = 0
                block = blocks.get(index)
                function = call.get("function") or {}
                if block is None:
                    out_index = next_index; next_index += 1
                    block = {"kind": "tool", "out_index": out_index}
                    blocks[index] = block
                    self.emit("content_block_start", {"type": "content_block_start", "index": out_index,
                                                       "content_block": {"type": "tool_use",
                                                                          "id": call.get("id") or "call_" + uuid.uuid4().hex,
                                                                          "name": function.get("name") or "tool",
                                                                          "input": {}}})
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    self.emit("content_block_delta", {"type": "content_block_delta", "index": block["out_index"],
                                                       "delta": {"type": "input_json_delta", "partial_json": arguments}})

        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    # If a provider ignores stream=true it may return one
                    # ordinary JSON document. Keep those bytes for the
                    # fallback parser below; comments/event headers are safe
                    # to ignore.
                    if not line.startswith(("event:", ":")):
                        non_sse.extend(raw_line)
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                process_choice(choices[0])
        except (http.client.IncompleteRead, ConnectionError, TimeoutError, urllib.error.URLError):
            # Some gateways close an SSE response after the last useful delta
            # without emitting [DONE]. Finalize the Anthropic stream locally
            # instead of writing a second HTTP response in the event stream.
            truncated = True
        except Exception:
            # urllib/http.client use a few gateway-specific exception classes
            # (for example RemoteDisconnected) that are not consistent across
            # Python releases. Once headers and useful deltas are sent, any
            # read-side exception must stay inside this SSE response; letting
            # do_POST emit a second 502 response corrupts Claude's stream.
            truncated = True

        # A 200 response with a JSON body is a common compatibility fallback.
        # Convert it to the same event sequence instead of emitting an empty
        # Anthropic message.  Empty/error bodies are surfaced as a retryable
        # Anthropic API error so Claude does not accept a malformed result.
        if not saw_choice and non_sse:
            try:
                fallback = json.loads(non_sse.decode("utf-8", "replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                fallback = None
            if isinstance(fallback, dict):
                if isinstance(fallback.get("usage"), dict):
                    usage = fallback["usage"]
                if isinstance(fallback.get("error"), dict):
                    detail = fallback["error"].get("message") or "upstream provider returned an error"
                    self.emit("error", {"type": "error", "error": {"type": "api_error", "message": str(detail)[:500]}})
                    response.close()
                    return
                choices = fallback.get("choices") or []
                if choices:
                    process_choice(choices[0])
        # A role-only delta still counts as a choice, but it is not a valid
        # final Claude message by itself. Treat any stream with no content or
        # tool block and no finish reason as malformed.
        if not blocks and finish is None:
            self.emit("error", {"type": "error", "error": {
                "type": "api_error", "message": "upstream provider returned an empty streaming response"}})
            response.close()
            return
        # Reasoning-only responses that terminate at the token limit are not
        # usable turns for Claude Code: there is no text or tool call for the
        # controller to act on. Surface them as a provider error so the native
        # client retries instead of accepting a malformed assistant turn.
        if not any(block.get("kind") in ("text", "tool") for block in blocks.values()):
            self.emit("error", {"type": "error", "error": {
                "type": "api_error", "message": "upstream response ended without text or tool output"}})
            response.close()
            return
        try:
            for block in blocks.values():
                self.emit("content_block_stop", {"type": "content_block_stop", "index": block.get("out_index", 0)})
            stop_reason = "tool_use" if finish == "tool_calls" or any(b.get("kind") == "tool" for b in blocks.values()) else {
                "length": "max_tokens", "content_filter": "end_turn"}.get(finish, "end_turn")
            if truncated and stop_reason == "end_turn":
                stop_reason = "max_tokens"
            out_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
            self.emit("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                                "stop_sequence": None},
                                          "usage": {"output_tokens": out_tokens}})
            self.emit("message_stop", {"type": "message_stop"})
        finally:
            response.close()

    def non_stream(self, response: Any, payload: dict[str, Any]) -> None:
        data = json.loads(response.read())
        response.close()
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") or {}
        blocks: list[dict[str, Any]] = []
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            blocks.append({"type": "thinking", "thinking": reasoning})
        content = message.get("content")
        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        calls = message.get("tool_calls") or []
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except (TypeError, ValueError):
                arguments = {}
            blocks.append({"type": "tool_use", "id": call.get("id") or "call_" + uuid.uuid4().hex,
                           "name": function.get("name") or "tool", "input": arguments})
        finish = choice.get("finish_reason")
        stop_reason = "tool_use" if finish == "tool_calls" or calls else {"length": "max_tokens"}.get(finish, "end_turn")
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        result = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                  "model": payload.get("model"), "content": blocks, "stop_reason": stop_reason,
                  "stop_sequence": None, "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                                     "output_tokens": usage.get("completion_tokens", 0)}}
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def emit(self, event: str, value: dict[str, Any]) -> None:
        raw = ("event: " + event + "\ndata: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
        self.wfile.write(raw)
        self.wfile.flush()


class BridgeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream: str, key: str, timeout: float) -> None:
        super().__init__(address, BridgeHandler)
        self.upstream, self.key, self.timeout = upstream, key, timeout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:18080")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    host, port_text = args.listen.rsplit(":", 1)
    key = open(args.key_file, encoding="utf-8").read().strip()
    if not key or "\n" in key or "\r" in key:
        raise SystemExit("invalid key file")
    server = BridgeServer((host, int(port_text)), args.upstream, key, args.timeout)
    print(json.dumps({"pid": os.getpid(), "listen": args.listen, "upstream": args.upstream}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
