"""LLMRouter — three backends with fallback chain and per-call logging.

Backends (all OpenAI-compatible chat-completions):
  - openrouter   cloud, Anthropic Opus 4.7 by default (uses API key)
  - lmstudio     network (Hub at 10.0.0.102:1234), free + smart, no auth
  - ondevice     Pi llama-server at 127.0.0.1:8081, free + slow, no auth

Locked design (2026-05-02):
  - Per-plugin default in plugin.toml [llm].backend; per-call backend= overrides
  - Fallback chain when the requested backend is unreachable: try next in chain
  - Every call logged to overseer.db.llm_calls (backend, sizes, latency, cost,
    degraded flag) — data-driven routing decisions later
  - Hub-offline mode: caller can ask for queue+retry+degrade behavior; the
    simple complete() falls back immediately. Background loop in 3c will use
    the queue path.
  - Never block: caller always gets a result (even from ondevice with the
    `degraded=True` flag set) unless every backend fails.

Secrets:
  - OpenRouter API key from ~/.cortex/secrets.toml [openrouter].api_key
    (or OPENROUTER_API_KEY env var, which overrides). chmod 600 on Pi.

Cost tracking:
  - OpenRouter responses include usage.{prompt_tokens, completion_tokens}.
    Pricing is fetched lazily from /models on first OpenRouter call;
    falls back to 0.0 if the lookup fails.
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


log = logging.getLogger("plugin.overseer.llm")


SECRETS_DEFAULT_CANDIDATES = (
    "/etc/cortex/secrets.toml",
    "~/.cortex/secrets.toml",
)


# Sub-agent model-tier resolution (2026-05-27).
#
# Tory's directive: B/C sub-agents run as inexpensively as possible
# by default, and Tory pulls the upgrade trigger when output is poor.
# The TIER is the human-facing knob; SUB_AGENT_TIER_TO_MODEL is the
# implementation-side mapping. If models update, change one constant
# here and every sub-agent gets the new model on its next dispatch.
#
# Cost shape (May 2026 OpenRouter pricing, approximate per-call):
#   flash  ~$0.001 — Gemini 2.0 Flash, ~140× cheaper than Opus
#   sonnet ~$0.02  — Claude Sonnet 4.6
#   opus   ~$0.10  — Claude Opus 4.7
SUB_AGENT_TIER_TO_MODEL = {
    "flash":  "google/gemini-2.0-flash-001",
    "sonnet": "anthropic/claude-sonnet-4.6",
    "opus":   "anthropic/claude-opus-4.7",
}


def resolve_sub_agent_model(tier, default_model=None):
    """Map a tier name to an OpenRouter model id. Unknown tier falls
    back to `default_model` if provided, else 'flash' resolution."""
    if tier in SUB_AGENT_TIER_TO_MODEL:
        return SUB_AGENT_TIER_TO_MODEL[tier]
    if default_model:
        return default_model
    return SUB_AGENT_TIER_TO_MODEL["flash"]


def _resolve_candidate(p) -> Path:
    """Expand ~ and env vars; return Path (not necessarily existing)."""
    return Path(os.path.expanduser(os.path.expandvars(str(p))))


def _load_secrets(candidates=None) -> tuple[dict, Path | None]:
    """Try each candidate path; return (parsed_dict, path_used).

    Order:
      1. CORTEX_SECRETS env var (if set, single path)
      2. Each path in `candidates` (first existing wins)
      3. Fall back to SECRETS_DEFAULT_CANDIDATES if `candidates` is empty

    Returns ({}, None) if nothing found.
    """
    paths_to_try: list[Path] = []
    env_path = os.environ.get("CORTEX_SECRETS", "").strip()
    if env_path:
        paths_to_try.append(_resolve_candidate(env_path))
    for cand in (candidates or SECRETS_DEFAULT_CANDIDATES):
        paths_to_try.append(_resolve_candidate(cand))

    for p in paths_to_try:
        if p.is_file():
            try:
                with open(p, "rb") as f:
                    return tomllib.load(f), p
            except Exception as e:
                log.warning("could not parse secrets file %s: %s", p, e)
                continue
    return {}, None


def _get_openrouter_api_key(secrets: dict) -> str | None:
    """OPENROUTER_API_KEY env var beats secrets.toml."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    section = secrets.get("openrouter") or {}
    key = (section.get("api_key") or "").strip()
    return key or None


class LLMRouterError(Exception):
    """Raised when every backend in the fallback chain fails."""


