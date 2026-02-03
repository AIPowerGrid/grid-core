# Phase 1: FastAPI Migration

## Overview

Migrate from Flask/Waitress to FastAPI/Uvicorn for native async support.

**Why FastAPI:**
- Native async/await (not monkey-patched)
- 10-100x more concurrent connections
- Native SSE and WebSocket support
- Almost identical syntax to Flask-RESTX
- Automatic OpenAPI docs (like Flask-RESTX)

## Step 1: Project Structure

Create new FastAPI app alongside Flask (run in parallel during migration):

```
horde/
├── apis/                    # Current Flask APIs (keep during migration)
├── fastapi_app/             # NEW: FastAPI application
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Settings and configuration
│   ├── dependencies.py      # Dependency injection
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── generate.py      # /api/v2/generate/* endpoints
│   │   ├── workers.py       # /api/v2/workers/* endpoints
│   │   ├── users.py         # /api/v2/users/* endpoints
│   │   └── status.py        # /api/v2/status/* endpoints
│   ├── models/              # Pydantic models (request/response)
│   │   ├── __init__.py
│   │   ├── generate.py
│   │   └── worker.py
│   └── services/            # Business logic
│       ├── __init__.py
│       ├── job_queue.py     # Redis Streams integration
│       └── streaming.py     # SSE/Pub-Sub integration
```

## Step 2: Dependencies

```bash
pip install fastapi uvicorn[standard] redis[hiredis] asyncpg sqlalchemy[asyncio] sse-starlette
```

Add to `requirements.txt`:

```txt
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
redis[hiredis]>=5.0.0
asyncpg>=0.29.0
sqlalchemy[asyncio]>=2.0.0
sse-starlette>=1.8.0
httpx>=0.26.0
```

## Step 3: FastAPI App Scaffold

### `horde/fastapi_app/main.py`

```python
"""FastAPI application for AIPG Core."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import generate, workers, users, status
from .services.job_queue import JobQueue
from .services.streaming import StreamingService


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    app.state.job_queue = JobQueue(settings.REDIS_URL)
    app.state.streaming = StreamingService(settings.REDIS_URL)
    await app.state.job_queue.connect()
    await app.state.streaming.connect()
    
    yield
    
    # Shutdown
    await app.state.job_queue.disconnect()
    await app.state.streaming.disconnect()


app = FastAPI(
    title="AI Power Grid API",
    version="2.0.0",
    description="Decentralized AI Worker Network API",
    lifespan=lifespan,
)

# CORS - tighten for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(generate.router, prefix="/api/v2/generate", tags=["generate"])
app.include_router(workers.router, prefix="/api/v2/workers", tags=["workers"])
app.include_router(users.router, prefix="/api/v2/users", tags=["users"])
app.include_router(status.router, prefix="/api/v2/status", tags=["status"])


@app.get("/health")
async def health_check():
    return {"status": "ok"}
```

### `horde/fastapi_app/config.py`

```python
"""Configuration settings."""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost/horde"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    
    # API
    API_KEY_HEADER: str = "apikey"
    
    # Job Queue
    JOB_STREAM_IMAGE: str = "jobs:image"
    JOB_STREAM_TEXT: str = "jobs:text"
    JOB_STREAM_VIDEO: str = "jobs:video"
    
    # Streaming
    STREAM_PREFIX: str = "stream:"
    
    # CORS
    CORS_ORIGINS: list[str] = ["*"]
    
    # Long polling (fallback)
    LONG_POLL_TIMEOUT: int = 30
    
    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

### `horde/fastapi_app/dependencies.py`

```python
"""FastAPI dependencies for dependency injection."""

from typing import Annotated, Optional
from fastapi import Depends, Header, HTTPException, status, Request

from .config import settings
# Import your existing database functions
from horde.database import functions as database


async def get_job_queue(request: Request):
    """Get job queue service from app state."""
    return request.app.state.job_queue


async def get_streaming(request: Request):
    """Get streaming service from app state."""
    return request.app.state.streaming


async def get_current_user(
    apikey: Annotated[Optional[str], Header()] = None
):
    """Validate API key and return user."""
    if not apikey:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )
    
    user = database.find_user_by_api_key(apikey)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    
    return user


