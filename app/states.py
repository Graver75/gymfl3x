from aiogram.fsm.state import State, StatesGroup


class OnboardingSG(StatesGroup):
    display_name = State()
    short_code = State()
    body_weight = State()
    height = State()
    experience = State()


class WorkoutSG(StatesGroup):
    pick_exercise = State()
    weight = State()
    custom_weight = State()
    reps = State()
    sets = State()
    difficulty = State()


class AdminSG(StatesGroup):
    new_template_name = State()
    new_template_hashtag = State()
    add_exercise_name = State()
    add_exercise_targets = State()
    edit_exercise_name = State()
    edit_exercise_targets = State()
    set_hours = State()


class ProfileSG(StatesGroup):
    edit_weight = State()
    edit_experience = State()
