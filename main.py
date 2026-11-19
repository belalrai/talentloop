import os, io, re, csv, json, time, base64, smtplib, secrets
from email.message import EmailMessage
import httpx
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

DBURL = os.getenv("DATABASE_URL", "")
_ready = False
sec = HTTPBasic()

def auth(c: HTTPBasicCredentials = Depends(sec)):
    ok = secrets.compare_digest(c.username, os.getenv("ADMIN_USER", "admin")) and \
         secrets.compare_digest(c.password, os.getenv("ADMIN_PASSWORD", "change-me"))
    if not ok:
        raise HTTPException(401, "Unauthorized", {"WWW-Authenticate": "Basic"})

app = FastAPI(dependencies=[Depends(auth)])

from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"detail": f"{type(exc).__name__}: {str(exc)[:200]}"}, status_code=500)

@app.get("/api/health")
def health():
    out = {}
    try:
        q("SELECT 1 x")
        out["database"] = "ok"
    except Exception as e:
        out["database"] = "FAILED: " + str(e)[:200]
    out["gemini_key_set"] = bool(os.getenv("GEMINI_API_KEY"))
    out["anthropic_key_set"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    try:
        sample = {"type": "text", "text": PROMPT + "\nCV: Ali Khan, ali@example.com, +923001234567, Software Engineer"}
        if os.getenv("GEMINI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
            out["parser"] = "ok: " + gemini(sample)[:80]
        else:
            import anthropic
            m = anthropic.Anthropic().messages.create(model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=50,
                                                      messages=[{"role": "user", "content": "Say ok"}])
            out["parser"] = "ok"
    except Exception as e:
        out["parser"] = "FAILED: " + str(e)[:300]
    out["smtp_host"] = os.getenv("SMTP_HOST") or "NOT SET"
    out["smtp_login_set"] = bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))
    return out

def q(sql, args=(), many=False, write=False):
    global _ready
    if not DBURL:
        raise RuntimeError("DATABASE_URL is not set in Vercel environment variables")
    if not _ready:
        _ready = True
        try:
            init()
        except Exception:
            _ready = False
            raise
    import psycopg  # imported lazily so a driver problem is reported instead of crashing the app
    from psycopg.rows import dict_row
    sql = sql.replace("?", "%s")
    ins = sql.lstrip().upper().startswith("INSERT")
    if ins:
        sql += " RETURNING id"
    with psycopg.connect(DBURL, row_factory=dict_row, autocommit=True, prepare_threshold=None) as c:
        cur = c.execute(sql, args or None)
        if write:
            return cur.fetchone()["id"] if ins else None
        rows = cur.fetchall()
        return rows if many else (rows[0] if rows else None)

def init():
    q("""CREATE TABLE IF NOT EXISTS candidates(id SERIAL PRIMARY KEY, name TEXT, email TEXT, phone TEXT,
         role TEXT, location TEXT, language TEXT, data TEXT, source TEXT, created DOUBLE PRECISION)""", write=True)
    q("""CREATE TABLE IF NOT EXISTS templates(id SERIAL PRIMARY KEY, name TEXT, subject TEXT, body TEXT)""", write=True)
    q("""CREATE TABLE IF NOT EXISTS messages(id SERIAL PRIMARY KEY, cid INTEGER, channel TEXT, tpl INTEGER,
         job TEXT, status TEXT, ts DOUBLE PRECISION)""", write=True)
    for t in ("candidates", "templates", "messages"):  # block Supabase public API access; the app uses the postgres role
        q(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY", write=True)
    if not q("SELECT 1 x FROM templates"):
        for n, s, b in [
            ("New job opportunity", "New opportunity: {job_title} at {company}",
             "Hi {first_name},\n\nI'm reaching out about a new {job_title} role at {company}. Your background looks like a strong match.\n\nWould you be open to a quick chat this week?\n\nBest regards"),
            ("Follow-up", "Following up: {job_title}",
             "Hi {first_name},\n\nFollowing up on my earlier note about the {job_title} role at {company}. Still interested?"),
            ("Interview invitation", "Interview invitation: {job_title} at {company}",
             "Hi {first_name},\n\nWe'd like to invite you to interview for the {job_title} role at {company}. Which day suits you?")]:
            q("INSERT INTO templates(name,subject,body) VALUES(?,?,?)", (n, s, b), write=True)

# ---------- CV parsing ----------
PROMPT = ('Extract this CV into JSON only, no prose or code fences, using exactly these keys: '
          '{"name","email","phone","location","language","current_role","years_experience","skills":[],'
          '"education":[{"school","degree","year"}],"experience":[{"company","title","start","end"}],"summary"}. '
          'Use null when missing. Keep text values in the CV\'s own language.')

def to_block(fn: str, data: bytes):
    ext = fn.lower().rsplit(".", 1)[-1]
    b64 = base64.b64encode(data).decode()
    if ext == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    if ext in ("png", "jpg", "jpeg", "webp"):
        mt = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in d.paragraphs)
        for t in d.tables:
            for r in t.rows:
                text += "\n" + " | ".join(c.text for c in r.cells)
    elif ext in ("txt", "md"):
        text = data.decode("utf-8", "ignore")
    else:
        raise HTTPException(400, "Unsupported file type. Use PDF, DOCX, TXT, PNG or JPG.")
    return {"type": "text", "text": "CV text:\n" + text}

