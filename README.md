# margin — pocket calculator for flipping clothes

A mobile-first web app for secondhand-clothing resellers on eBay, Vinted,
Depop and Grailed. Snap a photo, get instant AI identification, sold-price
estimates, fees + true profit, a flip score, and a copy-paste listing.

## What it does

1. **Photo scan** → user uploads or takes a picture of a clothing item.
2. **AI identifies** → **Google Gemini 3 Flash Preview** (vision) returns
   brand, item type, model, era, category and confidence.
3. **User confirms** condition (5 tiers), size, flaws, buy price.
4. **AI prices** → Gemini estimates realistic UK sold prices on eBay/Vinted/
   Depop/Grailed, generates a keyword-optimised listing title + description.
5. **Backend calculates** platform fees, postage, true profit, suggested max
   buy price, and a 1-10 flip score.
6. **Scans are saved** to the user's dashboard with lifetime profit tracked.

## Tech stack

- **Frontend**: React 19 (CRA + craco), Tailwind CSS, Shadcn UI, lucide-react,
  sonner (toasts), axios, react-router 7.
- **Backend**: FastAPI + motor (async MongoDB) + bcrypt/PyJWT for auth.
- **AI**: Google **Gemini 3 Flash Preview** via the `emergentintegrations`
  Python library (image analysis + JSON-mode text estimation).
- **Payments**: Stripe (managed payments / SMP) with a claimable sandbox key.
- **DB**: MongoDB.

## Local setup

### Prerequisites

