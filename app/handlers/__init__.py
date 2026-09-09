from aiogram import Router

from app.handlers import admin, errors, group, profile, program, start, workout


def setup_routers() -> Router:
    root = Router()
    root.include_router(errors.router)
    root.include_router(start.router)
    root.include_router(profile.router)
    root.include_router(program.router)
    root.include_router(workout.router)
    root.include_router(admin.router)
    root.include_router(group.router)
    return root
