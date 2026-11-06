"""Deterministic AP pipeline. The model only extracts; every decision below is plain rules."""
import io, json, os, re, time, difflib
from datetime import datetime
import pdfplumber
import db, extract as ex

PACE = float(os.environ.get("STAGE_PACE", "0.35"))
STAGES = ["Ingest", "Extract", "Normalise & sanity-check", "Vendor check", "PO match", "Amount vs PO balance", "Duplicate check", "Decision"]
DATE_FMTS = ["%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y"]
STOP = set("and the of for a an to in on with x".split())

def money(x): return "n/a" if x is None else f"${x:,.2f}"
def norm_date(s):
    for f in DATE_FMTS:
        try: return datetime.strptime(s.strip(), f).date().isoformat()
        except Exception: pass
    return None
def lev(a, b):
    a, b = a.lower(), b.lower(); prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1): cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
def words(s):
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z]+", (s or "").lower()) if w not in STOP}
def vname(s): return re.sub(r"\b(ltd|inc|llc|co|corp|pvt|limited)\b", "", re.sub(r"[^a-z0-9& ]", "", (s or "").lower())).strip()
def billed(c, po): return c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE po_number=?", (po,)).fetchone()[0]

class Ctx(dict):
    def flag(self, sev, reason, owner=None): self.setdefault("flags", []).append({"sev": sev, "reason": reason, "owner": owner})

# ---------------- stages: each returns (status, detail, data) ----------------
def s_ingest(c, x):
    with pdfplumber.open(io.BytesIO(x["pdf"])) as p:
        x["pages"] = len(p.pages); x["text"] = "\n".join((pg.extract_text() or "") for pg in p.pages)
    kind = "text layer present" if len(x["text"].strip()) >= 20 else "scanned image, no text layer (vision model needed)"
    x["sha"] = ex.sha256(x["pdf"])
    return "pass", f"{x['pages']} page(s), {kind}", {"sha256": x["sha"][:16], "pages": x["pages"], "text_chars": len(x["text"].strip())}

def s_extract(c, x):
    try: f, meta = ex.extract(x["pdf"], x["text"])
    except ex.ExtractError as e:
        x.flag("review", f"Document could not be read automatically: {e}", "AP reviewer (manual keying / OCR)"); x["halt"] = True
        return "fail", str(e), {}
    x["f"], x["meta"] = f, meta
    g = meta.get("grounding"); gtxt = ""
    if g is not None: gtxt = f" Grounding check: {sum(g.values())}/{len(g)} values found verbatim in the document text."
    else: gtxt = " Grounding check skipped (image-only document)."
    st = "pass"
    if g is not None and not all(g.values()):
        st = "warn"; x.flag("review", "Extracted values not found verbatim in document text (" + ", ".join(k for k, v in g.items() if not v) + ")", "AP reviewer")
    src = meta["source"] + (f" ({meta['model']})" if meta.get("model") else "")
    return st, f"Source: {src}." + gtxt, {"fields": f, "meta": meta}

def s_normalise(c, x):
    f, S = x["f"], x["S"]; notes = []
    f["date_iso"] = norm_date(f["invoice_date"]) if f.get("invoice_date") else None
    missing = [n for n, v in (("invoice number", f.get("invoice_number")), ("invoice date", f["date_iso"]), ("total amount", f.get("total")), ("vendor name", f.get("vendor_name"))) if not v]
    if missing:
        x["halt"] = True; x["missing"] = missing
        x.flag("info", "Missing or unreadable critical fields: " + ", ".join(missing), "Vendor (we'll email them)")
        return "fail", "Missing: " + ", ".join(missing) + ". Stopping, nothing will be guessed.", {"missing": missing}
    st = "pass"
    if f.get("subtotal") is not None and not f["tax_included"]:
        diff = abs(f["subtotal"] + (f.get("tax") or 0) - f["total"]) / f["total"] * 100
        if diff > S["math_tol_pct"]:
            st = "warn"; x.flag("review", f"Math doesn't reconcile: subtotal {money(f['subtotal'])} + tax {money(f.get('tax') or 0)} ≠ total {money(f['total'])}", "AP reviewer")
        notes.append(f"subtotal + tax = total ({'OK' if diff <= S['math_tol_pct'] else 'MISMATCH'})")
    elif f["tax_included"]: notes.append("tax is embedded in prices, so no separate tax check")
    ls = sum(li["amount"] for li in f["line_items"])
    if f["line_items"]:
        ref = f["total"] if (f["tax_included"] or f.get("subtotal") is None) else f["subtotal"]
        if abs(ls - ref) / ref * 100 > S["math_tol_pct"]:
            st = "warn"; x.flag("review", f"Line items sum to {money(ls)}, not {money(ref)}", "AP reviewer")
        notes.append(f"{len(f['line_items'])} line items sum to {money(ls)}")
    if f.get("suspicious_instructions"):
        st = "warn"; x.flag("review", "Document contains text aimed at automated systems or reviewers (e.g. 'approve this'). It was ignored, and the invoice is flagged for a person.", "Security / AP lead")
        notes.append("embedded instructions detected and ignored")
    return st, "; ".join(notes) or "ok", {"date_iso": f["date_iso"], "line_total": ls}

