"""Smart IV backend. Run:  uvicorn app:app --reload   then open http://localhost:8000"""
import sqlite3
import time
from contextlib import closing
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from classifier import classify

DB_PATH = "iv.db"
WINDOW_S = 30            # how much history the classifier sees
ALERT_COOLDOWN_S = 60    # don't re-alert the same device+type within this time
BEDS = {"iv-01": "Bed 1", "iv-02": "Bed 2"}   # hardcoded device -> bed

app = FastAPI(title="Smart IV Monitor")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS readings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            device_ts REAL,
            received_at REAL NOT NULL,
            drip_count INTEGER,
            last_drip_interval_ms REAL,
            time_since_last_drip_ms REAL,
            weight_g REAL,
            label TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_readings_dev_time ON readings(device_id, received_at);
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            bed_id TEXT,
            type TEXT NOT NULL,
            raised_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'   -- open | confirmed | dismissed
        );
        CREATE TABLE IF NOT EXISTS feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL REFERENCES alerts(id),
            action TEXT NOT NULL,                 -- confirm | dismiss
            created_at REAL NOT NULL
        );
        """)
        c.commit()


init_db()


class Reading(BaseModel):
    device_id: str
    ts: Optional[float] = None
    drip_count: int
    last_drip_interval_ms: Optional[float] = None
    time_since_last_drip_ms: float
    weight_g: float


class Feedback(BaseModel):
    action: Literal["confirm", "dismiss"]


@app.post("/readings")
def post_reading(r: Reading):
    now = time.time()
    with closing(db()) as c:
        c.execute(
            "INSERT INTO readings(device_id, device_ts, received_at, drip_count,"
            " last_drip_interval_ms, time_since_last_drip_ms, weight_g)"
            " VALUES (?,?,?,?,?,?,?)",
            (r.device_id, r.ts, now, r.drip_count, r.last_drip_interval_ms,
             r.time_since_last_drip_ms, r.weight_g),
        )
        reading_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        rows = c.execute(
            "SELECT drip_count, last_drip_interval_ms, time_since_last_drip_ms, weight_g"
            " FROM readings WHERE device_id=? AND received_at>=? ORDER BY received_at",
            (r.device_id, now - WINDOW_S),
        ).fetchall()
        label = classify([dict(x) for x in rows])
        c.execute("UPDATE readings SET label=? WHERE id=?", (label, reading_id))

        if label != "normal":
            recent = c.execute(
                "SELECT 1 FROM alerts WHERE device_id=? AND type=? AND raised_at>=?",
                (r.device_id, label, now - ALERT_COOLDOWN_S),
            ).fetchone()
            if not recent:
                c.execute(
                    "INSERT INTO alerts(device_id, bed_id, type, raised_at) VALUES (?,?,?,?)",
                    (r.device_id, BEDS.get(r.device_id, r.device_id), label, now),
                )
        c.commit()
    return {"label": label}


@app.get("/status")
def status():
    with closing(db()) as c:
        rows = c.execute(
            "SELECT r.* FROM readings r JOIN"
            " (SELECT device_id, MAX(id) AS mid FROM readings GROUP BY device_id) m"
            " ON r.id = m.mid"
        ).fetchall()
    return [{**dict(x), "bed_id": BEDS.get(x["device_id"], x["device_id"])} for x in rows]


@app.get("/alerts")
def alerts():
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 50").fetchall()
    return [dict(x) for x in rows]


@app.post("/alerts/{alert_id}/feedback")
def post_feedback(alert_id: int, fb: Feedback):
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone():
            raise HTTPException(404, "alert not found")
        c.execute("INSERT INTO feedback(alert_id, action, created_at) VALUES (?,?,?)",
                  (alert_id, fb.action, time.time()))
        c.execute("UPDATE alerts SET status=? WHERE id=?",
                  ("confirmed" if fb.action == "confirm" else "dismissed", alert_id))
        c.commit()
    return {"ok": True}


DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><title>Smart IV</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px;max-width:900px}
.card{display:inline-block;border:1px solid #ccc;border-radius:8px;padding:12px;margin:6px;min-width:200px}
.normal{background:#e8f7e8}.bad{background:#fde2e2}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
</style></head><body>
<h2>Smart IV Monitor</h2>
<button onclick="soundOn=true;beep()">Enable alert sound</button>
<h3>Beds</h3><div id="beds"></div>
<h3>Alerts</h3><table id="alerts"></table>
<script>
let soundOn=false,lastSeen=0;
function beep(){if(!soundOn)return;const a=new AudioContext(),o=a.createOscillator();
o.connect(a.destination);o.frequency.value=880;o.start();setTimeout(()=>o.stop(),300);}
async function act(id,action){await fetch('/alerts/'+id+'/feedback',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});refresh();}
async function refresh(){
 const st=await (await fetch('/status')).json();
 document.getElementById('beds').innerHTML=st.map(s=>`<div class="card ${s.label==='normal'?'normal':'bad'}">
  <b>${s.bed_id}</b> (${s.device_id})<br>Status: ${s.label}<br>Weight: ${s.weight_g} g<br>
  Since last drip: ${Math.round(s.time_since_last_drip_ms)} ms</div>`).join('');
 const al=await (await fetch('/alerts')).json();
 document.getElementById('alerts').innerHTML='<tr><th>Time</th><th>Bed</th><th>Type</th><th>Status</th><th></th></tr>'+
  al.map(a=>`<tr><td>${new Date(a.raised_at*1000).toLocaleTimeString()}</td><td>${a.bed_id}</td><td>${a.type}</td>
  <td>${a.status}</td><td>${a.status==='open'?`<button onclick="act(${a.id},'confirm')">Confirm</button>
  <button onclick="act(${a.id},'dismiss')">Dismiss</button>`:''}</td></tr>`).join('');
 const newest=al.length?al[0].id:0; if(newest>lastSeen&&lastSeen>0)beep(); lastSeen=Math.max(lastSeen,newest);
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD