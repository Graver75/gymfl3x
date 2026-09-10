from aiogram.fsm.state import State, StatesGroup


class OnboardingSG(StatesGroup):
    display_name = State()
    short_code = State()
    body_weight = State()
    height = State()
    experience = State()


class WorkoutSG(StatesGroup):
    pick_mode = State()
    pick_template = State()
    pick_archive = State()
    pick_exercise = State()
    weight = State()
    custom_weight = State()
    reps = State()
    custom_reps = State()
    set_rpe = State()
    after_set = State()
    difficulty = State()
    note = State()
    checkin_energy = State()
    checkin_sleep = State()
    checkin_pain = State()


class EditSessionSG(StatesGroup):
    pick_set = State()
    edit_weight = State()
    edit_reps = State()


class AdminSG(StatesGroup):
    new_template_name = State()
    new_template_hashtag = State()
    add_exercise_name = State()
    add_exercise_targets = State()
    edit_exercise_name = State()
    edit_exercise_targets = State()
    set_hours = State()
    compose_chat = State()


class ProfileSG(StatesGroup):
    edit_weight = State()
    edit_experience = State()
