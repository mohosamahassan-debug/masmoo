"""
مسموع — خادم البلاغات المركزي (الإصدار ٣)
مستشفى الدمازين التعليمي — ولاية النيل الأزرق

يستقبل بلاغات المرضى ويدفعها فوراً كإشعار إلى هاتف ضابط المناوبة.

التشغيل:
    pip install -r requirements.txt
    python3 gen_vapid.py          # مرة واحدة — لتوليد مفاتيح الإشعارات
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
import os, json, secrets, datetime, csv, io
from typing import List, Optional

import db as DB
from db import db, UPSERT_SUB

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:
    from pywebpush import webpush, WebPushException
    PUSH_OK = True
except ImportError:                                   # يعمل بدون إشعارات
    PUSH_OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
ORIGINS = [o.strip() for o in os.getenv("MASMOO_ORIGINS", "*").split(",")]

# رموز دخول الإدارة — غيّرها من متغيرات البيئة قبل التشغيل الفعلي
OFFICERS = json.loads(os.getenv("MASMOO_OFFICERS", json.dumps({
    "2026": {"name": "ضابط المناوبة", "role": "officer"},
    "1970": {"name": "مدير المستشفى", "role": "director"},
}, ensure_ascii=False)))

VAPID_PUB = os.getenv("MASMOO_VAPID_PUBLIC", "")
VAPID_PRIV = os.getenv("MASMOO_VAPID_PRIVATE", "")
VAPID_SUB = os.getenv("MASMOO_VAPID_SUBJECT", "mailto:masmoo@damazin-hospital.sd")

_vf = os.path.join(HERE, "vapid.json")
if not VAPID_PUB and os.path.exists(_vf):
    _v = json.load(open(_vf))
    VAPID_PUB, VAPID_PRIV = _v.get("public", ""), _v.get("private", "")

app = FastAPI(title="مسموع API", version="3.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])

# --------------------------------------------------------------- قاعدة البيانات
DB.init()

# مفاتيح الإشعارات: تُولَّد تلقائياً عند أول تشغيل وتُحفظ في قاعدة البيانات
if not (VAPID_PUB and VAPID_PRIV):
    try:
        VAPID_PUB, VAPID_PRIV = DB.ensure_vapid()
    except Exception:
        pass

now_ms = lambda: int(datetime.datetime.now().timestamp() * 1000)

# --------------------------------------------------------------- النماذج
class TL(BaseModel):
    status: str
    note: str = ""
    at: int
    by: str = "النظام"

class Report(BaseModel):
    num: str
    type: str
    cat: str = "شكوى"
    pri: str = "عادي"
    dept: str = ""
    when: str = ""
    text: str
    photo: Optional[str] = None
    name: str = ""
    phone: str = ""
    file: str = ""
    anon: bool = True
    createdAt: int
    status: str = "جديد"
    assignee: str = ""
    rating: int = 0
    timeline: List[TL] = Field(default_factory=list)

class LoginIn(BaseModel):
    code: str

class StatusIn(BaseModel):
    status: str
    assignee: str = ""
    note: str = ""
    rating: int = 0
    by: str = "ضابط المناوبة"

class SubIn(BaseModel):
    subscription: dict
    device: str = ""

def to_dict(r) -> dict:
    return {"num": r["num"], "type": r["type"], "cat": r["cat"], "pri": r["pri"],
            "dept": r["dept"] or "", "when": r["when_"] or "", "text": r["text"],
            "photo": r["photo"], "name": r["name"] or "", "phone": r["phone"] or "",
            "file": r["file"] or "", "anon": bool(r["anon"]), "createdAt": r["created_at"],
            "status": r["status"], "assignee": r["assignee"] or "", "rating": r["rating"] or 0,
            "timeline": json.loads(r["timeline"])}

def public_view(d: dict) -> dict:
    """ما يراه المريض عن بلاغه — بلا بيانات إدارية داخلية."""
    return {k: d[k] for k in ("num", "type", "cat", "pri", "dept", "when", "text",
                              "createdAt", "status", "assignee", "rating", "timeline")}

# --------------------------------------------------------------- المصادقة
def auth(authorization: str = Header(default="")):
    tok = authorization.replace("Bearer ", "").strip()
    if not tok:
        raise HTTPException(401, "مطلوب تسجيل الدخول")
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
    if not s:
        raise HTTPException(401, "الجلسة منتهية — سجّل الدخول مجدداً")
    return {"name": s["name"], "role": s["role"]}

def director(u=Depends(auth)):
    if u["role"] != "director":
        raise HTTPException(403, "هذا الإجراء متاح لمدير المستشفى فقط")
    return u

# --------------------------------------------------------------- الإشعارات
def notify_officers(rep: dict):
    """يدفع إشعاراً فورياً إلى كل أجهزة الضباط المسجّلة."""
    if not (PUSH_OK and VAPID_PRIV):
        return
    urgent = rep.get("pri") in ("عاجل", "حرج")
    body = f"{rep.get('type','')} · {rep.get('pri','عادي')}"
    if rep.get("dept"):
        body += f" · {rep['dept']}"
    payload = json.dumps({
        "title": ("بلاغ عاجل — مسموع" if urgent else "بلاغ جديد — مسموع"),
        "body": f"{rep['num']} — {body}",
        "num": rep["num"], "urgent": urgent, "tag": "masmoo-" + rep["num"],
    }, ensure_ascii=False)

    dead = []
    with db() as con:
        subs = con.execute("SELECT * FROM subscriptions").fetchall()
        for s in subs:
            try:
                webpush(subscription_info=json.loads(s["data"]), data=payload,
                        vapid_private_key=VAPID_PRIV,
                        vapid_claims={"sub": VAPID_SUB})
            except WebPushException as e:
                if getattr(e, "response", None) is not None and e.response.status_code in (404, 410):
                    dead.append(s["endpoint"])
            except Exception:
                pass
        for d in dead:
            con.execute("DELETE FROM subscriptions WHERE endpoint=?", (d,))

# --------------------------------------------------------------- نقاط النهاية
@app.get("/api/health")
def health():
    with db() as con:
        n = con.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
    return {"ok": True, "reports": n, "engine": DB.engine_name(),
            "push": bool(PUSH_OK and VAPID_PRIV), "version": "3.0.0"}

@app.post("/api/login")
def login(b: LoginIn):
    u = OFFICERS.get(b.code.strip())
    if not u:
        raise HTTPException(401, "رمز الدخول غير صحيح")
    tok = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO sessions (token,name,role,created_at) VALUES (?,?,?,?)",
                    (tok, u["name"], u["role"], now_ms()))
    return {"token": tok, "name": u["name"], "role": u["role"]}

@app.post("/api/logout")
def logout(authorization: str = Header(default="")):
    tok = authorization.replace("Bearer ", "").strip()
    with db() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (tok,))
    return {"ok": True}

# ---- المريض: إنشاء بلاغ (بلا مصادقة) ----
@app.post("/api/reports")
def create(rep: Report):
    d = rep.model_dump()
    last = max([t["at"] for t in d["timeline"]] or [d["createdAt"]])
    with db() as con:
        exists = con.execute("SELECT num FROM reports WHERE num=?", (d["num"],)).fetchone()
        if exists:
            return {"ok": True, "num": d["num"], "duplicate": True}
        con.execute("""INSERT INTO reports
            (num,type,cat,pri,dept,when_,text,photo,name,phone,file,anon,
             created_at,status,assignee,rating,timeline,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["num"], d["type"], d["cat"], d["pri"], d["dept"], d["when"], d["text"],
             d["photo"], d["name"], d["phone"], d["file"], 1 if d["anon"] else 0,
             d["createdAt"], d["status"], d["assignee"], d["rating"],
             json.dumps(d["timeline"], ensure_ascii=False), last))
    notify_officers(d)
    return {"ok": True, "num": d["num"]}

