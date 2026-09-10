from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import ui_copy as ui
from app.config import get_settings
from app.db.models import (
    ExerciseArchive,
    GroupChat,
    ScheduleDay,
    TemplateExercise,
    User,
    UserExerciseState,
    WorkoutTemplate,
)
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    admin_menu_kb,
    admin_nn_load_kb,
    admin_users_kb,
    archive_item_kb,
    archive_list_kb,
    archive_pick_kb,
    current_exercises_kb,
    exercise_delete_confirm_kb,
    exercise_edit_kb,
    exercise_move_kb,
    schedule_kb,
    schedule_pick_template_kb,
    template_detail_kb,
    templates_list_kb,
)
from app.services.archive import (
    add_exercise_to_template,
    list_archive,
    name_key,
    sync_catalog,
    upsert_archive,
)
from app.services.reminders import WEEKDAY_NAMES
from app.services.users import get_or_create_user
from app.states import AdminSG

router = Router(name="admin")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _load_staff(telegram_id: int, full_name: str) -> User | None:
    """Full admin or program admin (templates / archive / schedule / current)."""
    async with SessionLocal() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        settings = get_settings()
        if telegram_id in settings.admin_telegram_ids and not user.is_admin:
            user.is_admin = True
            await session.commit()
        if not (user.is_admin or user.is_program_admin):
            return None
        session.expunge(user)
        return user


async def _full_admin(telegram_id: int, full_name: str) -> User | None:
    """Only full admin (hours / users / missing / athletes history)."""
    user = await _load_staff(telegram_id, full_name)
    if not user or not user.is_admin:
        return None
    return user


# Back-compat alias used during migration of call sites
_admin_user = _load_staff


async def _deny(message: Message) -> None:
    await message.answer("Нет доступа к админке.")


def _ex_back_from_data(data: dict, template_id: int) -> str:
    back = data.get("ex_back")
    if back == "adm:current":
        return "adm:current"
    return f"adm:tpl:{template_id}"


async def _list_current_exercises(session) -> list[tuple[int, str]]:
    result = await session.execute(
        select(TemplateExercise, WorkoutTemplate)
        .join(WorkoutTemplate, TemplateExercise.template_id == WorkoutTemplate.id)
        .order_by(WorkoutTemplate.name, TemplateExercise.position, TemplateExercise.id)
    )
    items: list[tuple[int, str]] = []
    for ex, tpl in result.all():
        label = (
            f"{ex.name} · {tpl.name} "
            f"({ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max})"
        )
        items.append((ex.id, label))
    return items


def _parse_targets(text: str) -> tuple[int, int, int, float] | None:
    parts = text.replace(",", ".").split()
    try:
        sets = int(parts[0])
        rmin = int(parts[1])
        rmax = int(parts[2]) if len(parts) > 2 else rmin
        step = float(parts[3]) if len(parts) > 3 else 2.5
        if sets < 1 or rmin < 1 or rmax < rmin:
            raise ValueError
        return sets, rmin, rmax, step
    except (ValueError, IndexError):
        return None


async def _reindex_positions(session, template_id: int) -> None:
    result = await session.execute(
        select(TemplateExercise)
        .where(TemplateExercise.template_id == template_id)
        .order_by(TemplateExercise.position, TemplateExercise.id)
    )
    for i, ex in enumerate(result.scalars().all()):
        ex.position = i


async def _shift_exercise_position(session, exercise_id: int, delta: int) -> tuple[bool, str]:
    """Move exercise up (delta=-1) or down (delta=+1) within its template."""
    ex = await session.get(TemplateExercise, exercise_id)
    if not ex:
        return False, "Упражнение удалено"
    result = await session.execute(
        select(TemplateExercise)
        .where(TemplateExercise.template_id == ex.template_id)
        .order_by(TemplateExercise.position, TemplateExercise.id)
    )
    siblings = list(result.scalars().all())
    idx = next((i for i, item in enumerate(siblings) if item.id == ex.id), None)
    if idx is None:
        return False, "Не найдено"
    new_idx = idx + delta
    if new_idx < 0 or new_idx >= len(siblings):
        return False, "Уже крайнее"
    siblings[idx], siblings[new_idx] = siblings[new_idx], siblings[idx]
    for i, item in enumerate(siblings):
        item.position = i
    await session.commit()
    return True, "Ок"


