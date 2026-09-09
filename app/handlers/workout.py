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
    after_set_kb,
    difficulty_kb,
    main_menu,
    reps_kb,
    weight_kb,
    workout_exercise_kb,
)
from app.services.archive import upsert_archive
from app.services.history import build_smart_presets
from app.services.progression import (
    DIFFICULTY_LABELS,
    format_logged_parts,
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


def _unique_set_count(parts: list[dict]) -> int:
    return len({int(p["set_number"]) for p in parts}) if parts else 0


def _set_prompt(data: dict) -> str:
    set_no = int(data.get("current_set") or 1)
    drop = int(data.get("drop_index") or 0)
    if drop:
        return f"Подход {set_no}, дроп {drop}"
    return f"Подход {set_no}"


def _weight_kb_from_data(exercise, ex_state, data: dict, draft: float | None = None):
    return weight_kb(
        exercise,
        ex_state,
        draft if draft is not None else data.get("draft_weight"),
        weight_options=data.get("smart_weight_options"),
        can_repeat_last=bool(data.get("can_repeat_last")),
    )


def _reps_kb_from_data(exercise, data: dict, last_reps: int | None = None):
    preferred = data.get("smart_default_reps")
    if preferred is None:
        preferred = last_reps
    return reps_kb(
        exercise.target_reps_min,
        exercise.target_reps_max,
        preferred,
        reps_options=data.get("smart_reps_options"),
    )


async def _load_presets_into_state(
    state: FSMContext,
    *,
    user_id: int,
    exercise_id: int,
    exercise_name: str,
    suggested: float | None,
    working: float | None,
    set_number: int,
    drop_index: int,
    fallback_weight: float | None = None,
) -> dict:
    async with SessionLocal() as session:
        presets = await build_smart_presets(
            session,
            user_id,
            exercise_id=exercise_id,
            exercise_name=exercise_name,
            suggested_weight=suggested,
            working_weight=working,
            set_number=set_number,
            drop_index=drop_index,
        )
    draft = presets.default_weight
    if draft is None:
        draft = fallback_weight if fallback_weight is not None else (suggested or working or 20.0)
    weight_options = list(presets.weight_options)
    if drop_index > 0:
        for w in presets.last_drop_weights:
            if w not in weight_options:
                weight_options.insert(0, w)
        if draft not in weight_options:
            weight_options.insert(0, draft)
        weight_options = weight_options[:6]
    await state.update_data(
        draft_weight=draft,
        smart_weight_options=weight_options,
        smart_reps_options=presets.reps_options,
        smart_default_reps=presets.default_reps,
        last_parts=presets.last_parts,
        last_pattern_text=presets.last_pattern_text,
        last_pattern_date=presets.last_date,
        can_repeat_last=bool(presets.last_parts) and drop_index == 0 and set_number == 1,
    )
    return {
        "draft": draft,
        "hint": (
            f"\nПрошлый раз ({presets.last_date}): {presets.last_pattern_text}"
            + (f" {presets.last_difficulty}" if presets.last_difficulty else "")
            if presets.last_pattern_text
            else ""
        ),
        "presets": presets,
    }


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
        working = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight
            working = ex_state.working_weight
        data_now = await state.get_data()
        next_set = 1
        sid = data_now.get("session_id")
        if sid:
            result = await session.execute(
                select(WorkoutSession)
                .where(WorkoutSession.id == sid)
                .options(selectinload(WorkoutSession.sets))
            )
            ws = result.scalar_one_or_none()
            if ws:
                existing_nums = [
                    s.set_number
                    for s in ws.sets
                    if s.exercise_id == exercise_id and (s.set_number or 0) > 0
                ]
                if existing_nums:
                    next_set = max(existing_nums) + 1
        user_id = user.id
        exercise_name = exercise.name
        target = f"{exercise.target_sets}×{exercise.target_reps_min}-{exercise.target_reps_max}"

    await state.update_data(
        exercise_id=exercise_id,
        draft_reps=None,
        logged=[],
        current_set=next_set,
        drop_index=0,
    )
    loaded = await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=exercise_id,
        exercise_name=exercise_name,
        suggested=suggested,
        working=working,
        set_number=next_set,
        drop_index=0,
    )
    await state.set_state(WorkoutSG.weight)
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)
        kb = _weight_kb_from_data(exercise, ex_state, data)

    await callback.message.edit_text(
        f"{exercise.name}\nЦель: {target}{loaded['hint']}\n\n"
        f"{_set_prompt(data)}\nВыбери вес:",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WorkoutSG.weight, F.data == "wo:repeat_last")
