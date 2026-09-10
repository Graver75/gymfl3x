from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import ui_copy as ui
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu, skip_kb
from app.services.progression import PHASE_LABELS
from app.services.users import apply_onboarding, can_open_admin, get_or_create_user
from app.states import OnboardingSG

router = Router(name="start")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    if message.from_user is None:
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or message.from_user.username or "Athlete",
        )

    payload = (command.args or "").strip()
    if user.onboarding_done:
        await state.clear()
        text = (
            f"{ui.ICO_WAVE} Привет, {ui.b(user.display_name)}!\n"
            f"Код в сводке: {ui.b(user.short_code)}\n"
            f"Фаза: {ui.b(PHASE_LABELS[user.phase])}"
        )
        await message.answer(text, reply_markup=main_menu(show_admin=can_open_admin(user)))
        if payload == "workout":
            await message.answer(f"Жми «{ui.b(ui.BTN_WORKOUT)}», чтобы начать лог.")
        return

    await state.set_state(OnboardingSG.display_name)
    await message.answer(
        f"{ui.ICO_WAVE} Привет! Это {ui.b('Gymflex')} — бот для логов в качалке.\n\n"
        f"{ui.b('Сначала онбординг.')} Как тебя называть?"
    )


@router.message(OnboardingSG.display_name)
async def onb_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 1:
        await message.answer("Нужно имя.")
        return
    await state.update_data(display_name=name[:64])
    await state.set_state(OnboardingSG.short_code)
    default = name[0].upper()
    await message.answer(
        f"Короткий код для сводки в чат (как «И» или «К»).\n"
        f"Можно одним символом. Предлагаю: {default}"
    )


@router.message(OnboardingSG.short_code)
async def onb_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    if not (1 <= len(code) <= 4):
        await message.answer("Код 1–4 символа.")
        return
    await state.update_data(short_code=code)
    await state.set_state(OnboardingSG.body_weight)
    await message.answer("Вес тела в кг (например 78 или 78.5):")


@router.message(OnboardingSG.body_weight)
async def onb_weight(message: Message, state: FSMContext) -> None:
    try:
        weight = float((message.text or "").replace(",", "."))
        if not (30 < weight < 300):
            raise ValueError
    except ValueError:
        await message.answer("Введи нормальный вес, например 80.")
        return
    await state.update_data(body_weight=weight)
    await state.set_state(OnboardingSG.height)
    await message.answer("Рост в см (опционально):", reply_markup=skip_kb())


@router.callback_query(OnboardingSG.height, F.data == "onb:skip_height")
async def onb_skip_height(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(height_cm=None)
    await state.set_state(OnboardingSG.experience)
    if callback.message:
        await callback.message.answer(
            "Сколько месяцев уже занимаешься? Числом, например 3 или 24."
        )
    await callback.answer()


@router.message(OnboardingSG.height)
async def onb_height(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    if text in {"-", "skip", "пропустить", "нет"}:
        height = None
    else:
        try:
            height = float(text.replace(",", "."))
            if not (120 < height < 250):
                raise ValueError
        except ValueError:
            await message.answer("Рост в см или «пропустить».")
            return
    await state.update_data(height_cm=height)
    await state.set_state(OnboardingSG.experience)
    await message.answer("Сколько месяцев уже занимаешься? Числом, например 3 или 24.")


@router.message(OnboardingSG.experience)
async def onb_experience(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        months = int((message.text or "").strip())
        if months < 0 or months > 600:
            raise ValueError
    except ValueError:
        await message.answer("Целое число месяцев, например 8.")
        return

    data = await state.get_data()
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        user = await apply_onboarding(
            session,
            user,
            display_name=data["display_name"],
            short_code=data["short_code"],
            body_weight=data["body_weight"],
            height_cm=data.get("height_cm"),
            experience_months=months,
        )

    await state.clear()
    await message.answer(
        f"{ui.ICO_DONE} Готово!\n"
        f"Фаза прогрессии: {PHASE_LABELS[user.phase]}\n\n"
        "• медовый месяц (<6 мес) — быстрее поднимаем вес\n"
        "• средний (6–18) — умеренно\n"
        "• плато (>18) — сначала повторы, потом вес\n\n"
        f"Фазу можно сменить в {ui.BTN_PROFILE}.\n"
        "Админ задаёт общую программу — она read-only для остальных.\n\n"
        "Команды: /today /program /profile /admin",
        reply_markup=main_menu(show_admin=can_open_admin(user)),
    )
