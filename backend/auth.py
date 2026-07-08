import os
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Header
from passlib.context import CryptContext
import jwt

from models import UserCreate, UserLogin, UserPublic, UserDoc

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"
JWT_EXPIRY_HOURS = 24 * 30  # 30 days


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_context.verify(pw, hashed)


def create_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["sub"]
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def to_public(u: dict) -> UserPublic:
    # active subscription check
    is_sub = bool(u.get("is_subscribed"))
    exp = u.get("subscription_expires_at")
    if is_sub and exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now(timezone.utc):
                is_sub = False
        except Exception:
            pass
    return UserPublic(
        id=u["id"],
        email=u["email"],
        scans_remaining=u.get("scans_remaining", 0),
        is_subscribed=is_sub,
        subscription_expires_at=u.get("subscription_expires_at"),
        total_scans=u.get("total_scans", 0),
        total_profit_gbp=u.get("total_profit_gbp", 0.0),
        created_at=u["created_at"],
    )


def build_router(db):
    router = APIRouter(prefix="/auth", tags=["auth"])

    async def get_current_user(authorization: str = Header(None)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing token")
        token = authorization.split(" ", 1)[1]
        user_id = decode_token(token)
        user = await db.users.find_one({"id": user_id}, {"_id": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    @router.post("/register")
    async def register(payload: UserCreate):
        email = payload.email.lower().strip()
        if await db.users.find_one({"email": email}):
            raise HTTPException(status_code=400, detail="Email already registered")
        if len(payload.password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        doc = UserDoc(email=email, hashed_password=hash_password(payload.password))
        await db.users.insert_one(doc.model_dump())
        token = create_token(doc.id)
        return {"token": token, "user": to_public(doc.model_dump()).model_dump()}

    @router.post("/login")
    async def login(payload: UserLogin):
        email = payload.email.lower().strip()
        user = await db.users.find_one({"email": email}, {"_id": 0})
        if not user or not verify_password(payload.password, user["hashed_password"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_token(user["id"])
        return {"token": token, "user": to_public(user).model_dump()}

    @router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return to_public(user).model_dump()

    return router, get_current_user
