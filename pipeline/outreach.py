"""Apprenticeship outreach: a list of established GTA electrical companies plus apprenticeship organizations.

Run: python -m pipeline.outreach find      (search companies, add the organizations, skip ones already listed)
     python -m pipeline.outreach draft [N]  (write emails for the next N contacts)
     python -m pipeline.outreach review     (read each draft; send, edit, skip or never contact)
     python -m pipeline.outreach replies    (mark replies and bounces from your Gmail)
     python -m pipeline.outreach list       (who's on the list and their status)

Companies come from OpenWeb Ninja's Local Business Data API (Google Maps). Only businesses with at least
`min_reviews` reviews are kept, so one-person contractors and supply stores are left out. Every request
is logged like JSearch. Nothing is sent unless you press s for that email in review, at most
`max_per_day` a day.
"""
import base64
import csv
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

import httpx

from pipeline import db
from pipeline.settings import CONFIG, ROOT, claude
from pipeline.sources import USER_AGENT

CFG = CONFIG["outreach"]
URL = "https://api.openwebninja.com/local-business-data/search"
ORGS = ROOT / "outreach_orgs.csv"


def api_key() -> str | None:
    return os.environ.get("LOCAL_BUSINESS_API_KEY") or os.environ.get("JSEARCH_API_KEY")


def parse(biz: dict) -> dict:
    # shortcut: field names follow OpenWeb Ninja's Local Business Data responses as documented by the
    # vendor; the first live search confirms them (unknown fields just come back empty).
    contacts = biz.get("emails_and_contacts") or {}
    emails = contacts.get("emails") or biz.get("emails") or []
    return {"source_id": biz.get("business_id") or biz.get("place_id") or biz.get("name"),
            "name": biz.get("name", "").strip(), "email": emails[0] if emails else None,
            "phone": biz.get("phone_number") or (contacts.get("phone_numbers") or [None])[0],
            "website": biz.get("website"), "address": biz.get("full_address") or biz.get("address"),
            "rating": biz.get("rating"), "review_count": biz.get("review_count") or 0}


def keep(c: dict) -> bool:
    name = c["name"].lower()
    return bool(name) and c["review_count"] >= CFG["min_reviews"] and \
        not any(w in name for w in CFG["exclude_name_words"])


def save(con, contacts: list[dict], kind: str) -> int:
    added = 0
    with con:
        for c in contacts:
            cur = con.execute(
                "INSERT OR IGNORE INTO outreach (kind, source_id, name, email, phone, website, address, rating, "
                "review_count, notes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, c["source_id"], c["name"], c.get("email"), c.get("phone"), c.get("website"), c.get("address"),
                 c.get("rating"), c.get("review_count"), c.get("notes"), "new" if c.get("email") else "no_email"))
            added += cur.rowcount
    return added


def add_organizations(con) -> int:
    with open(ORGS, newline="", encoding="utf-8") as f:
        orgs = [{**row, "source_id": row["name"], "email": row["email"] or None} for row in csv.DictReader(f)]
    return save(con, orgs, "organization")


def find(con, client=None) -> int:
    """Searches each query once; returns how many new companies were added."""
    key = api_key()
    if not key:
        raise SystemExit("No API key: add LOCAL_BUSINESS_API_KEY (or JSEARCH_API_KEY) to .env.")
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)
    added = 0
    for query in CFG["queries"]:
        if db.searched_recently(con, "local_business", query, days=30):
            continue  # companies don't change often; a month between repeats
        db.log_usage(con, "local_business", 1, note=query)
        try:
            r = client.get(URL, headers={"X-API-Key": key}, params={
                "query": query, "limit": CFG["results_per_query"], "region": "ca", "language": "en",
                "extract_emails_and_contacts": "true"})
            error = None if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:  # a timeout shouldn't stop the other areas
            error = repr(e)
        if error:
            print(f"'{query}' failed with {error}")
            with con:  # a failed search (e.g. rate limit) isn't a search: let the next run retry it
                con.execute("DELETE FROM api_usage WHERE rowid = (SELECT MAX(rowid) FROM api_usage WHERE note = ?)", (query,))
            continue
        found = [parse(b) for b in r.json().get("data", [])]
        kept = [c for c in found if keep(c)]
        n = save(con, kept, "company")
        print(f"'{query}': {len(found)} found, {len(kept)} with {CFG['min_reviews']}+ reviews, {n} new")
        added += n
    return added