def s_vendor(c, x):
    f = x["f"]; best, bs = None, 0
    for v in c.execute("SELECT * FROM vendors WHERE status='approved'"):
        a, b = vname(f["vendor_name"]), vname(v["name"])
        sc = 1.0 if a == b else max(difflib.SequenceMatcher(None, a, b).ratio(), 0.9 if (a in b or b in a) and min(len(a), len(b)) > 4 else 0)
        if sc > bs: best, bs = v, sc
    if not best or bs < 0.8:
        x["halt"] = True; x.flag("reject", f"Vendor '{f['vendor_name']}' is not on the approved vendor list. Send to vendor onboarding first.", "Procurement")
        return "fail", f"'{f['vendor_name']}' has no match on the approved list (best similarity {bs:.0%})", {}
    x["vendor"] = best; d = f"Matched approved vendor '{best['name']}' (similarity {bs:.0%})"
    inv_bank = re.sub(r"[^A-Z0-9]", "", (f.get("bank_account") or "").upper()); rec = re.sub(r"[^A-Z0-9]", "", best["bank_account"].upper())
    if inv_bank and inv_bank != rec:
        x.flag("review", f"Bank details on the invoice ({f['bank_account']}) differ from the record on file. Verify by calling a known number before paying.", "Finance / treasury")
        return "warn", d + f". BANK MISMATCH: invoice {f['bank_account']} vs on file {best['bank_account']}", {"vendor_id": best["id"], "bank_match": False}
    return "pass", d + (". Bank details match the record on file" if inv_bank else ". No bank details on invoice to compare"), {"vendor_id": best["id"], "bank_match": bool(inv_bank)}

def s_po(c, x):
    f, S, v = x["f"], x["S"], x["vendor"]; po = f.get("po_reference")
    if po:
        row = c.execute("SELECT * FROM pos WHERE po_number=?", (po,)).fetchone()
        if not row:
            x["halt"] = True; x.flag("review", f"PO {po} on the invoice does not exist in the system.", "Procurement")
            return "fail", f"{po} not found", {}
        if row["vendor_id"] != v["id"]:
            x["halt"] = True; x.flag("review", f"PO {po} belongs to a different vendor than the invoice. Possible misdirected or fraudulent invoice.", "Procurement + AP lead")
            return "fail", f"{po} belongs to vendor #{row['vendor_id']}, not {v['name']}", {}
        x["po"], x["conf"], x["how"] = row, 1.0, "explicit reference"
    else:
        iw = set().union(*[words(li["description"]) for li in f["line_items"]]) if f["line_items"] else set()
        cands = []
        for row in c.execute("SELECT * FROM pos WHERE vendor_id=?", (v["id"],)):
            rem = row["amount"] - billed(c, row["po_number"])
            if rem <= 0: continue
            amt = 1 - min(1, abs(f["total"] - rem) / rem * 10); pw = words(row["description"])
            ds = len(iw & pw) / max(1, min(len(iw), len(pw))) if iw else 0
            cands.append({"po": row["po_number"], "remaining": rem, "amount_score": round(amt, 2), "desc_score": round(ds, 2), "conf": round(0.6 * amt + 0.4 * ds, 2), "row": row})
        cands.sort(key=lambda z: -z["conf"])
        view = [{k: v2 for k, v2 in z.items() if k != "row"} for z in cands]
        if not cands or cands[0]["conf"] < S["po_match_min_conf"]:
            x["halt"] = True; best = f" Best candidate {cands[0]['po']} at {cands[0]['conf']:.0%}." if cands else ""
            x.flag("review", f"No PO reference on the invoice and no confident match (need ≥{S['po_match_min_conf']:.0%}).{best}", "Procurement")
            return "fail", "No PO reference; inference below confidence threshold." + best, {"candidates": view}
        top = cands[0]; x["po"], x["conf"], x["how"] = top["row"], top["conf"], "inferred"
        x["cands"] = view
    row = x["po"]; b = billed(c, row["po_number"]); x["billed"], x["rem"] = b, row["amount"] - b
    conf = "" if x["conf"] == 1.0 else ", confidence " + format(x["conf"], ".0%")
    d = f"{row['po_number']} ({x['how']}{conf}): PO {money(row['amount'])}, billed so far {money(b)}, remaining {money(x['rem'])}"
    st = "pass"
    if x["how"] == "inferred": d += ". Inferred from vendor, amount and line-item similarity"
    return st, d, {"po": row["po_number"], "confidence": x["conf"], "billed": b, "remaining": x["rem"], "candidates": x.get("cands")}