def gemini(block):
    part = {"text": block["text"]} if block["type"] == "text" else \
        {"inline_data": {"mime_type": block["source"]["media_type"], "data": block["source"]["data"]}}
    models = [os.getenv("GEMINI_MODEL", "gemini-3.8-flash")]
    if os.getenv("GEMINI_FALLBACK_MODEL"):
        models.append(os.getenv("GEMINI_FALLBACK_MODEL"))
    body = {"contents": [{"parts": [part, {"text": PROMPT}]}], "generationConfig": {"responseMimeType": "application/json"}}
    err = ""
    for model in models:
        for attempt in range(2):  # retry temporary overloads (503) and rate limits (429)
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=120)
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            err = f"Gemini HTTP {r.status_code} ({model}): {r.text[:200]}"
            if r.status_code in (429, 503):
                time.sleep(1.5)
                continue
            break
    raise RuntimeError(err)

def ai_parse(fn, data):
    block = to_block(fn, data)
    if os.getenv("GEMINI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
        raw = gemini(block)
    else:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=2500,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("the AI returned no data: " + raw[:120])
    p = json.loads(m.group(0))
    if not (p.get("name") or p.get("email") or p.get("phone")):
        raise ValueError("no candidate details found (scanned/blank file?)")
    p["parser"] = "ai"
    return p

SKILLS = ["python", "java", "javascript", "typescript", "sql", "excel", "react", "node.js", "php", "laravel", "django", "flask",
          "aws", "azure", "docker", "git", "html", "css", "c++", "c#", ".net", "linux", "wordpress", "seo", "marketing", "sales",
          "accounting", "quickbooks", "photoshop", "figma", "power bi", "tableau", "machine learning", "data analysis",
          "recruitment", "customer service", "project management", "communication", "leadership", "autocad", "sap"]
ROLES = r"engineer|developer|manager|accountant|analyst|designer|recruiter|officer|teacher|consultant|executive|assistant|administrator|specialist|lead|director|intern"

def cv_text(fn, data):
    if fn.lower().endswith(".pdf"):
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
    else:
        b = to_block(fn, data)
        if b["type"] != "text":
            raise ValueError("image CVs need the AI parser (basic parser reads PDF, DOCX and TXT only)")
        text = b["text"][8:]
    if len(text.strip()) < 20:
        raise ValueError("no readable text (scanned PDF?). Basic parser cannot read scanned files.")
    return text

def basic_parse(fn, data):
    text = cv_text(fn, data)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    low = text.lower()
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    ph = re.search(r"(?<!\d)(\+?\d[\d\s().-]{8,16}\d)(?!\d)", text)
    name = next((l for l in lines[:10] if 2 <= len(l.split()) <= 4 and not re.search(r"[\d@:/]", l)
                 and not re.search(r"resume|curriculum|vitae|\bcv\b|profile|contact", l, re.I) and not re.search(ROLES, l, re.I)), None)
    role = next((l[:80] for l in lines[:15] if re.search(ROLES, l, re.I) and "@" not in l and len(l) < 80), None)
    loc = re.search(r"(?:address|location|city)\s*[:\-]\s*(.+)", text, re.I)
    yrs = [int(x) for x in re.findall(r"(\d{1,2})\+?\s*(?:years|yrs)", low)]
    edu = [{"school": None, "degree": l[:100], "year": (re.search(r"(19|20)\d\d", l) or [None])[0]}
           for l in lines if re.search(r"bachelor|master|\bb\.?sc\b|\bm\.?sc\b|\bmba\b|ph\.?d|diploma|intermediate|matric|\bb\.?s\b|\bm\.?s\b", l, re.I)][:3]
    return {"name": name, "email": em.group(0) if em else None, "phone": ph.group(1) if ph else None,
            "location": loc.group(1).strip()[:80] if loc else None, "language": None, "current_role": role,
            "years_experience": max(yrs) if yrs else None,
            "skills": [k for k in SKILLS if re.search(r"(?<![\w])" + re.escape(k) + r"(?![\w])", low)],
            "education": edu, "experience": [], "summary": None, "parser": "basic"}

@app.post("/api/parse")
def parse(file: UploadFile = File(...)):
    data = file.file.read()
    if len(data) > 4_000_000:
        raise HTTPException(413, "File too large (4 MB max on Vercel).")
    p, note = None, ""
    if os.getenv("PARSER", "auto") != "basic" and (os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
        try:
            p = ai_parse(file.filename, data)
        except HTTPException:
            raise
        except Exception as e:
            note = str(e)[:160]
    if p is None:  # AI off or failed: fall back to the built-in parser (no API, no limits)
        try:
            p = basic_parse(file.filename, data)
            if note:
                p["note"] = "AI unavailable, basic parser used: " + note
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, "Parsing failed: " + (note + " | " if note else "") + str(e))
    p["phone"] = re.sub(r"[^\d+]", "", p.get("phone") or "")
    try:
        cid = q("INSERT INTO candidates(name,email,phone,role,location,language,data,source,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (p.get("name"), p.get("email"), p["phone"], p.get("current_role"), p.get("location"),
                 p.get("language"), json.dumps(p), "cv:" + file.filename, time.time()), write=True)
    except Exception as e:
        raise HTTPException(500, f"Database error (check DATABASE_URL): {str(e)[:150]}")
    return {"id": cid, **p}