# ---------- the apprentice resume attached to every email ----------

RESUME_POSTING = """Title: Electrical Apprentice (309A)
Company: GTA electrical contractors
Location: Toronto, ON

Entry-level 309A electrical apprentice for construction and maintenance electrical work: installing,
wiring and troubleshooting electrical systems, reading drawings, using test equipment, and following
safety procedures and the Ontario Electrical Safety Code on job sites.

Page limit: 1 page. Choose 6-10 bullets."""


def apprentice_resume(con, client) -> Path:
    """One tailored apprentice resume, made once (same honesty check as job resumes)."""
    from pipeline.resume import load_master
    from pipeline.tailor import TAILOR_MODEL, TAILOR_RULES, TAILOR_SCHEMA, _ask, master_text, render_to_fit, validate

    master = load_master()
    path = ROOT / "output" / "outreach" / f"{master['contact']['name']} - Resume.pdf"
    if path.exists():
        return path
    system = [{"type": "text", "text": TAILOR_RULES + master_text(master), "cache_control": {"type": "ephemeral"}}]
    problems = []
    for _ in range(2):
        note = ("\n\nFix these problems from your last answer:\n- " + "\n- ".join(problems)) if problems else ""
        result = _ask(client, con, TAILOR_MODEL, system, RESUME_POSTING + note, TAILOR_SCHEMA, 8000)
        if result is None:  # no usable answer this time; try once more
            problems = ["no answer"]
            continue
        problems, _ = validate(master, result, [], "Electrical Apprentice (309A)")
        if not problems:
            render_to_fit(master, result, path)
            return path
    raise SystemExit("The apprentice resume failed the honesty check twice: " + "; ".join(problems))


# ---------- drafting ----------

EMAIL_SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["subject", "body"], "additionalProperties": False}
EMAIL_RULES = f"""You write a short cold email from the candidate, in their own voice, using only facts from
their master resume below and the contact details you're given.

- To a company: ask whether they take on 309A electrical apprentices, or expect to, and offer to talk.
  To an apprenticeship organization: ask how to apply to their apprenticeship program or intake.
- Subject: short and plain, e.g. "309A apprenticeship inquiry - <candidate's name>".
- Body: a greeting ("Hello <company> team,"), two short paragraphs, under {CFG['max_words']} words. Say the
  resume is attached. No signature: the name, phone and email are added automatically.
- One or two concrete facts from the resume (their diploma or certificate, hands-on
  hardware and test-equipment work). Mention something about the contact only from the details given
  (their name, area, website); never invent projects, clients or other facts about them, and never
  claim preferences the candidate hasn't stated (e.g. wanting to work in a particular area).
- No sign-off ("Best regards" etc.); it's added automatically.
- Never use these phrases: {", ".join(CONFIG["cover_letter"]["banned_phrases"])}. At most one dash.
  Write like a person, not a template.

Candidate's style guide (from their own writing):
"""


def email_problems(email: dict, known_text: str) -> list[str]:
    from pipeline.tailor import _numbers

    body = email["body"]
    problems = [f'uses the banned phrase "{p}"' for p in CONFIG["cover_letter"]["banned_phrases"] if p.lower() in body.lower()]
    if len(body.split()) > CFG["max_words"]:
        problems.append(f"{len(body.split())} words; keep it under {CFG['max_words']}")
    if len(re.findall(r"[—–]| - ", body)) > 1:
        problems.append("too many dashes")
    added = _numbers(body) - _numbers(known_text + " 309A")  # 309A is the trade's code, not a claim
    if added:
        problems.append(f"numbers not in the resume or contact details: {sorted(added)}")
    if not email["subject"].strip():
        problems.append("empty subject")
    return problems


