"""Status endpoint - health check and widget list."""
from fastapi import APIRouter
from app.widgets import widget_registry

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status():
    """Return dashboard status and available widgets."""
    return {
        "status": "ok",
        "widgets": [
            {
                "id": w.id,
                "title": w.title,
                "icon": w.icon,
                "refresh_interval": w.refresh_interval,
            }
            for w in widget_registry.all()
        ],
    }