# ---- المريض: متابعة بلاغه بالرقم ----
@app.get("/api/reports/{num}")
def get_one(num: str):
    with db() as con:
        r = con.execute("SELECT * FROM reports WHERE num=?", (num,)).fetchone()
    if not r:
        raise HTTPException(404, "لم يُعثر على هذا الرقم المرجعي")
    return public_view(to_dict(r))

# ---- المريض: تقييم الرضا ----
@app.post("/api/reports/{num}/rate")
def rate(num: str, body: dict):
    v = int(body.get("rating", 0))
    if not 1 <= v <= 5:
        raise HTTPException(400, "التقييم من ١ إلى ٥")
    with db() as con:
        r = con.execute("SELECT * FROM reports WHERE num=?", (num,)).fetchone()
        if not r:
            raise HTTPException(404, "لم يُعثر على البلاغ")
        tl = json.loads(r["timeline"])
        tl.append({"status": r["status"], "note": f"قيّم مقدّم البلاغ الخدمة بـ {v} من ٥.",
                   "at": now_ms(), "by": "مقدّم البلاغ"})
        con.execute("UPDATE reports SET rating=?, timeline=?, updated_at=? WHERE num=?",
                    (v, json.dumps(tl, ensure_ascii=False), now_ms(), num))
    return {"ok": True}