def s_amount(c, x):
    f, S, rem, po = x["f"], x["S"], x["rem"], x["po"]
    if f.get("currency") and f["currency"] != po["currency"]:
        x.flag("review", f"Invoice currency {f['currency']} differs from PO currency {po['currency']}.", "AP reviewer")
    if rem <= 0:
        x.flag("reject", f"{po['po_number']} is already fully billed.", "AP reviewer"); return "fail", "PO has no remaining balance", {}
    over = (f["total"] - rem) / rem * 100
    data = {"total": f["total"], "remaining": rem, "over_pct": round(over, 2)}
    if over <= S["tol_approve_pct"]:
        left = max(0, rem - f["total"])
        d = f"{money(f['total'])} vs remaining {money(rem)} ({over:+.1f}%): within ±{S['tol_approve_pct']:g}% tolerance."
        d += f" Partial billing: {money(left)} stays open on {po['po_number']}." if left > rem * S["tol_approve_pct"] / 100 else " Closes out the PO."
        data["remaining_after"] = left; return "pass", d, data
    if over <= S["tol_review_pct"]:
        x.flag("review", f"{money(f['total'])} is {over:.1f}% over the remaining {money(rem)} on {po['po_number']} (limit {S['tol_approve_pct']:g}%).", "Budget owner")
        return "warn", f"{over:.1f}% over remaining balance, between {S['tol_approve_pct']:g}% and {S['tol_review_pct']:g}%: needs sign-off", data
    x.flag("reject", f"{money(f['total'])} is {over:.1f}% over the remaining {money(rem)} on {po['po_number']}, beyond the {S['tol_review_pct']:g}% hard limit.", "AP reviewer")
    return "fail", f"{over:.1f}% over remaining balance, beyond {S['tol_review_pct']:g}% hard limit", data

def s_dup(c, x):
    f, S, v = x["f"], x["S"], x["vendor"]; hits = []
    prior = [dict(r) for r in c.execute("SELECT invoice_number, invoice_date, amount FROM ledger WHERE vendor_id=?", (v["id"],))]
    prior += [{"invoice_number": r["invoice_number"], "invoice_date": json.loads(r["fields"] or "{}").get("date_iso"), "amount": r["total"], "pending": True}
              for r in c.execute("SELECT invoice_number,total,fields FROM runs WHERE vendor=? AND decision='REVIEW'", (v["name"],))]
    same = c.execute("SELECT id, decision FROM runs WHERE sha=? AND decision IN ('APPROVE','REVIEW')", (x["sha"],)).fetchone()
    if same:
        x.flag("reject", f"This exact file was already processed (run #{same['id']}, {same['decision']}).", "AP reviewer")
        return "fail", f"Identical file hash already processed in run #{same['id']}", {"sha_match": same["id"]}
    for p in prior:
        if not p["invoice_number"]: continue
        d = lev(f["invoice_number"], p["invoice_number"])
        if d == 0:
            x.flag("reject", f"Invoice {f['invoice_number']} from {v['name']} was already processed ({money(p['amount'])}).", "AP reviewer")
            return "fail", f"Exact duplicate of {p['invoice_number']}", {"matched": p["invoice_number"]}
        c1 = 1.0 if d <= S["dup_inv_edit"] else 0.0
        c2 = 1.0 if p["amount"] and abs(f["total"] - p["amount"]) / p["amount"] * 100 <= S["dup_amount_pct"] else 0.0
        try: c3 = 1.0 if abs((datetime.fromisoformat(f["date_iso"]) - datetime.fromisoformat(p["invoice_date"])).days) <= S["dup_days"] else 0.0
        except Exception: c3 = 0.0
        score = 0.4 * c1 + 0.4 * c2 + 0.2 * c3
        hits.append({"vs": p["invoice_number"], "edit_distance": d, "same_amount": bool(c2), "close_date": bool(c3), "score": round(score, 2)})
    hits.sort(key=lambda h: -h["score"])
    if hits and hits[0]["score"] >= S["dup_score_threshold"]:
        h = hits[0]
        x.flag("review", f"Likely duplicate of {h['vs']}: invoice number differs by {h['edit_distance']} character(s), amount matches, dates close (score {h['score']:.2f} ≥ {S['dup_score_threshold']:.2f}).", "AP lead")
        return "warn", f"Possible near-duplicate of {h['vs']} (score {h['score']:.2f}): number differs by {h['edit_distance']} char(s), same amount, close date", {"matches": hits[:3]}
    return "pass", f"Compared against {len(prior)} prior invoice(s) from this vendor. Best similarity score {hits[0]['score']:.2f}, below threshold {S['dup_score_threshold']:.2f}" if hits else "No prior invoices from this vendor", {"matches": hits[:3]}

