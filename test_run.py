import os, sys, json, shutil, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp(); os.environ["STAGE_PACE"] = "0"
os.environ["NO_CACHE"] = sys.argv[1] if len(sys.argv) > 1 else "0"
import db, pipeline
db.init()
if os.environ["NO_CACHE"] != "1":
    for f in os.listdir("demo/cache"): shutil.copy("demo/cache/" + f, db.DATA + "/cache/" + f)
man = json.load(open("demo/manifest.json")); ok = 0
for m in man:
    for ev in pipeline.run(open("demo/" + m["file"], "rb").read(), m["file"]):
        if ev["type"] == "done": r = ev["run"]
    good = r["decision"] == m["expect"]; ok += good
    print(("OK  " if good else "FAIL"), m["file"][:34].ljust(34), r["decision"].ljust(13), "|", r["rationale"][:150])
    if not good:
        for a in r["audit"]: print("     ", a["name"], a["status"], a["detail"][:140])
print(ok, "/", len(man))
