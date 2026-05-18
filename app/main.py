from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(title="HR DV Worker", version="0.1.0")
app.include_router(webhooks_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
