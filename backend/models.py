from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, Field, EmailStr, ConfigDict
import uuid


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: str
    scans_remaining: int
    is_subscribed: bool
    subscription_expires_at: Optional[str] = None
    total_scans: int = 0
    total_profit_gbp: float = 0.0
    created_at: str


class UserDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    hashed_password: str
    scans_remaining: int = 3  # free tier
    is_subscribed: bool = False
    subscription_expires_at: Optional[str] = None
    total_scans: int = 0
    total_profit_gbp: float = 0.0
    created_at: str = Field(default_factory=now_iso)


class IdentifyResult(BaseModel):
    brand: str
    item_type: str
    model: str
    era: str
    category: str
    confidence: str  # low / medium / high


class ScanEstimateRequest(BaseModel):
    scan_id: str
    condition: str  # New with tags / Like new / Good / Fair / Worn
    size: str
    flaws: Optional[str] = ""
    buy_price_gbp: float = 0.0
    platform: Optional[str] = None  # ebay / vinted / depop / grailed


class ScanResult(BaseModel):
    sold_avg_gbp: float
    sold_low_gbp: float
    sold_high_gbp: float
    suggested_max_buy_gbp: float
    demand_level: str
    recommended_platform: str
    fees_gbp: float
    postage_gbp: float
    true_profit_gbp: float
    flip_score: int  # 1-10
    listing_title: str
    listing_description: str


class ScanDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    image_thumbnail: Optional[str] = None  # base64 preview
    identify: dict
    estimate: Optional[dict] = None
    condition: Optional[str] = None
    size: Optional[str] = None
    flaws: Optional[str] = None
    buy_price_gbp: float = 0.0
    source_spot: Optional[str] = None
    status: str = "identified"  # identified / estimated
    created_at: str = Field(default_factory=now_iso)


class SourceSpotUpdate(BaseModel):
    source_spot: str


class CheckoutRequest(BaseModel):
    lookup_key: str
    origin_url: str
