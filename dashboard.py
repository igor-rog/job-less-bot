"""Dashboard: review, edit, approve or skip everything the pipeline prepares.

Run: .venv\\Scripts\\streamlit run dashboard.py      (opens http://localhost:8501)

Anything that drives a browser (submitting a form, the Workday assistant, adding a job) runs as its own
process, so the page stays responsive. Nothing is submitted or sent without a click here.
"""
import html
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import pandas as pd
import streamlit as st
import yaml

from pipeline import answers, cover_letter, db, outreach, userscript
from pipeline.apply import mark_submitted, personal
from pipeline.assist import open_in_firefox
from pipeline.resume import MASTER_PATH, load_master
from pipeline.settings import CONFIG, ROOT, claude, set_config_value
from pipeline.sources.jsearch import link_rank

st.set_page_config(page_title="job(less)-bot", page_icon=":material/work:", layout="wide")
PY = sys.executable


def con():
    return db.connect()  # one per rerun: Streamlit reruns can switch threads


def run_module(*args: str) -> str:
    """Runs a pipeline command in its own process and returns its output."""
    out = subprocess.run([PY, "-X", "utf8", "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return (out.stdout + out.stderr).strip()


def open_file(path: str | None):
    if path and Path(path).exists():
        os.startfile(path)  # opens in your PDF viewer / image viewer
    else:
        st.toast("File not found")


# ---------- Review queue ----------

def review_page():
    c = con()
    today = date.today().isoformat()
    stats = c.execute("""SELECT
        (SELECT COUNT(*) FROM jobs WHERE date_found = ?),
        (SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.submitted_at IS NULL AND j.status != 'skipped' AND a.resume_path IS NOT NULL),
        (SELECT COUNT(*) FROM applications WHERE submitted_at IS NOT NULL),
        (SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.apply_status = 'manual' AND a.submitted_at IS NULL AND j.status != 'skipped')""", (today,)).fetchone()
    cols = st.columns(4)
    for col, label, value in zip(cols, ["New jobs today", "Ready to review", "Submitted", "Apply manually"], stats):
        col.metric(label, value)

    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    search = f1.text_input("Search", placeholder="Title or company")
    min_score = f2.slider("Minimum fit score", 0, 100, 0)
    sources = [r[0] for r in c.execute("SELECT DISTINCT source FROM jobs ORDER BY 1")]
    picked = f3.multiselect("Source", sources)
    view = f4.selectbox("Show", ["To review", "Submitted", "Skipped", "All"])

    where, params = ["a.resume_path IS NOT NULL", "COALESCE(j.fit_score, 0) >= ?"], [min_score]
    if search:
        where.append("(j.title LIKE ? OR j.company LIKE ?)")
        params += [f"%{search}%"] * 2
    if picked:
        where.append(f"j.source IN ({','.join('?' * len(picked))})")
        params += picked
    where.append({"To review": "a.submitted_at IS NULL AND j.status != 'skipped'", "Submitted": "a.submitted_at IS NOT NULL",
                  "Skipped": "j.status = 'skipped'", "All": "1"}[view])
    rows = c.execute(f"SELECT j.*, a.* FROM jobs j JOIN applications a ON a.job_id = j.id WHERE {' AND '.join(where)} "
                     "ORDER BY j.fit_score DESC", params).fetchall()
    st.caption(f"{len(rows)} applications")
    for r in rows:
        job_card(c, r)


def job_card(c, r):
    jid = r["job_id"]
    with st.container(border=True):
        top, right = st.columns([5, 2])
        top.markdown(f"<span style='font-size:1.15rem'><b>{html.escape(r['title'])}</b> · "
                     f"{html.escape(r['company'] or '')}</span>", unsafe_allow_html=True)
        kw = f"{r['keywords_matched']} of {r['keywords_total']} terms" if r["keywords_total"] else "-"
        where = r["apply_system"] if r["apply_system"] != "manual" else urlparse(r["url"]).netloc.removeprefix("www.")
        top.caption(f"{r['location'] or 'Location not stated'} · {where} · keywords {kw} · "
                    f"{r['apply_status'] or 'prepared'}"
                    + (" · submitted " + r["submitted_at"][:10] if r["submitted_at"] else ""))
        fit = r["fit_score"] or 0
        right.progress(fit / 100, text=f"Fit {fit} / 100")
        if r["fit_reason"]:
            right.caption(r["fit_reason"])
        notes = [n for n in (r["apply_note"], r["flag"], r["letter_flag"]) if n]
        if notes:
            with st.expander(f"Notes to check ({len(notes)})"):
                for label, note in zip(["Form", "Resume", "Cover letter"], (r["apply_note"], r["flag"], r["letter_flag"])):
                    if note:
                        st.markdown(f"**{label}:** " + note.replace("; ", "  \n"))
        if r["letter_status"] == "needs_edit":
            st.caption(":blue[Cover letter: make your personal edit, or mark it reviewed, before approving.]")

        b = st.columns(5)
        if b[0].button("Resume", key=f"res{jid}", icon=":material/description:"):
            open_file(r["resume_path"])
        if b[1].button("Cover letter", key=f"let{jid}", icon=":material/mail:"):
            open_file(r["letter_path"])
        if r["screenshot_path"] and Path(r["screenshot_path"]).exists() and b[2].button("Form screenshot", key=f"shot{jid}",
                                                                                        icon=":material/image:"):
            open_file(r["screenshot_path"])
        if r["resume_path"] and b[3].button("Files", key=f"dir{jid}", icon=":material/folder_open:"):
            open_file(str(Path(r["resume_path"]).parent))
        if b[4].button("Skip", key=f"skip{jid}", icon=":material/block:"):
            with c:
                c.execute("UPDATE jobs SET status = 'skipped' WHERE id = ?", (jid,))
            st.rerun()

        with st.expander("Edit cover letter"):
            edit_letter(c, r)
        if r["form_answers"]:
            with st.expander("Edit answers"):
                edit_answers(c, r)
        if not r["submitted_at"]:
            approve_controls(c, r)


def edit_letter(c, r):
    if not r["letter"]:
        st.caption("No cover letter yet.")
        return
    letter = json.loads(r["letter"])
    text = st.text_area("Your edit", "\n\n".join(letter["paragraphs"]), height=260, key=f"lt{r['job_id']}",
                        help="Paragraphs are separated by a blank line. Saving re-renders the PDF and text version.")
    s1, s2 = st.columns(2)
    if s1.button("Save and re-render", key=f"ls{r['job_id']}", type="primary"):
        letter["paragraphs"] = [p.strip() for p in text.split("\n\n") if p.strip()]
        problems = cover_letter.style_problems(letter)
        job = c.execute("SELECT * FROM jobs WHERE id = ?", (r["job_id"],)).fetchone()
        cover_letter.render_letter(load_master(), job, letter)
        with c:
            c.execute("UPDATE applications SET letter = ?, letter_status = 'edited', letter_flag = ? WHERE job_id = ?",
                      (json.dumps(letter), "; ".join(problems) or None, r["job_id"]))
        st.toast("Saved" + (f" (check: {'; '.join(problems)})" if problems else ""))
        st.rerun()
    if r["letter_status"] == "needs_edit" and s2.button("Looks right as is", key=f"lr{r['job_id']}"):
        with c:
            c.execute("UPDATE applications SET letter_status = 'reviewed' WHERE job_id = ?", (r["job_id"],))
        st.rerun()


def edit_answers(c, r):
    filled = json.loads(r["form_answers"])
    new = {}
    for i, a in enumerate(filled):
        new[a["question"]] = st.text_input(f"{a['question']}  ({a['status']})", a["answer"], key=f"fa{r['job_id']}_{i}")
    if st.button("Save to answer bank", key=f"fas{r['job_id']}"):
        for q, text in new.items():
            row = answers.find(c, q)
            if row is not None:
                answers.approve(c, row["id"], text)
            elif text:
                with c:
                    c.execute("INSERT INTO answers (question, answer, status, source) VALUES (?, ?, 'approved', 'you')", (q, text))
        with c:
            c.execute("UPDATE applications SET apply_status = NULL WHERE job_id = ? AND apply_status = 'needs_answers'",
                      (r["job_id"],))
        st.toast("Saved. The form is filled again with these on the next run.")


def approve_controls(c, r):
    jid, system = r["job_id"], r["apply_system"]
    ready = r["letter_status"] != "needs_edit"
    a1, a2 = st.columns([2, 3])
    if system in ("greenhouse", "lever"):
        disabled = not ready or r["apply_status"] != "filled"
        if a1.button("Approve and submit", key=f"ap{jid}", type="primary", disabled=disabled, icon=":material/send:"):
            with st.spinner("A browser window opens and submits. If the site asks for a security code, enter it "
                            "there from your email and press Submit..."):
                st.code(run_module("pipeline.apply", "submit", str(jid)))
        if disabled:
            a2.caption("Needs a filled form and a reviewed cover letter first.")
    else:
        if system == "workday":
            if a1.button("Open Workday assistant", key=f"wd{jid}", type="primary", icon=":material/smart_toy:"):
                subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", PY, "-m", "pipeline.workday", str(jid)], cwd=ROOT)
                st.toast("Opened in a new window: sign in there, then follow the prompts.")
        else:
            rank = link_rank(r["url"])
            if a1.button("Apply", key=f"as{jid}", type="primary", icon=":material/auto_fix_high:"):
                open_file(str(Path(r["resume_path"]).parent))  # your resume and letter, ready to attach
                if rank == 3:  # a job-board copy: apply on the employer's own page instead
                    title = re.split(r"\s[—–-]\s", r["title"])[0]
                    open_in_firefox("https://www.google.com/search?q=" + quote_plus(f'"{r["company"]}" {title} careers'))
                    st.toast("Job-board copy: Firefox is searching for the employer's own posting. Use Autofill there.")
                elif rank == 2:  # Indeed, LinkedIn and the like block automated browsers; never automated anyway
                    open_in_firefox(r["url"])
                    st.toast("Opened in Firefox: use the Autofill button on the application form.")
                else:
                    subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", PY, "-m", "pipeline.assist", str(jid)], cwd=ROOT)
                    st.toast("Opened with autofill in a new window (Firefox instead if the site blocks it).")
            a1.caption({3: "Finds the employer's own posting in Firefox", 2: "Opens in Firefox"}.get(rank, "Opens with autofill")
                       + " and your files folder")
            if rank >= 2:  # found the employer's posting? The tool can then fill it, drafting job-specific answers
                link = a2.text_input("Employer's application link", key=f"el{jid}", placeholder="Paste it here",
                                     label_visibility="collapsed")
                if link and a2.button("Use employer's link", key=f"rl{jid}", icon=":material/link:"):
                    with st.spinner("Filling the employer's form in the background..."):
                        st.code(run_module("pipeline.apply", "relink", str(jid), link.strip())[-600:])
                    st.rerun()
        if a2.button("I submitted it", key=f"ms{jid}"):
            mark_submitted(c, jid)
            st.rerun()


# ---------- Add a job ----------

def add_job_page():
    st.subheader("Add a job")
    links = st.text_area("Job links (one per line)")
    desc = st.text_area("Job description (only for a single link that can't be fetched)", height=150)
    st.caption("Indeed and LinkedIn links aren't fetched. Copy the job description into a text file and add "
               "--file description.txt (here: paste it into the description box).")
    if st.button("Add and prepare", type="primary") and links.strip():
        args = ["pipeline.add_job", *links.split()]
        if desc.strip():
            if len(links.split()) != 1:
                st.error("A description goes with exactly one link.")
                return
            tmp = ROOT / "output" / "pasted_description.txt"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_text(desc, encoding="utf-8")
            args += ["--file", str(tmp)]
        with st.spinner("Fetching, scoring, tailoring and writing the cover letter..."):
            st.code(run_module(*args))


# ---------- Watchlist ----------

def watchlist_page():
    st.subheader("Watchlist")
    path = ROOT / "watchlist.csv"
    df = pd.read_csv(path, dtype=str).fillna("")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True, hide_index=True)
    st.caption("Add a row at the bottom; select rows and press Delete to remove. title_words: words separated by ; "
               "(leave empty for the default electrical/technician words).")
    if st.button("Save watchlist", type="primary"):
        edited.to_csv(path, index=False, lineterminator="\n")
        st.toast("Saved")


# ---------- Answer bank ----------

def answers_page():
    st.subheader("Answer bank")
    c = con()
    pending = c.execute("SELECT * FROM answers WHERE status != 'approved' ORDER BY status, id DESC").fetchall()
    st.markdown(f"**Waiting on you ({len(pending)})**")
    for r in pending:
        with st.container(border=True):
            st.markdown(r["question"])
            st.caption(("AI draft, check it" if r["status"] == "draft" else "Only you can answer this")
                       + (" · for one job only" if r["job_id"] else ""))
            if st.session_state.pop(f"revised{r['id']}", False):  # set before the boxes exist, or the browser's old text wins
                st.session_state[f"ans{r['id']}"], st.session_state[f"chg{r['id']}"] = r["answer"], ""
            text = st.text_area("Answer", r["answer"], key=f"ans{r['id']}", label_visibility="collapsed")
            ch, go = st.columns([5, 1], vertical_alignment="bottom")
            change = ch.text_input("Change with AI", key=f"chg{r['id']}",
                                   placeholder='e.g. "shorter", "mention my capstone project", "I\'m a Canadian citizen in Toronto"')
            # Buttons here are never disabled: typed text only registers on Enter or click-away, so a
            # disabled check would leave them greyed out while you type.
            if go.button("Revise", key=f"rev{r['id']}", icon=":material/auto_fix_high:"):
                if not change.strip():
                    st.toast("Type the change first.")
                else:
                    try:
                        with st.spinner("Rewriting..."):
                            answers.revise(c, claude(), r["id"], text, change)
                    except SystemExit as e:  # e.g. the monthly budget is reached
                        st.error(str(e))
                    else:
                        st.session_state[f"revised{r['id']}"] = True  # show the new text, clear the change box
                        st.rerun()
            x1, x2, _ = st.columns([1, 1, 4])
            if x1.button("Approve", key=f"apr{r['id']}", type="primary"):
                if not text.strip():
                    st.toast("Write an answer first.")
                else:
                    answers.approve(c, r["id"], text)
                    st.rerun()
            if x2.button("Delete", key=f"del{r['id']}"):
                with c:
                    c.execute("DELETE FROM answers WHERE id = ?", (r["id"],))
                st.rerun()
    st.markdown("**Approved**")
    approved = pd.DataFrame([dict(r) for r in c.execute(
        "SELECT id, question, answer FROM answers WHERE status = 'approved' ORDER BY question")], columns=["id", "question", "answer"])
    edited = st.data_editor(approved, disabled=["id", "question"], hide_index=True, use_container_width=True)
    if st.button("Save changes"):
        for _, row in edited.iterrows():
            answers.approve(c, int(row["id"]), row["answer"])
        st.toast("Saved")


# ---------- Outreach ----------

def outreach_page():
    st.subheader("Apprenticeship outreach")
    c = con()
    counts = dict(c.execute("SELECT status, COUNT(*) FROM outreach GROUP BY status").fetchall())
    cols = st.columns(5)
    for col, key, label in zip(cols, ["drafted", "sent", "replied", "bounced", "new"],
                               ["Drafts to review", "Sent", "Replied", "Bounced", "Not drafted yet"]):
        col.metric(label, counts.get(key, 0))
    o1, o2, o3, _ = st.columns([1, 1, 1, 2])
    if o1.button("Find companies"):
        with st.spinner("Searching..."):
            st.code(run_module("pipeline.outreach", "find")[-1500:])
    if o2.button("Write 20 drafts"):
        with st.spinner("Writing emails..."):
            st.code(run_module("pipeline.outreach", "draft", "20")[-1500:])
    if o3.button("Check replies"):
        with st.spinner("Checking Gmail..."):
            st.toast(run_module("pipeline.outreach", "replies").splitlines()[-1])
        st.rerun()

    replies = c.execute("SELECT * FROM outreach WHERE status IN ('replied', 'bounced') ORDER BY replied_at DESC").fetchall()
    if replies:
        st.markdown(f"**Replies ({len(replies)})**")
        for r in replies:
            with st.container(border=True):
                st.markdown(f"**{r['name']}** · {'bounced' if r['status'] == 'bounced' else 'replied'} {(r['replied_at'] or '')[:16]}"
                            f" · [open in Gmail](https://mail.google.com/mail/u/0/#all/{r['thread_id']})")
                st.caption(r["reply"] or "Click Check replies to load the reply text.")

    sent_today = outreach.sent_today(c)
    st.caption(f"Sent today: {sent_today} of {outreach.CFG['max_per_day']}. Each email is sent only when you press Send.")
    me = personal(load_master())
    for r in c.execute("SELECT * FROM outreach WHERE status = 'drafted' ORDER BY kind DESC, review_count DESC").fetchall():
        email = json.loads(r["draft"])
        with st.container(border=True):
            st.markdown(f"**{r['name']}** · {r['email']}")
            details = [f"{r['review_count']} Google reviews" if r["review_count"] else "", r["website"] or "", r["phone"] or ""]
            st.caption(" · ".join(d for d in details if d))
            subject = st.text_input("Subject", email["subject"], key=f"os{r['id']}")
            body = st.text_area("Email", email["body"], height=200, key=f"ob{r['id']}")
            st.caption("Signature added automatically:" + outreach.signature(me).replace("\n", "  \n") +
                       "  \nAttached: your apprentice resume")
            if email.get("problems"):
                st.caption(f":orange[Check: {'; '.join(email['problems'])}]")
            x1, x2, x3, _ = st.columns([1, 1, 1, 3])
            if x1.button("Send", key=f"osend{r['id']}", type="primary", disabled=sent_today >= outreach.CFG["max_per_day"]):
                from pipeline.settings import claude
                from pipeline.sources.gmail import SEND_SCOPES, service
                email.update(subject=subject, body=body)
                outreach.send(c, service(SEND_SCOPES), r, email, outreach.apprentice_resume(c, claude()), me)
                st.toast(f"Sent to {r['email']}")
                st.rerun()
            if x2.button("Skip", key=f"oskip{r['id']}"):
                with c:
                    c.execute("UPDATE outreach SET status = 'new', draft = NULL WHERE id = ?", (r["id"],))
                st.rerun()
            if x3.button("Never contact", key=f"onev{r['id']}"):
                with c:
                    c.execute("UPDATE outreach SET status = 'do_not_contact' WHERE id = ?", (r["id"],))
                st.rerun()
    with st.expander("Everyone on the list"):
        st.dataframe(pd.DataFrame([dict(r) for r in c.execute(
            "SELECT name, status, email, phone, review_count, sent_at, replied_at FROM outreach ORDER BY status, review_count DESC")]),
            hide_index=True, use_container_width=True)


# ---------- Profile ----------

def profile_page():
    st.subheader("Profile")
    tab1, tab2, tab3 = st.tabs(["Master resume", "Writing samples", "Style guide"])
    with tab1:
        text = st.text_area("data/master_resume.yaml", MASTER_PATH.read_text(encoding="utf-8"), height=500)
        if st.button("Save master resume", type="primary"):
            backup = MASTER_PATH.read_text(encoding="utf-8")
            MASTER_PATH.write_text(text, encoding="utf-8")
            try:
                load_master()  # validates YAML and unique bullet IDs
                st.toast("Saved")
            except Exception as e:
                MASTER_PATH.write_text(backup, encoding="utf-8")
                st.error(f"Not saved, the file has a problem: {e}")
        up = st.file_uploader("Upload your latest resume PDF (kept in data/ for reference)", type="pdf")
        if up:
            (ROOT / "data" / "uploaded_resume.pdf").write_bytes(up.getvalue())
            st.toast("Saved to data/uploaded_resume.pdf")
    with tab2:
        folder = cover_letter.SAMPLES
        folder.mkdir(exist_ok=True)
        for f in sorted(folder.glob("*.txt")):
            s1, s2 = st.columns([5, 1])
            s1.markdown(f"`{f.name}`")
            if s2.button("Delete", key=f"ws{f.name}"):
                f.unlink()
                st.rerun()
        name = st.text_input("New sample name", placeholder="email_to_manager")
        body = st.text_area("Paste an email or cover letter you wrote yourself")
        if st.button("Add sample") and name and body:
            (folder / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.txt").write_text(body, encoding="utf-8")
            st.toast("Added; the style guide is rebuilt on the next cover letter run.")
            st.rerun()
    with tab3:
        guide = cover_letter.STYLE_GUIDE
        if guide.exists():
            marker, _, text = guide.read_text(encoding="utf-8").partition("\n")
            new = st.text_area("Style guide (edit freely)", text, height=300)
            if st.button("Save style guide"):
                guide.write_text(f"{marker}\n{new}", encoding="utf-8")
                st.toast("Saved")
        else:
            st.caption("Created the first time cover letters are written.")


# ---------- Settings ----------

def settings_page():
    st.subheader("Settings")
    path = ROOT / "config.yaml"
    text = path.read_text(encoding="utf-8")
    s1, s2, s3 = st.columns(3)
    score = s1.number_input("Minimum fit score", 0, 100, CONFIG["scoring"]["min_fit_score"])
    budget = s2.number_input("Monthly Claude budget ($)", 0.0, 500.0, float(CONFIG["limits"]["monthly_budget_usd"]), step=1.0)
    cap = s3.number_input("Daily application cap (0 = none)", 0, 500, CONFIG["limits"]["daily_application_cap"] or 0)
    m1, m2 = st.columns(2)
    scoring = m1.text_input("Scoring model", CONFIG["models"]["scoring"])
    tailoring = m2.text_input("Tailoring and letters model", CONFIG["models"]["tailoring"])
    if st.button("Save these", type="primary"):
        for key, value in [("  min_fit_score", score), ("  monthly_budget_usd", f"{budget:.2f}"),
                           # indented: the model lines, not the top-level "scoring:" section
                           ("  daily_application_cap", cap or "null"), ("  scoring", scoring), ("  tailoring", tailoring)]:
            text = set_config_value(text, key, value)
        try:
            yaml.safe_load(text)
            path.write_text(text, encoding="utf-8")
            st.toast("Saved. Takes effect on the next run.")
        except yaml.YAMLError as e:
            st.error(f"Not saved: {e}")
    with st.expander("Everything else (keywords, searches, locations, outreach...)"):
        raw = st.text_area("config.yaml", text, height=500)
        if st.button("Save config.yaml"):
            try:
                yaml.safe_load(raw)
                path.write_text(raw, encoding="utf-8")
                st.toast("Saved")
            except yaml.YAMLError as e:
                st.error(f"Not saved, the YAML has a problem: {e}")
    st.caption("Scan schedule: set up in Phase 8 (Windows Task Scheduler).")


# ---------- Usage ----------

def usage_page():
    st.subheader("Usage this month")
    c = con()
    _, spent = db.usage_this_month(c, "claude")
    budget = CONFIG["limits"]["monthly_budget_usd"]
    st.metric("Claude API spend", rf"\${spent:.2f} of \${budget:.2f}")  # escaped: "$...$" renders as math
    st.progress(min(spent / budget, 1.0) if budget else 0.0)
    used, _ = db.usage_this_month(c, "jsearch")
    limit = CONFIG["limits"]["jsearch_monthly_requests"]
    st.metric("JSearch requests", f"{used} of {limit}")
    st.progress(min(used / limit, 1.0))
    st.metric("Local Business Data requests", db.usage_this_month(c, "local_business")[0])
    st.markdown("**Daily runs**")
    st.dataframe(pd.DataFrame([dict(r) for r in c.execute(
        "SELECT started_at, finished_at, new_jobs, claude_cost_usd, claude_tokens, jsearch_requests, failed "
        "FROM runs ORDER BY id DESC LIMIT 60")]), hide_index=True, use_container_width=True)
    st.markdown("**Every API call**")
    st.dataframe(pd.DataFrame([dict(r) for r in c.execute(
        "SELECT ts, service, units, ROUND(cost_usd, 4) AS cost_usd, note FROM api_usage ORDER BY ts DESC LIMIT 100")]),
        hide_index=True, use_container_width=True)


with st.sidebar:  # usage at a glance on every page
    _c = con()
    _jsearch = db.usage_this_month(_c, "jsearch")[0]
    _spent = db.usage_this_month(_c, "claude")[1]
    st.caption(f"JSearch: {_jsearch} of {CONFIG['limits']['jsearch_monthly_requests']} requests this month  \n"
               rf"Claude: \${_spent:.2f} of \${CONFIG['limits']['monthly_budget_usd']:.2f}")
    _run = _c.execute("SELECT *, started_at > datetime('now', 'localtime', '-2 hours') AS recent "
                      "FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    _running = bool(_run and not _run["finished_at"] and _run["recent"])  # older unfinished runs were stopped
    if st.button("Run pipeline now", icon=":material/refresh:", disabled=_running,
                 help="Finds new jobs, scores them, writes resumes and letters, prepares forms. Nothing is submitted."):
        subprocess.Popen(["cmd", "/c", "start", "job-less-bot run", "cmd", "/k", PY, "-X", "utf8", "-m", "pipeline.run"], cwd=ROOT)
        st.toast("Running in a new window; it takes a few minutes. Reload the page when it says Done.")
    if _run:
        _when = datetime.strptime(_run["started_at"], "%Y-%m-%d %H:%M:%S").strftime("%b %d, %I:%M %p").replace(" 0", " ")
        st.caption(f"Last run: {_when} · " + (
            "running now" if _running else "stopped before finishing" if not _run["finished_at"] else
            f"{_run['new_jobs']} new jobs" + (f"  \n:orange[Failed: {_run['failed']}]" if _run["failed"] else "")))
    else:
        st.caption("Last run: never")
    if st.button("Firefox autofill script", icon=":material/folder_open:", help="Drag the file into Firefox to (re)install it"):
        userscript.refresh(_c)
        open_file(str(userscript.OUT.parent))

st.navigation([
    st.Page(review_page, title="Review queue", icon=":material/inbox:", default=True),
    st.Page(add_job_page, title="Add a job", url_path="add-job", icon=":material/add_link:"),
    st.Page(outreach_page, title="Outreach", url_path="outreach", icon=":material/forward_to_inbox:"),
    st.Page(answers_page, title="Answer bank", url_path="answers", icon=":material/quiz:"),
    st.Page(watchlist_page, title="Watchlist", url_path="watchlist", icon=":material/list:"),
    st.Page(profile_page, title="Profile", url_path="profile", icon=":material/person:"),
    st.Page(settings_page, title="Settings", url_path="settings", icon=":material/settings:"),
    st.Page(usage_page, title="Usage", url_path="usage", icon=":material/monitoring:"),
]).run()

try:  # keep the Firefox script in step with the answer bank and profile
    if userscript.refresh(con()):
        st.toast("Firefox autofill script updated with your latest answers. To use them, drag "
                 "output/firefox/job-autofill.user.js into Firefox and reinstall.", icon=":material/sync:")
except Exception as e:  # e.g. the master resume is mid-edit; never break the page over it
    st.sidebar.caption(f"Autofill script not updated: {type(e).__name__}")