# ---- الضابط: كل البلاغات ----
@app.get("/api/reports")
def all_reports(status: Optional[str] = None, type: Optional[str] = None,
                limit: int = 800, u=Depends(auth)):
    q, p = "SELECT * FROM reports WHERE 1=1", []
    if status:
        q += " AND status=?"; p.append(status)
    if type:
        q += " AND type=?"; p.append(type)
    q += " ORDER BY created_at DESC LIMIT ?"; p.append(limit)
    with db() as con:
        return [to_dict(r) for r in con.execute(q, p).fetchall()]

# ---- الضابط: تحديث الحالة ----
@app.post("/api/reports/{num}/status")
def set_status(num: str, b: StatusIn, u=Depends(auth)):
    with db() as con:
        r = con.execute("SELECT * FROM reports WHERE num=?", (num,)).fetchone()
        if not r:
            raise HTTPException(404, "لم يُعثر على البلاغ")
        tl = json.loads(r["timeline"])
        note = b.note or (f"أُحيل البلاغ إلى {b.assignee} لاتخاذ اللازم."
                          if b.assignee else "تم تحديث حالة البلاغ.")
        tl.append({"status": b.status, "note": note, "at": now_ms(), "by": u["name"]})
        con.execute("""UPDATE reports SET status=?, assignee=?, rating=?, timeline=?, updated_at=?
                       WHERE num=?""",
                    (b.status, b.assignee, b.rating or r["rating"],
                     json.dumps(tl, ensure_ascii=False), now_ms(), num))
        row = con.execute("SELECT * FROM reports WHERE num=?", (num,)).fetchone()
    return to_dict(row)

# ---- الإشعارات ----
@app.get("/api/push/key")
def push_key():
    return {"publicKey": VAPID_PUB, "enabled": bool(PUSH_OK and VAPID_PRIV)}

@app.post("/api/push/subscribe")
def push_sub(b: SubIn, u=Depends(auth)):
    ep = b.subscription.get("endpoint", "")
    if not ep:
        raise HTTPException(400, "بيانات الاشتراك ناقصة")
    with db() as con:
        con.execute(UPSERT_SUB,
                    (ep, json.dumps(b.subscription), b.device or u["name"], now_ms()))
    return {"ok": True}

@app.post("/api/push/test")
def push_test(u=Depends(auth)):
    notify_officers({"num": "MS-TEST-000", "type": "اختبار الإشعارات",
                     "pri": "عادي", "dept": ""})
    return {"ok": True}