async def repeat_last(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    data = await state.get_data()
    parts = list(data.get("last_parts") or [])
    if not parts:
        await callback.answer("Нет прошлой записи", show_alert=True)
        return
    if data.get("logged"):
        await callback.answer("Уже есть подходы — сначала отмени", show_alert=True)
        return

    await state.update_data(
        logged=parts,
        current_set=max(int(p["set_number"]) for p in parts),
        drop_index=0,
        can_repeat_last=False,
        draft_weight=float(parts[-1]["weight"]),
        draft_reps=int(parts[-1]["reps"]),
    )
    await state.set_state(WorkoutSG.after_set)

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        target_sets = exercise.target_sets if exercise else 3
        name = exercise.name if exercise else "Упражнение"

    date_hint = data.get("last_pattern_date") or ""
    await callback.message.edit_text(
        f"{name}\nКак в прошлый раз{f' ({date_hint})' if date_hint else ''}:\n"
        f"{format_logged_parts(parts)}\n\nЧто дальше?",
        reply_markup=after_set_kb(target_sets, _unique_set_count(parts)),
    )
    await callback.answer("Подставил прошлый раз")


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
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, exercise_id)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, exercise_id)

        if action == "=":
            await state.set_state(WorkoutSG.reps)
            last_reps = ex_state.last_reps if ex_state else None
            await callback.message.edit_text(
                f"{exercise.name}\n{_set_prompt(data)}\nВес: {draft:g} кг\nСколько повторений?",
                reply_markup=_reps_kb_from_data(exercise, data, last_reps),
            )
            await callback.answer()
            return

        await callback.message.edit_reply_markup(
            reply_markup=_weight_kb_from_data(exercise, ex_state, data, draft)
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
        f"{_set_prompt(data)}\nВес: {weight:g} кг\nСколько повторений?",
        reply_markup=_reps_kb_from_data(exercise, data, last_reps),
    )


@router.callback_query(WorkoutSG.reps, F.data == "wo:r:custom")
async def custom_reps_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(WorkoutSG.custom_reps)
    await callback.message.answer("Введи число повторений, например 11 или 7")
    await callback.answer()


