# Letit — FastAPI common backend (first milestone)

This is **working initial source code**, not the full 26-API implementation.

## Already implemented

- `/health` (FastAPI server running)
- `/health/db` (database ping)
- 8 DB models (UUID IDs, stepId/order, missionType, placeCategory, estimatedDurationMinutes)
- `POST /api/v1/auth/signup`, `POST /api/v1/auth/login` (JWT + Argon2 hashing)
- `PUT /api/v1/users/me/defaults`, `GET /api/v1/users/me/defaults`
- FastAPI Swagger UI at `/docs`

Remaining **22 functional endpoints**, AI mission generation, Kakao integration, and full daily/flower logic are NOT implemented.

## Windows (PowerShell) Quick Start

Install Python 3.12 or 3.13. Open VS Code terminal in the unzipped folder.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Open `.env` and replace `JWT_SECRET_KEY` with a long unpredictable string. For local testing leave `DATABASE_URL=sqlite:///./letit_local.db` unchanged.

```powershell
python -m app.init_db
python -m uvicorn app.main:app --reload
```

Go to <http://127.0.0.1:8000/docs>, <http://127.0.0.1:8000/health>, <http://127.0.0.1:8000/health/db>.

In a separate terminal, run:

```powershell
python -m pytest -q
```

If PowerShell refuses activation, use `.venv\Scripts\python.exe` instead of `python` in the commands; activation is optional.

## Supabase PostgreSQL — actual connection

1. Create a Supabase project (one project shared by both backend developers).
2. Dashboard → **Connect** → copy **Session pooler** connection string for Postgres (or Direct connection if IPv6/your network supports it). Prefer port 5432 session pooler for a long-running FastAPI server.
3. Edit your local `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres.PROJECT_REF:URL_ENCODED_DB_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
JWT_SECRET_KEY=REPLACE_WITH_LONG_RANDOM_SECRET
CORS_ORIGINS=http://localhost:5173,https://YOUR-VERCEL-SITE.vercel.app
```

**Use the real URL provided by Supabase, not this fictional example.** URL-encode special password characters (or choose a generated password with connection string safe characters). Never send your DB password or service role key into public chats or commit them.
4. In **Supabase SQL Editor**, paste and run `sql/001_schema.sql` once. Do not use the local SQLite file for your shared DB.
5. Restart FastAPI and open `/health/db` → expected `{"database":"connected"}`. Then use Swagger UI to sign up and test `GET /api/v1/users/me/defaults`.

The SQL Editor execution, network authentication, and Supabase connection **have not been performed by the assistant**. They require your Supabase project and private credentials.

## Collaboration

- Share this folder with both developers. One person owns `app/models.py` and `sql/001_schema.sql`.
- Developer A can extend `routers/auth.py`, `routers/users.py`, then mission progress/records/flowers.
- Developer B can develop new `routers/recommendations.py`, `routers/places.py`, AI/Kakao services; both import the SAME models.
- On merge, ensure routes are registered in `app/main.py`. `GET /missions/resumable` should be registered before `GET /missions/{missionId}` when they share a router.
- Frontend base URL: `VITE_API_BASE_URL=https://YOUR-BACKEND-DOMAIN/api/v1`. `localhost` does not work across devices.
- API requests and responses must match the Notion contract. OpenAPI schema is auto-generated at `/openapi.json`.

## Key constraints

- No secrets in Vite public `VITE_` variables.
- CORS is not authentication. All private endpoints need JWT and owner checks.
- Archive deletion is a *hide*, not a physical delete; acquired flowers persist.
- Mission `paused` and day `partial` are different concepts.
- Test with synthetic data, not real sensitive diary or location data.

## Supabase security note

SQL enables row-level security on all eight tables without public policies. The browser should **never** query these tables directly using a Supabase anon key; all reads/writes flow through authenticated FastAPI routes. The backend uses its private PostgreSQL connection. Never put `DATABASE_URL` in frontend code.