async def get_worker_auth(
    apikey: Annotated[Optional[str], Header()] = None,
    worker_name: str = None,
):
    """Validate worker credentials."""
    user = await get_current_user(apikey)
    
    if not worker_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Worker name required",
        )
    
    worker = database.find_worker_by_name(worker_name)
    if worker and worker.user != user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Wrong credentials for worker",
        )
    
    return {"user": user, "worker": worker}
```

## Step 4: Migrate Endpoints

### Example: Generate Submit (Flask → FastAPI)

**Flask (current):**

```python
@api.route('/async')
class AsyncGenerate(GenerateTemplate):
    def post(self):
        self.args = parsers.generate_parser.parse_args()
        # ... validation, create WaitingPrompt, etc.
        return {"id": self.wp.id, "kudos": self.kudos}, 202
```

**FastAPI (new):**

```python
# horde/fastapi_app/routers/generate.py

from fastapi import APIRouter, Depends, HTTPException
from ..dependencies import get_current_user, get_job_queue
from ..models.generate import GenerateRequest, GenerateResponse

router = APIRouter()


@router.post("/async", response_model=GenerateResponse, status_code=202)
async def submit_generation(
    request: GenerateRequest,
    user = Depends(get_current_user),
    job_queue = Depends(get_job_queue),
):
    """Submit an image generation request."""
    
    # Validation
    if not request.prompt:
        raise HTTPException(status_code=400, detail="Prompt required")
    
    # Create job (using existing logic or new)
    job_id = await job_queue.submit_job(
        job_type="image",
        user_id=user.id,
        payload=request.model_dump(),
    )
    
    return GenerateResponse(
        id=job_id,
        kudos=request.calculate_kudos(),
    )
```

### Example: Worker Pop (Flask → FastAPI with Long Poll)

**Flask (current):** Returns immediately if no jobs.

**FastAPI (new):** Holds connection until job available or timeout.

```python
# horde/fastapi_app/routers/workers.py

import asyncio
from fastapi import APIRouter, Depends
from ..dependencies import get_worker_auth, get_job_queue
from ..models.worker import PopRequest, PopResponse
from ..config import settings

router = APIRouter()


@router.post("/pop", response_model=PopResponse)
async def pop_job(
    request: PopRequest,
    auth = Depends(get_worker_auth),
    job_queue = Depends(get_job_queue),
):
    """Pop a job for the worker. Long-polls if no jobs available."""
    
    worker = auth["worker"]
    
    # Try to get a job, with long-poll fallback
    job = await job_queue.pop_job(
        worker=worker,
        models=request.models,
        max_pixels=request.max_pixels,
        timeout=settings.LONG_POLL_TIMEOUT,
    )
    
    if job:
        return PopResponse(
            id=job["id"],
            payload=job["payload"],
            model=job["model"],
        )
    
    return PopResponse(id=None, skipped={})
```

## Step 5: Run in Parallel

During migration, run both Flask and FastAPI:

```bash
# Terminal 1: Flask (existing, port 7001)
python server.py --port 7001

# Terminal 2: FastAPI (new, port 7002)
uvicorn horde.fastapi_app.main:app --host 0.0.0.0 --port 7002 --workers 4
```

Use nginx or your load balancer to route:
- `/api/v2/generate/async` → FastAPI (new)
- `/api/v2/generate/pop` → FastAPI (new)
- Everything else → Flask (existing)

## Step 6: Database - Async SQLAlchemy

For full async benefits, migrate database calls to async:

```python
# horde/fastapi_app/database.py

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from .config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
```

**Note:** You can initially use sync database calls in FastAPI (it works, just blocks). Migrate to async incrementally.

## Rollback Plan

If issues arise:
1. Route traffic back to Flask via load balancer
2. FastAPI runs in parallel, no data migration needed
3. Same database, same Redis

## Success Criteria

- [ ] FastAPI app running on separate port
- [ ] `/api/v2/generate/async` working on FastAPI
- [ ] `/api/v2/generate/pop` working with long-poll
- [ ] All tests passing
- [ ] No increase in error rates

## Next Phase

Once FastAPI is stable, proceed to [Phase 2: Redis Streams](./02-redis-streams.md).