def _contact_text(c) -> str:
    return (f"Contact: {c['name']} ({'apprenticeship organization' if c['kind'] == 'organization' else 'electrical company'})\n"
            f"Website: {c['website'] or 'unknown'}\nAddress: {c['address'] or 'unknown'}\nNotes: {c['notes'] or ''}")


def draft(con, client, limit: int | None = None) -> int:
    """Writes emails for contacts that have an email and no draft yet; returns how many."""
    from pipeline.cover_letter import style_guide
    from pipeline.resume import load_master
    from pipeline.tailor import TAILOR_MODEL, _ask, master_text

    rows = con.execute("SELECT * FROM outreach WHERE status = 'new' AND email IS NOT NULL "
                       "ORDER BY kind DESC, review_count DESC LIMIT ?", (limit or CFG["draft_batch"],)).fetchall()
    if not rows:
        return 0
    master = load_master()
    whole = master_text(master)
    system = [{"type": "text", "text": EMAIL_RULES + style_guide(client, con) + "\n\nMaster resume:\n" + whole,
               "cache_control": {"type": "ephemeral"}}]
    for c in rows:
        user, email, problems = _contact_text(c), None, []
        for _ in range(2):
            note = ("\n\nYour last draft was rejected. Fix:\n- " + "\n- ".join(problems)) if problems else ""
            email = _ask(client, con, TAILOR_MODEL, system, user + note, EMAIL_SCHEMA, 4000)
            if email is None:
                break
            email["body"] = strip_sign_off(email["body"])
            problems = email_problems(email, whole + user)
            if not problems:
                break
        if email is None:
            continue
        email["problems"] = problems  # shown when you review it
        with con:
            con.execute("UPDATE outreach SET draft = ?, status = 'drafted' WHERE id = ?", (json.dumps(email), c["id"]))
        print(f"  drafted: {c['name']}" + (f" | check: {'; '.join(problems)}" if problems else ""))
    return len(rows)


# ---------- sending (only from review, one email at a time, after you press s) ----------

def signature(me: dict) -> str:
    return f"\n\nBest regards,\n{me['name']}\n{me['phone']}\n{me['email']}"


def strip_sign_off(body: str) -> str:
    """The signature adds the sign-off; drop one the model wrote anyway so it isn't doubled."""
    return re.sub(r"\n+\s*(best regards|kind regards|warm regards|regards|sincerely|thanks|thank you),?\s*$", "",
                  body.rstrip(), flags=re.I)


def send(con, svc, contact, email: dict, resume: Path, me: dict):
    msg = EmailMessage()
    msg["To"], msg["Subject"] = contact["email"], email["subject"]
    msg.set_content(email["body"] + signature(me))
    msg.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=resume.name)
    sent = svc.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
    with con:
        con.execute("UPDATE outreach SET status = 'sent', thread_id = ?, sent_at = datetime('now', 'localtime') "
                    "WHERE id = ?", (sent["threadId"], contact["id"]))


