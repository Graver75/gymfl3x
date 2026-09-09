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
    ExerciseArchive,
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
    workout_archive_kb,
    workout_exercise_kb,
    workout_mode_kb,
    workout_templates_kb,
)
from app.services.archive import list_archive, upsert_archive
from app.services.history import (
    build_smart_presets,
    compare_line_for_exercise,
    exercise_personal_records,
    format_pr_line,
)
from app.services.progression import (
    DIFFICULTY_LABELS,
    format_logged_parts,
    suggest_next_weight,
)
from app.services.recap import build_personal_retrospective
from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday
from app.services.users import get_or_create_user
from app.services.workout_ui import (
    load_template_with_exercises,
    next_exercise_id,
    resolve_exercise,
    rest_line,
    session_map_text,
    touch_action_iso,
    weight_hints_for_user,
)
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


async def _show_session_map(
    message_or_cb_message,
    state: FSMContext,
    *,
    user_id: int,
    template: WorkoutTemplate,
    ws: WorkoutSession,
    prefix: str = "",
    edit: bool = True,
) -> None:
    done = await _done_exercise_ids(ws)
    async with SessionLocal() as session:
        hints = await weight_hints_for_user(session, user_id, template.exercises)
    nxt = next_exercise_id(template.exercises, done)
    text = session_map_text(template.name, template.exercises, done)
    if prefix:
        text = f"{prefix}\n\n{text}"
    kb = workout_exercise_kb(template.exercises, done, weight_hints=hints, next_id=nxt)
    await state.set_state(WorkoutSG.pick_exercise)
    await state.update_data(session_id=ws.id, template_id=template.id)
    if edit:
        await message_or_cb_message.edit_text(text, reply_markup=kb)
    else:
        await message_or_cb_message.answer(text, reply_markup=kb)


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
        if existing and existing.template_id:
            template = await load_template_with_exercises(session, existing.template_id)
            if template:
                await _show_session_map(
                    message,
                    state,
                    user_id=db_user.id,
                    template=template,
                    ws=existing,
                    prefix="Продолжаем тренировку:",
                    edit=False,
                )
                return
        if existing and not existing.template_id:
            await state.set_state(WorkoutSG.pick_exercise)
            await state.update_data(session_id=existing.id, template_id=None, free_session=True)
            await message.answer(
                "Свободная сессия активна. Добавь упражнение из архива или закончи.",
                reply_markup=workout_exercise_kb([], set()),
            )
            # Offer archive add via separate buttons on a simple kb - patch below
            items = await list_archive(session)
            await message.answer(
                "Выбери упражнение из архива:",
                reply_markup=workout_archive_kb(items),
            )
            await state.set_state(WorkoutSG.pick_archive)
            return

        today_tpl = await get_template_for_weekday(session, weekday)

    await state.set_state(WorkoutSG.pick_mode)
    await state.update_data(session_date=today.isoformat())
    hint = (
        f"Сегодня по графику: {today_tpl.name}."
        if today_tpl
        else f"Сегодня {WEEKDAY_NAMES[weekday]} — в графике выходной."
    )
    await message.answer(
        f"{hint}\nКак стартуем?",
        reply_markup=workout_mode_kb(has_today=bool(today_tpl)),
    )


@router.callback_query(WorkoutSG.pick_mode, F.data == "wo:mode:today")
async def workout_mode_today(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        template = await get_template_for_weekday(session, weekday)
        if not template:
            await callback.answer("Сегодня нет дня в графике", show_alert=True)
            return
        ws = WorkoutSession(
            user_id=user.id,
            template_id=template.id,
            session_date=today,
            status=SessionStatus.active,
        )
        session.add(ws)
        await session.commit()
        await session.refresh(ws)
        template = await load_template_with_exercises(session, template.id)
        await _show_session_map(
            callback.message,
            state,
            user_id=user.id,
            template=template,
            ws=ws,
            prefix=f"Старт: {template.name} (#{template.hashtag})",
        )
    await callback.answer()


@router.callback_query(F.data == "wo:mode:free")
@router.callback_query(WorkoutSG.pick_mode, F.data == "wo:mode:free")
async def workout_mode_free(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    async with SessionLocal() as session:
        templates = list(
            (
                await session.execute(select(WorkoutTemplate).order_by(WorkoutTemplate.name))
            ).scalars().all()
        )
    await state.set_state(WorkoutSG.pick_template)
    await callback.message.edit_text(
        "Свободная тренировка — выбери шаблон или упражнение из архива:",
        reply_markup=workout_templates_kb(templates),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.pick_mode, F.data == "wo:mode:back")
@router.callback_query(WorkoutSG.pick_template, F.data == "wo:mode:back")
async def workout_mode_back(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()
    async with SessionLocal() as session:
        today_tpl = await get_template_for_weekday(session, weekday)
    await state.set_state(WorkoutSG.pick_mode)
    hint = (
        f"Сегодня по графику: {today_tpl.name}."
        if today_tpl
        else f"Сегодня {WEEKDAY_NAMES[weekday]} — в графике выходной."
    )
    await callback.message.edit_text(
        f"{hint}\nКак стартуем?",
        reply_markup=workout_mode_kb(has_today=bool(today_tpl)),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.pick_template, F.data.startswith("wo:tpl:"))
async def workout_pick_template(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    template_id = int(callback.data.split(":")[-1])
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        template = await load_template_with_exercises(session, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        ws = WorkoutSession(
            user_id=user.id,
            template_id=template.id,
            session_date=today,
            status=SessionStatus.active,
            notes="free",
        )
        session.add(ws)
        await session.commit()
        await session.refresh(ws)
        await _show_session_map(
            callback.message,
            state,
            user_id=user.id,
            template=template,
            ws=ws,
            prefix=f"Свободная: {template.name}",
        )
    await callback.answer()


@router.callback_query(F.data == "wo:arch")
@router.callback_query(WorkoutSG.pick_template, F.data == "wo:arch")
@router.callback_query(F.data.startswith("wo:archp:"))
async def workout_arch_list(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    page = 0
    if callback.data and callback.data.startswith("wo:archp:"):
        page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        items = await list_archive(session)
    await state.set_state(WorkoutSG.pick_archive)
    await callback.message.edit_text(
        "Упражнение из архива:",
        reply_markup=workout_archive_kb(items, page=page),
    )
    await callback.answer()


@router.callback_query(WorkoutSG.pick_archive, F.data.startswith("wo:archpick:"))
async def workout_arch_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    arch_id = int(callback.data.split(":")[-1])
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        item = await session.get(ExerciseArchive, arch_id)
        if not item:
            await callback.answer("Нет в архиве", show_alert=True)
            return
        data = await state.get_data()
        sid = data.get("session_id")
        if sid:
            ws = await session.get(WorkoutSession, sid)
        else:
            ws = WorkoutSession(
                user_id=user.id,
                template_id=None,
                session_date=today,
                status=SessionStatus.active,
                notes="free-archive",
            )
            session.add(ws)
            await session.commit()
            await session.refresh(ws)
        free = {
            "name": item.name,
            "target_sets": item.target_sets,
            "target_reps_min": item.target_reps_min,
            "target_reps_max": item.target_reps_max,
            "weight_step": item.weight_step,
        }
        await state.update_data(
            session_id=ws.id,
            template_id=ws.template_id,
            exercise_id=None,
            free_exercise=free,
            free_session=True,
            draft_reps=None,
            logged=[],
            current_set=1,
            drop_index=0,
            last_action_at=touch_action_iso(),
        )
        user_id = user.id
        exercise_name = item.name
        suggested = None
        working = None

    loaded = await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=None,
        exercise_name=exercise_name,
        suggested=suggested,
        working=working,
        set_number=1,
        drop_index=0,
    )
    await state.set_state(WorkoutSG.weight)
    data = await state.get_data()
    async with SessionLocal() as session:
        exercise = await resolve_exercise(session, data)
        kb = _weight_kb_from_data(exercise, None, data)
    target = f"{free['target_sets']}×{free['target_reps_min']}-{free['target_reps_max']}"
    await callback.message.edit_text(
        f"{exercise_name}\nЦель: {target}{loaded['hint']}"
        f"{rest_line(data)}\n\n{_set_prompt(data)}\nВыбери вес:",
        reply_markup=kb,
    )
    await callback.answer()


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
        note = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight
            working = ex_state.working_weight
            note = ex_state.note
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
        pr = await exercise_personal_records(
            session, user_id, exercise_id=exercise_id, exercise_name=exercise_name
        )
        compare = await compare_line_for_exercise(
            session, user_id, exercise_id=exercise_id, exercise_name=exercise_name
        )

    await state.update_data(
        exercise_id=exercise_id,
        free_exercise=None,
        draft_reps=None,
        logged=[],
        current_set=next_set,
        drop_index=0,
        last_action_at=touch_action_iso(),
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

    extra = ""
    pr_line = format_pr_line(pr)
    if pr_line:
        extra += f"\n{pr_line}"
    if compare:
        extra += f"\n{compare}"
    if note:
        extra += f"\nЗаметка: {note}"
    await callback.message.edit_text(
        f"{exercise.name}\nЦель: {target}{loaded['hint']}{extra}"
        f"{rest_line(data)}\n\n{_set_prompt(data)}\nВыбери вес:",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WorkoutSG.difficulty, F.data == "wo:note")
async def note_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(WorkoutSG.note)
    await callback.message.answer(
        "Короткая заметка к упражнению (хват, боль, нюанс).\n"
        "Пришли текст или «-» чтобы очистить."
    )
    await callback.answer()


@router.message(WorkoutSG.note)
async def note_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    data = await state.get_data()
    exercise_id = data.get("exercise_id")
    note_val = None if text in {"-", "—", ""} else text[:200]
    if exercise_id:
        async with SessionLocal() as session:
            user = await get_or_create_user(
                session, message.from_user.id, message.from_user.full_name or "Athlete"
            )
            ex_state = await _get_state(session, user.id, exercise_id)
            if ex_state is None:
                ex_state = UserExerciseState(user_id=user.id, exercise_id=exercise_id)
                session.add(ex_state)
            ex_state.note = note_val
            await session.commit()
    logged = data.get("logged") or []
    note_line = f"\nЗаметка: {note_val}" if note_val else "\nЗаметка очищена"
    await state.set_state(WorkoutSG.difficulty)
    await message.answer(
        f"Лог: {format_logged_parts(logged)}{note_line}\nКак в целом прошло упражнение?",
        reply_markup=difficulty_kb(),
    )


@router.callback_query(WorkoutSG.weight, F.data.startswith("wo:w:"))
async def pick_weight(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None or callback.from_user is None:
        return
    parts = callback.data.split(":")
    action = parts[2]

    data = await state.get_data()
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

    await state.update_data(draft_weight=draft, last_action_at=touch_action_iso())
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
            ex_state = await _get_state(session, user.id, data["exercise_id"])

        if action == "=":
            await state.set_state(WorkoutSG.reps)
            last_reps = ex_state.last_reps if ex_state else None
            await callback.message.edit_text(
                f"{exercise.name}\n{_set_prompt(data)}\nВес: {draft:g} кг"
                f"{rest_line(data)}\nСколько повторений?",
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

    await state.update_data(draft_weight=weight, last_action_at=touch_action_iso())
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
            ex_state = await _get_state(session, user.id, data["exercise_id"])
        last_reps = ex_state.last_reps if ex_state else None

    await state.set_state(WorkoutSG.reps)
    await message.answer(
        f"{_set_prompt(data)}\nВес: {weight:g} кг{rest_line(data)}\nСколько повторений?",
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
    await state.update_data(
        logged=logged,
        draft_reps=reps,
        last_action_at=touch_action_iso(),
    )
    await state.set_state(WorkoutSG.after_set)
    data = await state.get_data()

    async with SessionLocal() as session:
        exercise = await resolve_exercise(session, data)
        target_sets = exercise.target_sets if exercise else 3
        name = exercise.name if exercise else "Упражнение"

    text = (
        f"{name}\n{format_logged_parts(logged)}"
        f"{rest_line(data)}\n\nЧто дальше?"
    )
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
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
            ex_state = await _get_state(session, user.id, data["exercise_id"])
        suggested = None
        working = None
        if ex_state:
            suggested = ex_state.suggested_weight or ex_state.working_weight
            working = ex_state.working_weight
        user_id = user.id
        name = exercise.name if exercise else "Упражнение"
        exercise_name = name

    await state.update_data(
        current_set=next_no, drop_index=0, last_action_at=touch_action_iso()
    )
    await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=data.get("exercise_id"),
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
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
            ex_state = await _get_state(session, user.id, data["exercise_id"])
        kb = _weight_kb_from_data(exercise, ex_state, data)

    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}\n\n{_set_prompt(data)}"
        f"{rest_line(data)}\nВыбери вес:",
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
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
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

    await state.update_data(drop_index=drop, last_action_at=touch_action_iso())
    await _load_presets_into_state(
        state,
        user_id=user_id,
        exercise_id=data.get("exercise_id"),
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
        exercise = await resolve_exercise(session, data)
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ex_state = None
        if data.get("exercise_id"):
            ex_state = await _get_state(session, user.id, data["exercise_id"])
        kb = _weight_kb_from_data(exercise, ex_state, data)

    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}\n\n{_set_prompt(data)}"
        f"{rest_line(data)}\nДроп: выбери вес (по умолчанию −{step:g}):",
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
        last_action_at=touch_action_iso(),
    )
    if not logged:
        await state.set_state(WorkoutSG.weight)
        async with SessionLocal() as session:
            exercise = await resolve_exercise(session, data)
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = None
            if data.get("exercise_id"):
                ex_state = await _get_state(session, user.id, data["exercise_id"])
            data = await state.get_data()
            kb = _weight_kb_from_data(exercise, ex_state, data, float(last["weight"]))
        await callback.message.edit_text(
            f"{exercise.name}\n{_set_prompt(data)}{rest_line(data)}\nВыбери вес:",
            reply_markup=kb,
        )
        await callback.answer("Отменил")
        return

    await state.set_state(WorkoutSG.after_set)
    async with SessionLocal() as session:
        exercise = await resolve_exercise(session, data)
        target_sets = exercise.target_sets if exercise else 3
        name = exercise.name if exercise else "Упражнение"
    data = await state.get_data()
    await callback.message.edit_text(
        f"{name}\n{format_logged_parts(logged)}{rest_line(data)}\n\nЧто дальше?",
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
        exercise = await resolve_exercise(session, data)
        ws = await session.get(WorkoutSession, data["session_id"])
        if not ws or not exercise:
            await callback.answer("Сессия потеряна, начни заново", show_alert=True)
            await state.clear()
            return

        ex_id = getattr(exercise, "id", None)
        for part in logged:
            r = int(part["reps"])
            w = float(part["weight"])
            session.add(
                SessionSet(
                    session_id=ws.id,
                    exercise_id=ex_id,
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

        suggested = weight
        note = "без автопрогрессии"
        if ex_id:
            ex_state = await _get_state(session, user.id, ex_id)
            if ex_state is None:
                ex_state = UserExerciseState(user_id=user.id, exercise_id=ex_id)
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
        template = None
        if ws.template_id:
            template = await load_template_with_exercises(session, ws.template_id)

        summary = (
            f"Записал: {exercise.name}\n"
            f"{user.short_code} {format_logged_parts(logged)} {DIFFICULTY_LABELS[difficulty]}\n"
            f"Следующий вес: {suggested:g} кг ({note})"
        )

        await state.update_data(
            exercise_id=None,
            free_exercise=None,
            draft_weight=None,
            draft_reps=None,
            logged=[],
            current_set=1,
            drop_index=0,
            last_action_at=touch_action_iso(),
        )

        if template:
            await _show_session_map(
                callback.message,
                state,
                user_id=user.id,
                template=template,
                ws=ws,
                prefix=summary,
            )
        else:
            items = await list_archive(session)
            await state.set_state(WorkoutSG.pick_archive)
            await state.update_data(session_id=ws.id, free_session=True)
            await callback.message.edit_text(
                summary + "\n\nДобавь ещё из архива или закончи тренировку.",
                reply_markup=workout_archive_kb(items),
            )
            # Also need finish on archive screen - add finish to archive kb when in session
    await callback.answer("Сохранено")


@router.callback_query(F.data == "wo:back")
async def workout_back(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    current = await state.get_state()
    data = await state.get_data()

    if current == WorkoutSG.note.state:
        await state.set_state(WorkoutSG.difficulty)
        logged = data.get("logged") or []
        await callback.message.edit_text(
            f"Лог: {format_logged_parts(logged)}\nКак в целом прошло упражнение?",
            reply_markup=difficulty_kb(),
        )
    elif current == WorkoutSG.difficulty.state:
        await state.set_state(WorkoutSG.after_set)
        async with SessionLocal() as session:
            exercise = await resolve_exercise(session, data)
            target_sets = exercise.target_sets if exercise else 3
            name = exercise.name if exercise else "Упражнение"
        logged = data.get("logged") or []
        await callback.message.edit_text(
            f"{name}\n{format_logged_parts(logged)}{rest_line(data)}\n\nЧто дальше?",
            reply_markup=after_set_kb(target_sets, _unique_set_count(logged)),
        )
    elif current in {WorkoutSG.reps.state, WorkoutSG.custom_reps.state}:
        await state.set_state(WorkoutSG.weight)
        async with SessionLocal() as session:
            exercise = await resolve_exercise(session, data)
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            ex_state = None
            if data.get("exercise_id"):
                ex_state = await _get_state(session, user.id, data["exercise_id"])
            kb = _weight_kb_from_data(
                exercise, ex_state, data, float(data.get("draft_weight") or 20)
            )
        await callback.message.edit_text(
            f"{exercise.name}\n{_set_prompt(data)}{rest_line(data)}\nВыбери вес:",
            reply_markup=kb,
        )
    elif current in {WorkoutSG.weight.state, WorkoutSG.custom_weight.state, WorkoutSG.after_set.state}:
        logged = data.get("logged") or []
        if logged and current != WorkoutSG.after_set.state:
            await state.set_state(WorkoutSG.after_set)
            async with SessionLocal() as session:
                exercise = await resolve_exercise(session, data)
                target_sets = exercise.target_sets if exercise else 3
                name = exercise.name if exercise else "Упражнение"
            await callback.message.edit_text(
                f"{name}\n{format_logged_parts(logged)}{rest_line(data)}\n\nЧто дальше?",
                reply_markup=after_set_kb(target_sets, _unique_set_count(logged)),
            )
            await callback.answer()
            return
        async with SessionLocal() as session:
            user = await get_or_create_user(
                session, callback.from_user.id, callback.from_user.full_name or "Athlete"
            )
            result = await session.execute(
                select(WorkoutSession)
                .where(WorkoutSession.id == data["session_id"])
                .options(selectinload(WorkoutSession.sets))
            )
            ws = result.scalar_one_or_none()
            if ws and ws.template_id:
                template = await load_template_with_exercises(session, ws.template_id)
                await _show_session_map(
                    callback.message,
                    state,
                    user_id=user.id,
                    template=template,
                    ws=ws,
                )
            else:
                items = await list_archive(session)
                await state.set_state(WorkoutSG.pick_archive)
                await callback.message.edit_text(
                    "Свободная сессия — упражнение из архива:",
                    reply_markup=workout_archive_kb(items),
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

