"""Coach LLM provider registry: Gemini + OpenAI-compatible presets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AppSetting
from app.db.session import SessionLocal
from app.services.gemini_client import GeminiResult, generate_content, ping_gemini
from app.services.openai_llm import chat_completions, ping_openai_compatible

logger = logging.getLogger("gymflex.llm_providers")

SETTING_PROVIDER = "coach_provider"
SETTING_ENABLED = "coach_enabled"

# Preset ids used in admin UI and DB
PROVIDER_GEMINI = "gemini"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_QWEN = "qwen"
PROVIDER_TOKENN = "tokenn"

PROVIDER_LABELS = {
    PROVIDER_GEMINI: "Gemini",
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_QWEN: "Qwen",
    PROVIDER_TOKENN: "Tokenn",
}


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str  # gemini | openai
    api_key: str
    model: str
    base_url: str = ""


def _specs_from_settings(settings: Settings | None = None) -> dict[str, ProviderSpec]:
    s = settings or get_settings()
    return {
        PROVIDER_GEMINI: ProviderSpec(
            id=PROVIDER_GEMINI,
            label="Gemini",
            kind="gemini",
            api_key=(s.gemini_api_key or "").strip(),
            model=(s.gemini_model or "gemini-2.5-flash-lite").strip(),
        ),
        PROVIDER_TOKENN: ProviderSpec(
            id=PROVIDER_TOKENN,
            label="Tokenn",
            kind="openai",
            api_key=(s.tokenn_api_key or "").strip(),
            model=(s.tokenn_model or "gemini-3.7-flash").strip(),
            base_url=(s.tokenn_base_url or "https://api.tokenn.pro/v1").rstrip("/"),
        ),
        PROVIDER_DEEPSEEK: ProviderSpec(
            id=PROVIDER_DEEPSEEK,
            label="DeepSeek",
            kind="openai",
            api_key=(s.deepseek_api_key or "").strip(),
            model=(s.deepseek_model or "deepseek-chat").strip(),
            base_url=(s.deepseek_base_url or "https://api.deepseek.com").rstrip("/"),
        ),
        PROVIDER_QWEN: ProviderSpec(
            id=PROVIDER_QWEN,
            label="Qwen",
            kind="openai",
            api_key=(s.qwen_api_key or "").strip(),
            model=(s.qwen_model or "qwen-flash").strip(),
            base_url=(
                s.qwen_base_url
                or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
            ).rstrip("/"),
        ),
    }


def list_provider_specs() -> list[ProviderSpec]:
    return list(_specs_from_settings().values())


def get_provider_spec(provider_id: str) -> ProviderSpec | None:
    return _specs_from_settings().get(provider_id)


def provider_configured(spec: ProviderSpec) -> bool:
    if spec.kind == "gemini":
        return bool(spec.api_key)
    return bool(spec.api_key and spec.base_url)


async def get_active_provider_id(session: AsyncSession | None = None) -> str:
    """DB override if set and known; else env COACH_PROVIDER; else first configured."""
    settings = get_settings()
    env_default = (settings.coach_provider or PROVIDER_GEMINI).strip().lower()

    async def _from_db(s: AsyncSession) -> str | None:
        row = await s.get(AppSetting, SETTING_PROVIDER)
        if row and row.value:
            return row.value.strip().lower()
        return None

    if session is not None:
        db_val = await _from_db(session)
    else:
        async with SessionLocal() as s:
            db_val = await _from_db(s)

    specs = _specs_from_settings()
    if db_val and db_val in specs:
        return db_val
    if env_default in specs:
        return env_default
    for pid in (PROVIDER_TOKENN, PROVIDER_GEMINI, PROVIDER_DEEPSEEK, PROVIDER_QWEN):
        if provider_configured(specs[pid]):
            return pid
    return env_default if env_default in specs else PROVIDER_GEMINI


async def set_active_provider(session: AsyncSession, provider_id: str) -> bool:
    pid = provider_id.strip().lower()
    if pid not in _specs_from_settings():
        return False
    row = await session.get(AppSetting, SETTING_PROVIDER)
    if row is None:
        session.add(AppSetting(key=SETTING_PROVIDER, value=pid))
    else:
        row.value = pid
    await session.commit()
    return True


def _parse_enabled_value(raw: str | None) -> bool:
    if raw is None or raw.strip() == "":
        return True
    text = raw.strip().lower()
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    return True


async def is_coach_enabled(session: AsyncSession | None = None) -> bool:
    """Env NN_ENABLED AND admin DB toggle (default on)."""
    settings = get_settings()
    if not settings.nn_enabled:
        return False

    async def _from_db(s: AsyncSession) -> bool:
        row = await s.get(AppSetting, SETTING_ENABLED)
        return _parse_enabled_value(row.value if row else None)

    if session is not None:
        return await _from_db(session)
    async with SessionLocal() as s:
        return await _from_db(s)


async def set_coach_enabled(session: AsyncSession, enabled: bool) -> None:
    value = "1" if enabled else "0"
    row = await session.get(AppSetting, SETTING_ENABLED)
    if row is None:
        session.add(AppSetting(key=SETTING_ENABLED, value=value))
    else:
        row.value = value
    await session.commit()


async def ping_provider(spec: ProviderSpec, *, timeout: float | None = None) -> bool:
    settings = get_settings()
    t = timeout if timeout is not None else settings.nn_health_timeout_sec
    if not provider_configured(spec):
        return False
    if spec.kind == "gemini":
        # ping_gemini uses settings key; temporarily ok since same key as spec
        return await ping_gemini()
    return await ping_openai_compatible(
        api_key=spec.api_key, base_url=spec.base_url, timeout=t
    )


async def generate_with_provider(
    *,
    provider_id: str | None = None,
    system: str,
    user_text: str,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, GeminiResult]:
    """Returns (provider_id_used, result). Never raises."""
    settings = get_settings()
    pid = provider_id or await get_active_provider_id()
    spec = get_provider_spec(pid)
    if spec is None or not provider_configured(spec):
        return pid, GeminiResult(status="error", error=f"provider {pid} not configured")

    if spec.kind == "gemini":
        result = await generate_content(
            system=system, user_text=user_text, history=history
        )
        return pid, result

    result = await chat_completions(
        api_key=spec.api_key,
        base_url=spec.base_url,
        model=spec.model,
        system=system,
        user_text=user_text,
        history=history,
        timeout=settings.nn_timeout_sec,
    )
    return pid, result


async def providers_status_snapshot() -> list[dict[str, Any]]:
    """Ping all presets for admin UI."""
    active = await get_active_provider_id()
    out: list[dict[str, Any]] = []
    for spec in list_provider_specs():
        configured = provider_configured(spec)
        online = False
        if configured:
            try:
                online = await ping_provider(spec)
            except Exception:
                online = False
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "kind": spec.kind,
                "model": spec.model,
                "configured": configured,
                "online": online,
                "active": spec.id == active,
                "base_url": spec.base_url or None,
            }
        )
    return out
