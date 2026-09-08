from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    Difficulty,
    SessionSet,
    SessionStatus,
    TemplateExercise,
    User,
    UserExerciseState,
    WorkoutSession,
    WorkoutTemplate,
)
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    difficulty_kb,
    main_menu,
    reps_kb,
    sets_kb,
    weight_kb,
    workout_exercise_kb,
)
from app.services.progression import (
    DIFFICULTY_LABELS,
    format_block,
    suggest_next_weight,
)
from app.services.recap import build_personal_retrospective
from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday
from app.services.users import get_or_create_user
from app.states import WorkoutSG

router = Router(name="workout")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _load_user(telegram_id: int, full_name: str) -> User | None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        if not user.onboarding_done:
            return None
        session.expunge(user)
        return user


async def _active_session(session, user_id: int) -> WorkoutSession | None:
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == SessionStatus.active,
        )
        .options(
            selectinload(WorkoutSession.sets),
            selectinload(WorkoutSession.template).selectinload(WorkoutTemplate.exercises),
        )
        .order_by(WorkoutSession.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_state(session, user_id: int, exercise_id: int) -> UserExerciseState | None:
    result = await session.execute(
        select(UserExerciseState).where(
            UserExerciseState.user_id == user_id,
            UserExerciseState.exercise_id == exercise_id,
        )
    )
    return result.scalar_one_or_none()


async def _done_exercise_ids(ws: WorkoutSession) -> set[int]:
    return {s.exercise_id for s in ws.sets if s.exercise_id is not None}


@router.message(Command("workout"))
@router.message(F.text == "Тренировка")
async def start_workout(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    user = await _load_user(
        message.from_user.id,
        message.from_user.full_name or "Athlete",
    )
    if not user:
        await message.answer("Сначала /start.")
        return

    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()

    async with SessionLocal() as session:
        db_user = await get_or_create_user(
            session, message.from_user.id, message.from_user.full_name or "Athlete"
        )
        existing = await _active_session(session, db_user.id)
        if existing and existing.template:
            template = existing.template
            done = await _done_exercise_ids(existing)
            await state.set_state(WorkoutSG.pick_exercise)
            await state.update_data(session_id=existing.id, template_id=template.id)
            await message.answer(
                f"Продолжаем: {template.name}\nВыбери упражнение:",
                reply_markup=workout_exercise_kb(template.exercises, done),
            )
            return

        template = await get_template_for_weekday(session, weekday)
        if not template:
            await message.answer(
                f"Сегодня {WEEKDAY_NAMES[weekday]} — нет тренировки в графике.\n"
                "Админ может назначить день в Админке."
            )
            return

        ws = WorkoutSession(
            user_id=db_user.id,
            template_id=template.id,
            session_date=today,
            status=SessionStatus.active,
        )
        session.add(ws)
        await session.commit()
        await session.refresh(ws)
        result = await session.execute(
            select(WorkoutTemplate)
            .where(WorkoutTemplate.id == template.id)
            .options(selectinload(WorkoutTemplate.exercises))
        )
        template = result.scalar_one()

        await state.set_state(WorkoutSG.pick_exercise)
        await state.update_data(session_id=ws.id, template_id=template.id)
        await message.answer(
            f"Старт: {template.name} (#{template.hashtag})\n"
            "Выбери упражнение — бот подскажет рабочий вес.",
            reply_markup=workout_exercise_kb(template.exercises, set()),
        )


@router.callback_query(WorkoutSG.pick_exercise, F.data.startswith("wo:ex:"))
async def pick_exercise(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    exercise_id = int(callback.data.split(":")[-1])

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        if not exercise:
            await callback.answer("Упражнение удалено")
            return
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)
        suggested = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight

    await state.update_data(
        exercise_id=exercise_id,
        draft_weight=suggested if suggested is not None else 20.0,
        draft_reps=None,
        draft_sets=None,
    )
    await state.set_state(WorkoutSG.weight)

    hint = ""
    if suggested is not None:
        hint = f"\nРабочий/предложенный вес: {suggested:g} кг"
    target = f"{exercise.target_sets}×{exercise.target_reps_min}-{exercise.target_reps_max}"

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)
        data = await state.get_data()
        kb = weight_kb(exercise, ex_state, data.get("draft_weight"))

    await callback.message.edit_text(
        f"{exercise.name}\nЦель: {target}{hint}\n\nВыбери вес:",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WorkoutSG.weight, F.data.startswith("wo:w:"))
async def pick_weight(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    parts = callback.data.split(":")
    action = parts[2]

    data = await state.get_data()
    exercise_id = data["exercise_id"]
    draft = float(data.get("draft_weight") or 20.0)

    if action == "custom":
        await state.set_state(WorkoutSG.custom_weight)
        await callback.message.answer("Введи вес числом, например 55 или 57.5")
        await callback.answer()
        return

    if action == "+":
        draft += float(parts[3])
    elif action == "-":
        draft = max(0.0, draft - float(parts[3]))
    elif action == "=":
        draft = float(parts[3])

    await state.update_data(draft_weight=draft)

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)

        if action == "=":
            # confirmed weight -> go to reps
            await state.set_state(WorkoutSG.reps)
            last_reps = ex_state.last_reps if ex_state else None
            await callback.message.edit_text(
                f"{exercise.name}\nВес: {draft:g} кг\nСколько повторений в рабочих подходах?",
                reply_markup=reps_kb(exercise.target_reps_min, exercise.target_reps_max, last_reps),
            )
            await callback.answer()
            return

        await callback.message.edit_reply_markup(
            reply_markup=weight_kb(exercise, ex_state, draft)
        )
    await callback.answer(f"{draft:g} кг")


@router.message(WorkoutSG.custom_weight)
async def custom_weight(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        weight = float((message.text or "").replace(",", "."))
        if weight < 0 or weight > 500:
            raise ValueError
    except ValueError:
        await message.answer("Число кг, например 40.")
        return

    await state.update_data(draft_weight=weight)
    data = await state.get_data()
    exercise_id = data["exercise_id"]

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)
        last_reps = ex_state.last_reps if ex_state else None

    await state.set_state(WorkoutSG.reps)
    await message.answer(
        f"Вес: {weight:g} кг\nСколько повторений?",
        reply_markup=reps_kb(exercise.target_reps_min, exercise.target_reps_max, last_reps),
    )


@router.callback_query(WorkoutSG.reps, F.data.startswith("wo:r:"))
async def pick_reps(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    reps = int(callback.data.split(":")[-1])
    await state.update_data(draft_reps=reps)
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, data["exercise_id"])
        last_sets = ex_state.last_sets if ex_state else None

    await state.set_state(WorkoutSG.sets)
    reps_label = "отказ" if reps == 0 else str(reps)
    await callback.message.edit_text(
        f"{exercise.name}\n{reps_label} × {data['draft_weight']:g} кг\nСколько подходов?",
        reply_markup=sets_kb(exercise.target_sets, last_sets),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.sets, F.data.startswith("wo:s:"))
async def pick_sets(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None:
        return
    sets_count = int(callback.data.split(":")[-1])
    await state.update_data(draft_sets=sets_count)
    data = await state.get_data()
    reps = data["draft_reps"]
    block = format_block(reps if reps else 0, float(data["draft_weight"]), sets_count)
    if reps == 0:
        block = f"отказ×{data['draft_weight']:g}×{sets_count}"

    await state.set_state(WorkoutSG.difficulty)
    await callback.message.edit_text(
        f"Лог: {block}\nКак прошло?",
        reply_markup=difficulty_kb(),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.difficulty, F.data.startswith("wo:d:"))
async def pick_difficulty(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    difficulty = Difficulty(callback.data.split(":")[-1])
    data = await state.get_data()
    raw_reps = int(data["draft_reps"])
    weight = float(data["draft_weight"])
    sets_count = int(data["draft_sets"])
    reps = raw_reps
    if raw_reps == 0:
        # "Отказ" on reps keyboard → log as failure-style set
        reps = 0
        if difficulty != Difficulty.failure:
            difficulty = Difficulty.failure

    volume = max(reps, 1) * weight * sets_count

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        ws = await session.get(WorkoutSession, data["session_id"])
        if not ws or not exercise:
            await callback.answer("Сессия потеряна, начни заново", show_alert=True)
            await state.clear()
            return

        session.add(
            SessionSet(
                session_id=ws.id,
                exercise_id=exercise.id,
                exercise_name=exercise.name,
                reps=reps,
                weight=weight,
                sets_count=sets_count,
                difficulty=difficulty,
                volume=volume,
            )
        )

        ex_state = await _get_state(session, user.id, exercise.id)
        if ex_state is None:
            ex_state = UserExerciseState(user_id=user.id, exercise_id=exercise.id)
            session.add(ex_state)

        suggested, note = suggest_next_weight(
            phase=user.phase,
            exercise=exercise,
            weight=weight,
            reps=reps,
            sets_count=sets_count,
            difficulty=difficulty,
            state=ex_state,
        )
        if difficulty in (Difficulty.hard, Difficulty.failure):
            ex_state.hard_streak = (ex_state.hard_streak or 0) + 1
        else:
            ex_state.hard_streak = 0

        ex_state.working_weight = weight
        ex_state.suggested_weight = suggested
        ex_state.last_reps = reps if reps > 0 else None
        ex_state.last_sets = sets_count
        ex_state.last_difficulty = difficulty

        await session.commit()

        result = await session.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == ws.id)
            .options(selectinload(WorkoutSession.sets))
        )
        ws = result.scalar_one()
        result = await session.execute(
            select(WorkoutTemplate)
            .where(WorkoutTemplate.id == ws.template_id)
            .options(selectinload(WorkoutTemplate.exercises))
        )
        template = result.scalar_one()
        done = await _done_exercise_ids(ws)

        block = format_block(reps, weight, sets_count)
        summary = (
            f"Записал: {exercise.name}\n"
            f"{user.short_code} {block} {DIFFICULTY_LABELS[difficulty]}\n"
            f"Следующий вес: {suggested:g} кг ({note})"
        )

        await state.set_state(WorkoutSG.pick_exercise)
        await state.update_data(
            exercise_id=None,
            draft_weight=None,
            draft_reps=None,
            draft_sets=None,
        )
        await callback.message.edit_text(
            summary + "\n\nСледующее упражнение:",
            reply_markup=workout_exercise_kb(template.exercises, done),
        )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "wo:back")