@router.message(WorkoutSG.custom_reps)
async def custom_reps(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        reps = int((message.text or "").strip())
        if reps < 0 or reps > 200:
            raise ValueError
    except ValueError:
        await message.answer("Целое число повторений, например 11.")
        return
    await _append_reps_and_continue(message, state, reps)


@router.callback_query(WorkoutSG.reps, F.data.regexp(r"^wo:r:\d+$"))
async def pick_reps(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    reps = int(callback.data.split(":")[-1])
    await _append_reps_and_continue(callback.message, state, reps, edit=True)
    await callback.answer()


async def _append_reps_and_continue(
    target: Message,
    state: FSMContext,
    reps: int,
    *,
    edit: bool = False,
) -> None:
    data = await state.get_data()
    logged = list(data.get("logged") or [])
    logged.append(
        {
            "set_number": int(data.get("current_set") or 1),
            "drop_index": int(data.get("drop_index") or 0),
            "weight": float(data["draft_weight"]),
            "reps": reps,
        }
    )
    await state.update_data(logged=logged, draft_reps=reps)
    await state.set_state(WorkoutSG.after_set)

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        target_sets = exercise.target_sets if exercise else 3
        name = exercise.name if exercise else "Упражнение"

    text = f"{name}\n{format_logged_parts(logged)}\n\nЧто дальше?"
    kb = after_set_kb(target_sets, _unique_set_count(logged))
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(WorkoutSG.after_set, F.data == "wo:more")
async def next_set(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    data = await state.get_data()
    logged = data.get("logged") or []
    next_no = _unique_set_count(logged) + 1
    last_main = next(
        (p for p in reversed(logged) if int(p["drop_index"]) == 0),
        logged[-1] if logged else None,
    )
    fallback = float(last_main["weight"]) if last_main else float(data.get("draft_weight") or 20)

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, data["exercise_id"])
        suggested = None
        working = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight
            working = ex_state.working_weight
        user_id = user.id
        name = exercise.name if exercise else "Упражнение"
        exercise_name = name

    await state.update_data(current_set=next_no, drop_index=0)
    await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=data["exercise_id"],
        exercise_name=exercise_name,
        suggested=suggested,
        working=working,
        set_number=next_no,
        drop_index=0,
        fallback_weight=fallback,
    )
    await state.set_state(WorkoutSG.weight)
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, data["exercise_id"])
        kb = _weight_kb_from_data(exercise, ex_state, data)

    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}\n\n{_set_prompt(data)}\nВыбери вес:",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WorkoutSG.after_set, F.data == "wo:drop")
async def drop_set(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    data = await state.get_data()
    logged = data.get("logged") or []
    last = logged[-1] if logged else None
    drop = int(data.get("drop_index") or 0) + 1
    set_no = int(data.get("current_set") or 1)
    step = 2.5
    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, data["exercise_id"])
        if exercise:
            step = exercise.weight_step or 2.5
        name = exercise.name if exercise else "Упражнение"
        user_id = user.id
        suggested = None
        working = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight
            working = ex_state.working_weight
        last_w = float(last["weight"]) if last else float(data.get("draft_weight") or 20)
        fallback = max(0.0, last_w - step)

    await state.update_data(drop_index=drop)
    await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=data["exercise_id"],
        exercise_name=name,
        suggested=suggested,
        working=working,
        set_number=set_no,
        drop_index=drop,
        fallback_weight=fallback,
    )
    await state.set_state(WorkoutSG.weight)
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = await _get_state(session, user.id, data["exercise_id"])
        kb = _weight_kb_from_data(exercise, ex_state, data)

    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}\n\n{_set_prompt(data)}\n"
        f"Дроп: выбери вес (по умолчанию −{step:g}):",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WorkoutSG.after_set, F.data == "wo:undo")
async def undo_segment(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    data = await state.get_data()
    logged = list(data.get("logged") or [])
    if not logged:
        await callback.answer("Нечего отменять")
        return
    last = logged.pop()
    await state.update_data(
        logged=logged,
        current_set=int(last["set_number"]),
        drop_index=int(last["drop_index"]),
        draft_weight=float(last["weight"]),
        can_repeat_last=bool(data.get("last_parts")) and not logged and int(last["set_number"]) == 1 and int(last["drop_index"]) == 0,
    )
    if not logged:
        await state.set_state(WorkoutSG.weight)
        async with SessionLocal() as session:
            exercise = await session.get(TemplateExercise, data["exercise_id"])
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = await _get_state(session, user.id, data["exercise_id"])
            data = await state.get_data()
            kb = _weight_kb_from_data(exercise, ex_state, data, float(last["weight"]))
        await callback.message.edit_text(
            f"{exercise.name}\n{_set_prompt(data)}\nВыбери вес:",
            reply_markup=kb,
        )
        await callback.answer("Отменил")
        return

    await state.set_state(WorkoutSG.after_set)
    async with SessionLocal() as session:
        exercise = await session.get(TemplateExercise, data["exercise_id"])
        target_sets = exercise.target_sets if exercise else 3
        name = exercise.name if exercise else "Упражнение"
    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}\n\nЧто дальше?",
        reply_markup=after_set_kb(target_sets, _unique_set_count(logged)),
    )
    await callback.answer("Отменил")


