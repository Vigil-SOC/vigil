from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from typing import Optional
from services.api.routers.timeline import normalize_timestamp, _aware
app = FastAPI()
@app.get("/t")
def t(start: Optional[datetime] = Query(None)):
    return {"v": _aware(start).isoformat() if start else None}
c = TestClient(app)
for s in ["2026-10-22T00:00:00.000Z","2026-10-22T00:00:00","2026-10-22T05:30:00+05:30","2026-10-22","2026-10-22 00:00:00","20261022T000000","1700000000","not-a-date",""]:
    try: old = normalize_timestamp(s).isoformat()
    except Exception as e: old = f"500 ({type(e).__name__})"
    r = c.get("/t", params={"start": s})
    new = r.json()["v"] if r.status_code == 200 else str(r.status_code)
    print(f"{s!r:32} old={old:32} new={new}")