# ---------- Spreadsheet import ----------
KEYS = {"name": ["name"], "email": ["mail"], "phone": ["phone", "mobile", "whatsapp", "tel"],
        "role": ["role", "title", "position"], "location": ["location", "city", "country"]}

def cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)

@app.post("/api/sheet")
def sheet(file: UploadFile = File(...)):
    data, fn = file.file.read(), file.filename.lower()
    if fn.endswith(".xlsx"):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = [[cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
    elif fn.endswith(".xls"):
        import xlrd
        sh = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
        rows = [[cell(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
    elif fn.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "ignore"))))
    else:
        raise HTTPException(400, "Upload a .csv, .xlsx or .xls file.")
    if len(rows) < 2:
        raise HTTPException(400, "The file has no data rows.")
    head = [h.strip().lower() for h in rows[0]]
    ix = {k: next((i for i, h in enumerate(head) if any(w in h for w in ws_)), -1) for k, ws_ in KEYS.items()}
    if ix["name"] < 0 or (ix["email"] < 0 and ix["phone"] < 0):
        raise HTTPException(400, "Need a name column and an email or phone column.")
    get = lambda r, k: r[ix[k]].strip() if ix[k] >= 0 and ix[k] < len(r) else ""
    out = []
    for r in rows[1:]:
        if not get(r, "name"):
            continue
        ph = re.sub(r"[^\d+]", "", get(r, "phone"))
        cid = q("INSERT INTO candidates(name,email,phone,role,location,language,data,source,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (get(r, "name"), get(r, "email"), ph, get(r, "role"), get(r, "location"), "", "{}", "sheet:" + file.filename, time.time()), write=True)
        out.append({"id": cid, "name": get(r, "name"), "email": get(r, "email"), "phone": ph})
    return {"count": len(out), "matched": {k: v >= 0 for k, v in ix.items()}, "candidates": out}

# ---------- Data ----------
@app.get("/api/candidates")
def candidates():
    return q("SELECT id,name,email,phone,role,location,language,source FROM candidates ORDER BY id DESC LIMIT 500", many=True)

@app.get("/api/stats")
def stats():
    n = lambda s, a=(): q(s, a)["n"]
    return {"parsed": n("SELECT COUNT(*) n FROM candidates WHERE source LIKE 'cv:%'"),
            "candidates": n("SELECT COUNT(*) n FROM candidates"),
            "templates": n("SELECT COUNT(*) n FROM templates"),
            "email": n("SELECT COUNT(*) n FROM messages WHERE channel='Email' AND status='sent'"),
            "whatsapp": n("SELECT COUNT(*) n FROM messages WHERE channel='WhatsApp' AND status='sent'"),
            "failed": n("SELECT COUNT(*) n FROM messages WHERE status LIKE 'failed%'"),
            "roles": q("SELECT COALESCE(NULLIF(role,''),'Unknown') role, COUNT(*) n FROM candidates GROUP BY 1 ORDER BY n DESC LIMIT 6", many=True)}

class Tpl(BaseModel):
    name: str
    subject: str
    body: str

@app.get("/api/templates")
def templates():
    return q("SELECT * FROM templates ORDER BY id", many=True)

@app.post("/api/templates")
def add_template(t: Tpl):
    return {"id": q("INSERT INTO templates(name,subject,body) VALUES(?,?,?)", (t.name, t.subject, t.body), write=True)}

# ---------- Sending ----------
class Send(BaseModel):
    channel: str
    template_id: int | None = None
    subject: str = ""
    body: str
    job: str
    company: str

def fill(s, c, job, co):
    first = (c["name"] or "").split(" ")[0] or "there"
    return s.replace("{first_name}", first).replace("{job_title}", job).replace("{company}", co)

def wa_send(to, text, first, job, co):
    p = {"messaging_product": "whatsapp", "to": to.lstrip("+")}
    tn = os.getenv("WA_TEMPLATE_NAME")
    if tn:  # business-initiated messages must use an approved template
        p.update(type="template", template={"name": tn, "language": {"code": os.getenv("WA_LANG", "en")},
                 "components": [{"type": "body", "parameters": [{"type": "text", "text": x} for x in (first, job, co)]}]})
    else:   # plain text only delivers inside a 24h customer-service window
        p.update(type="text", text={"body": text})
    r = httpx.post(f"https://graph.facebook.com/v21.0/{os.environ['WA_PHONE_ID']}/messages",
                   headers={"Authorization": "Bearer " + os.environ["WA_TOKEN"]}, json=p, timeout=30)
    r.raise_for_status()

def worker(cands, s: Send):
    smtp, fails = None, 0
    for c in cands:
        status = "sent"
        try:
            body = fill(s.body, c, s.job, s.company)
            if s.channel == "Email":
                if smtp is None:
                    smtp = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", 587)), timeout=30)
                    smtp.starttls(); smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                m = EmailMessage()
                m["From"], m["To"] = os.getenv("FROM_EMAIL", os.environ["SMTP_USER"]), c["email"]
                m["Subject"] = fill(s.subject or "Job opportunity", c, s.job, s.company)
                m.set_content(body)
                smtp.send_message(m)
            else:
                wa_send(c["phone"], body, (c["name"] or "").split(" ")[0] or "there", s.job, s.company)
                time.sleep(0.2)
        except Exception as e:
            status = "failed: " + str(e)[:120]
            fails += 1
        q("INSERT INTO messages(cid,channel,tpl,job,status,ts) VALUES(?,?,?,?,?,?)",
          (c["id"], s.channel, s.template_id, s.job, status, time.time()), write=True)
    if smtp:
        smtp.quit()
    return fails

BATCH = 25

@app.post("/api/send")
def send(s: Send):
    if s.channel not in ("Email", "WhatsApp"):
        raise HTTPException(400, "Unknown channel")
    col = "email" if s.channel == "Email" else "phone"
    todo = q(f"""SELECT id,name,email,phone FROM candidates WHERE COALESCE({col},'')!=''
                 AND id NOT IN (SELECT cid FROM messages WHERE channel=? AND job=?) ORDER BY id""",
             (s.channel, s.job), many=True)
    batch = todo[:BATCH]
    failed = worker(batch, s) if batch else 0
    return {"processed": len(batch), "failed": failed, "remaining": len(todo) - len(batch)}
