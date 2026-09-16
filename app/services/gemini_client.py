"""Google Gemini generateContent client (never raises to callers via Result)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("gymflex.gemini")

_BASE = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class GeminiResult:
    text: str | None = None
    status: str = "error"  # ok | error | 429
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    quota_id: str | None = None
    quota_value: str | None = None
    duration_sec: float = 0.0


def _parse_quota(details: list[Any] | None) -> tuple[str | None, str | None]:
    if not details:
        return None, None
    for d in details:
        if not isinstance(d, dict):
            continue
        violations = d.get("violations") or []
        for v in violations:
            if not isinstance(v, dict):
                continue
            qid = v.get("quotaId") or v.get("quota_id")
            qval = v.get("quotaValue") or v.get("quota_value")
            if qid or qval:
                return (
                    str(qid) if qid else None,
                    str(qval) if qval is not None else None,
                )
        # Sometimes metric lives on the violation differently
        metric = d.get("quotaMetric") or d.get("quota_metric")
        if metric:
            return str(metric), None
    return None, None


def _extract_retry_seconds(message: str) -> float | None:
    m = re.search(r"retry in ([0-9.]+)s", message, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


async def ping_gemini() -> bool:
    """Lightweight models list; False if key missing or request fails."""
    settings = get_settings()
    key = (settings.gemini_api_key or "").strip()
    if not key:
        return False
    url = f"{_BASE}/models"
    try:
        async with httpx.AsyncClient(timeout=settings.nn_health_timeout_sec) as client:
            r = await client.get(url, params={"key": key, "pageSize": 1})
            return r.status_code == 200
    except Exception as exc:
        logger.debug("Gemini ping failed: %s", exc)
        return False


async def generate_content(
    *,
    system: str,
    user_text: str,
    history: list[dict[str, str]] | None = None,
) -> GeminiResult:
    """Call Gemini generateContent. Never raises."""
    settings = get_settings()
    key = (settings.gemini_api_key or "").strip()
    if not key:
        return GeminiResult(status="error", error="GEMINI_API_KEY empty")

    model = settings.gemini_model.strip() or "gemini-2.5-flash-lite"
    url = f"{_BASE}/models/{model}:generateContent"

    contents: list[dict[str, Any]] = []
    for turn in history or []:
        role = turn.get("role") or "user"
        # Gemini: user | model
        g_role = "model" if role == "assistant" else "user"
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        contents.append({"role": g_role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    payload: dict[str, Any] = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 1024,
        },
    }

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.nn_timeout_sec) as client:
            r = await client.post(url, params={"key": key}, json=payload)
            body: dict[str, Any]
            try:
                body = r.json()
            except Exception:
                body = {}

            if r.status_code == 429:
                err = body.get("error") or {}
                msg = str(err.get("message") or r.text)[:500]
                qid, qval = _parse_quota(err.get("details"))
                retry = _extract_retry_seconds(msg)
                if retry and retry < 30:
                    await asyncio.sleep(min(retry + 0.2, 8.0))
                    r2 = await client.post(url, params={"key": key}, json=payload)
                    try:
                        body2 = r2.json()
                    except Exception:
                        body2 = {}
                    if r2.status_code == 200:
                        return _ok_from_body(body2, time.monotonic() - started)
                    if r2.status_code == 429:
                        err2 = body2.get("error") or {}
                        msg2 = str(err2.get("message") or r2.text)[:500]
                        qid2, qval2 = _parse_quota(err2.get("details"))
                        return GeminiResult(
                            status="429",
                            error=msg2,
                            quota_id=qid2 or qid,
                            quota_value=qval2 or qval,
                            duration_sec=time.monotonic() - started,
                        )
                return GeminiResult(
                    status="429",
                    error=msg,
                    quota_id=qid,
                    quota_value=qval,
                    duration_sec=time.monotonic() - started,
                )

            if r.status_code != 200:
                err = body.get("error") or {}
                msg = str(err.get("message") or r.text)[:500]
                logger.warning("Gemini HTTP %s: %s", r.status_code, msg[:200])
                return GeminiResult(
                    status="error",
                    error=msg,
                    duration_sec=time.monotonic() - started,
                )

            return _ok_from_body(body, time.monotonic() - started)
    except Exception as exc:
        logger.warning("Gemini request failed: %s", exc)
        return GeminiResult(
            status="error",
            error=str(exc)[:500],
            duration_sec=time.monotonic() - started,
        )


def _ok_from_body(body: dict[str, Any], duration: float) -> GeminiResult:
    text = ""
    for cand in body.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                text += str(part["text"])
    usage = body.get("usageMetadata") or body.get("usage_metadata") or {}
    prompt_tok = int(usage.get("promptTokenCount") or usage.get("prompt_token_count") or 0)
    out_tok = int(
        usage.get("candidatesTokenCount")
        or usage.get("candidates_token_count")
        or 0
    )
    text = text.strip()
    if not text:
        return GeminiResult(
            status="error",
            error="empty candidates",
            prompt_tokens=prompt_tok,
            output_tokens=out_tok,
            duration_sec=duration,
        )
    return GeminiResult(
        text=text,
        status="ok",
        prompt_tokens=prompt_tok,
        output_tokens=out_tok,
        duration_sec=duration,
    )