def s_decide(c, x):
    fl = x.get("flags", [])
    sev = {s: [f for f in fl if f["sev"] == s] for s in ("reject", "info", "review")}
    f = x.get("f") or {}
    if sev["info"]: dec = "REQUEST_INFO"
    elif sev["reject"]: dec = "REJECT"
    elif sev["review"]: dec = "REVIEW"
    else: dec = "APPROVE"
    x["decision"] = dec
    if dec == "APPROVE":
        po = x["po"]["po_number"]
        why = f"Approved vendor, {po} matched ({x['how']}), {money(f['total'])} fits the remaining {money(x['rem'])}, no duplicates, math reconciles. Queued for payment."
        c.execute("INSERT INTO ledger(vendor_id,invoice_number,invoice_date,amount,po_number,source) VALUES(?,?,?,?,?,'run')", (x["vendor"]["id"], f["invoice_number"], f["date_iso"], f["total"], po))
    elif dec == "REQUEST_INFO":
        why = "Cannot decide without: " + ", ".join(x["missing"]) + ". Drafted a specific request to the vendor."
        name = f.get("vendor_name") or "team"
        x["email"] = (f"Subject: Information needed to process your invoice\n\nHi {name},\n\nThanks for your invoice. We couldn't process it because the following could not be found or read clearly: "
                      + ", ".join(x["missing"]) + ".\nPlease reply with a corrected invoice that shows each of these and we'll pick it up straight away.\n\nThanks,\nAccounts Payable")
    else:
        top = sev["reject"] or sev["review"]
        why = ("Rejected: " if dec == "REJECT" else "Needs human review: ") + " ".join(t["reason"] for t in top)
        if dec == "REJECT" and sev["review"]: why += " Also flagged: " + " ".join(t["reason"] for t in sev["review"])
    x["why"] = why
    return {"APPROVE": "pass", "REVIEW": "warn", "REQUEST_INFO": "warn", "REJECT": "fail"}[dec], dec + ": " + why, {"decision": dec, "flags": fl}

FUNCS = [s_ingest, s_extract, s_normalise, s_vendor, s_po, s_amount, s_dup, s_decide]

def run(pdf: bytes, filename: str):
    c = db.conn(); x = Ctx(pdf=pdf, S=db.get_settings(c)); audit = []; t_start = time.time()
    for name, fn in zip(STAGES, FUNCS):
        if x.get("halt") and name != "Decision":
            ev = {"type": "stage", "name": name, "status": "skip", "detail": "Skipped: an earlier stage ended the run", "ms": 0}
            audit.append(ev); yield ev; continue
        yield {"type": "stage", "name": name, "status": "running"}
        time.sleep(PACE); t = time.time()
        try: st, detail, data = fn(c, x)
        except Exception as e:  # never crash the demo: convert to a human-review outcome
            x.flag("review", f"Internal error in '{name}': {e}", "Engineering"); st, detail, data = "fail", f"Error: {e}", {}
            x["halt"] = True
            if name == "Decision": raise
        ev = {"type": "stage", "name": name, "status": st, "detail": detail, "data": data, "ms": int((time.time() - t) * 1000)}
        audit.append(ev); yield ev
    f = x.get("f") or {}
    po = x["po"]["po_number"] if x.get("po") else f.get("po_reference")
    rid = c.execute("INSERT INTO runs(created,filename,sha,decision,vendor,invoice_number,total,po_number,rationale,flags,audit,fields,email,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), filename, x.get("sha"), x["decision"], x["vendor"]["name"] if x.get("vendor") else f.get("vendor_name"),
         f.get("invoice_number"), f.get("total"), po, x["why"], json.dumps(x.get("flags", [])), json.dumps(audit, default=str), json.dumps(f, default=str), x.get("email"), int((time.time() - t_start) * 1000))).lastrowid
    c.execute("UPDATE ledger SET run_id=? WHERE run_id IS NULL AND source='run' AND invoice_number=? ", (rid, f.get("invoice_number")))
    c.commit()
    yield {"type": "done", "run": get_run(c, rid)}; c.close()

def get_run(c, rid):
    r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not r: return None
    d = dict(r)
    for k in ("flags", "audit", "fields"): d[k] = json.loads(d[k] or "null")
    return d
