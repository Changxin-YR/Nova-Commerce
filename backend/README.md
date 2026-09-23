# Nova Commerce Backend

See repository root README and docs/architecture.

## Outbox worker

With MySQL and Redis running and the schema migrated, start a worker and a beat
process in separate terminals from `backend/`:

```powershell
& ..\.venv\Scripts\celery.exe -A app.celery_app:celery_app worker --pool=solo --loglevel=info
& ..\.venv\Scripts\celery.exe -A app.celery_app:celery_app beat --loglevel=info
```

Beat scans due outbox rows every five seconds. The worker writes events to the
`nova:<APP_ENV>:outbox:events:v1` Redis Stream. Each entry carries the database
`event_id`; consumers must deduplicate by this id because a Redis write can
succeed before the MySQL publication-state commit. The Stream is unbounded until
a consumer retention policy is established; monitor its length and storage.