# ---- المؤشرات والتصدير ----
@app.get("/api/stats")
def stats(u=Depends(auth)):
    closed = ("تمت المعالجة", "مغلق")
    sla = {"عادي": 48, "عاجل": 8, "حرج": 2}
    with db() as con:
        rows = [to_dict(r) for r in con.execute("SELECT * FROM reports").fetchall()]
    t = now_ms()
    open_ = [r for r in rows if r["status"] not in closed]
    late = [r for r in open_ if t - r["createdAt"] > sla.get(r["pri"], 48) * 3600_000]
    done = [r for r in rows if r["status"] in closed]
    rated = [r for r in rows if r["rating"]]
    avg = (sum(max(x["at"] for x in r["timeline"]) - r["createdAt"] for r in done)
           / len(done) / 3600_000) if done else None
    return {"total": len(rows), "open": len(open_), "late": len(late), "closed": len(done),
            "completion_rate": round(len(done) / len(rows) * 100) if rows else 0,
            "avg_resolution_hours": round(avg, 1) if avg else None,
            "satisfaction": round(sum(r["rating"] for r in rated) / len(rated), 2) if rated else None}

@app.get("/api/export.csv")
def export_csv(u=Depends(director)):
    with db() as con:
        rows = [to_dict(r) for r in con.execute(
            "SELECT * FROM reports ORDER BY created_at DESC").fetchall()]
    buf = io.StringIO(); buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["الرقم المرجعي", "الجهة", "طبيعة الرسالة", "الأهمية", "القسم",
                "الحالة", "الجهة المسؤولة", "تاريخ التسجيل", "التقييم", "النص"])
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["createdAt"] / 1000).strftime("%Y-%m-%d %H:%M")
        w.writerow([r["num"], r["type"], r["cat"], r["pri"], r["dept"], r["status"],
                    r["assignee"], dt, r["rating"] or "", r["text"].replace("\n", " ")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="masmoo-reports.csv"'})


# ================================================================= الواجهات
# التطبيقان يُقدَّمان من نفس الخادم:
#   /p/  → تطبيق المرضى        /a/  → تطبيق الإدارة
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse

# ملفات الواجهة: داخل مجلد web/ إن وُجد، وإلا فبجوار هذا الملف مباشرة
_w = os.path.join(HERE, "web")
WEB = _w if os.path.isdir(_w) else HERE

def _send(name: str, media: str = None):
    path = os.path.join(WEB, name)
    if not os.path.exists(path):
        raise HTTPException(404, "الملف غير موجود")
    kw = {"media_type": media} if media else {}
    return FileResponse(path, **kw)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/p/")

# ---- تطبيق المرضى ----
@app.get("/p/", include_in_schema=False)
@app.get("/p/index.html", include_in_schema=False)
def p_index():
    return _send("patient.html", "text/html; charset=utf-8")

@app.get("/p/sw.js", include_in_schema=False)
def p_sw():
    return _send("patient-sw.js", "application/javascript; charset=utf-8")

@app.get("/p/manifest.json", include_in_schema=False)
def p_manifest():
    return _send("patient-manifest.json", "application/manifest+json; charset=utf-8")

# ---- تطبيق الإدارة ----
@app.get("/a/", include_in_schema=False)
@app.get("/a/index.html", include_in_schema=False)
def a_index():
    return _send("admin.html", "text/html; charset=utf-8")

@app.get("/a/sw.js", include_in_schema=False)
def a_sw():
    return _send("admin-sw.js", "application/javascript; charset=utf-8")

@app.get("/a/manifest.json", include_in_schema=False)
def a_manifest():
    return _send("admin-manifest.json", "application/manifest+json; charset=utf-8")

# ---- الأيقونات (مشتركة) ----
@app.get("/p/icons/{fname}", include_in_schema=False)
@app.get("/a/icons/{fname}", include_in_schema=False)
def icons(fname: str):
    if "/" in fname or ".." in fname or not fname.endswith(".png"):
        raise HTTPException(404, "غير موجود")
    return _send(fname, "image/png")

# ---- صفحة التوزيع ----
@app.get("/download", include_in_schema=False)
def download_page():
    return _send("download.html", "text/html; charset=utf-8")


# ---- التحقق من ملكية التطبيق (Digital Asset Links) ----
# يخبر أندرويد أن تطبيق مسموع يملك هذا الموقع، فيختفي شريط عنوان المتصفح.
@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
    return _send("assetlinks.json", "application/json")
