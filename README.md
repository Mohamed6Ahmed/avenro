# Avenro (Windows)

Put this folder at `D:\website\avenro`, then **double-click `start.bat`**. It checks for Python, creates the database, starts the server and opens http://localhost:8000.
Python 3.9+ is the only requirement (https://www.python.org/downloads/, tick "Add python.exe to PATH"). Nothing else to install.

    avenro\
    ├── start.bat          run everything
    ├── reset-db.bat       rebuild the database from db\seed.sql (asks first, deletes accounts and orders)
    ├── server.py          web server + API + SQLite
    ├── .env.example       copy to ".env" and fill in your Paymob keys
    ├── public\index.html  the website
    ├── db\                schema.sql, seed.sql (products, prices), avenro.db (created on first run)
    └── assets\images\     product photos

## What's new
- **Google sign-in** ("Continue with Google") next to email + password. See `.env.example` for the two keys.
- **Payment methods**: Visa / Mastercard (Paymob), Vodafone Cash (Paymob wallet, or manual transfer), InstaPay (manual transfer), cash on delivery. Each shows only when configured.
- **Manual payments**: the customer sees your number / InstaPay address, sends the money, enters the transfer reference. You open `http://localhost:8000/admin.html`, enter `ADMIN_TOKEN`, check the money arrived and press Confirm (or Reject, which releases the stock). A transfer reference can only be used once.
- **Nothing hard-coded on the page**: shop tabs come from the `collections` table, the carousel and the "In this look" panel (piece count, prices, button text, subtitle) come from `looks` / `look_items` in `db\seed.sql`. Add a look there and run `reset-db.bat`.
- New product: Avenro Signature Hoodie — Graphite (logo print). Price is a placeholder.

## Accounts
Customers register and sign in from the account button in the header. Passwords are stored as salted PBKDF2 hashes, the session is an HttpOnly cookie (30 days), and repeated wrong passwords are throttled. Checkout requires an account. "Your orders" in the account window shows order history.
Not built yet: "forgot password" (needs an email service).

## Payments
Two ways to pay at checkout:
- **Cash on delivery** (`ENABLE_COD=1`): the order is confirmed immediately.
- **Card online through Paymob**: the server creates a Paymob payment (Intention API), sends the customer to Paymob's hosted checkout, and marks the order paid only after Paymob's signed webhook (HMAC-SHA512) or signed return redirect confirms the exact amount and currency. Card details never touch this server.

Until all four `PAYMOB_*` values exist in `.env`, the card option opens a **fake test page** (no money moves). The start-up message says which mode is active.

### Switching on real Paymob
1. Copy `.env.example` to `.env` and fill in `PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_INTEGRATION_ID` (your card integration) and `PAYMOB_HMAC_SECRET` from the Paymob dashboard. Start with the **test** keys.
2. In the Paymob dashboard set the integration's *Transaction processed callback* to `https://YOUR-SITE/api/payments/paymob/webhook`. Paymob cannot reach `localhost`: while testing on your PC use a tunnel (e.g. ngrok) and put that address in `PUBLIC_URL`.
3. Pay with Paymob's test card and check the order shows as Paid under "Your orders".
4. For a real website: `HOST=0.0.0.0`, `PUBLIC_URL=https://your-domain`, `COOKIE_SECURE=1`, put it behind https (e.g. Caddy or nginx), then switch to the live keys.

Prices in `db\seed.sql` are placeholders in the currency set by `CURRENCY` (default EGP). Paymob Egypt accounts charge in EGP. Online orders hold stock for 30 minutes; unpaid ones are then cancelled and the stock is released.

## Edit products and prices
Change `db\seed.sql`, then run `reset-db.bat` (this also wipes accounts and orders). Photos go in `assets\images\...` and are named in the seed file.

## Anime "Codex" designs
The Itachi / Luffy / Zoro / Gojo / Eren / Levi / Guts hoodies use characters owned by other companies. They are in the database but switched off (`active = 0`), so they do not appear on the site. Selling them needs licences from the rights holders; once you have them, set `active` to 1 for those rows in `db\seed.sql` (and add `'codex'` to the tabs in `public\index.html`).

## Troubleshooting
- Open the site through `start.bat` (the `http://localhost` address), not by double-clicking `index.html`.
- Port busy: the server automatically tries the next ports (8001, 8002, ...) and prints the address.
- Windows Firewall: by default the server only listens on your own PC (127.0.0.1), so no prompt should appear.
- A payment shows "Awaiting payment" after paying: the webhook has not arrived. Check the callback URL and `PUBLIC_URL`, and the server window for "!!" lines.

## API
Public: GET `/api/config`, `/api/products`, `/api/collections`, `/api/looks` · POST `/api/subscribe`
Google: GET `/api/auth/google/start`, `/api/auth/google/callback`
Manual payments: POST `/api/orders/proof` · admin (header `X-Admin-Token`): GET `/api/admin/orders`, POST `/api/admin/orders/confirm|reject`
Accounts: POST `/api/auth/register`, `/api/auth/login`, `/api/auth/logout` · GET `/api/auth/me`
Orders (signed in): POST `/api/orders` (prices and totals are always computed from the database) · GET `/api/orders/mine`
Payments: POST `/api/payments/paymob/webhook` · GET `/api/payments/paymob/return` · (test mode only) POST `/api/payments/mock/confirm`
