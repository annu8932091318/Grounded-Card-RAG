"""Boots the API. uvicorn handles SIGINT/SIGTERM gracefully: stops accepting, drains in-flight requests,
then runs the app lifespan shutdown (closes the DB pool)."""
import uvicorn

from src.const.config import config

if __name__ == "__main__":
    uvicorn.run("src.app:app", host="0.0.0.0", port=config.port, reload=config.env == "development",
                timeout_graceful_shutdown=30)
