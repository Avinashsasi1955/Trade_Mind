# Run in VS Code

## Quick local preview

1. Extract the archive and open the extracted folder in VS Code.
2. Open the integrated terminal.
3. Install/use Python 3.12, then create and activate a Python environment:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env`. For the portable SQLite preview, keep `DATABASE_URL` and `REDIS_URL` blank. Keep all real credentials out of source control.
5. Start the application:

```bash
python -m backend.server
```

6. Open `http://127.0.0.1:4173`.

Demo login: `arjun@example.com` / `nivesh123`.

## Verify

```bash
python -m unittest discover -s tests -q
```

The portable archive contains the active local SQLite databases for offline research and preview. PostgreSQL production deployment requires PostgreSQL/Redis, the migrations under `migrations/postgres`, and environment-specific `DATABASE_URL`/`REDIS_URL`. Live order submission remains locked by design.
