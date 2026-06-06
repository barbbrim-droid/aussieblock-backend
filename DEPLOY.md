# Putting Aussieblock online (Render)

This is the click-by-click for taking the app live on **Render**. You'll end up
with two pieces:

| Piece | What it is | Render type | Cost |
|-------|------------|-------------|------|
| **aussieblock-api** | the backend (FastAPI + database + QuickBooks sync) | Web Service (always-on) | ~$7/mo |
| **aussieblock-app** | the phone/dispatch app people open | Static Site | free |

Why the backend isn't free: it must stay awake so the QuickBooks A/R sync runs
around the clock, and it needs a small **persistent disk** so your database and
QuickBooks token survive restarts. Free tiers sleep and wipe the disk — not OK
for a real business app.

You do this once. After it's up, every `git push` auto-deploys the change.

---

## Before you start

1. A **GitHub** account — https://github.com (free). 
2. A **Render** account — https://render.com (sign up with your GitHub login;
   it makes connecting repos one click).
3. **Git** installed locally — check by running `git --version` in PowerShell.
   If missing: https://git-scm.com/download/win (or install GitHub Desktop,
   which bundles it).

---

## Part A — Put the code on GitHub (two private repos)

We keep the backend and frontend as **two separate private repos** — each maps
1:1 to a folder you already have, so nothing has to be rearranged.

> Private matters: it keeps your code and customer data out of public view.
> (Your secrets are already gitignored and never get pushed regardless.)

### A1. Backend repo

In PowerShell:

```powershell
cd C:\Users\accou\Downloads\aussieblock-backend\aussieblock-backend
git init
git add .
git commit -m "Aussieblock backend"
```

Now create the GitHub repo and push. Easiest with the GitHub CLI (`gh`):

```powershell
gh auth login                         # one-time, follow the prompts
gh repo create aussieblock-backend --private --source=. --push
```

No `gh`? Create an empty **private** repo named `aussieblock-backend` on
github.com (don't add a README), then:

```powershell
git branch -M main
git remote add origin https://github.com/<your-username>/aussieblock-backend.git
git push -u origin main
```

### A2. Frontend repo

```powershell
cd C:\Users\accou\Downloads\aussieblock-frontend
git init
git add .
git commit -m "Aussieblock frontend"
gh repo create aussieblock-frontend --private --source=. --push
```

(or the manual `git remote add … / git push` form, same as above.)

> Sanity check: open each repo on GitHub and confirm there is **no `.env`, no
> `aussieblock.db`, and no `qbo_tokens.json`**. There shouldn't be — `.gitignore`
> excludes them — but it's worth a look since those hold your secrets.

---

## Part B — Deploy the backend

1. In Render: **New +  →  Blueprint**.
2. Connect/pick the **aussieblock-backend** repo. Render reads `render.yaml` and
   shows a service called **aussieblock-api** with a 1 GB disk.
3. It will prompt for the secret values (the ones marked "sync: false"). Open
   your local `.env` (in the backend folder) and copy each across:
   - `SECRET_KEY`
   - `QBO_CLIENT_ID`
   - `QBO_CLIENT_SECRET`
   - `QBO_REFRESH_TOKEN`  ← copy the **current** value from `qbo_tokens.json` if it
     differs from `.env` (that file holds the latest rotated token).
   - `QBO_REALM_ID`
4. Click **Apply / Create**. First build takes a few minutes.
5. When it's live, note the URL at the top — something like
   `https://aussieblock-api.onrender.com`. Open `…/health` in a browser; you
   should see `{"ok": true}`.

> The `qbo_tokens.json` note matters: your refresh token rotates over time, and
> the freshest one is in `qbo_tokens.json`, not `.env`. Use that value so the
> server connects on the first try. If QuickBooks ever rejects it, mint a new
> refresh token via the Intuit OAuth Playground (see `NEXT_STEPS.md`) and update
> the `QBO_REFRESH_TOKEN` env var in Render.

---

## Part C — Load your real data (fresh import)

The hosted database starts empty. Seed it from QuickBooks in one command using
Render's built-in shell:

1. Open the **aussieblock-api** service → **Shell** tab.
2. Run (pick your own staff password):

   ```bash
   python bootstrap_production.py "your-staff-password-here"
   ```

   This imports the QuickBooks roster, syncs ready-mix A/R, drops block-only
   customers, sets terms (Net 10 default, Landers = Net 14), and creates the
   staff login `ops@aussieblock.com` with the password you gave. It prints a
   summary as it goes.

3. Add your real customer logins, one per customer (still in the Shell):

   ```bash
   python create_login.py billing@acme.com "their-password" "Acme"
   ```

   (The last argument matches the start of the customer's name in QuickBooks.)

---

## Part D — Deploy the frontend

1. In Render: **New +  →  Blueprint** again, pick **aussieblock-frontend**.
2. It shows a static site **aussieblock-app** and prompts for **VITE_API_BASE**.
   Set it to your backend URL from Part B, e.g.
   `https://aussieblock-api.onrender.com` (no trailing slash).
3. **Apply / Create**. When it finishes you get a URL like
   `https://aussieblock-app.onrender.com` — that's the app your customers and
   staff open.

> `VITE_API_BASE` is baked in at build time. If you ever change the backend URL,
> update the variable in Render and **Manual Deploy → Clear build cache & deploy**
> the frontend so it rebuilds with the new value.

---

## Part E — Verify it's working

Open the frontend URL and check:

- **Staff** login (`ops@aussieblock.com` + the password from Part C) → dispatch
  board loads (stat tiles, fleet map, orders).
- **Customer** login (one you made in Part C) → phone app → **Account** screen
  shows their balance + invoices.
- Tap **Make a payment** (or an open invoice) → a QuickBooks-hosted pay page
  opens in a new tab. (You don't have to complete a payment to confirm the link
  works.)

---

## Later (not required to launch)

- **Custom domain.** Both services start on free `*.onrender.com` URLs. To use
  e.g. `app.aussieblock.com`, add the domain under the static site's **Settings →
  Custom Domains** and point a CNAME at Render (they show you the exact record).
- **Tighten CORS.** The backend currently allows any origin (`allow_origins=["*"]`
  in `app/main.py`). Once the frontend has a fixed domain, swap `*` for that one
  URL.
- **Auto-deploy.** Both services redeploy automatically on `git push` to `main`.
