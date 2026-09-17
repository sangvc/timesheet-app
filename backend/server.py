from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------- Models ----------
class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    color: str = "#059669"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class TaskCreate(BaseModel):
    name: str
    color: Optional[str] = "#059669"

class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_name: str
    start_time: str  # ISO
    end_time: Optional[str] = None  # ISO
    duration_seconds: int = 0
    date: str  # YYYY-MM-DD
    note: str = ""
    is_active: bool = True

class SessionStart(BaseModel):
    task_name: str

class SessionManual(BaseModel):
    task_name: str
    start_time: str  # ISO
    end_time: str  # ISO
    note: Optional[str] = ""

# ---------- Seed defaults ----------
DEFAULT_TASKS = [
    {"name": "Lập trình", "color": "#059669"},
    {"name": "Họp khách hàng", "color": "#2563EB"},
    {"name": "Thiết kế UI", "color": "#D97706"},
    {"name": "Nghiên cứu", "color": "#8B5CF6"},
    {"name": "Báo cáo & Tài liệu", "color": "#EC4899"},
    {"name": "Khác", "color": "#64748B"},
]

@app.on_event("startup")
async def seed_defaults():
    count = await db.tasks.count_documents({})
    if count == 0:
        for t in DEFAULT_TASKS:
            task = Task(name=t["name"], color=t["color"])
            await db.tasks.insert_one(task.model_dump())
    # Seed sample sessions if none
    sess_count = await db.sessions.count_documents({})
    if sess_count == 0:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        samples = [
            (0, 9, 30, 120, "Lập trình"),
            (0, 14, 0, 90, "Họp khách hàng"),
            (1, 10, 15, 75, "Thiết kế UI"),
            (2, 9, 0, 180, "Lập trình"),
            (3, 13, 30, 60, "Nghiên cứu"),
            (5, 8, 45, 150, "Báo cáo & Tài liệu"),
            (7, 10, 0, 105, "Lập trình"),
            (10, 15, 30, 45, "Khác"),
            (15, 9, 0, 200, "Lập trình"),
            (20, 14, 0, 80, "Thiết kế UI"),
            (35, 10, 0, 130, "Nghiên cứu"),
            (50, 9, 30, 160, "Lập trình"),
        ]
        for days_ago, hour, minute, dur_min, task_name in samples:
            start = (now - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            end = start + timedelta(minutes=dur_min)
            session = Session(
                task_name=task_name,
                start_time=start.isoformat(),
                end_time=end.isoformat(),
                duration_seconds=dur_min * 60,
                date=start.strftime("%Y-%m-%d"),
                is_active=False,
            )
            await db.sessions.insert_one(session.model_dump())

# ---------- Task endpoints ----------
@api_router.get("/tasks", response_model=List[Task])
async def list_tasks():
    docs = await db.tasks.find({}, {"_id": 0}).to_list(1000)
    return docs

@api_router.post("/tasks", response_model=Task)
async def create_task(payload: TaskCreate):
    task = Task(name=payload.name.strip(), color=payload.color or "#059669")
    if not task.name:
        raise HTTPException(400, "Task name required")
    await db.tasks.insert_one(task.model_dump())
    return task

@api_router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    res = await db.tasks.delete_one({"id": task_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Task not found")
    return {"ok": True}

# ---------- Session endpoints ----------
@api_router.get("/sessions", response_model=List[Session])
async def list_sessions():
    docs = await db.sessions.find({}, {"_id": 0}).sort("start_time", -1).to_list(5000)
    return docs

@api_router.get("/sessions/active", response_model=Optional[Session])
async def active_session():
    doc = await db.sessions.find_one({"is_active": True}, {"_id": 0})
    return doc

@api_router.post("/sessions/start", response_model=Session)
async def start_session(payload: SessionStart):
    # Stop any existing active session first
    await db.sessions.update_many(
        {"is_active": True},
        {"$set": {"is_active": False}},
    )
    now = datetime.now(timezone.utc)
    session = Session(
        task_name=payload.task_name,
        start_time=now.isoformat(),
        date=now.strftime("%Y-%m-%d"),
        is_active=True,
    )
    await db.sessions.insert_one(session.model_dump())
    return session

@api_router.post("/sessions/{session_id}/stop", response_model=Session)
async def stop_session(session_id: str):
    doc = await db.sessions.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Session not found")
    if not doc.get("is_active"):
        return doc
    now = datetime.now(timezone.utc)
    start_dt = datetime.fromisoformat(doc["start_time"])
    duration = int((now - start_dt).total_seconds())
    updated = {
        "end_time": now.isoformat(),
        "duration_seconds": duration,
        "is_active": False,
    }
    await db.sessions.update_one({"id": session_id}, {"$set": updated})
    doc.update(updated)
    return doc

@api_router.post("/sessions/manual", response_model=Session)
async def create_manual_session(payload: SessionManual):
    start_dt = datetime.fromisoformat(payload.start_time)
    end_dt = datetime.fromisoformat(payload.end_time)
    if end_dt <= start_dt:
        raise HTTPException(400, "End time must be after start time")
    duration = int((end_dt - start_dt).total_seconds())
    session = Session(
        task_name=payload.task_name,
        start_time=payload.start_time,
        end_time=payload.end_time,
        duration_seconds=duration,
        date=start_dt.strftime("%Y-%m-%d"),
        note=payload.note or "",
        is_active=False,
    )
    await db.sessions.insert_one(session.model_dump())
    return session

@api_router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    res = await db.sessions.delete_one({"id": session_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Session not found")
    return {"ok": True}

@api_router.delete("/sessions")
async def delete_all_sessions():
    res = await db.sessions.delete_many({})
    return {"deleted": res.deleted_count}

@api_router.get("/")
async def root():
    return {"message": "ChronoWork API"}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
