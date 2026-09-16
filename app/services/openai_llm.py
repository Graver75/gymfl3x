"""OpenAI-compatible chat completions (DeepSeek, Qwen DashScope, etc.)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.services.gemini_client import GeminiResult

logger = logging.getLogger("gymflex.openai_llm")


async def ping_openai_compatible(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 5.0,
) -> bool:
    key = (api_key or "").strip()
    base = (base_url or "").rstrip("/")
    if not key or not base:
        return False
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Prefer /models; Tokenn and some gateways expose /balance instead
            for path in ("/models", "/balance"):
                r = await client.get(f"{base}{path}", headers=headers)
                if r.status_code == 200:
                    return True
                if r.status_code in {401, 403}:
                    return False
            return False
    except Exception as exc:
        logger.debug("OpenAI-compatible ping failed: %s", exc)
        return False


async def chat_completions(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    user_text: str,
    history: list[dict[str, str]] | None = None,
    timeout: float = 60.0,
) -> GeminiResult:
    """Never raises."""
    key = (api_key or "").strip()
    base = (base_url or "").rstrip("/")
    if not key or not base:
        return GeminiResult(status="error", error="OpenAI-compatible key/url empty")

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in history or []:
        role = turn.get("role") or "user"
        if role not in {"user", "assistant", "system"}:
            role = "user"
        content = (turn.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    url = f"{base}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 1024,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            try:
                body = r.json()
            except Exception:
                body = {}

            if r.status_code == 429:
                err = body.get("error") or {}
                msg = str(err.get("message") or body.get("message") or r.text)[:500]
                return GeminiResult(
                    status="429",
                    error=msg,
                    quota_id="openai_rpm_or_rpd",
                    duration_sec=time.monotonic() - started,
                )
            if r.status_code != 200:
                err = body.get("error") or {}
                msg = str(err.get("message") or body.get("message") or r.text)[:500]
                logger.warning("OpenAI LLM HTTP %s: %s", r.status_code, msg[:200])
                return GeminiResult(
                    status="error",
                    error=msg,
                    duration_sec=time.monotonic() - started,
                )

            choices = body.get("choices") or []
            text = ""
            if choices:
                msg = choices[0].get("message") or {}
                text = str(msg.get("content") or "").strip()
            usage = body.get("usage") or {}
            prompt_tok = int(usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or 0)
            if not text:
                return GeminiResult(
                    status="error",
                    error="empty choices",
                    prompt_tokens=prompt_tok,
                    output_tokens=out_tok,
                    duration_sec=time.monotonic() - started,
                )
            return GeminiResult(
                text=text,
                status="ok",
                prompt_tokens=prompt_tok,
                output_tokens=out_tok,
                duration_sec=time.monotonic() - started,
            )
    except Exception as exc:
        logger.warning("OpenAI LLM failed: %s", exc)
        return GeminiResult(
            status="error",
            error=str(exc)[:500],
            duration_sec=time.monotonic() - started,
        )