class LLMRouter:
    """OpenAI-compatible chat router with three backends and a fallback chain."""

    def __init__(self, *, manifest_llm: dict, db, secrets_paths=None):
        """
        manifest_llm: parsed [llm] table from plugin.toml. Expects keys:
            backend, model, fallback (list[str]), per-backend url/model/
            timeout_s entries, and optionally `secrets_paths` (list of
            candidate secrets.toml locations).
        db: OverseerDB instance for logging llm_calls. May be None (logging disabled).
        secrets_paths: explicit override for the candidate list (for testing).
        """
        self._llm = dict(manifest_llm or {})
        self._db = db
        candidates = secrets_paths or self._llm.get("secrets_paths")
        self._secrets, self._secrets_path = _load_secrets(candidates)
        self._openrouter_key = _get_openrouter_api_key(self._secrets)
        self._openrouter_pricing = None  # lazy-loaded {model_id: (prompt, completion) per token}

        if self._openrouter_key:
            log.info("openrouter key loaded from %s (%d chars)",
                     self._secrets_path, len(self._openrouter_key))
        else:
            log.info("no openrouter key found in any of %d candidate paths; "
                     "openrouter backend will fail",
                     len(candidates or SECRETS_DEFAULT_CANDIDATES) +
                     (1 if os.environ.get("CORTEX_SECRETS") else 0))

    # ── Public API ──────────────────────────────────────────────

    def complete(
        self,
        prompt: str,
        *,
        backend: str | None = None,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        purpose: str = "",
        images: list[dict] | None = None,
    ) -> dict:
        """Run a chat completion. Tries the requested (or default) backend
        first, then walks the fallback chain on failure.

        Returns:
            {
                "ok": bool,
                "text": str,                 # response content (empty if !ok)
                "backend": str,              # backend that succeeded ("" if none)
                "model": str,                # actual model used
                "latency_ms": int,
                "degraded": bool,            # True if fallback chain used
                "requested_backend": str,
                "error": str,                # set if !ok
                "prompt_tokens": int,
                "completion_tokens": int,
                "cost_usd": float,
            }

        Always logs the attempt to overseer.db.llm_calls (one row per attempt).

        images: optional list of {mime_type, data_base64} dicts. When
        non-empty, the user content becomes a multimodal block list
        (OpenAI-compat: text part + image_url parts). OpenRouter
        normalizes this to Anthropic's native vision format. The Pi
        on-device backend (a local llama-server) doesn't support
        vision — when images are passed, the on-device backend will
        be skipped from the chain (see _chain_for).
        """
        requested = backend or self._llm.get("backend") or "openrouter"
        chain = self._chain_for(requested, has_images=bool(images))
        last_err = "no backends configured"

        # Per-task model override: if the caller didn't pass an explicit
        # `model=` and the purpose has a registered override, use it.
        # Cheap models for small structured tasks, smart models for the
        # high-stakes interpretive work.
        if model is None and purpose:
            overrides = self._llm.get("model_overrides") or {}
            if purpose in overrides:
                model = overrides[purpose]

        for idx, b in enumerate(chain):
            degraded = (idx > 0)
            t0 = time.monotonic()
            try:
                text, usage, used_model = self._call_backend(
                    b, prompt, system=system, model=model,
                    max_tokens=max_tokens, temperature=temperature,
                    images=images,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                cost = self._estimate_cost(b, used_model, usage)
                self._log_call(
                    requested_backend=requested, actual_backend=b,
                    model=used_model, prompt_chars=len(prompt),
                    response_chars=len(text),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    response_tokens=usage.get("completion_tokens", 0),
                    latency_ms=latency_ms, cost_usd=cost,
                    degraded=degraded, ok=True, purpose=purpose,
                )
                return {
                    "ok": True,
                    "text": text,
                    "backend": b,
                    "model": used_model,
                    "latency_ms": latency_ms,
                    "degraded": degraded,
                    "requested_backend": requested,
                    "error": "",
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cost_usd": cost,
                }
            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                last_err = "{}: {}".format(b, e)
                log.warning("backend %s failed (%dms): %s", b, latency_ms, e)
                self._log_call(
                    requested_backend=requested, actual_backend=b, model="",
                    prompt_chars=len(prompt), latency_ms=latency_ms,
                    degraded=degraded, ok=False, error=str(e)[:500],
                    purpose=purpose,
                )
                continue

        return {
            "ok": False, "text": "", "backend": "", "model": "",
            "latency_ms": 0, "degraded": True, "requested_backend": requested,
            "error": last_err, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0,
        }

    def complete_messages(
        self,
        messages: list,
        *,
        backend: str | None = None,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        purpose: str = "",
        tools: list | None = None,
    ) -> dict:
        """Slice 10 — completion with a full message history and optional
        tool-use support. Use this (not `complete()`) when you want the
        LLM to be able to call tools, or when you need to send a
        multi-turn message list (e.g. tool_call → tool_result → next
        assistant turn).

        Returns:
            {
                ok: bool,
                message: dict,            # the assistant message returned
                                          # (content + tool_calls + role)
                text: str,                # message.content extracted as text
                tool_calls: list,         # parsed function-call list (may be empty)
                finish_reason: str,       # 'stop' | 'tool_calls' | 'length' | ...
                backend, model, latency_ms, prompt_tokens, completion_tokens,
                cost_usd, error, requested_backend, degraded,
            }

        The caller is responsible for: dispatching tool_calls, appending
        a `{role:'tool', tool_call_id, content}` message for each, and
        invoking complete_messages again until `finish_reason='stop'`.
        See `chat_tools.dispatch_tool` and `MAX_TOOL_ITER` for the loop.
        """
        requested = backend or self._llm.get("backend") or "openrouter"
        # Tool calls aren't supported by the on-device backend, so if
        # tools are passed and the chain would include `ondevice`,
        # filter it out. Same for vision-style prompts.
        chain = self._chain_for(requested, has_images=False)
        if tools:
            chain = [b for b in chain if b != "ondevice"]
            if not chain:
                return {
                    "ok": False, "text": "",
                    "error": "no backend supports tool use",
                    "message": {}, "tool_calls": [], "finish_reason": "",
                    "backend": "", "model": "", "latency_ms": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0, "requested_backend": requested,
                    "degraded": True,
                }
        last_err = "no backends configured"

        # Per-task model override (same logic as complete()).
        if model is None and purpose:
            overrides = self._llm.get("model_overrides") or {}
            if purpose in overrides:
                model = overrides[purpose]

        for idx, b in enumerate(chain):
            degraded = (idx > 0)
            t0 = time.monotonic()
            try:
                msg, usage, used_model, finish = self._call_backend_messages(
                    b, messages=messages, system=system, model=model,
                    max_tokens=max_tokens, temperature=temperature,
                    tools=tools,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                cost = self._estimate_cost(b, used_model, usage)
                content = msg.get("content")
                if isinstance(content, list):
                    # Block list — extract text, ignore image refs
                    text = "".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                else:
                    text = content or ""
                tool_calls = msg.get("tool_calls") or []
                self._log_call(
                    requested_backend=requested, actual_backend=b,
                    model=used_model,
                    prompt_chars=sum(len(json.dumps(m)) for m in messages),
                    response_chars=len(text or ""),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    response_tokens=usage.get("completion_tokens", 0),
                    latency_ms=latency_ms, cost_usd=cost,
                    degraded=degraded, ok=True, purpose=purpose,
                )
                return {
                    "ok": True,
                    "message": msg,
                    "text": text,
                    "tool_calls": tool_calls,
                    "finish_reason": finish,
                    "backend": b,
                    "model": used_model,
                    "latency_ms": latency_ms,
                    "degraded": degraded,
                    "requested_backend": requested,
                    "error": "",
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cost_usd": cost,
                }
            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                last_err = "{}: {}".format(b, e)
                log.warning("backend %s failed (messages, %dms): %s",
                            b, latency_ms, e)
                self._log_call(
                    requested_backend=requested, actual_backend=b, model="",
                    prompt_chars=sum(len(json.dumps(m)) for m in messages),
                    latency_ms=latency_ms,
                    degraded=degraded, ok=False, error=str(e)[:500],
                    purpose=purpose,
                )
                continue

        return {
            "ok": False, "message": {}, "text": "",
            "tool_calls": [], "finish_reason": "",
            "backend": "", "model": "",
            "latency_ms": 0, "degraded": True, "requested_backend": requested,
            "error": last_err, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0,
        }

    def _call_backend_messages(self, backend, *, messages, system, model,
                                max_tokens, temperature, tools):
        """Backend dispatch for the messages-form completion."""
        if backend == "openrouter":
            base_url = self._llm.get(
                "openrouter_url", "https://openrouter.ai/api/v1")
            requested = model or self._llm.get(
                "model", "anthropic/claude-opus-4.7")
            if not self._openrouter_key:
                raise RuntimeError("OpenRouter API key not configured")
            msg, usage, _resolved, finish = self._call_oai_messages(
                base_url=base_url, model=requested,
                messages=messages, system=system,
                max_tokens=max_tokens, temperature=temperature,
                timeout=self._llm.get("openrouter_timeout_s", 60),
                auth_header="Bearer {}".format(self._openrouter_key),
                extra_headers={
                    "HTTP-Referer": "https://github.com/turfptax/cortex-core",
                    "X-Title": "Cortex Overseer",
                },
                tools=tools,
            )
            return msg, usage, requested, finish
        if backend == "lmstudio":
            base_url = self._llm.get("lmstudio_url", "http://10.0.0.102:1234/v1")
            requested = model or self._llm.get(
                "lmstudio_model", "qwen3.5-9b")
            msg, usage, _resolved, finish = self._call_oai_messages(
                base_url=base_url, model=requested,
                messages=messages, system=system,
                max_tokens=max_tokens, temperature=temperature,
                timeout=self._llm.get("lmstudio_timeout_s", 60),
                tools=tools,
            )
            return msg, usage, requested, finish
        # No tools support on-device — caller already filtered, but
        # be defensive.
        raise RuntimeError(
            "backend {} does not support messages form".format(backend))

    def _call_oai_messages(self, *, base_url, model, messages, system,
                            max_tokens, temperature, timeout,
                            auth_header=None, extra_headers=None,
                            tools=None):
        """OAI chat-completions call with messages list and optional
        tools. Returns the assistant message dict + usage + resolved
        model + finish_reason."""
        url = base_url.rstrip("/") + "/chat/completions"
        body_messages: list = []
        if system:
            body_messages.append({"role": "system", "content": system})
        body_messages.extend(messages)
        body = {
            "model": model,
            "messages": body_messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth_header:
            headers["Authorization"] = auth_header
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                "HTTP {}: {}".format(e.code, err_body or e.reason))
        except urllib.error.URLError as e:
            raise RuntimeError("network: {}".format(e.reason))

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("non-JSON response: {}".format(raw[:200]))

        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("no choices in response: {}".format(raw[:200]))
        choice = choices[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason") or ""
        usage = payload.get("usage") or {}
        used_model = payload.get("model") or model
        return msg, usage, used_model, finish

    # ── Backend chain ───────────────────────────────────────────

    def _chain_for(self, requested: str, *, has_images: bool = False) -> list[str]:
        """Build the try-list: requested first, then declared fallback chain
        (with anything already tried filtered out).

        When has_images is True, the on-device backend is excluded — the
        local llama-server build doesn't support vision. The fallback
        order otherwise stays as declared in plugin.toml."""
        fb = self._llm.get("fallback") or []
        if isinstance(fb, str):
            fb = [fb]
        chain = [requested]
        for b in fb:
            if b and b not in chain:
                chain.append(b)
        if has_images:
            chain = [b for b in chain if b != "ondevice"]
        return chain

    # ── Per-backend dispatch ────────────────────────────────────

    def _call_backend(self, backend, prompt, *, system, model,
                      max_tokens, temperature, images=None):
        """Returns (text, usage_dict, model_id_used)."""
        if backend == "openrouter":
            return self._call_openrouter(prompt, system=system, model=model,
                                         max_tokens=max_tokens,
                                         temperature=temperature,
                                         images=images)
        if backend == "lmstudio":
            return self._call_oai(
                base_url=self._llm.get("lmstudio_url",
                                       "http://10.0.0.102:1234/v1"),
                model=model or self._llm.get("lmstudio_model", "auto"),
                prompt=prompt, system=system,
                max_tokens=max_tokens, temperature=temperature,
                timeout=self._llm.get("lmstudio_timeout_s", 60),
                auth_header=None,
                images=images,
            )
        if backend == "ondevice":
            # Sanity guard: should already be filtered out by _chain_for
            # when images were supplied. If a caller bypasses that,
            # silently drop images here rather than blow up.
            return self._call_oai(
                base_url=self._llm.get("ondevice_url",
                                       "http://127.0.0.1:8081/v1"),
                model=model or self._llm.get("ondevice_model", "auto"),
                prompt=prompt, system=system,
                max_tokens=max_tokens, temperature=temperature,
                timeout=self._llm.get("ondevice_timeout_s", 120),
                auth_header=None,
                images=None,
            )
        raise ValueError("unknown backend: {}".format(backend))

    def _call_openrouter(self, prompt, *, system, model, max_tokens,
                         temperature, images=None):
        if not self._openrouter_key:
            raise RuntimeError("OpenRouter API key not configured")
        base_url = self._llm.get("openrouter_url", "https://openrouter.ai/api/v1")
        requested = model or self._llm.get("model", "anthropic/claude-opus-4.7")
        text, usage, _resolved = self._call_oai(
            base_url=base_url,
            model=requested,
            prompt=prompt, system=system,
            max_tokens=max_tokens, temperature=temperature,
            timeout=self._llm.get("openrouter_timeout_s", 60),
            auth_header="Bearer {}".format(self._openrouter_key),
            extra_headers={
                # OpenRouter best-practice attribution headers; harmless.
                "HTTP-Referer": "https://github.com/turfptax/cortex-core",
                "X-Title": "Cortex Overseer",
            },
            images=images,
        )
        # Return the REQUESTED model id rather than the resolved variant.
        # OpenRouter's /models lists slugs like "anthropic/claude-opus-4.7"
        # but actual responses come back tagged with timestamped variants
        # (e.g. "anthropic/claude-4.7-opus-20260416") that aren't in the
        # pricing dict. Using the requested slug downstream keeps the cost
        # lookup honest and matches what we paid for.
        return text, usage, requested

    # ── OAI chat-completions transport ──────────────────────────

    def _call_oai(self, *, base_url, model, prompt, system,
                  max_tokens, temperature, timeout,
                  auth_header=None, extra_headers=None, images=None):
        url = base_url.rstrip("/") + "/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            # OpenAI-compat multimodal content list. OpenRouter
            # normalizes image_url with a data: URL to Anthropic's
            # native vision format. Each image dict must carry
            # mime_type + data_base64.
            parts: list[dict] = []
            if prompt:
                parts.append({"type": "text", "text": prompt})
            for img in images:
                mime = (img.get("mime_type") or "image/png").strip()
                b64 = img.get("data_base64") or ""
                if not b64:
                    continue
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "data:{};base64,{}".format(mime, b64),
                    },
                })
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": prompt})
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth_header:
            headers["Authorization"] = auth_header
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError("HTTP {}: {}".format(e.code, err_body or e.reason))
        except urllib.error.URLError as e:
            raise RuntimeError("network: {}".format(e.reason))

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("non-JSON response: {}".format(raw[:200]))

        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("no choices in response: {}".format(raw[:200]))
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        usage = payload.get("usage") or {}
        used_model = payload.get("model") or model
        return text, usage, used_model

    # ── Cost estimation (OpenRouter only) ───────────────────────

    def _estimate_cost(self, backend, model_id, usage):
        if backend != "openrouter" or not usage:
            return 0.0
        pricing = self._get_openrouter_pricing()
        if not pricing or not model_id:
            return 0.0
        rates = pricing.get(model_id)
        if not rates:
            return 0.0
        prompt_per_token, completion_per_token = rates
        pt = usage.get("prompt_tokens") or 0
        ct = usage.get("completion_tokens") or 0
        return round(pt * prompt_per_token + ct * completion_per_token, 6)

    def _get_openrouter_pricing(self):
        """Lazy-load model pricing from OpenRouter /models. Cached for
        process lifetime."""
        if self._openrouter_pricing is not None:
            return self._openrouter_pricing
        self._openrouter_pricing = {}
        try:
            base_url = self._llm.get("openrouter_url",
                                     "https://openrouter.ai/api/v1")
            req = urllib.request.Request(
                base_url.rstrip("/") + "/models",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for m in payload.get("data") or []:
                mid = m.get("id")
                pricing = m.get("pricing") or {}
                # Pricing values are USD per token (string), e.g. "0.000003"
                try:
                    p = float(pricing.get("prompt") or 0)
                    c = float(pricing.get("completion") or 0)
                except (TypeError, ValueError):
                    continue
                if mid:
                    self._openrouter_pricing[mid] = (p, c)
            log.info("loaded openrouter pricing for %d models",
                     len(self._openrouter_pricing))
        except Exception as e:
            log.warning("openrouter pricing fetch failed: %s", e)
        return self._openrouter_pricing

    # ── DB logging ──────────────────────────────────────────────

    def _log_call(self, **kwargs):
        if self._db is None:
            return
        try:
            self._db.log_llm_call(**kwargs)
        except Exception as e:
            log.warning("llm_calls logging failed: %s", e)