- Node.js 20+ and **yarn** (do NOT use npm — resolutions won't apply)
- Python 3.11+
- MongoDB running locally (or a Mongo Atlas connection string)

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Install the emergentintegrations library (custom index)
pip install emergentintegrations --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/

# Create .env from the example and fill in the values
cp .env.example .env
# then edit backend/.env with your keys (see "Environment variables" below)

# (Optional) create Stripe products/prices — only needed once per account
python setup_stripe.py

# Start the API
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

### 2. Frontend

```bash
cd frontend
yarn install
cp .env.example .env
# edit REACT_APP_BACKEND_URL to point at http://localhost:8001 for local dev
yarn start
```

Open http://localhost:3000 in your browser.

## Environment variables

### `backend/.env`

| Key | Required | Description |
| --- | --- | --- |
| `MONGO_URL` | ✅ | MongoDB connection string, e.g. `mongodb://localhost:27017` |
| `DB_NAME` | ✅ | MongoDB database name, e.g. `margin` |
| `CORS_ORIGINS` | ✅ | Comma-separated allowed origins or `*` |
| `JWT_SECRET` | ✅ | Long random string for JWT signing (generate with `openssl rand -hex 32`) |
| `EMERGENT_LLM_KEY` | ✅ | Universal Emergent LLM key — used for Gemini 3 Flash. Get one at emergent.sh (Profile → Universal Key). Alternatively, swap the `emergentintegrations` calls for the official Google `google-genai` SDK and use a Google AI Studio key. |
| `STRIPE_SECRET_KEY` | ✅ | Stripe secret key (`sk_test_...` for testing). Get one at https://dashboard.stripe.com/apikeys |
| `STRIPE_PUBLISHABLE_KEY` | ⚪ | Not used by the backend directly; kept for reference |
| `STRIPE_WEBHOOK_SECRET` | ✅ (for live) | Webhook signing secret from Stripe dashboard |
| `STRIPE_ACCOUNT_ID` | ⚪ | For platform accounts; safe to leave unset |
| `STRIPE_MODE` | ⚪ | `test` or `live` |

### `frontend/.env`

| Key | Required | Description |
| --- | --- | --- |
| `REACT_APP_BACKEND_URL` | ✅ | Fully-qualified URL of the backend, e.g. `http://localhost:8001` (dev) or `https://api.your-domain.com` (prod). No trailing slash. |
| `WDS_SOCKET_PORT` | ⚪ | Only needed behind a reverse proxy. `443` for HTTPS preview. |

## AI integration details

**Provider**: Google Gemini
**Model**: `gemini-3-flash-preview` (vision-capable)
**Library**: `emergentintegrations.llm.chat.LlmChat`

All AI calls live in **`backend/vision.py`**. Two calls per scan:

### 1. Identify — image → structured brand/model JSON
- **System prompt**: "You are Margin, a clothing reseller expert…"
- **User prompt**: asks for JSON with keys `brand, item_type, model, era, category, confidence`. Full text is in `identify()` inside `vision.py`.
- **Image**: JPEG/PNG/WEBP up to 8 MB, sent as base64 via `ImageContent`.

### 2. Estimate — brand/condition → sold prices + listing JSON
- **System prompt**: "You are Margin, an expert on secondhand clothing resale markets…"
- **User prompt**: passes the identified item + condition + size + flaws and asks for JSON with keys `sold_avg_gbp, sold_low_gbp, sold_high_gbp, demand_level, recommended_platform, listing_title, listing_description`. Full text in `estimate()` inside `vision.py`.
- **No image** on this call — text only.

### Swapping to your own Google API key

If you want to drop the emergentintegrations dependency, replace the two calls
in `vision.py` with the standard [google-genai](https://pypi.org/project/google-genai/)
SDK:

```python
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
part = types.Part.from_bytes(data=image_bytes, mime_type=mime)
resp = client.models.generate_content(
    model="gemini-3-flash",
    contents=[part, prompt],
    config=types.GenerateContentConfig(response_mime_type="application/json"),
)
data = json.loads(resp.text)
```

The rest of the pipeline (fee calc, flip score, storage) stays unchanged.

## Pricing / Stripe setup

- Products/prices are created by `backend/setup_stripe.py` — run it once
  against your Stripe account. It creates three prices with these lookup keys:
  - `margin_pack_15` — £3 one-time, 15 scans
  - `margin_pack_150` — £20 one-time, 150 scans
  - `margin_unlimited_monthly` — £15/month subscription
- The backend uses **Stripe Managed Payments (SMP)** by default. To disable
  SMP, edit `payments.py` and remove `managed_payments={"enabled": True}` from
  the `stripe.checkout.Session.create` call.
- Point your Stripe webhook at `POST /api/payments/webhook` and set
  `STRIPE_WEBHOOK_SECRET` to the signing secret from Stripe.

## API surface

All endpoints prefixed with `/api`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | — | email + password → `{token, user}` |
| POST | `/api/auth/login` | — | email + password → `{token, user}` |
| GET | `/api/auth/me` | Bearer | current user |
| POST | `/api/scan/identify` | Bearer | multipart image → identification + `scan_id`, deducts 1 credit |
| POST | `/api/scan/estimate` | Bearer | `{scan_id, condition, size, flaws, buy_price_gbp}` → prices + listing |
| GET | `/api/scan/history` | Bearer | list of user scans |
| GET | `/api/scan/stats` | Bearer | totals for dashboard |
| PATCH | `/api/scan/{id}/source` | Bearer | log where the item was sourced |
| DELETE | `/api/scan/{id}` | Bearer | delete a scan |
| GET | `/api/payments/plans` | — | list of pricing plans |
| POST | `/api/payments/checkout` | Bearer | create a Stripe Checkout session |
| GET | `/api/payments/status/{session_id}` | — | poll payment status |
| POST | `/api/payments/webhook` | Stripe | fulfilment webhook |

## Deployment

- **Backend**: any host that runs Python 3.11 + uvicorn (Fly.io, Render, Railway).
- **Frontend**: any static host (Vercel, Netlify, Cloudflare Pages). Set
  `REACT_APP_BACKEND_URL` to your deployed backend origin.
- **Mongo**: MongoDB Atlas free tier is plenty for a launch.

## Licence

Do what you want with it. It's your business.
