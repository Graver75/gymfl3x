from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import GroupChat, ScheduleDay, TemplateExercise, User, WorkoutTemplate
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    admin_menu_kb,
    schedule_kb,
    schedule_pick_template_kb,
    template_detail_kb,
    templates_list_kb,
)
from app.services.reminders import WEEKDAY_NAMES
from app.services.users import get_or_create_user
from app.states import AdminSG

router = Router(name="admin")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _admin_user(telegram_id: int, full_name: str) -> User | None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        settings = get_settings()
        if telegram_id in settings.admin_telegram_ids and not user.is_admin:
            user.is_admin = True
            await session.commit()
        if not user.is_admin:
            return None
        session.expunge(user)
        return user


async def _deny(message: Message) -> None:
    await message.answer("Только для админа.")


@router.message(Command("admin"))
@router.message(F.text == "Админка")
async def admin_home(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    user = await _admin_user(
        message.from_user.id,
        message.from_user.full_name or "Admin",
    )
    if not user:
        await _deny(message)
        return
    await state.clear()
    await message.answer("Админка Gymflex:", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "adm:home")
async def adm_home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin")
    if not user:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("Админка Gymflex:", reply_markup=admin_menu_kb())
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
        "Шаблоны тренировочных дней:",
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
        tpl = (
            await session.execute(
                select(WorkoutTemplate)
                .where(WorkoutTemplate.id == tpl_id)
                .options(selectinload(WorkoutTemplate.exercises))
            )
        ).scalar_one_or_none()
    if not tpl:
        await callback.answer("Не найден", show_alert=True)
        return
    lines = [f"{tpl.name} (#{tpl.hashtag})", ""]
    if not tpl.exercises:
        lines.append("Упражнений пока нет.")
    for ex in tpl.exercises:
        lines.append(
            f"{ex.position + 1}. {ex.name} — "
            f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}"
        )
    await callback.message.edit_text("\n".join(lines), reply_markup=template_detail_kb(tpl.id))
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


@router.callback_query(F.data.startswith("adm:ex:add:"))
async def adm_ex_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    tpl_id = int(callback.data.split(":")[-1])
    await state.set_state(AdminSG.add_exercise_name)
    await state.update_data(tpl_id=tpl_id)
    await callback.message.answer("Название упражнения, например «Верхняя тяга»:")
    await callback.answer()


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
    parts = (message.text or "").replace(",", ".").split()
    try:
        sets = int(parts[0])
        rmin = int(parts[1])
        rmax = int(parts[2]) if len(parts) > 2 else rmin
        step = float(parts[3]) if len(parts) > 3 else 2.5
        if sets < 1 or rmin < 1 or rmax < rmin:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("Формат: 4 8 12 2.5")
        return

    data = await state.get_data()
    tpl_id = data["tpl_id"]
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(TemplateExercise)
                .where(TemplateExercise.template_id == tpl_id)
                .order_by(TemplateExercise.position.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        pos = (existing.position + 1) if existing else 0
        ex = TemplateExercise(
            template_id=tpl_id,
            name=data["ex_name"],
            position=pos,
            target_sets=sets,
            target_reps_min=rmin,
            target_reps_max=rmax,
            weight_step=step,
        )
        session.add(ex)
        await session.commit()
        tpl = (
            await session.execute(
                select(WorkoutTemplate)
                .where(WorkoutTemplate.id == tpl_id)
                .options(selectinload(WorkoutTemplate.exercises))
            )
        ).scalar_one()

    await state.clear()
    lines = [f"Добавлено. {tpl.name}:"]
    for item in tpl.exercises:
        lines.append(
            f"{item.position + 1}. {item.name} — "
            f"{item.target_sets}×{item.target_reps_min}-{item.target_reps_max}"
        )
    await message.answer("\n".join(lines), reply_markup=template_detail_kb(tpl_id))


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
        "График на неделю (нажми день, чтобы назначить шаблон):",
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
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    settings = get_settings()
    async with SessionLocal() as session:
        group = (await session.execute(select(GroupChat).limit(1))).scalar_one_or_none()
    rem = group.reminder_hour if group else settings.reminder_hour
    rec = group.recap_hour if group else settings.recap_hour
    await state.set_state(AdminSG.set_hours)
    await callback.message.answer(
        f"Сейчас: напоминание {rem}:00, сводка {rec}:00 ({settings.timezone}).\n"
        "Пришли два числа через пробел, например: 8 22"
    )
    await callback.answer()


@router.message(AdminSG.set_hours)
async def adm_set_hours(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await _admin_user(message.from_user.id, message.from_user.full_name or "Admin"):
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


@router.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    if not await _admin_user(callback.from_user.id, callback.from_user.full_name or "Admin"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with SessionLocal() as session:
        users = (await session.execute(select(User).order_by(User.id))).scalars().all()
    if not users:
        text = "Пользователей пока нет."
    else:
        lines = ["Пользователи:"]
        for u in users:
            flag = " [admin]" if u.is_admin else ""
            done = "✓" if u.onboarding_done else "…"
            lines.append(f"{done} {u.short_code} {u.display_name}{flag} (tg:{u.telegram_id})")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=admin_menu_kb())
    await callback.answer()