@router.callback_query(WorkoutSG.after_set, F.data == "wo:exdone")
async def finish_exercise_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    data = await state.get_data()
    logged = data.get("logged") or []
    if not logged:
        await callback.answer("Сначала запиши хотя бы один подход", show_alert=True)
        return
    await state.set_state(WorkoutSG.difficulty)
    await callback.message.edit_text(
        f"Лог: {format_logged_parts(logged)}\nКак в целом прошло упражнение?",
        reply_markup=difficulty_kb(),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.difficulty, F.data.startswith("wo:d:"))
async def pick_difficulty(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    difficulty = Difficulty(callback.data.split(":")[-1])
    data = await state.get_data()
    logged = list(data.get("logged") or [])
    if not logged:
        await callback.answer("Пустой лог", show_alert=True)
        return

    mains = [p for p in logged if int(p["drop_index"]) == 0]
    work = mains[-1] if mains else logged[-1]
    weight = float(work["weight"])
    reps = int(work["reps"])
    sets_count = _unique_set_count(logged)

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

        for part in logged:
            r = int(part["reps"])
            w = float(part["weight"])
            session.add(
                SessionSet(
                    session_id=ws.id,
                    exercise_id=exercise.id,
                    exercise_name=exercise.name,
                    reps=r,
                    weight=w,
                    sets_count=1,
                    difficulty=difficulty,
                    volume=max(r, 1) * w,
                    set_number=int(part["set_number"]),
                    drop_index=int(part["drop_index"]),
                )
            )
        await upsert_archive(session, exercise.name, overwrite_targets=False)

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

        summary = (
            f"Записал: {exercise.name}\n"
            f"{user.short_code} {format_logged_parts(logged)} {DIFFICULTY_LABELS[difficulty]}\n"
            f"Следующий вес: {suggested:g} кг ({note})"
        )

        await state.set_state(WorkoutSG.pick_exercise)
        await state.update_data(
            exercise_id=None,
            draft_weight=None,
            draft_reps=None,
            logged=[],
            current_set=1,
            drop_index=0,
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
        await state.set_state(WorkoutSG.after_set)
        async with SessionLocal() as session:
            exercise = await session.get(TemplateExercise, data["exercise_id"])
            target_sets = exercise.target_sets if exercise else 3
            name = exercise.name if exercise else "Упражнение"
        logged = data.get("logged") or []
        await callback.message.edit_text(
            f"{name}\n{format_logged_parts(logged)}\n\nЧто дальше?",
            reply_markup=after_set_kb(target_sets, _unique_set_count(logged)),
        )
    elif current in {WorkoutSG.reps.state, WorkoutSG.custom_reps.state}:
        await state.set_state(WorkoutSG.weight)
        async with SessionLocal() as session:
            exercise = await session.get(TemplateExercise, data["exercise_id"])
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = await _get_state(session, user.id, data["exercise_id"])
            kb = _weight_kb_from_data(
                exercise, ex_state, data, float(data.get("draft_weight") or 20)
            )
        await callback.message.edit_text(
            f"{exercise.name}\n{_set_prompt(data)}\nВыбери вес:",
            reply_markup=kb,
        )
    elif current in {WorkoutSG.weight.state, WorkoutSG.custom_weight.state, WorkoutSG.after_set.state}:
        logged = data.get("logged") or []
        if logged and current != WorkoutSG.after_set.state:
            await state.set_state(WorkoutSG.after_set)
            async with SessionLocal() as session:
                exercise = await session.get(TemplateExercise, data["exercise_id"])
                target_sets = exercise.target_sets if exercise else 3
                name = exercise.name if exercise else "Упражнение"
            await callback.message.edit_text(
                f"{name}\n{format_logged_parts(logged)}\n\nЧто дальше?",
                reply_markup=after_set_kb(target_sets, _unique_set_count(logged)),
            )
            await callback.answer()
            return
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

