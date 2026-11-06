import os, json, shutil
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import db, pipeline, extract

HERE = os.path.dirname(os.path.abspath(__file__)); DEMO = os.path.join(HERE, "demo")
db.init()
for f in os.listdir(os.path.join(DEMO, "cache")):  # pre-seeded extraction cache (never overwrites a real Gemini result)
    dst = os.path.join(db.DATA, "cache", f)
    if not os.path.exists(dst): shutil.copy(os.path.join(DEMO, "cache", f), dst)
app = FastAPI(title="Invoice Desk")

def sse(gen, pdf_name=None):
    for ev in gen:
        yield f"data: {json.dumps(ev, default=str)}\n\n"

def save_pdf(b: bytes):
    h = extract.sha256(b); p = os.path.join(db.DATA, "uploads", h + ".pdf")
    if not os.path.exists(p): open(p, "wb").write(b)

@app.post("/api/run")
async def run_upload(file: UploadFile = File(...)):
    b = await file.read()
    if not b.startswith(b"%PDF"): raise HTTPException(400, "Please upload a PDF file")
    save_pdf(b); return StreamingResponse(sse(pipeline.run(b, file.filename)), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/run-demo/{name}")
def run_demo(name: str):
    p = os.path.join(DEMO, os.path.basename(name))
    if not os.path.exists(p): raise HTTPException(404)
    b = open(p, "rb").read(); save_pdf(b)
    return StreamingResponse(sse(pipeline.run(b, os.path.basename(name))), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/demo")
def demo_list(): return json.load(open(os.path.join(DEMO, "manifest.json")))

@app.get("/api/demo/{name}/pdf")
def demo_pdf(name: str): return FileResponse(os.path.join(DEMO, os.path.basename(name)), media_type="application/pdf")

@app.get("/api/runs")
def runs():
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT id,created,filename,decision,vendor,invoice_number,total,po_number,rationale,duration_ms FROM runs ORDER BY id DESC")]; c.close(); return r

@app.get("/api/runs/{rid}")
def run_detail(rid: int):
    c = db.conn(); r = pipeline.get_run(c, rid); c.close()
    if not r: raise HTTPException(404)
    return r

@app.get("/api/runs/{rid}/pdf")
def run_pdf(rid: int):
    c = db.conn(); r = c.execute("SELECT sha FROM runs WHERE id=?", (rid,)).fetchone(); c.close()
    p = os.path.join(db.DATA, "uploads", f"{r['sha']}.pdf") if r else ""
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")

@app.get("/api/stats")
def stats():
    c = db.conn(); out = {"total": 0, "value_approved": 0.0, "by": {}}
    for r in c.execute("SELECT decision, COUNT(*) n, COALESCE(SUM(total),0) v FROM runs GROUP BY decision"):
        out["by"][r["decision"]] = r["n"]; out["total"] += r["n"]
        if r["decision"] == "APPROVE": out["value_approved"] = r["v"]
    avg = c.execute("SELECT AVG(duration_ms) FROM runs").fetchone()[0]; out["avg_ms"] = int(avg or 0)
    c.close(); return out

@app.get("/api/pos")
def pos():
    c = db.conn(); out = []
    for p in c.execute("SELECT p.*, v.name vendor FROM pos p JOIN vendors v ON v.id=p.vendor_id"):
        inv = [dict(i) for i in c.execute("SELECT invoice_number, invoice_date, amount, source FROM ledger WHERE po_number=? ORDER BY id", (p["po_number"],))]
        b = sum(i["amount"] for i in inv); out.append({**dict(p), "billed": b, "remaining": p["amount"] - b, "invoices": inv})
    c.close(); return out

@app.get("/api/vendors")
def vendors():
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM vendors")]; c.close(); return r

@app.get("/api/settings")
def get_settings():
    c = db.conn(); s = db.get_settings(c); c.close(); return s

@app.put("/api/settings")
async def put_settings(req: Request):
    body = await req.json(); c = db.conn()
    for k, v in body.items():
        if k in db.DEFAULT_SETTINGS:
            try: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, float(v)))
            except (TypeError, ValueError): raise HTTPException(400, f"Bad value for {k}")
    c.commit(); s = db.get_settings(c); c.close(); return s

@app.post("/api/reset")
def reset(): db.reset_data(); return {"ok": True}

@app.get("/api/health")
def health(): return {"ok": True, "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")), "models": extract.MODELS}

app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