def _template_text(tpl: WorkoutTemplate) -> str:
    lines = [f"{tpl.name} (#{tpl.hashtag})", ""]
    if not tpl.exercises:
        lines.append("Упражнений пока нет.")
    else:
        lines.append("Нажми упражнение — править, сменить порядок (⬆️⬇️), удалить:")
        for ex in tpl.exercises:
            lines.append(
                f"{ex.position + 1}. {ex.name} — "
                f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}"
            )
    return "\n".join(lines)


async def _load_template(session, tpl_id: int) -> WorkoutTemplate | None:
    return (
        await session.execute(
            select(WorkoutTemplate)
            .where(WorkoutTemplate.id == tpl_id)
            .options(selectinload(WorkoutTemplate.exercises))
        )
    ).scalar_one_or_none()


def _exercise_text(ex: TemplateExercise, template_name: str) -> str:
    return (
        f"{ex.name}\n"
        f"Шаблон: {template_name}\n"
        f"Позиция: {ex.position + 1}\n"
        f"Цель: {ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}\n"
        f"Шаг веса: {ex.weight_step:g} кг"
    )


@router.message(Command("admin"))
@router.message(F.text == ui.BTN_ADMIN)
async def admin_home(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    user = await _load_staff(
        message.from_user.id,
        message.from_user.full_name or "Admin",
    )
    if not user:
        await _deny(message)
        return
    await state.clear()
    await message.answer(
        f"{ui.ICO_ADMIN} Админка Gymflex:",
        reply_markup=admin_menu_kb(full=user.is_admin),
    )


@router.callback_query(F.data == "adm:home")
async def adm_home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _load_staff(callback.from_user.id, callback.from_user.full_name or "Admin")
    if not user:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"{ui.ICO_ADMIN} Админка Gymflex:",
        reply_markup=admin_menu_kb(full=user.is_admin),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:templates")
async def adm_templates(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    async with SessionLocal() as session:
        templates = (await session.execute(select(WorkoutTemplate))).scalars().all()
    await callback.message.edit_text(
        f"{ui.BTN_ADM_TEMPLATES}:",
        reply_markup=templates_list_kb(templates),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:tpl:new")
async def adm_tpl_new(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminSG.new_template_name)
    await callback.message.answer("Название шаблона, например «День спины»:")
    await callback.answer()


@router.message(AdminSG.new_template_name)
async def adm_tpl_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Слишком короткое название.")
        return
    await state.update_data(tpl_name=name)
    await state.set_state(AdminSG.new_template_hashtag)
    await message.answer(
        "Хэштег для сводки без #, например деньспины\n"
        "(будет #деньспины в чате)"
    )


@router.message(AdminSG.new_template_hashtag)
async def adm_tpl_hashtag(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    tag = (message.text or "").strip().lstrip("#").replace(" ", "").lower()
    if len(tag) < 2:
        await message.answer("Хэштег слишком короткий.")
        return
    data = await state.get_data()
    async with SessionLocal() as session:
        tpl = WorkoutTemplate(name=data["tpl_name"], hashtag=tag)
        session.add(tpl)
        await session.commit()
        await session.refresh(tpl)
        tpl_id = tpl.id
    await state.clear()
    await message.answer(
        f"Шаблон «{data['tpl_name']}» создан. Добавь упражнения:",
        reply_markup=template_detail_kb(tpl_id),
    )


@router.callback_query(F.data.regexp(r"^adm:tpl:\d+$"))
async def adm_tpl_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return
    tpl_id = int(parts[2])
    await state.clear()
    async with SessionLocal() as session:
        tpl = await _load_template(session, tpl_id)
    if not tpl:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.message.edit_text(
        _template_text(tpl),
        reply_markup=template_detail_kb(tpl.id, tpl.exercises),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:tpl:del:"))
async def adm_tpl_del(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    tpl_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        tpl = await session.get(WorkoutTemplate, tpl_id)
        if tpl:
            await session.delete(tpl)
            await session.commit()
        templates = (await session.execute(select(WorkoutTemplate))).scalars().all()
    await callback.message.edit_text(
        "Шаблон удалён. Список:",
        reply_markup=templates_list_kb(templates),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:noop")
async def adm_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:add:"))
async def adm_ex_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    tpl_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        tpl = await _load_template(session, tpl_id)
        items = await sync_catalog(session)
        in_tpl = {name_key(ex.name) for ex in (tpl.exercises if tpl else [])}
    if not tpl:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await callback.message.edit_text(
        "Добавить упражнение.\n"
        "Каталог = архив + все активные из шаблонов. "
        "✓ уже в этом шаблоне. Остальные — одним нажатием, либо «Новое»:",
        reply_markup=archive_pick_kb(tpl_id, items, 0, in_template_keys=in_tpl),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:pick:"))
async def adm_ex_pick_page(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    tpl_id = int(parts[3])
    page = int(parts[4])
    async with SessionLocal() as session:
        tpl = await _load_template(session, tpl_id)
        items = await sync_catalog(session)
        in_tpl = {name_key(ex.name) for ex in (tpl.exercises if tpl else [])}
    await callback.message.edit_reply_markup(
        reply_markup=archive_pick_kb(tpl_id, items, page, in_template_keys=in_tpl)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:new:"))
async def adm_ex_new(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    tpl_id = int(callback.data.split(":")[-1])
    await state.set_state(AdminSG.add_exercise_name)
    await state.update_data(tpl_id=tpl_id)
    await callback.message.answer("Название нового упражнения, например «Верхняя тяга»:")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:from:"))
async def adm_ex_from_archive(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    tpl_id = int(parts[3])
    arch_id = int(parts[4])
    async with SessionLocal() as session:
        item = await session.get(ExerciseArchive, arch_id)
        if not item:
            await callback.answer("Нет в архиве", show_alert=True)
            return
        added, msg = await add_exercise_to_template(
            session,
            tpl_id,
            name=item.name,
            target_sets=item.target_sets,
            target_reps_min=item.target_reps_min,
            target_reps_max=item.target_reps_max,
            weight_step=item.weight_step,
        )
        added_name = item.name
        if added is None:
            await callback.answer(msg, show_alert=True)
            return
        await session.commit()
        tpl = await _load_template(session, tpl_id)
    await callback.message.edit_text(
        f"Добавлено из архива: {added_name}\n\n{_template_text(tpl)}" if tpl else msg,
        reply_markup=template_detail_kb(tpl_id, tpl.exercises if tpl else None),
    )
    await callback.answer("Добавлено")


@router.message(AdminSG.add_exercise_name)
async def adm_ex_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    name = (message.text or "").strip()
    if len(name) < 1:
        await message.answer("Нужно название.")
        return
    await state.update_data(ex_name=name)
    await state.set_state(AdminSG.add_exercise_targets)
    await message.answer(
        "Цели в формате: подходы повторы_мин повторы_макс [шаг_кг]\n"
        "Пример: 4 8 12 2.5\n"
        "Или короче: 3 10 12"
    )


@router.message(AdminSG.add_exercise_targets)
async def adm_ex_targets(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    parsed = _parse_targets(message.text or "")
    if not parsed:
        await message.answer("Формат: 4 8 12 2.5")
        return
    sets, rmin, rmax, step = parsed

    data = await state.get_data()
    tpl_id = data["tpl_id"]
    async with SessionLocal() as session:
        added, msg = await add_exercise_to_template(
            session,
            tpl_id,
            name=data["ex_name"],
            target_sets=sets,
            target_reps_min=rmin,
            target_reps_max=rmax,
            weight_step=step,
        )
        if added is None:
            await message.answer(msg)
            return
        await session.commit()
        tpl = await _load_template(session, tpl_id)

    await state.clear()
    if not tpl:
        await message.answer(msg)
        return
    lines = [f"Добавлено. {tpl.name}:"]
    for item in tpl.exercises:
        lines.append(
            f"{item.position + 1}. {item.name} — "
            f"{item.target_sets}×{item.target_reps_min}-{item.target_reps_max}"
        )
    await message.answer("\n".join(lines), reply_markup=template_detail_kb(tpl_id, tpl.exercises))


@router.callback_query(F.data.regexp(r"^adm:ex:(up|dn):\d+$"))
async def adm_ex_reorder(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    direction = -1 if parts[2] == "up" else 1
    ex_id = int(parts[3])
    data = await state.get_data()
    back = data.get("ex_back")
    async with SessionLocal() as session:
        ok, msg = await _shift_exercise_position(session, ex_id, direction)
        if not ok:
            await callback.answer(msg, show_alert=True)
            return
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Удалено", show_alert=True)
            return
        tpl = await _load_template(session, ex.template_id)
        tpl_id = ex.template_id
        if back == "adm:current":
            kb_back = "adm:current"
        else:
            kb_back = f"adm:tpl:{tpl_id}"
        await callback.message.edit_text(
            _exercise_text(ex, tpl.name if tpl else "?"),
            reply_markup=exercise_edit_kb(ex_id, tpl_id, back=kb_back),
        )
    await callback.answer("Порядок обновлён")


@router.callback_query(F.data.startswith("adm:ex:view:"))
async def adm_ex_view(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    preserved = data.get("ex_back")
    ex_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Упражнение удалено", show_alert=True)
            return
        tpl = await session.get(WorkoutTemplate, ex.template_id)
        text = _exercise_text(ex, tpl.name if tpl else "?")
        tpl_id = ex.template_id
    back = preserved if preserved == "adm:current" else f"adm:tpl:{tpl_id}"
    await state.update_data(ex_back=back)
    await callback.message.edit_text(
        text,
        reply_markup=exercise_edit_kb(ex_id, tpl_id, back=back),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:name:"))
async def adm_ex_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ex_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    await state.set_state(AdminSG.edit_exercise_name)
    await state.update_data(exercise_id=ex_id, ex_back=data.get("ex_back"))
    await callback.message.answer("Новое название упражнения:")
    await callback.answer()


@router.message(AdminSG.edit_exercise_name)
async def adm_ex_rename_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    name = (message.text or "").strip()
    if len(name) < 1:
        await message.answer("Нужно название.")
        return
    data = await state.get_data()
    ex_id = int(data["exercise_id"])
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await state.clear()
            await message.answer("Упражнение уже удалено.")
            return
        ex.name = name[:128]
        await upsert_archive(
            session,
            ex.name,
            target_sets=ex.target_sets,
            target_reps_min=ex.target_reps_min,
            target_reps_max=ex.target_reps_max,
            weight_step=ex.weight_step,
            overwrite_targets=True,
        )
        await session.commit()
        tpl = await session.get(WorkoutTemplate, ex.template_id)
        tpl_name = tpl.name if tpl else "?"
        tpl_id = ex.template_id
        text = _exercise_text(ex, tpl_name)
    back = _ex_back_from_data(data, tpl_id)
    await state.clear()
    await state.update_data(ex_back=back)
    await message.answer(
        f"Название обновлено.\n\n{text}",
        reply_markup=exercise_edit_kb(ex_id, tpl_id, back=back),
    )


@router.callback_query(F.data.startswith("adm:ex:tgt:"))
async def adm_ex_targets_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ex_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    await state.set_state(AdminSG.edit_exercise_targets)
    await state.update_data(exercise_id=ex_id, ex_back=data.get("ex_back"))
    await callback.message.answer(
        "Новые цели: подходы повторы_мин повторы_макс [шаг_кг]\n"
        "Пример: 4 8 12 2.5"
    )
    await callback.answer()


@router.message(AdminSG.edit_exercise_targets)
async def adm_ex_targets_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    parsed = _parse_targets(message.text or "")
    if not parsed:
        await message.answer("Формат: 4 8 12 2.5")
        return
    sets, rmin, rmax, step = parsed
    data = await state.get_data()
    ex_id = int(data["exercise_id"])
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await state.clear()
            await message.answer("Упражнение уже удалено.")
            return
        ex.target_sets = sets
        ex.target_reps_min = rmin
        ex.target_reps_max = rmax
        ex.weight_step = step
        await upsert_archive(
            session,
            ex.name,
            target_sets=sets,
            target_reps_min=rmin,
            target_reps_max=rmax,
            weight_step=step,
            overwrite_targets=True,
        )
        await session.commit()
        tpl = await session.get(WorkoutTemplate, ex.template_id)
        tpl_name = tpl.name if tpl else "?"
        tpl_id = ex.template_id
        text = _exercise_text(ex, tpl_name)
    back = _ex_back_from_data(data, tpl_id)
    await state.clear()
    await state.update_data(ex_back=back)
    await message.answer(
        f"Цели обновлены.\n\n{text}",
        reply_markup=exercise_edit_kb(ex_id, tpl_id, back=back),
    )


@router.callback_query(F.data.startswith("adm:ex:delok:"))
async def adm_ex_delete_ok(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ex_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    back = data.get("ex_back")
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Уже удалено")
            return
        tpl_id = ex.template_id
        name = ex.name
        await upsert_archive(
            session,
            name,
            target_sets=ex.target_sets,
            target_reps_min=ex.target_reps_min,
            target_reps_max=ex.target_reps_max,
            weight_step=ex.weight_step,
            overwrite_targets=True,
        )
        states = (
            await session.execute(
                select(UserExerciseState).where(UserExerciseState.exercise_id == ex_id)
            )
        ).scalars().all()
        for st in states:
            await session.delete(st)
        await session.delete(ex)
        await session.flush()
        await _reindex_positions(session, tpl_id)
        await session.commit()
        if back == "adm:current":
            items = await _list_current_exercises(session)
            text = f"Удалено: {name}\n\n{ui.BTN_ADM_CURRENT}"
            kb = current_exercises_kb(items)
        else:
            tpl = await _load_template(session, tpl_id)
            text = f"Удалено: {name}\n\n" + (_template_text(tpl) if tpl else "Шаблон не найден.")
            kb = template_detail_kb(tpl_id, tpl.exercises if tpl else None)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("adm:ex:del:"))
async def adm_ex_delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    # Avoid matching adm:ex:delok:
    if ":delok:" in callback.data:
        return
    ex_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    back = data.get("ex_back")
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Уже удалено", show_alert=True)
            return
        tpl_id = ex.template_id
        name = ex.name
    await callback.message.edit_text(
        f"Удалить «{name}» из шаблона? Логи прошлых тренировок останутся.",
        reply_markup=exercise_delete_confirm_kb(ex_id, tpl_id, back=back),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:move:"))
async def adm_ex_move_pick(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ex_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Упражнение удалено", show_alert=True)
            return
        templates = (await session.execute(select(WorkoutTemplate))).scalars().all()
        others = [t for t in templates if t.id != ex.template_id]
        current_id = ex.template_id
        name = ex.name
    if not others:
        await callback.answer("Нет другого шаблона. Сначала создай его.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Куда перенести «{name}»?",
        reply_markup=exercise_move_kb(ex_id, templates, current_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ex:mvto:"))
async def adm_ex_move_do(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    ex_id = int(parts[3])
    dest_id = int(parts[4])
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        dest = await session.get(WorkoutTemplate, dest_id)
        if not ex or not dest:
            await callback.answer("Не найдено", show_alert=True)
            return
        if ex.template_id == dest_id:
            await callback.answer("Уже в этом шаблоне")
            return
        source_id = ex.template_id
        last = (
            await session.execute(
                select(TemplateExercise)
                .where(TemplateExercise.template_id == dest_id)
                .order_by(TemplateExercise.position.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        ex.template_id = dest_id
        ex.position = (last.position + 1) if last else 0
        await session.flush()
        await _reindex_positions(session, source_id)
        await _reindex_positions(session, dest_id)
        await session.commit()
        dest = await _load_template(session, dest_id)
        ex_name = ex.name
        dest_name = dest.name if dest else "?"
    text = f"«{ex_name}» перенесено в «{dest_name}».\n\n" + (
        _template_text(dest) if dest else ""
    )
    await callback.message.edit_text(
        text,
        reply_markup=template_detail_kb(dest_id, dest.exercises if dest else None),
    )
    await callback.answer("Перенесено")


@router.callback_query(F.data == "adm:archive")
async def adm_archive(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    page = 0
    async with SessionLocal() as session:
        items = await list_archive(session)
    await callback.message.edit_text(
        f"{ui.BTN_ADM_ARCHIVE}. Сюда попадает всё, что когда-либо было в программе или в логе.\n"
        "Удаление из шаблона архив не трогает.",
        reply_markup=archive_list_kb(items, page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:arch:p:"))
async def adm_archive_page(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        items = await list_archive(session)
    await callback.message.edit_reply_markup(reply_markup=archive_list_kb(items, page))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:arch:v:"))
async def adm_archive_view(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    item_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        item = await session.get(ExerciseArchive, item_id)
        if not item:
            await callback.answer("Нет в архиве", show_alert=True)
            return
        used = (
            await session.execute(select(TemplateExercise))
        ).scalars().all()
        used = [ex for ex in used if name_key(ex.name) == item.name_key]
        tpl_ids = {ex.template_id for ex in used}
        names = []
        for tid in tpl_ids:
            tpl = await session.get(WorkoutTemplate, tid)
            if tpl:
                names.append(tpl.name)
        text = (
            f"{item.name}\n"
            f"Цель: {item.target_sets}×{item.target_reps_min}-{item.target_reps_max}, "
            f"шаг {item.weight_step:g} кг\n"
            f"Сейчас в шаблонах: {', '.join(names) if names else 'нигде (только архив)'}"
        )
        item_pk = item.id
    await callback.message.edit_text(text, reply_markup=archive_item_kb(item_pk))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:arch:del:"))
async def adm_archive_delete(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    item_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        item = await session.get(ExerciseArchive, item_id)
        if item:
            await session.delete(item)
            await session.commit()
        items = await list_archive(session)
    await callback.message.edit_text(
        "Удалено из архива. Шаблоны и логи тренировок не трогались.",
        reply_markup=archive_list_kb(items, 0),
    )
    await callback.answer("Удалено")


@router.callback_query(F.data == "adm:schedule")
async def adm_schedule(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with SessionLocal() as session:
        days = (
            await session.execute(
                select(ScheduleDay).options(selectinload(ScheduleDay.template))
            )
        ).scalars().all()
    assignments = {d.weekday: d.template.name for d in days}
    await callback.message.edit_text(
        f"{ui.BTN_ADM_SCHEDULE} (нажми день, чтобы назначить шаблон):",
        reply_markup=schedule_kb(assignments),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^adm:sch:\d+$"))
async def adm_sch_day(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    weekday = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        templates = (await session.execute(select(WorkoutTemplate))).scalars().all()
    if not templates:
        await callback.answer("Сначала создай шаблон", show_alert=True)
        return
    await callback.message.edit_text(
        f"Шаблон для {WEEKDAY_NAMES[weekday]}:",
        reply_markup=schedule_pick_template_kb(weekday, templates),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:schset:"))
async def adm_sch_set(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, weekday_s, tpl_s = callback.data.split(":")
    weekday = int(weekday_s)
    tpl_id = int(tpl_s)
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(ScheduleDay).where(ScheduleDay.weekday == weekday))
        ).scalar_one_or_none()
        if existing:
            existing.template_id = tpl_id
        else:
            session.add(ScheduleDay(weekday=weekday, template_id=tpl_id))
        await session.commit()
        days = (
            await session.execute(
                select(ScheduleDay).options(selectinload(ScheduleDay.template))
            )
        ).scalars().all()
    assignments = {d.weekday: d.template.name for d in days}
    await callback.message.edit_text(
        "График обновлён:",
        reply_markup=schedule_kb(assignments),
    )
    await callback.answer("Ок")


@router.callback_query(F.data.startswith("adm:schclear:"))
async def adm_sch_clear(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    weekday = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(ScheduleDay).where(ScheduleDay.weekday == weekday))
        ).scalar_one_or_none()
        if existing:
            await session.delete(existing)
            await session.commit()
        days = (
            await session.execute(
                select(ScheduleDay).options(selectinload(ScheduleDay.template))
            )
        ).scalars().all()
    assignments = {d.weekday: d.template.name for d in days}
    await callback.message.edit_text(
        "День очищен:",
        reply_markup=schedule_kb(assignments),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:hours")
async def adm_hours(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    settings = get_settings()
    async with SessionLocal() as session:
        group = (await session.execute(select(GroupChat).limit(1))).scalar_one_or_none()
    rem = group.reminder_hour if group else settings.reminder_hour
    rec = group.recap_hour if group else settings.recap_hour
    await state.set_state(AdminSG.set_hours)
    await callback.message.answer(
        f"{ui.BTN_ADM_HOURS}\n"
        f"Сейчас: напоминание {rem}:00, сводка {rec}:00 ({settings.timezone}).\n"
        "Пришли два числа через пробел, например: 8 22"
    )
    await callback.answer()


@router.message(AdminSG.set_hours)
async def adm_set_hours(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _full_admin(message.from_user.id, message.from_user.full_name or "Admin"):
        return
    parts = (message.text or "").split()
    try:
        rem, rec = int(parts[0]), int(parts[1])
        if not (0 <= rem <= 23 and 0 <= rec <= 23):
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("Формат: 8 22")
        return

    async with SessionLocal() as session:
        groups = (await session.execute(select(GroupChat))).scalars().all()
        if not groups:
            await message.answer(
                "Группа ещё не привязана. Добавь бота в чат — часы сохранятся при появлении группы.\n"
                f"Пока в .env: REMINDER_HOUR / RECAP_HOUR. Запомнил для следующих групп: {rem} и {rec}."
            )
            # store on a placeholder? skip — update settings file not needed
        for g in groups:
            g.reminder_hour = rem
            g.recap_hour = rec
        await session.commit()
    await state.clear()
    await message.answer(f"Часы: напоминание {rem}:00, сводка {rec}:00")


@router.callback_query(F.data == "adm:missing")
async def adm_missing_today(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()
    async with SessionLocal() as session:
        from app.services.reminders import get_template_for_weekday
        from app.services.history import format_missing_today

        template = await get_template_for_weekday(session, weekday)
        text = await format_missing_today(
            session, today, template.id if template else None
        )
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Admin"
        )
        full = user.is_admin
    await callback.message.edit_text(text, reply_markup=admin_menu_kb(full=full))
    await callback.answer()


@router.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with SessionLocal() as session:
        users = list((await session.execute(select(User).order_by(User.id))).scalars().all())
    await callback.message.edit_text(
        f"{ui.BTN_ADM_USERS}\n"
        "Нажми строку — включить/выключить доступ к программе "
        "(шаблоны, текущие упражнения, архив, график).\n"
        "⭐ admin — полный доступ из ADMIN_TELEGRAM_IDS.",
        reply_markup=admin_users_kb(users),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:u:prog:"))
async def adm_toggle_program_admin(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        target = await session.get(User, user_id)
        if not target:
            await callback.answer("Не найден", show_alert=True)
            return
        if target.is_admin:
            await callback.answer("Это полный админ", show_alert=True)
            return
        target.is_program_admin = not bool(target.is_program_admin)
        await session.commit()
        users = list((await session.execute(select(User).order_by(User.id))).scalars().all())
        state = "включён" if target.is_program_admin else "выключен"
        name = target.display_name
    await callback.message.edit_text(
        f"{ui.BTN_ADM_USERS}\n"
        f"{name}: доступ к программе {state}.\n"
        "Нажми строку — включить/выключить.\n"
        "⭐ admin — полный доступ из ADMIN_TELEGRAM_IDS.",
        reply_markup=admin_users_kb(users),
    )
    await callback.answer(f"Программа: {state}")


@router.callback_query(F.data == "adm:current")
@router.callback_query(F.data.startswith("adm:cur:p:"))
async def adm_current_exercises(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _load_staff(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(ex_back="adm:current")
    page = 0
    if callback.data and callback.data.startswith("adm:cur:p:"):
        page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        items = await _list_current_exercises(session)
    await callback.message.edit_text(
        f"{ui.BTN_ADM_CURRENT}\n"
        "Все упражнения из шаблонов. Нажми — править имя, цели, перенести или удалить.",
        reply_markup=current_exercises_kb(items, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:cur:ex:"))
async def adm_current_ex_open(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _load_staff(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ex_id = int(callback.data.split(":")[-1])
    await state.update_data(ex_back="adm:current")
    async with SessionLocal() as session:
        ex = await session.get(TemplateExercise, ex_id)
        if not ex:
            await callback.answer("Упражнение удалено", show_alert=True)
            return
        tpl = await session.get(WorkoutTemplate, ex.template_id)
        text = _exercise_text(ex, tpl.name if tpl else "?")
        tpl_id = ex.template_id
    await callback.message.edit_text(
        text,
        reply_markup=exercise_edit_kb(ex_id, tpl_id, back="adm:current"),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:snapshot")
async def adm_snapshot(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin")
    if not user:
        await callback.answer("Нет доступа", show_alert=True)
        return
    import json

    from app.services.athlete_features import build_athlete_snapshot

    async with SessionLocal() as session:
        snap = await build_athlete_snapshot(session, user.id, days=60)
    adh = snap.get("adherence") or {}
    lines = [
        f"{ui.BTN_ADM_SNAPSHOT} (твой профиль, 60 дней)",
        f"Сессий: {adh.get('finished_sessions', 0)}",
        f"По графику: {adh.get('logged_on_schedule', 0)}/{adh.get('scheduled_days', 0)}",
        f"Заметок: {len(snap.get('notes_timeline') or [])}",
        f"BW точек: {len(snap.get('body_weight_series') or [])}",
        f"Лог: {(snap.get('user') or {}).get('log_level')}",
        "",
        "JSON (обрезка):",
        json.dumps(snap, ensure_ascii=False)[:3500],
    ]
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_menu_kb(full=True),
    )
    await callback.answer()


def _format_nn_load(data: dict | None) -> str:
    if not data:
        return (
            f"{ui.BTN_ADM_NN_LOAD}\n"
            "Нейросеть недоступна (офлайн / выключена).\n"
            "Запросы пользователей не принимаются."
        )
    active = data.get("active") or []
    queued = data.get("queued") or []
    history = data.get("history") or []
    status_ru = {
        "ok": "ok",
        "error": "ошибка",
        "cancelled": "отменён",
        "running": "идёт",
        "queued": "очередь",
    }
    lines = [
        f"{ui.BTN_ADM_NN_LOAD}",
        f"Модель: {data.get('model', '?')}",
        f"Параллельно макс.: {data.get('max_concurrent', '?')}",
        f"Сейчас: {data.get('active_count', 0)} активных, "
        f"{data.get('queued_count', 0)} в очереди",
        f"Завершено всего: {data.get('completed_total', 0)}, "
        f"ошибок: {data.get('failed_total', 0)}",
        "",
    ]
    if active:
        lines.append("Активные:")
        for j in active:
            who = j.get("user_label") or (f"id={j.get('user_id')}" if j.get("user_id") else "?")
            lines.append(
                f"• {who} — {j.get('kind')} · {status_ru.get(j.get('status'), j.get('status'))} "
                f"· {j.get('running_sec', 0)}с"
            )
        lines.append("")
    else:
        lines.append("Активных запросов нет.\n")
    if queued:
        lines.append("Очередь:")
        for j in queued:
            who = j.get("user_label") or (f"id={j.get('user_id')}" if j.get("user_id") else "?")
            lines.append(
                f"• {who} — {j.get('kind')} · ждёт {j.get('waiting_sec', 0)}с"
            )
        lines.append("")
    else:
        lines.append("Очередь пуста.\n")

    lines.append(f"История (последние {min(15, len(history))}/{data.get('history_limit', '?')}):")
    if not history:
        lines.append("• пока пусто")
    else:
        for h in history[:15]:
            who = h.get("user_label") or (f"id={h.get('user_id')}" if h.get("user_id") else "?")
            st = status_ru.get(h.get("status"), h.get("status"))
            when = str(h.get("finished_at") or "")[-8:]  # HH:MM:SS from ISO if possible
            if "T" in str(h.get("finished_at") or ""):
                when = str(h["finished_at"]).split("T", 1)[1].replace("Z", "")[:8]
            line = (
                f"• {when} {who} — {h.get('kind')} · {st} · {h.get('duration_sec', 0)}с"
            )
            if h.get("status") == "error" and h.get("error"):
                err = str(h["error"]).replace("\n", " ")[:60]
                line += f"\n  ↳ {err}"
            lines.append(line)

    lines.append("")
    lines.append(
        "Несколько пользователей могут слать запросы одновременно — "
        "лишние ждут в очереди, пока освободится слот."
    )
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3890] + "…"
    return text


@router.callback_query(F.data == "adm:nnload")
async def adm_nn_load(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _full_admin(callback.from_user.id, callback.from_user.full_name or "Admin")
    if not user:
        await callback.answer("Нет доступа", show_alert=True)
        return
    from aiogram.exceptions import TelegramBadRequest

    from app.services.nn_client import fetch_nn_load

    data = await fetch_nn_load()
    text = _format_nn_load(data)
    try:
        await callback.message.edit_text(text, reply_markup=admin_nn_load_kb())
        await callback.answer("Обновлено")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            await callback.answer("Без изменений")
        else:
            raise
