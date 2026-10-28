# job(less)-bot

A personal job-application pipeline that runs on your own computer. Every day it finds new postings, scores how well each one fits, writes a tailored resume and cover letter **using only your real experience**, and fills in the application form. Nothing is ever submitted or sent until you approve it in the dashboard.

I built it for my own job search as an electrical engineering technician, working with Claude Code as a coding partner, and use it every day.

## What it does

```
 job sources                  AI steps (Claude)                   you
 ───────────                  ─────────────────                   ───
 JSearch API  ─┐
 Gmail alerts ─┼─► dedupe ─► score fit ─► tailor resume ─► write cover letter ─► fill form ─► review + approve
 Watchlist    ─┘   (free      (Haiku,     (Sonnet + code     (Sonnet + banned-     (Playwright)    (Streamlit
                    filters)   0-100)      honesty check)     phrase checks)                       dashboard)
```

- **Finds jobs** from Google for Jobs (JSearch API), Indeed / LinkedIn / Job Bank alert emails (Gmail API, read-only), and company career boards (Greenhouse, Lever and Workday JSON APIs). Duplicates across sources are merged.
- **Scores fit** 0-100 with a one-line reason, using a cheap model and prompt caching (about $0.0001 a job). Plain-code rules rank main-target roles above secondary ones.
- **Tailors the resume** by choosing and lightly rewording bullets from a master resume file. A plain-code validator rejects any skill, bullet, number or claim that isn't in the master file, so the AI cannot invent experience. ATS keyword coverage is reported, gaps are shown, never faked.
- **Writes cover letters** in the candidate's own voice (a style guide distilled from their writing samples), checked in code for banned "AI-sounding" phrases, length, lists and untraceable claims.
- **Fills applications**: Greenhouse and Lever forms end to end (resume upload, questions from an answer bank, screenshots), an assisted mode for Workday, and a one-click Autofill button for other sites (in the tool's browser, or in Firefox through a generated userscript).
- **Answer bank**: fuzzy matching plus a model check reuses approved answers across differently-worded questions; new answers are drafted for review, and questions only a person can answer (work authorization, salary, demographics) are never guessed.
- **Apprenticeship outreach**: finds established local contractors (Local Business Data API), drafts short personal emails, sends only on approval with a daily cap, and tracks replies and bounces through Gmail.
- **Dashboard** (Streamlit): review queue, edit letters and answers with an AI "change this" box, approve, outreach, usage and budget, and a "Run pipeline now" button.

## Design choices

- **Human in the loop.** The only code path that submits an application or sends an email starts from a dashboard button. Scheduled runs only prepare.
- **Honesty enforced in code, not just prompts.** Resumes, letters, emails and answers are validated against the master resume; failures are retried once and then blocked.
- **Respectful automation.** No CAPTCHA solving or bot-detection evasion: sites that challenge the browser are handed to the person. Indeed and LinkedIn are never scraped or auto-applied. Requests are rate-limited and identify the tool.
- **Cost control.** Every Claude call checks a monthly budget first; every call's tokens and cost are logged, and each daily run is recorded. Prompt caching, the Batch API and model tiering (Haiku for scoring, Sonnet for writing) keep a month of daily use to a few dollars.
- **Private by default.** Keys live in `.env`; the master resume, database, generated documents and logs live in git-ignored folders.

## Tech stack

Python 3.12 · Anthropic Claude API (structured JSON outputs, prompt caching, Batch API) · Playwright · Streamlit · SQLite · Jinja2 HTML-to-PDF · Gmail API (OAuth) · httpx · RapidFuzz · pytest (129 tests) · Windows Task Scheduler

## Try it with the sample profile

The repo includes a fictional candidate in [samples/](samples/). After installing (below), copy `samples/master_resume.example.yaml` to `data/master_resume.yaml` and `samples/answer_bank.example.yaml` to `data/answer_bank.yaml`, edit `config.yaml` (location, searches, `scoring.preferences`), add your keys to `.env`, then run `python -m pipeline.run` or open the dashboard.

## Install (Windows)

Needs Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Then open `.env` and paste in your keys:

- `PIPELINE_ANTHROPIC_KEY`: your Claude API key from https://console.anthropic.com
- `JSEARCH_API_KEY`: your OpenWeb Ninja JSearch key

## Check that your key works

```powershell
python -m pipeline.check_api
```

It makes one tiny call to the cheap scoring model and prints `Key works.` (cost: a fraction of a cent).

## Running it every day

```powershell
python -m pipeline.run                 # the whole pipeline now: discover, score, tailor, letters, prepare forms
python -m pipeline.run jsearch gmail   # only some sources (the later steps always run)
```

Or click **Run pipeline now** in the dashboard sidebar: it runs in its own window, and the sidebar shows when it last ran and how many new jobs it found.

To run it automatically instead, double-click **`schedule.bat`** once. Windows Task Scheduler then runs `run_daily.bat` every day at 8:00 AM while you're signed in; if the computer was off or asleep then, it runs as soon as it's back. To pick another time, edit `8:00am` in `schedule.bat` and double-click it again. `unschedule.bat` removes it.

- **Nothing is submitted or sent** by a scheduled run. It only gets applications ready for you to review.
- **It never stops to ask you anything.** If the Gmail sign-in has expired, Gmail is skipped for that day and the other sources still run; sign in again with `python -m pipeline.sources.gmail`.
- **A failing step doesn't stop the rest**, and every source's jobs are saved as soon as that source finishes.
- **Every run is recorded**: new jobs, Claude tokens and cost, JSearch requests and any failed steps. See the **Usage** page, or the "Last run" line in the dashboard sidebar. The full output goes to `data/logs/daily.log`.
- **Budget**: every Claude call checks `monthly_budget_usd` ($20) first and stops when it's reached.

## Dashboard

```powershell
.venv\Scripts\streamlit run dashboard.py
```

Or double-click `dashboard.bat` in the project folder. Opens http://localhost:8501. Everything you review, edit and approve is here:

- **Review queue**: counts (new today, ready to review, submitted, apply manually), search and filters, and a card per application with fit score, keyword match, and notes to check. **Resume**, **Cover letter**, **Files** (opens the job's folder, ready to drag into an upload box) and **Skip**; edit the cover letter (saving re-renders the PDF and text) or mark it reviewed; edit form answers (saved to the answer bank). Then:
  - **Approve and submit** for Greenhouse and Lever, once the form is filled and the letter reviewed.
  - **Open Workday assistant** for Workday (opens in its own window).
  - **Apply** for everything else. It opens your files folder and picks the browser: a job-board copy (jobleads, learn4good...) opens a search for the employer's own posting in Firefox; Indeed, LinkedIn, Glassdoor and Job Bank open in Firefox; employer sites open in the tool's window with the Autofill button. Found the employer's own posting? Paste it into **Employer's application link**: if it's Greenhouse or Lever, the tool fills it completely, drafting any job-specific answers for you. **I submitted it** marks a job you applied to yourself.
- **Add a job**: paste links (and a description for Indeed/LinkedIn).
- **Outreach**: apprenticeship email drafts with Send / Skip / Never contact, find companies, write drafts, and **Check replies**. Replies are listed with the start of their text and an **open in Gmail** link.
- **Answer bank**: approve, edit or delete drafts and questions waiting on you; edit approved answers. **Change with AI**: type a change ("shorter", "mention my capstone project", or facts like "I'm a Canadian citizen in Toronto") and **Revise** rewrites the draft as one cohesive answer, using only your resume, the job and what you typed. It stays a draft until you approve it.
- **Watchlist**, **Profile** (master resume, writing samples, style guide, resume upload), **Settings** (common settings, or the whole config.yaml), **Usage** (Claude spend vs budget, JSearch and Local Business requests, one row per daily run). The sidebar shows this month's usage, the last run, and a **Firefox autofill script** button.

If the dashboard misbehaves after the code changed, close its window and double-click `dashboard.bat` again.

## Your master resume

`data/master_resume.yaml` holds everything you've done: contact info, a few summary options, grouped skills, and every job and project with all its bullets. Each bullet has a unique ID. Tailored resumes can only pick from this file, never add to it. See [samples/master_resume.example.yaml](samples/master_resume.example.yaml) for the format.

Render the whole thing to check it:

```powershell
python -m pipeline.resume
```

This writes `output/master_full.pdf` (single column, standard headings, real text, so applicant tracking systems read it cleanly).

## Finding jobs

```powershell
python -m pipeline.discover              # all sources
python -m pipeline.discover jsearch      # just one source
```

Sources (each returns jobs in the same format; duplicates across sources are merged and only never-seen jobs are saved to `data/pipeline.db`):

- **jsearch**: Google for Jobs results (Indeed, LinkedIn, Job Bank, Glassdoor...) via OpenWeb Ninja. The free plan is 200 requests/month, so each query in `config.yaml` runs at most once every `repeat_every_days` days and the run stops at the monthly limit. This is the only source with a limit.
- **gmail**: your Indeed, LinkedIn and Job Bank job alert emails from the last 14 days, read-only. Each email is processed once; your inbox is never changed. The Indeed format is verified against real emails; LinkedIn and Job Bank will be checked once those alerts start arriving.
- **watchlist**: companies in [watchlist.csv](watchlist.csv). Workday, Greenhouse and Lever boards are read through their JSON APIs; any other careers page is opened in a headless browser and job links are picked out by title. Only jobs whose title contains one of `watchlist_title_words` (or the company's own `title_words` column, used for banks) and that are in the GTA are kept.

Requests identify the tool honestly and are spaced out. Indeed and LinkedIn are never scraped directly, and jobs found there are always marked "apply manually".

## Adding a job yourself

Found a job the pipeline missed? Paste its link:

```powershell
python -m pipeline.add_job https://example.wd3.myworkdayjobs.com/en-US/Careers/job/...
python -m pipeline.add_job <link1> <link2>                  # several at once
python -m pipeline.add_job <indeed or linkedin link> --file description.txt
python -m pipeline.add_job <link> --no-prepare              # add now, prepare on the next run
```

The page is opened in a headless browser and Haiku reads the title, company and location (a fraction of a cent). Indeed and LinkedIn pages are never fetched: copy the description into a text file and pass it with `--file`. Jobs you add skip the free filters and are always prepared (scored for information, tailored resume, cover letter), whatever their score. A job already in your list isn't duplicated; it's moved back into the queue if it had been filtered out or scored low.

## Scoring jobs

```powershell
python -m pipeline.score            # results right away
python -m pipeline.score --batch    # Batch API: half price, usually done within the hour
```

1. **Free filters first** (no AI): drop titles containing any `exclude_title_terms` (senior, manager, IT support...) and locations outside the GTA (remote within Canada is kept).
2. **Haiku scores the rest** 0-100 with a one-line reason, as JSON. Your profile (built from the master resume plus `scoring.preferences` in `config.yaml`) is prompt-cached, so each job costs about $0.0001.
3. Jobs at or above `min_fit_score` become **match**; the rest **low_fit**. Every call's cost is logged, and scoring stops if the month's Claude spend reaches `monthly_budget_usd`.

## Tailoring resumes

```powershell
python -m pipeline.tailor
```

For every **match** (best scores first, no daily cap). Technician and electrical jobs (the main-target title words in `config.yaml`) get up to **two pages**, so your hands-on and GitHub projects fit; every other job gets one page.

1. **Keywords**: Haiku lists the posting's ATS terms (skills, tools, certifications, exact title).
2. **Select and reword**: Sonnet picks a summary, the most relevant bullets and skills from your master resume, and lightly rewords bullets to the posting's phrases where they truthfully match. Your master resume is prompt-cached across jobs. About $0.02 per job.
3. **Honesty check** (plain code): every bullet ID, skill and summary must exist in the master resume; reworded text may not add numbers, drift far from the original, or grow much longer. On failure it retries once with the problems listed; a second failure marks the job **flagged** and no PDF is made. Posting phrases swapped in that your master resume never uses (e.g. "inventory management" for "stock tracking") are kept but noted for you to check.
4. **Keyword report**: "14 of 19 terms" plus the missing terms. Gaps are shown to you, never added to the resume.
5. **PDF**: `output/applications/<job id>/<Company> - <Title>.pdf`. If it runs long, the least relevant bullets are dropped until it fits. Redoing a resume keeps the job's cover letter and form answers, and a redo blocked by the honesty check keeps the earlier resume.

## Cover letters

```powershell
python -m pipeline.cover_letter
```

For every tailored resume:

1. **Style guide**: put a few emails or cover letters you wrote yourself in `writing_samples/*.txt` (gitignored). They're distilled once into `data/style_guide.md`, which you can edit; it's rebuilt only when the samples change.
2. **Letter**: Sonnet writes three short paragraphs (under 250 words) from the posting, the bullets already on the tailored resume, your style guide and your master resume. It must include one real detail from the posting and one or two accomplishments from your resume.
3. **Checks** (plain code): the banned phrases in `config.yaml` ("excited to apply", "leverage", "passionate"...), more than one dash, several three-item lists, length, numbers or resume bullets that don't trace to your master resume, and a company detail that isn't in the posting. One retry; anything left is flagged.
4. **Output**: `<Company> - <Title> - Cover Letter.pdf` (same header and fonts as the resume) and a `.txt` for forms with a paste-in box. Every letter starts as **needs edit** so you change a sentence or two in your own words before approving.

## Answer bank

Your approved answers to application questions (work authorization, salary, start date...), stored in the database so the form filler rarely needs the AI.

```powershell
python -m pipeline.answers import data/answer_bank.yaml   # add or update your answers (format: samples/answer_bank.example.yaml)
python -m pipeline.answers list                           # drafts and questions waiting on you
python -m pipeline.answers approve 12                     # approve draft 12 as written
python -m pipeline.answers approve 12 "My own wording."   # or with your edit
python -m pipeline.answers ask "Desired salary"           # try the matcher
```

How a form question is answered:

1. **Per-job questions** ("Why do you want to work here?", patterns in `config.yaml`) are always drafted fresh for that job.
2. **Matching**: exact or near-exact wording is reused for free. Otherwise fuzzy matching shortlists the closest questions and Haiku confirms whether one asks for exactly the same thing (fuzzy scores alone rate "salary expectations" and "hours expectations" as alike). A confirmed wording is saved as an alias, so it's free next time.
3. **No match**: Sonnet drafts a short answer from your master resume and the job, saved as a **draft** for your review and reused for that question until you approve or change it.
4. **Never guessed**: anything only you can answer (work authorization in other countries, relocation, licences not on your resume, references, demographics) is saved as **needs input** and left blank on forms.

When a form gives fixed options, the answer must be one of them, or it's left for you.

## Filling application forms

```powershell
python -m pipeline.apply
```

For every application with a resume and cover letter:

- **Greenhouse and Lever**: a headless browser opens the form, fills your name, email, phone, location, current company and links, uploads the tailored resume and cover letter (Lever: the letter text goes in "additional information"), and answers each question from the answer bank. Searchable dropdowns are picked by exact option text. A full-page screenshot (`form.png`) and a step log (`apply.log`) are saved in the application folder. **It never submits.** Status becomes **filled**, or **needs answers** if a required question has no answer yet (the note lists them).
- **Demographic / equal-opportunity questions** are never answered automatically.
- **Everything else** (Workday, SuccessFactors, Indeed, LinkedIn, Job Bank...) becomes **apply manually** with the direct link; the resume, letter and answers are ready to copy.

**Submitting** happens only through the dashboard's Approve button (`apply.approve_and_submit`), and only for a **filled** form: it fills the form again in a visible window and clicks submit. If the site then asks for an email security code or shows a captcha, the window waits up to 10 minutes for you to finish it yourself; captchas are never bypassed. A job counts as submitted only when a confirmation ("Thank you for applying", "Application received"...) appears after the click; otherwise it's marked failed with a note to check your email first. An approved job is never filled again by the daily run, so it can't be submitted twice. Jobs you apply to yourself are marked submitted from the dashboard (`apply.mark_submitted`).

## Workday applications (assisted)

```powershell
python -m pipeline.workday <job id>
```

A visible browser opens the job, clicks Apply -> Autofill with Resume and uploads the tailored resume. **You sign in or create the account** in that window (once per company; the browser profile in `%LOCALAPPDATA%\job-less-bot\browser_profile` keeps you signed in on this computer, outside the project and OneDrive). Press Enter in the terminal, and the tool walks the application:

- **Fills each page by its visible labels**: name, street address, city, province, postal code, country, phone and device type, email, LinkedIn, the resume and cover letter uploads, "how did you hear" and the other questions from your answer bank. Then it clicks **Save and Continue**.
- **Hands the page to you** (you finish it, click Save and Continue, and press Enter in the terminal) when a required field is still blank, on voluntary / self-identify pages (never answered for you), and for required consent checkboxes.
- **Stops at Review.** You check everything and click Submit yourself; the tool then asks whether you submitted and marks the job.

Screenshots of each page and `workday.log` go in the application folder.

### The Autofill button

Every page of the tool's browser window gets a blue **Autofill** button (bottom right). Click it whenever a page has empty fields: it fills what it can from your profile, resume, cover letter and answer bank, then shows what's still left. It never clicks Continue or Submit. It's there while the Workday assistant hands a page to you, and for employer-site jobs through the dashboard's **Apply** (or `python -m pipeline.assist <job id>`). If the site shows a "verify you are human" check, the window closes and the job opens in Firefox instead. Your street address lives in `data/master_resume.yaml` under `contact.address`, used only for forms.

### Sites with a "verify you are human" check: Firefox autofill

Some sites (jobleads.com and other Cloudflare-protected pages) block the tool's browser, and Indeed and LinkedIn are never automated, so **Apply** opens those in Firefox. To get the same button there, in Firefox:

1. Install the **Violentmonkey** add-on from addons.mozilla.org.
2. Run `python -m pipeline.userscript`. It writes `output/firefox/job-autofill.user.js` with your contact details and approved answer-bank answers. The file stays on your computer; `output/` is gitignored.
3. Drag that file into a Firefox window, then click **Confirm installation**.

Pass the human check yourself, then click **Autofill** on the form. Each filled field gets an orange outline so you can check it, and the button lists any required fields that are still empty. It never touches demographic questions, fields about other people (references, emergency contacts) or "Why us?" questions. It also can't attach files: pick your resume and cover letter from `output/applications/` yourself, then submit. The dashboard rebuilds the file whenever your approved answers or profile change, and tells you; drag it into Firefox again to update (the sidebar's **Firefox autofill script** button opens its folder). The button never appears on the dashboard itself.

## Apprenticeship outreach

```powershell
python -m pipeline.outreach find      # search companies and add the apprenticeship organizations
python -m pipeline.outreach draft 20  # write emails for the next 20 contacts (about $0.01 each)
python -m pipeline.outreach review    # read each one: [s]end, [e]dit in Notepad, [k] skip, [n]ever contact
python -m pipeline.outreach replies   # mark replies and bounces from your Gmail
python -m pipeline.outreach list      # everyone and their status
```

- **Contacts**: established GTA electrical companies from OpenWeb Ninja's Local Business Data API (subscribe at https://app.openwebninja.com/api/local-business-data/overview; your OpenWeb Ninja key works once subscribed), at least `outreach.min_reviews` Google reviews, no supply stores or HVAC/plumbing companies, each search repeated at most monthly. Plus the apprenticeship organizations in [outreach_orgs.csv](outreach_orgs.csv) (Electrical Apprenticeship Training Alliance / IBEW Local 353, Merit Ontario, Ontario Electrical League) with the contacts they publish.
- **Emails**: under 150 words in your voice (the cover letter style guide); companies are asked whether they take on 309A apprentices, organizations how to apply. Only facts from your master resume and the contact's listing; banned phrases, dashes and invented numbers are checked. Your sign-off, name, phone and email are added by code, with a one-page apprentice resume (`output/outreach/`) attached.
- **Sending**: only when you press **s** for that email in `review`, at most `outreach.max_per_day` (15) a day. The first send asks Google for permission to send (one browser click).
- **Tracking**: `replies` checks each sent email's Gmail thread: a reply marks it **replied** (and saves the start of its text for the dashboard), a delivery failure **bounced**. **n** marks a contact never to be emailed again; honour any "please don't contact me" the same way.

### Gmail setup (once)

1. In https://console.cloud.google.com create a project, enable the **Gmail API**, set the app audience to **External**, and add your Gmail address under **Test users**.
2. Create an OAuth client of type **Desktop app**, download it, and save it as `credentials.json` in this folder.
3. Run `python -m pipeline.sources.gmail` and click **Allow** in the browser. This saves `token.json`.

While the Google app is in Testing mode, Google asks you to sign in again about once a week. When you run things yourself, the browser opens for you. A scheduled run skips Gmail instead and the dashboard sidebar shows it; run `python -m pipeline.sources.gmail` to sign in again.

## Settings

Everything you might want to change lives in [config.yaml](config.yaml): search keywords, location, title terms to exclude, minimum fit score, which Claude models to use, the daily application cap, and the monthly budget (`limits.monthly_budget_usd`, $20). Most of these can also be changed on the dashboard's **Settings** page.

## Folder layout

```
config.yaml          your settings (committed, no secrets)
.env                 your API keys (never committed)
pipeline/            the Python code
  settings.py        loads config.yaml and .env; makes the Claude client
  check_api.py       confirms your API key works
  resume.py          loads the master resume, renders chosen bullets/skills to PDF
  db.py              SQLite storage: jobs, applications, answers, outreach, API usage, runs
  discover.py        runs all job sources and saves new jobs
  score.py           free filters, then AI fit scoring
  tailor.py          keywords, bullet selection/rewording, honesty check, tailored PDF
  cover_letter.py    style guide, letter writing, banned-phrase and honesty checks, PDF + text
  answers.py         answer bank: matching, drafting, approving
  apply.py           Greenhouse/Lever form filling, screenshot, submit-on-approval; manual for the rest
  workday.py         assisted Workday applications in a visible browser (you sign in and submit)
  assist.py          the Autofill button in the tool's browser; opens Firefox for blocked sites
  userscript.py      builds the Firefox (Violentmonkey) autofill script
  outreach.py        apprenticeship outreach: find companies, draft, send on approval, track replies
  run.py             the whole daily pipeline, with a record of each run
  add_job.py         add jobs by link and prepare them right away
  sources/           one module per job source (jsearch, gmail, watchlist, manual)
dashboard.py         the Streamlit dashboard (dashboard.bat starts it)
run_daily.bat        what the daily schedule runs; schedule.bat / unschedule.bat set it up
watchlist.csv        companies to check directly
templates/           HTML/CSS resume and cover letter templates (shared header in _shared.html.j2)
writing_samples/     your own writing, for the cover letter style guide (never committed)
samples/             example data files (fake person) showing the formats
tests/               pytest tests  (run: python -m pytest)
data/                your private files: master resume, answer bank, database, logs (never committed)
output/              generated resumes, letters, screenshots, Firefox script (never committed)
```

## Privacy

`.gitignore` keeps `.env`, `credentials.json`, `token.json`, the `data/`, `output/` and `writing_samples/` folders, any `.db` file, and `master_resume.yaml` out of git, so your keys and personal info never reach GitHub.
