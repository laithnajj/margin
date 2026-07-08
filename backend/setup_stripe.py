"""One-shot catalog setup for Stripe. Idempotent."""
import os
import stripe
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

CATALOG = [
    {
        "emergent_product_id": "margin_pack_15",
        "name": "Margin - 15 Scan Pack",
        "tax_code": "txcd_10000000",
        "prices": [
            {"lookup_key": "margin_pack_15", "amount": 300, "currency": "gbp"},
        ],
    },
    {
        "emergent_product_id": "margin_pack_150",
        "name": "Margin - 150 Scan Pack",
        "tax_code": "txcd_10000000",
        "prices": [
            {"lookup_key": "margin_pack_150", "amount": 2000, "currency": "gbp"},
        ],
    },
    {
        "emergent_product_id": "margin_unlimited_sub",
        "name": "Margin - Unlimited Monthly",
        "tax_code": "txcd_10103001",
        "prices": [
            {"lookup_key": "margin_unlimited_monthly", "amount": 1500, "currency": "gbp", "interval": "month"},
        ],
    },
]


def get_or_create_product(entry):
    for p in stripe.Product.list(active=True).auto_paging_iter():
        if p.to_dict().get("metadata", {}).get("emergent_product_id") == entry["emergent_product_id"]:
            return p
    return stripe.Product.create(
        name=entry["name"],
        tax_code=entry.get("tax_code"),
        metadata={"managed_by": "emergent", "emergent_product_id": entry["emergent_product_id"]},
    )


def upsert_price(product, p):
    existing = stripe.Price.list(lookup_keys=[p["lookup_key"]], active=True, limit=1).data
    if existing and (existing[0].unit_amount != p["amount"] or existing[0].currency != p["currency"]):
        stripe.Price.modify(existing[0].id, active=False)
        existing = []
    if not existing:
        kwargs = dict(
            product=product.id,
            unit_amount=p["amount"],
            currency=p["currency"],
            lookup_key=p["lookup_key"],
            transfer_lookup_key=True,
        )
        if p.get("interval"):
            kwargs["recurring"] = {"interval": p["interval"]}
        price = stripe.Price.create(**kwargs)
        print(f"Created price {p['lookup_key']} => {price.id}")
    else:
        print(f"Price {p['lookup_key']} already exists => {existing[0].id}")


for entry in CATALOG:
    product = get_or_create_product(entry)
    print(f"Product: {product.name} => {product.id}")
    for p in entry["prices"]:
        upsert_price(product, p)

print("Catalog setup complete.")
