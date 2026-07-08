import os
import json
import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Callable
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException

from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent

from models import (
    IdentifyResult,
    ScanEstimateRequest,
    ScanResult,
    ScanDoc,
    SourceSpotUpdate,
)

EMERGENT_LLM_KEY = os.environ["EMERGENT_LLM_KEY"]
GEMINI_MODEL = "gemini-3-flash-preview"


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_from_llm(text: str) -> dict:
    text = _strip_code_fence(text)
    # find first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail=f"Bad LLM output: {text[:200]}")
    return json.loads(match.group(0))


async def _llm_json(system: str, user_text: str, image_b64: str | None = None) -> dict:
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"margin-{uuid.uuid4().hex[:8]}",
        system_message=system,
    ).with_model("gemini", GEMINI_MODEL)

    file_contents = []
    if image_b64:
        file_contents.append(ImageContent(image_base64=image_b64))

    msg = UserMessage(text=user_text, file_contents=file_contents or None)
    resp = await chat.send_message(msg)
    return _parse_json_from_llm(resp)


# Fee models (buyer's £ price × seller fee % + fixed)
PLATFORM_FEES = {
    "ebay":    {"pct": 0.128, "fixed": 0.30},   # eBay UK final value fee approx
    "vinted":  {"pct": 0.00,  "fixed": 0.00},   # Vinted seller = free (buyer pays protection)
    "depop":   {"pct": 0.10,  "fixed": 0.00},
    "grailed": {"pct": 0.09,  "fixed": 0.00},
}
DEFAULT_POSTAGE_GBP = 3.50


def compute_estimate(
    sold_avg: float, sold_low: float, sold_high: float,
    demand_level: str, platform: str, buy_price: float,
) -> dict:
    platform_key = (platform or "ebay").lower()
    if platform_key not in PLATFORM_FEES:
        platform_key = "ebay"
    fees_conf = PLATFORM_FEES[platform_key]
    fees = round(sold_avg * fees_conf["pct"] + fees_conf["fixed"], 2)
    postage = DEFAULT_POSTAGE_GBP
    true_profit = round(sold_avg - fees - postage - buy_price, 2)

    # Suggested max buy = target 3x return on sale profit (post-fees)
    net_after_fees = sold_avg - fees - postage
    suggested_max_buy = round(max(net_after_fees / 3.0, 1.0), 2)

    # Flip score: demand + margin
    demand_score = {"low": 3, "medium": 6, "high": 9}.get((demand_level or "").lower(), 5)
    margin_pct = (true_profit / sold_avg) if sold_avg > 0 else 0
    margin_score = min(10, max(0, int(margin_pct * 15)))
    flip_score = int(min(10, max(1, round((demand_score * 0.6) + (margin_score * 0.4)))))

    return {
        "sold_avg_gbp": round(sold_avg, 2),
        "sold_low_gbp": round(sold_low, 2),
        "sold_high_gbp": round(sold_high, 2),
        "suggested_max_buy_gbp": suggested_max_buy,
        "demand_level": demand_level,
        "recommended_platform": platform_key,
        "fees_gbp": fees,
        "postage_gbp": postage,
        "true_profit_gbp": true_profit,
        "flip_score": flip_score,
    }