async def workout_back(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    current = await state.get_state()
    data = await state.get_data()

    if current == WorkoutSG.difficulty.state:
        await state.set_state(WorkoutSG.sets)
        async with SessionLocal() as session:
            exercise = await session.get(TemplateExercise, data["exercise_id"])
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = await _get_state(session, user.id, data["exercise_id"])
            last_sets = ex_state.last_sets if ex_state else None
        await callback.message.edit_text(
            "Сколько подходов?",
            reply_markup=sets_kb(exercise.target_sets, last_sets),
        )
    elif current == WorkoutSG.sets.state:
        await state.set_state(WorkoutSG.reps)
        async with SessionLocal() as session:
            exercise = await session.get(TemplateExercise, data["exercise_id"])
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = await _get_state(session, user.id, data["exercise_id"])
            last_reps = ex_state.last_reps if ex_state else None
        await callback.message.edit_text(
            "Сколько повторений?",
            reply_markup=reps_kb(exercise.target_reps_min, exercise.target_reps_max, last_reps),
        )
    elif current in {WorkoutSG.reps.state, WorkoutSG.weight.state, WorkoutSG.custom_weight.state}:
        await state.set_state(WorkoutSG.pick_exercise)
        async with SessionLocal() as session:
            result = await session.execute(
                select(WorkoutSession)
                .where(WorkoutSession.id == data["session_id"])
                .options(selectinload(WorkoutSession.sets))
            )
            ws = result.scalar_one_or_none()
            result = await session.execute(
                select(WorkoutTemplate)
                .where(WorkoutTemplate.id == data["template_id"])
                .options(selectinload(WorkoutTemplate.exercises))
            )
            template = result.scalar_one()
            done = await _done_exercise_ids(ws) if ws else set()
        await callback.message.edit_text(
            "Выбери упражнение:",
            reply_markup=workout_exercise_kb(template.exercises, done),
        )
    await callback.answer()


@router.callback_query(F.data == "wo:finish")
async def finish_workout(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    data = await state.get_data()
    session_id = data.get("session_id")
    if not session_id:
        await callback.answer("Нет активной сессии")
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        result = await session.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == session_id)
            .options(
                selectinload(WorkoutSession.sets),
                selectinload(WorkoutSession.user),
                selectinload(WorkoutSession.template),
            )
        )
        ws = result.scalar_one_or_none()
        if not ws:
            await callback.answer("Сессия не найдена", show_alert=True)
            await state.clear()
            return
        ws.status = SessionStatus.finished
        ws.finished_at = datetime.now(ZoneInfo(get_settings().timezone))
        await session.commit()
        text = await build_personal_retrospective(session, ws)
        is_admin = user.is_admin

    await state.clear()
    await callback.message.edit_text(text)
    await callback.message.answer("Готово. Сводка уйдёт в общий чат вечером.", reply_markup=main_menu(is_admin))
    await callback.answer()


@router.callback_query(F.data == "wo:cancel")
async def cancel_workout(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    data = await state.get_data()
    session_id = data.get("session_id")
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        if session_id:
            result = await session.execute(
                select(WorkoutSession)
                .where(WorkoutSession.id == session_id)
                .options(selectinload(WorkoutSession.sets))
            )
            ws = result.scalar_one_or_none()
            if ws and ws.status == SessionStatus.active and not ws.sets:
                await session.delete(ws)
                await session.commit()
            elif ws and ws.status == SessionStatus.active:
                ws.status = SessionStatus.skipped
                await session.commit()
        is_admin = user.is_admin
    await state.clear()
    await callback.message.edit_text("Тренировка отменена.")
    await callback.message.answer("Меню:", reply_markup=main_menu(is_admin))
    await callback.answer()
