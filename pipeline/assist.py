"""A floating "Autofill" button on every page of the tool's browser window.

Run: python -m pipeline.assist <job id>     (opens the job's link; works on most application sites)

Click the button whenever a page has empty fields: the tool fills what it can from your profile, resume,
cover letter and answer bank, and the button shows what's still left. It never clicks Continue or Submit.
The Workday assistant uses the same button while a page is handed to you.
"""
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from pipeline import db
from pipeline.apply import mark_submitted, personal
from pipeline.resume import load_master
from pipeline.settings import claude

# Added to every page the window opens; a click only sets a flag, which the tool's loop picks up.
BUTTON_JS = """(() => {
  const add = () => {
    if (document.getElementById('jlb-autofill') || !document.body) return;
    const b = document.createElement('button');
    b.id = 'jlb-autofill';
    b.type = 'button';  // never a submit button
    b.textContent = 'Autofill';
    b.title = 'Fill this page from your profile and answer bank (job-less-bot)';
    b.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647;padding:10px 16px;' +
      'border:0;border-radius:20px;background:#1f6feb;color:#fff;font:600 14px Arial,sans-serif;' +
      'box-shadow:0 2px 8px rgba(0,0,0,.3);cursor:pointer;max-width:340px;text-align:left';
    b.addEventListener('click', e => { e.preventDefault(); e.stopPropagation();
      window.__jlbFill = true; b.textContent = 'Filling...'; });
    document.body.appendChild(b);
  };
  // This runs before the page exists: watch the document itself, and add the button once it loads
  // (and again if the site re-renders and removes it).
  document.addEventListener('DOMContentLoaded', add);
  new MutationObserver(add).observe(document, {childList: true, subtree: true});
})();"""


BOT_CHECK = ("verify you are human", "performing security verification", "checking your browser",
             "are you a robot", "press & hold")


def is_bot_check(page) -> bool:
    """Sites like Cloudflare block automated browsers. We never try to get around that."""
    try:
        text = (page.title() + " " + page.inner_text("body")[:3000]).lower()
    except Exception:
        return False
    return any(phrase in text for phrase in BOT_CHECK) or page.title().lower().startswith("just a moment")


def open_in_firefox(url: str):
    """Your own Firefox (where the job-less-bot autofill userscript lives); the default browser if it isn't installed."""
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    exe = shutil.which("firefox") or next((str(p) for r in roots if r for p in [Path(r) / "Mozilla Firefox" / "firefox.exe"]
                                           if p.exists()), None)
    if exe:
        subprocess.Popen([exe, url])
    else:
        webbrowser.open(url)


def install(context):
    context.add_init_script(BUTTON_JS)


def fill_if_clicked(session) -> bool:
    """One check: if the Autofill button was clicked, fill the page and report on the button."""
    page = session.page
    if not page.evaluate("() => !!window.__jlbFill"):
        return False
    page.evaluate("() => { window.__jlbFill = false; }")
    try:
        left = session.fill_page()
        msg = ("Filled. Still needed: " + "; ".join(left)[:160]) if left else "Filled. Nothing required is left."
    except Exception as e:  # report on the button instead of crashing the window
        msg = f"Couldn't fill everything ({type(e).__name__})."
        session.log(f"autofill error: {e!r}"[:300])
    session.log(f"autofill: {msg}")
    page.evaluate("m => { const b = document.getElementById('jlb-autofill'); if (b) b.textContent = 'Autofill · ' + m; }", msg)
    return True


def wait_with_button(session, prompt: str):
    """Waits for Enter in the terminal while answering Autofill clicks in the browser."""
    import msvcrt  # Windows: lets us watch the keyboard without blocking

    print(prompt, end="", flush=True)
    while True:
        if msvcrt.kbhit() and msvcrt.getwch() in "\r\n":
            print()
            return
        try:
            fill_if_clicked(session)
        except Exception:
            return  # the browser was closed
        time.sleep(0.4)


def run(job_id: int):
    from playwright.sync_api import sync_playwright

    from pipeline.workday import PROFILE_DIR, Session

    con = db.connect()
    job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    app = con.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if job is None or app is None or not app["resume_path"]:
        raise SystemExit(f"Job {job_id} has no prepared application yet.")
    print(f"{job['title']} @ {job['company']}\n  {job['url']}\n"
          "  Use the blue Autofill button (bottom right) on any page that needs filling. Submit yourself.")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900})
        install(browser)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(job["url"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)
        if is_bot_check(page):
            browser.close()
            print("  This site blocks automated browsers with a 'verify you are human' check, so it's opened in Firefox\n"
                  "  instead. Pass the check, click Autofill there, attach your files and submit.")
            open_in_firefox(job["url"])
            if input("  -> Did you submit the application? (y/n) ").strip().lower().startswith("y"):
                mark_submitted(con, job_id)
            return
        session = Session(page, con, claude(), job, app, personal(load_master()))
        session.log(f"assist: {job['title']} @ {job['company']}")
        wait_with_button(session, "  -> Press Enter here when you're done: ")
        if input("  -> Did you submit the application? (y/n) ").strip().lower().startswith("y"):
            mark_submitted(con, job_id)
            session.log("marked as submitted")
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Usage: python -m pipeline.assist <job id>")
    run(int(sys.argv[1]))