def build_router(db, get_current_user: Callable):
    router = APIRouter(prefix="/scan", tags=["scan"])

    def has_credit(user: dict) -> bool:
        if user.get("is_subscribed"):
            exp = user.get("subscription_expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp) >= datetime.now(timezone.utc):
                        return True
                except Exception:
                    pass
            else:
                return True
        return user.get("scans_remaining", 0) > 0

    async def deduct_credit(user: dict):
        if user.get("is_subscribed"):
            return
        await db.users.update_one(
            {"id": user["id"]},
            {"$inc": {"scans_remaining": -1, "total_scans": 1}},
        )

    @router.post("/identify")
    async def identify(
        file: UploadFile = File(...),
        user: dict = Depends(get_current_user),
    ):
        if not has_credit(user):
            raise HTTPException(
                status_code=402,
                detail="Out of scans. Buy a pack or subscribe for unlimited.",
            )

        image_bytes = await file.read()
        if len(image_bytes) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image too large (max 8MB)")

        mime = file.content_type or "image/jpeg"
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(status_code=400, detail="Only JPG/PNG/WEBP allowed")

        img_b64 = base64.b64encode(image_bytes).decode()

        system = (
            "You are Margin, a clothing reseller expert. You identify secondhand clothing "
            "items from photos to help resellers on eBay/Vinted/Depop/Grailed. "
            "Return strict JSON only, no prose, no code fences."
        )
        prompt = (
            "Identify this clothing item. Respond ONLY with a JSON object with these keys:\n"
            '{"brand": string, "item_type": string, "model": string, '
            '"era": string, "category": string, "confidence": "low"|"medium"|"high"}\n\n'
            "Rules:\n"
            "- brand: single brand name (e.g. 'Nike', 'Ralph Lauren'). Use 'Unknown' if not visible.\n"
            "- item_type: e.g. 'T-shirt', 'Hoodie', 'Denim jacket', 'Trainers', 'Handbag'.\n"
            "- model: specific model/collection if identifiable (e.g. 'Air Jordan 1 Mid', "
            "'Levi's 501', 'Polo Bear Sweater'), else 'Generic'.\n"
            "- era: e.g. 'Y2K', '2010s', 'Modern', 'Vintage 90s'.\n"
            "- category: 'Menswear', 'Womenswear', 'Unisex', 'Kids', 'Accessories', or 'Footwear'.\n"
            "- confidence: your overall confidence.\n"
            "No explanation. Just JSON."
        )
        data = await _llm_json(system, prompt, image_b64=img_b64)

        # normalise
        result = IdentifyResult(
            brand=str(data.get("brand", "Unknown")),
            item_type=str(data.get("item_type", "")),
            model=str(data.get("model", "Generic")),
            era=str(data.get("era", "Modern")),
            category=str(data.get("category", "Unisex")),
            confidence=str(data.get("confidence", "medium")).lower(),
        )

        # store thumbnail (small preview)
        thumb = f"data:{mime};base64,{img_b64}" if len(image_bytes) < 300_000 else None

        scan = ScanDoc(
            user_id=user["id"],
            image_thumbnail=thumb,
            identify=result.model_dump(),
        )
        await db.scans.insert_one(scan.model_dump())
        await deduct_credit(user)

        # return fresh user
        fresh = await db.users.find_one({"id": user["id"]}, {"_id": 0, "hashed_password": 0})
        return {
            "scan_id": scan.id,
            "identify": result.model_dump(),
            "scans_remaining": fresh.get("scans_remaining", 0),
        }

    @router.post("/estimate")
    async def estimate(
        payload: ScanEstimateRequest,
        user: dict = Depends(get_current_user),
    ):
        scan = await db.scans.find_one({"id": payload.scan_id, "user_id": user["id"]}, {"_id": 0})
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        ident = scan["identify"]
        system = (
            "You are Margin, an expert on secondhand clothing resale markets. You know "
            "realistic SOLD (not asking) prices on eBay UK, Vinted UK, Depop, and Grailed "
            "for common streetwear, vintage, luxury and mid-market brands. Return strict JSON."
        )
        prompt = (
            f"Estimate realistic UK SOLD prices in GBP for this secondhand item.\n\n"
            f"Item: {ident.get('brand')} {ident.get('model')} — {ident.get('item_type')}\n"
            f"Era: {ident.get('era')}  Category: {ident.get('category')}\n"
            f"Condition: {payload.condition}\n"
            f"Size: {payload.size}\n"
            f"Flaws: {payload.flaws or 'None reported'}\n\n"
            "Return ONLY JSON with keys:\n"
            '{"sold_avg_gbp": number, "sold_low_gbp": number, "sold_high_gbp": number, '
            '"demand_level": "low"|"medium"|"high", '
            '"recommended_platform": "ebay"|"vinted"|"depop"|"grailed", '
            '"listing_title": string (max 80 chars, keyword-stuffed for search), '
            '"listing_description": string (5-8 short lines, bullet-style with • prefix, '
            'covering brand/model/size/condition/measurements-if-relevant/postage). '
            "No prose, no code fences.\n\n"
            "Rules: Base prices on actual sold comps you recall. Adjust down for Fair/Worn, "
            "up for New with tags. If brand='Unknown' use similar-tier estimation and keep "
            "sold_avg modest (£8–£25). Always give realistic UK numbers, not aspirational."
        )
        data = await _llm_json(system, prompt)

        platform = payload.platform or str(data.get("recommended_platform", "ebay"))
        computed = compute_estimate(
            sold_avg=float(data.get("sold_avg_gbp", 0)),
            sold_low=float(data.get("sold_low_gbp", 0)),
            sold_high=float(data.get("sold_high_gbp", 0)),
            demand_level=str(data.get("demand_level", "medium")),
            platform=platform,
            buy_price=float(payload.buy_price_gbp or 0),
        )
        result = ScanResult(
            **computed,
            listing_title=str(data.get("listing_title", ""))[:120],
            listing_description=str(data.get("listing_description", "")),
        )

        await db.scans.update_one(
            {"id": payload.scan_id},
            {"$set": {
                "estimate": result.model_dump(),
                "condition": payload.condition,
                "size": payload.size,
                "flaws": payload.flaws,
                "buy_price_gbp": payload.buy_price_gbp,
                "status": "estimated",
            }},
        )

        # track lifetime profit (only positive)
        if result.true_profit_gbp > 0:
            await db.users.update_one(
                {"id": user["id"]},
                {"$inc": {"total_profit_gbp": result.true_profit_gbp}},
            )
        return result.model_dump()

    @router.get("/history")
    async def history(user: dict = Depends(get_current_user)):
        cursor = db.scans.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).limit(100)
        items = await cursor.to_list(100)
        return items

    @router.get("/stats")
    async def stats(user: dict = Depends(get_current_user)):
        pipeline = [
            {"$match": {"user_id": user["id"], "status": "estimated"}},
            {"$group": {
                "_id": None,
                "total_scans": {"$sum": 1},
                "total_profit": {"$sum": "$estimate.true_profit_gbp"},
                "avg_flip_score": {"$avg": "$estimate.flip_score"},
                "best_flip_profit": {"$max": "$estimate.true_profit_gbp"},
            }},
        ]
        agg = await db.scans.aggregate(pipeline).to_list(1)
        row = agg[0] if agg else {}
        return {
            "total_scans": row.get("total_scans", 0),
            "total_profit_gbp": round(row.get("total_profit", 0.0) or 0.0, 2),
            "avg_flip_score": round(row.get("avg_flip_score", 0.0) or 0.0, 1),
            "best_flip_profit_gbp": round(row.get("best_flip_profit", 0.0) or 0.0, 2),
        }

    @router.patch("/{scan_id}/source")
    async def set_source(
        scan_id: str,
        payload: SourceSpotUpdate,
        user: dict = Depends(get_current_user),
    ):
        res = await db.scans.update_one(
            {"id": scan_id, "user_id": user["id"]},
            {"$set": {"source_spot": payload.source_spot}},
        )
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail="Scan not found")
        return {"ok": True}

    @router.delete("/{scan_id}")
    async def delete_scan(scan_id: str, user: dict = Depends(get_current_user)):
        await db.scans.delete_one({"id": scan_id, "user_id": user["id"]})
        return {"ok": True}

    return router
