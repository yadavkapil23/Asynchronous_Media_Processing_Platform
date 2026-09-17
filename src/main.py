from fastapi import FastAPI
app = FastAPI(title="Media API", version="1.0.0")

from src.api.v1 import uploads,health

app.include_router(uploads.router,prefix="/v1/uploads",tags=["uploads"])
app.include_router(health.router,prefix="/v1/health",tags=["health"])