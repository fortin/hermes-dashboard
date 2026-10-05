import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import init_db
from .localtime import load_timezone_preference
from .mcp.client import registry
from .routers import agent, calendar, daily_note, email, preferences, tasks, widgets
from .services import briefing_push, omnifocus_agent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    await load_timezone_preference()
    push_task = asyncio.create_task(briefing_push.run_loop())
    gladys_task = asyncio.create_task(omnifocus_agent.run_loop())
    try:
        yield
    finally:
        push_task.cancel()
        gladys_task.cancel()
        with suppress(asyncio.CancelledError):
            await push_task
        with suppress(asyncio.CancelledError):
            await gladys_task
        await registry.close_all()


app = FastAPI(title="Hermes Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calendar.router)
app.include_router(tasks.router)
app.include_router(daily_note.router)
app.include_router(agent.router)
app.include_router(email.router)
app.include_router(widgets.router)
app.include_router(preferences.router)

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "hermes-dashboard"}


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str = ""):
        index = DIST / "index.html"
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
        app_dir=str(Path(__file__).resolve().parents[1]),
    )


if __name__ == "__main__":
    run()
