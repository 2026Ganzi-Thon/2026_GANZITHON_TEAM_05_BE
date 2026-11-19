# B PR integration — required before the end-to-end demo

**Do not merge B PR #1 without making the mission completion → calendar/flower integration.**

A's new endpoints read the existing 8 tables; no DB migration is needed.

## 1. Merge order / router registration

- B PR #1 introduces `app/routers/missions.py`, `recommendations.py`, `alternatives.py`, `places.py` and updates `app/main.py`.
- A's overlay introduces `app/routers/records.py`, `collection.py`, `app/services/record_service.py`, `flower_service.py` and also updates `app/main.py`.
- Merge `main.py` by **keeping every B router and additionally adding**:

```python
from app.routers import records, collection
app.include_router(records.router, prefix='/api/v1')
app.include_router(collection.router, prefix='/api/v1')
```

- On actual merge, **do not overwrite B's main.py with A's sample main.py**; that would remove B's routes. Prefer adding the two import/registration lines above.
- B's static `/missions/resumable` route must remain before `/{mission_id}`.

## 2. B `app/routers/missions.py` hook — completion

Add at module top:

```python
from app.services.record_service import on_mission_changed, activity_date
```

In the `complete_mission` handler, before `mission.status = 'completed'`:

```python
previous_day = activity_date(mission)
```

After assigning `status` and `completed_at` and before `db.commit()`:

```python
projection = on_mission_changed(db, mission, previous_date=previous_day)
```

Optionally expand `CompletedOut` to match the Notion contract:

```python
from pydantic import Field

# add to existing CompletedOut model:
daily_status: str = Field(alias='dailyStatus')
new_flower_pending: bool = Field(alias='newFlowerPending')
```

And in its return value add:

```python
dailyStatus=projection['dailyStatuses'][activity_date(mission).isoformat()],
newFlowerPending=bool(projection['newFlowerNumbers']),
```

**Important:** The helper **does not call commit**; B's existing `db.commit()` persists all changes together. Call exactly once per state change.

## 3. B hooks — other state changes

After changing mission status and before each existing `db.commit()`:

- `start_mission`: `on_mission_changed(db, mission)` after flushing new mission.
- `save_mission_progress`: `on_mission_changed(db, mission)` after `status='paused'`.
- `resume_mission`: `on_mission_changed(db, mission)` after `status='in_progress'`.
- `abandon_mission`: `on_mission_changed(db, mission)` after `status='abandoned'`.

These update the calendar snapshot but **never issue new flowers** unless the status is `completed`.

For `alternatives.py`, after changing `old_mission.status='replaced'`, adding the new mission and `db.flush()`, call `on_mission_changed(db, old_mission)` and `on_mission_changed(db, new_mission)` before commit. This excludes the old mission from reflection/calendars.

If B and A are already merged, all calls are small additions; **no SQL schema modification**.

## 4. Test (isolated SQLite)

```bash
python -m pytest -q tests/test_a_endpoints.py
python -m pytest -q tests/test_app.py
```

Run each file separately because the existing test file and new test file use independent temporary SQLite databases with environment variables fixed at import time.

Integration smoke test on a local test account:
1. Signup/login.
2. B recommends and starts a mission.
3. B checks five steps and calls `/complete`.
4. A `/reflection/calendar` returns `completed` on the completion date.
5. Complete missions on 3 distinct dates using a dedicated synthetic test DB.
6. A `/collection/flowers` returns the first flower with `pendingClaimCount=1`.
7. A `/claim` acknowledges it; repeating the call does not grant new flowers.

For shared Supabase, do not alter system date or insert artificial production mission records just to test the flower rule. The tests above use isolated SQLite.

## 5. Known choices

- `paused`: mission progress state; `partial`: day-level reflection state.
- A's archived deletion is a soft hide; statistics and previously awarded flowers remain.
- A counts completions by KST `completed_at` date. A paused mission is counted on its KST start date.
- `ever_completed` records whether a local date **ever** qualified as fully complete, to avoid revoking awarded flowers. The live calendar status can later become `partial`.
- `GET /reflection/calendar` returns only dates with a record. Frontend displays missing dates as `undecided`.
- `GET /reflection/summary` counts completed/partial **mission records**; `notCompleted` is a **rest day** count. Revisit only if UI wants a different unit.
- `GET /archive/{id}` disallows viewing hidden/replaced records; `GET /missions/{id}` belongs to B and may have different access rules.
