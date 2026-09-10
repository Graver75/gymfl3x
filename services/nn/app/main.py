from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.load import LoadTracker
from app.prompts import DATA_SCHEMA_RU, SYSTEM_PROMPT, user_prompt_for

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gymflex-nn")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b-instruct")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT_SEC", "180"))
MAX_HISTORY = int(os.getenv("NN_MAX_HISTORY", "12"))
MAX_CONCURRENT = int(os.getenv("NN_MAX_CONCURRENT", "1"))

app = FastAPI(title="gymflex-nn", version="0.3.0")
tracker = LoadTracker(max_concurrent=MAX_CONCURRENT)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CoachRequest(BaseModel):
    kind: Literal["session", "week", "month", "exercise", "live_set"]
    locale: str = "ru"
    user_id: int | None = None
    user_label: str | None = None
    athlete: dict[str, Any] = Field(default_factory=dict)
    focus: dict[str, Any] = Field(default_factory=dict)
    history: list[HistoryMessage] = Field(default_factory=list)


class CoachResponse(BaseModel):
    text: str
    model: str
    truncated: bool = False
    history_used: int = 0
    job_id: str | None = None
    queue_waited: bool = False


async def _ollama_tags() -> list[str]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{OLLAMA_HOST}/api/tags")
        r.raise_for_status()
        models = r.json().get("models") or []
        return [m.get("name", "") for m in models if isinstance(m, dict)]


def _model_ready(names: list[str]) -> bool:
    want = OLLAMA_MODEL
    want_base = want.split(":")[0]
    for name in names:
        if name == want or name.startswith(want) or name.startswith(want_base):
            return True
    return False


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        names = await _ollama_tags()
    except Exception as exc:
        logger.warning("Ollama health failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "ollama": "down", "model": OLLAMA_MODEL, "error": str(exc)},
        ) from exc

    if not _model_ready(names):
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "ollama": "up",
                "model": OLLAMA_MODEL,
                "error": "model_not_pulled",
                "available": names[:20],
            },
        )
    load = tracker.snapshot()
    return {
        "ok": True,
        "ollama": "up",
        "model": OLLAMA_MODEL,
        "active_count": load["active_count"],
        "queued_count": load["queued_count"],
    }


@app.get("/v1/meta")
async def meta() -> dict[str, Any]:
    """System prompt + description of athlete payload (for Telegram UI)."""
    return {
        "model": OLLAMA_MODEL,
        "system_prompt": SYSTEM_PROMPT,
        "data_schema_ru": DATA_SCHEMA_RU,
        "max_history": MAX_HISTORY,
        "max_concurrent": MAX_CONCURRENT,
    }


@app.get("/v1/load")
async def load() -> dict[str, Any]:
    """Admin: active jobs + queue (multiple users can enqueue concurrently)."""
    snap = tracker.snapshot()
    return {
        "ok": True,
        "model": OLLAMA_MODEL,
        **snap,
    }


async def _call_ollama(messages: list[dict[str, str]]) -> str:
    body = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 512},
        "messages": messages,
    }
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        r = await client.post(f"{OLLAMA_HOST}/api/chat", json=body)
        r.raise_for_status()
        data = r.json()
    message = (data.get("message") or {}).get("content") or ""
    text = str(message).strip()
    if not text:
        raise HTTPException(status_code=502, detail="empty_model_response")
    return text


@app.post("/v1/coach", response_model=CoachResponse)
async def coach(req: CoachRequest) -> CoachResponse:
    payload = json.dumps(req.athlete, ensure_ascii=False, separators=(",", ":"))
    truncated = False
    if len(payload) > 12_000:
        payload = payload[:12_000] + "…"
        truncated = True

    user_content = user_prompt_for(req.kind, payload, req.focus, locale=req.locale)
    prior = [
        {"role": m.role, "content": m.content}
        for m in req.history[-MAX_HISTORY:]
        if m.role in {"user", "assistant"} and m.content.strip()
    ]
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(prior)
    messages.append({"role": "user", "content": user_content})

    label = req.user_label
    if not label and isinstance(req.athlete.get("user"), dict):
        label = req.athlete["user"].get("code") or req.athlete["user"].get("short_code")

    # Snapshot before enqueue to know if we will wait
    before = tracker.snapshot()
    will_wait = before["active_count"] >= tracker.max_concurrent

    async def _work() -> str:
        try:
            return await _call_ollama(messages)
        except httpx.HTTPError as exc:
            logger.exception("Ollama chat failed user_id=%s", req.user_id)
            raise HTTPException(status_code=502, detail=f"ollama_error: {exc}") from exc

    text = await tracker.run(
        user_id=req.user_id,
        user_label=label,
        kind=req.kind,
        work=_work,
    )
    if len(text) > 3500:
        text = text[:3490] + "…"
        truncated = True
    return CoachResponse(
        text=text,
        model=OLLAMA_MODEL,
        truncated=truncated,
        history_used=len(prior),
        queue_waited=will_wait,
    )
