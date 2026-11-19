import os, io, re, csv, json, time, base64, smtplib, secrets
import psycopg
from psycopg.rows import dict_row
from email.message import EmailMessage
import anthropic, httpx
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

DBURL = os.environ["DATABASE_URL"]
sec = HTTPBasic()

def auth(c: HTTPBasicCredentials = Depends(sec)):
    ok = secrets.compare_digest(c.username, os.getenv("ADMIN_USER", "admin")) and \
         secrets.compare_digest(c.password, os.getenv("ADMIN_PASSWORD", "change-me"))
    if not ok:
        raise HTTPException(401, "Unauthorized", {"WWW-Authenticate": "Basic"})

app = FastAPI(dependencies=[Depends(auth)])

def q(sql, args=(), many=False, write=False):
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
init()

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

@app.post("/api/parse")
def parse(file: UploadFile = File(...)):
    data = file.file.read()
    if len(data) > 4_000_000:
        raise HTTPException(413, "File too large (4 MB max on Vercel).")
    try:
        msg = anthropic.Anthropic().messages.create(
            model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=2500,
            messages=[{"role": "user", "content": [to_block(file.filename, data), {"type": "text", "text": PROMPT}]}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
        p = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Parsing failed: {e}")
    p["phone"] = re.sub(r"[^\d+]", "", p.get("phone") or "")
    cid = q("INSERT INTO candidates(name,email,phone,role,location,language,data,source,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (p.get("name"), p.get("email"), p["phone"], p.get("current_role"), p.get("location"),
             p.get("language"), json.dumps(p), "cv:" + file.filename, time.time()), write=True)
    return {"id": cid, **p}

# ---------- Spreadsheet import ----------
KEYS = {"name": ["name"], "email": ["mail"], "phone": ["phone", "mobile", "whatsapp", "tel"],
        "role": ["role", "title", "position"], "location": ["location", "city", "country"]}

@app.post("/api/sheet")
def sheet(file: UploadFile = File(...)):
    data, fn = file.file.read(), file.filename.lower()
    if fn.endswith(".xlsx"):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = [[("" if v is None else str(v)) for v in r] for r in ws.iter_rows(values_only=True)]
    elif fn.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "ignore"))))
    else:
        raise HTTPException(400, "Upload a .csv or .xlsx file.")
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
