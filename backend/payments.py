import os
from datetime import datetime, timezone, timedelta
from typing import Callable
from fastapi import APIRouter, Depends, Request, HTTPException
import stripe

from models import CheckoutRequest

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# lookup_key -> credit_amount OR None if subscription
PACK_CREDITS = {
    "margin_pack_15": 15,
    "margin_pack_150": 150,
    "margin_unlimited_monthly": None,  # subscription
}


def build_router(db, get_current_user: Callable):
    router = APIRouter(prefix="/payments", tags=["payments"])

    @router.get("/plans")
    async def plans():
        return {
            "plans": [
                {"lookup_key": "margin_pack_15", "name": "15 Scans", "price_gbp": 3.00, "scans": 15, "type": "one_time"},
                {"lookup_key": "margin_pack_150", "name": "150 Scans", "price_gbp": 20.00, "scans": 150, "type": "one_time"},
                {"lookup_key": "margin_unlimited_monthly", "name": "Unlimited", "price_gbp": 15.00, "scans": -1, "type": "subscription"},
            ]
        }

    @router.post("/checkout")
    async def create_checkout(payload: CheckoutRequest, user: dict = Depends(get_current_user)):
        if payload.lookup_key not in PACK_CREDITS:
            raise HTTPException(status_code=400, detail="Unknown plan")

        prices = stripe.Price.list(lookup_keys=[payload.lookup_key], active=True, limit=1).data
        if not prices:
            raise HTTPException(status_code=500, detail=f"Price not found: {payload.lookup_key}")
        price = prices[0]

        kwargs = dict(
            line_items=[{"price": price.id, "quantity": 1}],
            mode="subscription" if price.recurring else "payment",
            success_url=f"{payload.origin_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{payload.origin_url}/payment/cancel",
            metadata={"user_id": user["id"], "lookup_key": payload.lookup_key},
        )
        try:
            session = stripe.checkout.Session.create(**kwargs, managed_payments={"enabled": True})
        except stripe.error.InvalidRequestError as e:
            msg = (getattr(e, "user_message", "") or "").lower()
            if "managed payments" in msg or "ineligible" in msg:
                session = stripe.checkout.Session.create(
                    **kwargs, automatic_tax={"enabled": True}, billing_address_collection="required",
                )
            else:
                raise

        await db.payment_transactions.insert_one({
            "session_id": session.id,
            "user_id": user["id"],
            "lookup_key": payload.lookup_key,
            "amount": (price.unit_amount or 0),
            "currency": price.currency,
            "status": "initiated",
            "payment_status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"checkout_url": session.url, "session_id": session.id}

    async def _fulfill(session_id: str, user_id: str, lookup_key: str):
        # Guard: only fulfill once
        record = await db.payment_transactions.find_one({"session_id": session_id})
        if record and record.get("fulfilled"):
            return
        credits = PACK_CREDITS.get(lookup_key)
        if credits is None:
            # subscription: extend for 30 days from now (or from current expiry)
            user = await db.users.find_one({"id": user_id}, {"_id": 0})
            now = datetime.now(timezone.utc)
            base = now
            if user and user.get("subscription_expires_at"):
                try:
                    cur = datetime.fromisoformat(user["subscription_expires_at"])
                    if cur > now:
                        base = cur
                except Exception:
                    pass
            new_exp = (base + timedelta(days=30)).isoformat()
            await db.users.update_one(
                {"id": user_id},
                {"$set": {"is_subscribed": True, "subscription_expires_at": new_exp}},
            )
        else:
            await db.users.update_one(
                {"id": user_id},
                {"$inc": {"scans_remaining": credits}},
            )
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"fulfilled": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    @router.get("/status/{session_id}")
    async def status(session_id: str):
        record = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if not record:
            raise HTTPException(status_code=404, detail="Session not found")
        if record.get("payment_status") != "paid":
            try:
                s = stripe.checkout.Session.retrieve(session_id)
                if s.payment_status == "paid" or s.status == "complete":
                    await db.payment_transactions.update_one(
                        {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                        {"$set": {
                            "status": "completed",
                            "payment_status": "paid",
                            "stripe_subscription_id": s.subscription,
                            "stripe_payment_intent_id": s.payment_intent,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                    )
                    await _fulfill(session_id, record["user_id"], record["lookup_key"])
                    record = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
            except stripe.error.StripeError:
                pass
        return {
            "session_id": record["session_id"],
            "status": record.get("status"),
            "payment_status": record.get("payment_status"),
            "lookup_key": record.get("lookup_key"),
        }

    @router.post("/webhook")
    async def stripe_webhook(request: Request):
        payload = await request.body()
        sig = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")
        obj = event["data"]["object"]
        t = event["type"]
        if t == "checkout.session.completed":
            await db.payment_transactions.update_one(
                {"session_id": obj["id"], "payment_status": {"$ne": "paid"}},
                {"$set": {
                    "status": "completed",
                    "payment_status": obj.get("payment_status", "paid"),
                    "stripe_subscription_id": obj.get("subscription"),
                    "stripe_payment_intent_id": obj.get("payment_intent"),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            record = await db.payment_transactions.find_one({"session_id": obj["id"]})
            if record:
                await _fulfill(obj["id"], record["user_id"], record["lookup_key"])
        elif t == "checkout.session.async_payment_succeeded":
            await db.payment_transactions.update_one(
                {"session_id": obj["id"]},
                {"$set": {"payment_status": "paid", "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
            record = await db.payment_transactions.find_one({"session_id": obj["id"]})
            if record:
                await _fulfill(obj["id"], record["user_id"], record["lookup_key"])
        elif t == "checkout.session.expired":
            await db.payment_transactions.update_one(
                {"session_id": obj["id"]},
                {"$set": {"status": "expired", "payment_status": "expired",
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
        return {"status": "ok"}

    return router
