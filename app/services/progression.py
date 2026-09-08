from __future__ import annotations

from app.db.models import Difficulty, TemplateExercise, TrainingPhase, UserExerciseState


DIFFICULTY_LABELS = {
    Difficulty.easy: "легко",
    Difficulty.normal: "норм",
    Difficulty.hard: "тяжело",
    Difficulty.failure: "отказ",
}

PHASE_LABELS = {
    TrainingPhase.honeymoon: "медовый месяц",
    TrainingPhase.intermediate: "средний уровень",
    TrainingPhase.plateau: "плато",
}


def phase_from_experience(months: int) -> TrainingPhase:
    if months < 6:
        return TrainingPhase.honeymoon
    if months <= 18:
        return TrainingPhase.intermediate
    return TrainingPhase.plateau


def suggest_next_weight(
    *,
    phase: TrainingPhase,
    exercise: TemplateExercise,
    weight: float,
    reps: int,
    sets_count: int,
    difficulty: Difficulty,
    state: UserExerciseState | None,
) -> tuple[float, str]:
    """Return (suggested_weight, explanation)."""
    step = exercise.weight_step or 2.5
    target_sets = exercise.target_sets
    reps_min = exercise.target_reps_min
    reps_max = exercise.target_reps_max
    plan_hit = sets_count >= target_sets and reps >= reps_min

    if difficulty in (Difficulty.hard, Difficulty.failure):
        streak = (state.hard_streak + 1) if state else 1
        note = "вес оставляем"
        if streak >= 2:
            note = "два раза тяжело подряд — застрял, вес не трогаем"
        return weight, note

    if phase == TrainingPhase.honeymoon:
        if plan_hit and difficulty in (Difficulty.easy, Difficulty.normal):
            bump = step * 2 if difficulty == Difficulty.easy and reps >= reps_max else step
            return weight + bump, f"медовый месяц -> +{bump:g} кг"
        return weight, "план не добрали — вес тот же"

    if phase == TrainingPhase.plateau:
        if difficulty == Difficulty.easy and reps >= reps_max and sets_count >= target_sets:
            return weight + step, f"плато: верх повторов -> +{step:g} кг"
        if plan_hit and difficulty == Difficulty.easy:
            return weight, "плато: держим вес, цель — больше повторов"
        return weight, "плато: вес без изменений"

    # intermediate
    if plan_hit and difficulty == Difficulty.easy and reps >= reps_max:
        return weight + step, f"+{step:g} кг на следующий раз"
    if plan_hit and difficulty == Difficulty.normal:
        return weight, "норм — вес тот же, можно добавить повторы"
    return weight, "вес без изменений"


def format_block(reps: int, weight: float, sets_count: int) -> str:
    if weight <= 0:
        return f"{reps}×отказ×{sets_count}" if reps else f"отказ×{sets_count}"
    return f"{reps}×{weight:g}×{sets_count}"