def edit_in_notepad(email: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(f"Subject: {email['subject']}\n\n{email['body']}")
    subprocess.run(["notepad", f.name])  # waits until you close Notepad
    text = Path(f.name).read_text(encoding="utf-8")
    os.unlink(f.name)
    first, _, body = text.partition("\n")
    return {**email, "subject": first.removeprefix("Subject:").strip(), "body": body.strip(), "problems": []}


def sent_today(con) -> int:
    return con.execute("SELECT COUNT(*) FROM outreach WHERE date(sent_at) = date('now', 'localtime')").fetchone()[0]


def review(con, ask=input, edit=edit_in_notepad, gmail_service=None, client=None) -> int:
    """Shows each draft; sends only the ones you choose. Returns how many were sent."""
    from pipeline.apply import personal
    from pipeline.resume import load_master

    me, svc, resume, sent = personal(load_master()), None, None, 0
    rows = con.execute("SELECT * FROM outreach WHERE status = 'drafted' ORDER BY kind DESC, review_count DESC").fetchall()
    for c in rows:
        if sent_today(con) >= CFG["max_per_day"]:
            print(f"Daily limit of {CFG['max_per_day']} emails reached (protects your Gmail from spam flags).")
            break
        email = json.loads(c["draft"])
        while True:
            print(f"\n=== {c['name']} <{c['email']}> | {c['review_count'] or '-'} reviews | {c['website'] or ''}")
            print(f"Subject: {email['subject']}\n\n{email['body']}{signature(me)}\n[attached: apprentice resume]")
            if email.get("problems"):
                print("Check: " + "; ".join(email["problems"]))
            choice = ask("[s]end  [e]dit  [k] skip  [n]ever contact  [q]uit: ").strip().lower()[:1]
            if choice == "e":
                email = edit(email)
                with con:
                    con.execute("UPDATE outreach SET draft = ? WHERE id = ?", (json.dumps(email), c["id"]))
                continue
            break
        if choice == "q":
            break
        if choice == "n":
            with con:
                con.execute("UPDATE outreach SET status = 'do_not_contact' WHERE id = ?", (c["id"],))
        elif choice == "s":
            if svc is None:
                from pipeline.sources.gmail import SEND_SCOPES, service
                svc = gmail_service or service(SEND_SCOPES)
                resume = apprentice_resume(con, client or claude())
            send(con, svc, c, email, resume, me)
            sent += 1
            print(f"  sent to {c['email']}")
    return sent


# ---------- replies ----------

def check_replies(con, svc, my_email: str) -> dict:
    """Marks sent emails replied (or bounced) by looking at each email's Gmail thread. Read-only."""
    counts = {"replied": 0, "bounced": 0}
    rows = con.execute("SELECT * FROM outreach WHERE thread_id IS NOT NULL AND (status = 'sent' "
                       "OR (status IN ('replied', 'bounced') AND reply IS NULL))").fetchall()  # also fills in older replies' text
    for c in rows:
        thread = svc.users().threads().get(userId="me", id=c["thread_id"], format="metadata",
                                           metadataHeaders=["From"]).execute()
        theirs = [(h["value"].lower(), m.get("snippet", "")) for m in thread.get("messages", [])
                  for h in m.get("payload", {}).get("headers", []) if h["name"] == "From"
                  and my_email.lower() not in h["value"].lower()]
        if not theirs:
            continue
        status = "bounced" if any("mailer-daemon" in s or "postmaster" in s for s, _ in theirs) else "replied"
        if c["status"] == "sent":
            counts[status] += 1
        with con:
            con.execute("UPDATE outreach SET status = ?, reply = ?, replied_at = COALESCE(replied_at, datetime('now', 'localtime')) "
                        "WHERE id = ?", (status, html.unescape(theirs[0][1]), c["id"]))
    return counts


def main(argv=None) -> int:
    args = argv or sys.argv[1:] or ["list"]
    con = db.connect()
    if args[0] == "find":
        print(f"Organizations added: {add_organizations(con)}")
        print(f"Companies added: {find(con)}")
    elif args[0] == "draft":
        print(f"Drafted {draft(con, claude(), int(args[1]) if len(args) > 1 else None)} emails. Next: review")
        return 0
    elif args[0] == "review":
        print(f"Sent {review(con)} emails.")
        return 0
    elif args[0] == "replies":
        from pipeline.resume import load_master
        from pipeline.sources.gmail import service
        found = check_replies(con, service(), load_master()["contact"]["email"])
        print(f"New replies: {found['replied']}, bounced: {found['bounced']}")
        return 0
    rows = con.execute("SELECT * FROM outreach ORDER BY kind DESC, review_count DESC").fetchall()
    for r in rows:
        print(f"[{r['id']}] {r['status']:14} {r['name'][:38]:38} {r['email'] or '(no email)':36} "
              f"{r['phone'] or '':13} {r['review_count'] or '':>4} {('sent ' + r['sent_at'][:10]) if r['sent_at'] else ''}")
    counts = dict(con.execute("SELECT status, COUNT(*) FROM outreach GROUP BY status").fetchall())
    print(f"{len(rows)} on the list: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
