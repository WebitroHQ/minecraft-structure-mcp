"""Model providers for the Studio. Claude goes through the official Anthropic SDK; anything that speaks the
OpenAI chat-completions protocol (OpenAI, OpenRouter, Groq, Ollama, LM Studio, vLLM...) goes over plain HTTP.

Both keep their own message history in their own format and expose the same small interface:
    turn(tools) -> Reply(text, calls, usage, stop)
    add_user(text) / add_results([(call, text, png_or_None)])
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str
    calls: list[Call] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop: str = ""


#: Claude models that accept the server-side refusal fallback (see the Claude API docs on refusals).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AnthropicChat:
    """Claude via the Anthropic SDK: streaming (long tool inputs), adaptive thinking, prompt caching."""

    def __init__(self, api_key: str, model: str, system: str, effort: str = "high", base_url: str | None = None):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key or None, base_url=base_url or None)
        self.model = model
        self.system = system
        self.effort = effort
        self.extras = True              # context editing + refusal fallback (dropped if the API rejects them)
        self.messages: list[dict] = []

    def _stream(self, kwargs):
        api = self.client.beta.messages if "betas" in kwargs else self.client.messages
        with api.stream(**kwargs) as stream:
            return stream.get_final_message()

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_results(self, results) -> None:
        content = []
        for call, text, png, is_error in results:
            body: list[dict] = []
            if png:
                body.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": base64.standard_b64encode(png).decode()}})
            body.append({"type": "text", "text": text})
            item = {"type": "tool_result", "tool_use_id": call.id, "content": body}
            if is_error:
                item["is_error"] = True
            content.append(item)
        self.messages.append({"role": "user", "content": content})

    def turn(self, tools: list[dict]) -> Reply:
        defs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"],
                 "eager_input_streaming": True} for t in tools]
        kwargs = dict(model=self.model, max_tokens=64000, system=self.system, tools=defs, messages=self.messages,
                      output_config={"effort": self.effort}, extra_body={"cache_control": {"type": "ephemeral"}})
        if self.extras:
            # long builds pile up renders and tool output: let the server clear old tool results, and route
            # a refusal to a fallback model instead of ending the build
            kwargs["betas"] = ["context-management-2025-06-27"]
            kwargs["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
            if self.model in _FALLBACK_MODELS:
                kwargs["betas"].append("server-side-fallback-2026-07-01")
                kwargs["extra_body"]["fallbacks"] = "default"
        try:
            msg = self._stream(kwargs)
        except self.anthropic.BadRequestError:
            if not self.extras:
                raise
            self.extras = False          # this account/model rejects a beta option: carry on without them
            for k in ("betas", "context_management"):
                kwargs.pop(k, None)
            kwargs["extra_body"].pop("fallbacks", None)
            msg = self._stream(kwargs)
        # keep the whole content (thinking blocks too) so the history stays append-only
        self.messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]})
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [] if msg.stop_reason in ("max_tokens", "refusal") else \
            [Call(b.id, b.name, b.input if isinstance(b.input, dict) else {}) for b in msg.content if b.type == "tool_use"]
        if msg.stop_reason == "max_tokens" and any(b.type == "tool_use" for b in msg.content):
            # a cut-off tool call must still be answered, or the next request is rejected
            self.add_results([(Call(b.id, b.name, {}), "Your tool call was cut off (max_tokens). Send it again, "
                               "smaller.", None, True) for b in msg.content if b.type == "tool_use"])
        u = msg.usage
        return Reply(text, calls, (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                     + (getattr(u, "cache_creation_input_tokens", 0) or 0), u.output_tokens or 0, msg.stop_reason or "")


class OpenAIChat:
    """Any OpenAI-compatible /chat/completions endpoint. Images go in a user message after the tool results
    (tool messages cannot carry images), only when `vision` is on."""

    def __init__(self, api_key: str, model: str, system: str, base_url: str, vision: bool = True):
        self.key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.vision = vision
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_results(self, results) -> None:
        images = []
        for call, text, png, is_error in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": ("ERROR: " if is_error else "") + text})
            if png and self.vision:
                images.append({"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.standard_b64encode(png).decode()}})
        if images:
            self.messages.append({"role": "user", "content": [{"type": "text", "text": "The render(s) above:"}, *images]})

    def turn(self, tools: list[dict]) -> Reply:
        body = {"model": self.model, "messages": self.messages,
                "tools": [{"type": "function", "function": t} for t in tools]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        if self.key:
            req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} from {self.url}: {e.read()[:500].decode(errors='replace')}") from None
        choice = data["choices"][0]
        m = choice["message"]
        self.messages.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls")})
        calls = []
        for tc in m.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid__": tc["function"].get("arguments")}
            calls.append(Call(tc["id"], tc["function"]["name"], args))
        u = data.get("usage") or {}
        return Reply(m.get("content") or "", calls, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                     choice.get("finish_reason") or "")


def make_chat(cfg: dict, system: str):
    if cfg.get("provider", "anthropic") == "anthropic":
        return AnthropicChat(cfg.get("api_key", ""), cfg.get("model") or "claude-opus-5-5", system,
                             cfg.get("effort") or "high", cfg.get("base_url") or None)
    return OpenAIChat(cfg.get("api_key", ""), cfg.get("model", ""), system, cfg.get("base_url", ""),
                      bool(cfg.get("vision", True)))
