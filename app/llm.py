"""One place that talks to the LLM (Groq, with an optional Gemini fallback), same pattern as VideoFit AI."""

import time
from typing import TypeVar

import groq
from pydantic import BaseModel

from app import config

T = TypeVar("T", bound=BaseModel)
ATTEMPTS = 2        # the model occasionally answers in plain text instead of calling the output tool
RETRY_DELAY = 3.0   # seconds; gives a short rate limit (HTTP 429) a moment to clear
FALLBACK_COOLDOWN = 300   # after Groq rate-limits us, keep using the fallback for this many seconds

# Running totals for this process. Agents read total_tokens() before and after a call to get that call's tokens.
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "fallbacks": 0}
_groq_blocked_until = 0.0   # time.time() until which we skip Groq


def total_tokens() -> int:
    return USAGE["input_tokens"] + USAGE["output_tokens"]


def track(handler) -> None:
    """Add the tokens a UsageMetadataCallbackHandler saw to the running total."""
    for usage in handler.usage_metadata.values():
        USAGE["calls"] += 1
        USAGE["input_tokens"] += usage.get("input_tokens", 0)
        USAGE["output_tokens"] += usage.get("output_tokens", 0)


def _chat(temperature: float, fallback: bool):
    """The chat model: Groq normally, Gemini when `fallback` is True. Both speak the same LangChain interface."""
    if fallback:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=config.FALLBACK_MODEL, temperature=temperature,
                                      google_api_key=config.GEMINI_API_KEY)
    from langchain_groq import ChatGroq
    return ChatGroq(model=config.LLM_MODEL, temperature=temperature)


def _run(call):
    """call(use_fallback) does one LLM request. Try Groq first. If Groq rate-limits us and a fallback is configured,
    run the same request on Gemini, and keep using Gemini for a few minutes so we do not wait on Groq every call."""
    global _groq_blocked_until
    if config.fallback_enabled() and time.time() < _groq_blocked_until:
        return call(True)
    try:
        return call(False)
    except groq.RateLimitError:
        if not config.fallback_enabled():
            raise
        _groq_blocked_until = time.time() + FALLBACK_COOLDOWN
        USAGE["fallbacks"] += 1
        return call(True)


def call_with_tools(messages: list, tools: list):
    """One chat turn where the model may ask to call our tools. Returns the model's AIMessage."""
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    config.require_groq_key()

    def call(fallback: bool):
        handler = UsageMetadataCallbackHandler()
        try:
            chat = _chat(0, fallback).bind_tools(tools)
            return chat.invoke(messages, config={"callbacks": [handler]})
        finally:
            track(handler)

    return _run(call)


def _invoke_once(schema: type[T], system: str, user: str, temperature: float, method: str) -> T:
    """method "tool" = the model calls an output function. "json" = plain JSON mode, schema written in the prompt."""
    import json

    from langchain_core.callbacks import UsageMetadataCallbackHandler

    if method == "json":
        system += "\n\nReply with ONLY a JSON object matching this JSON schema:\n" + json.dumps(schema.model_json_schema())

    def call(fallback: bool):
        handler = UsageMetadataCallbackHandler()
        chat = _chat(temperature, fallback)
        structured = chat.with_structured_output(schema, method="json_mode") if method == "json" else chat.with_structured_output(schema)
        try:
            return structured.invoke([("system", system), ("human", user)], config={"callbacks": [handler]})
        finally:
            track(handler)

    return _run(call)


def call_structured(schema: type[T], system: str, user: str, temperature: float = 0.0) -> T:
    """Ask the model for output matching `schema`. If the first attempt fails, wait briefly and retry once in
    JSON mode (this rescues both tool-call glitches and models with broken tool calling). Raises if both fail."""
    config.require_groq_key()
    for attempt in range(ATTEMPTS):
        try:
            return _invoke_once(schema, system, user, temperature, "tool" if attempt == 0 else "json")
        except Exception:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(RETRY_DELAY)
