from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from auth import build_router as build_auth_router
from vision import build_router as build_scan_router
from payments import build_router as build_payments_router

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="margin API")
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "margin", "status": "ok"}


@api_router.get("/health")
async def health():
    return {"ok": True}


# Wire up routers
auth_router, get_current_user = build_auth_router(db)
api_router.include_router(auth_router)
api_router.include_router(build_scan_router(db, get_current_user))
api_router.include_router(build_payments_router(db, get_current_user))

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.scans.create_index("user_id")
    await db.scans.create_index("id", unique=True)
    await db.payment_transactions.create_index("session_id", unique=True)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
