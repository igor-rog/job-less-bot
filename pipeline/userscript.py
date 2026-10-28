"""Firefox autofill for sites that block the tool's browser (Cloudflare "verify you are human" and similar).

Run: python -m pipeline.userscript
Then install output/firefox/job-autofill.user.js with the Violentmonkey add-on (see the README).

You open the job in your normal Firefox and pass any human check yourself. On a form, click the blue
Autofill button: it types your contact details and approved answer-bank answers into matching fields and
outlines each one in orange so you can check it. It never uploads files, answers demographic questions,
or clicks Continue or Submit.
"""
import json

from pipeline import answers, db
from pipeline.apply import DEMOGRAPHIC, personal
from pipeline.resume import load_master
from pipeline.settings import ROOT

OUT = ROOT / "output" / "firefox" / "job-autofill.user.js"

# Visible-label patterns (checked in order, whole words) -> key in the personal details.
FIELDS = [
    (r"\bfirst\s*name|\bgiven\s*name|\bpreferred\s*(first\s*)?name", "first"),
    (r"\blast\s*name|\bfamily\s*name|\bsurname", "last"),
    (r"^(full\s*|legal\s*|your\s*)?name\b", "name"),
    (r"\be-?mail\b", "email"),
    (r"\b(phone|mobile|cell|telephone)\b", "phone"),
    (r"\blinkedin\b", "linkedin"),
    (r"\b(github|website|portfolio|personal\s*(site|url|web))\b", "github"),
    (r"\bstreet\b|\baddress\s*(line\s*)?1\b|^(home\s*|mailing\s*)?address$", "street"),
    (r"\b(city|town|municipality)\b", "city"),
    (r"\b(province|state)\b", "province"),
    (r"\b(postal\s*code|zip(\s*code)?|postcode)\b", "postal_code"),
    (r"^country\b|\bcountry\s*of\s*residence\b", "country"),
    (r"\bcurrent\s*(company|employer)\b", "company"),
    (r"^(current\s*)?location\b|\bwhere\s*are\s*you\s*(located|based)\b", "location"),
]
# The browser's own autocomplete hints are the most reliable signal when a site sets them.
AUTOCOMPLETE = {"given-name": "first", "family-name": "last", "name": "name", "email": "email", "tel": "phone",
                "tel-national": "phone", "street-address": "street", "address-line1": "street",
                "address-level2": "city", "address-level1": "province", "postal-code": "postal_code",
                "country-name": "country", "country": "country", "url": "github"}
# Fields about someone else: never filled with your details.
OTHER_PERSON = r"\b(reference|referee|referr|emergency|manager|supervisor|recruiter|spouse|parent|guardian)"
ALIASES = {"Ontario": ["ON"], "Canada": ["CA", "CAN"]}

SCRIPT = r"""// ==UserScript==
// @name        job-less-bot autofill
// @description Fills job application fields from your profile when you click Autofill. Never submits.
// @match       *://*/*
// @exclude     *://localhost*/*
// @exclude     *://127.0.0.1*/*
// @grant       none
// @inject-into content
// @version     3
// ==/UserScript==
(() => {
  const ME = __ME__, FIELDS = __FIELDS__, AC = __AUTOCOMPLETE__, ANSWERS = __ANSWERS__, ALIASES = __ALIASES__;
  const SKIP = new RegExp(__SKIP__, 'i'), OTHER = new RegExp(__OTHER__, 'i');
  const clean = s => (s || '').replace(/[*:]/g, ' ').replace(/\s+/g, ' ').trim();
  const words = s => new Set(clean(s).toLowerCase().match(/[a-z0-9]{2,}/g) || []);
  const visibleLabel = el => {  // what a person reads next to the field
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(' ').map(i => document.getElementById(i)?.innerText).filter(Boolean).join(' ');
      if (t) return clean(t);
    }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    return clean(el.getAttribute('aria-label') || el.placeholder || '');
  };
  const personalFor = (el, label) => {
    const ac = (el.getAttribute('autocomplete') || '').split(' ').pop();
    if (AC[ac] && ME[AC[ac]] && !OTHER.test(label)) return [ME[AC[ac]], 'profile'];
    if (OTHER.test(label)) return null;
    for (const [re, key] of FIELDS) if (new RegExp(re, 'i').test(label)) return ME[key] ? [ME[key], 'profile'] : null;
    return null;
  };
  const bankFor = label => {  // the whole question has to match, not just part of it
    const w = words(label); let best = null, score = 0;
    for (const [q, a] of ANSWERS) {
      const qw = words(q), shared = [...qw].filter(x => w.has(x)).length;
      const s = shared / Math.max(qw.size, w.size, 1);
      if (s > score) { score = s; best = a; }
    }
    return score >= 0.75 ? [best, 'answer bank'] : null;
  };
  const set = (el, value) => {  // native setter + events, so React/Angular forms see the change
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement : el instanceof HTMLSelectElement ? HTMLSelectElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, value);
    ['input', 'change', 'blur'].forEach(t => el.dispatchEvent(new Event(t, {bubbles: true})));
  };
  const pickOption = (texts, value) => {  // exact match first, then "Yes, I am..." style answers
    const want = [value, ...(ALIASES[value] || [])].map(x => x.toLowerCase());
    let i = texts.findIndex(t => want.includes(t));
    if (i < 0) i = texts.findIndex(t => want.some(w => t.startsWith(w + ',') || t.startsWith(w + ' ') || t.startsWith(w + ' (')));
    return i;
  };
  const mark = (el, source) => { el.style.outline = '2px solid #f5a623'; el.title = 'Autofilled from your ' + source + ': check it'; };
  const fill = () => {
    let filled = 0; const left = [];
    const need = (el, label) => { if (el.required || el.getAttribute('aria-required') === 'true') left.push(label.slice(0, 40)); };
    document.querySelectorAll('input, textarea, select').forEach(el => {
      if (el.disabled || el.readOnly || !el.offsetParent) return;
      if (/^(hidden|file|password|checkbox|radio|submit|button|search|reset|image)$/.test(el.type)) return;
      if (el instanceof HTMLSelectElement ? el.selectedIndex > 0 : el.value) return;  // already filled
      const label = visibleLabel(el);
      if (SKIP.test(label)) return;  // demographic questions are always yours
      const hit = personalFor(el, label) || (label ? bankFor(label) : null);
      if (!hit) return need(el, label || el.name || 'a field');
      let [value, source] = hit;
      if (el instanceof HTMLSelectElement) {
        const i = pickOption([...el.options].map(o => clean(o.text).toLowerCase()), value);
        if (i < 0 || !el.options[i].value) return need(el, label);
        set(el, el.options[i].value);
      } else {
        if (el.type === 'number') { value = (value.replace(/,/g, '').match(/\d+(\.\d+)?/) || [''])[0]; if (!value) return need(el, label); }
        if (el.type === 'email' && !value.includes('@')) return need(el, label);
        set(el, value);
      }
      mark(el, source); filled++;
    });
    // Yes/No radio groups: the question is the group's legend or first line of text.
    const groups = {};
    document.querySelectorAll('input[type=radio]').forEach(r => { if (r.name && r.offsetParent) (groups[r.name] ||= []).push(r); });
    Object.values(groups).forEach(rs => {
      if (rs.some(r => r.checked)) return;
      const box = rs[0].closest('fieldset, [role=radiogroup]') || rs[0].parentElement.parentElement;
      const q = clean(box.querySelector('legend')?.innerText || box.getAttribute('aria-label') || box.innerText.split('\n')[0]);
      if (!q || SKIP.test(q)) return;
      const hit = bankFor(q); if (!hit) return;
      const i = pickOption(rs.map(r => clean(visibleLabel(r) || r.value).toLowerCase()), hit[0]);
      if (i < 0) return left.push(q.slice(0, 40));
      rs[i].click(); mark(rs[i].closest('label') || rs[i], hit[1]); filled++;
    });
    return `Filled ${filled} (outlined)` + (left.length ? `. Still needed: ${left.join('; ')}`.slice(0, 200) : '');
  };
  const add = () => {
    if (document.getElementById('jlb-autofill') || !document.body) return;
    if (document.querySelectorAll('input:not([type=hidden]), textarea, select').length < 3) return;  // not a form page
    const b = document.createElement('button');
    b.id = 'jlb-autofill'; b.type = 'button'; b.textContent = 'Autofill';
    b.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647;padding:10px 16px;border:0;' +
      'border-radius:20px;background:#1f6feb;color:#fff;font:600 14px Arial,sans-serif;max-width:340px;' +
      'text-align:left;box-shadow:0 2px 8px rgba(0,0,0,.3);cursor:pointer';
    b.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); b.textContent = 'Autofill · ' + fill(); });
    document.body.appendChild(b);
  };
  // Runs before the page exists: watch the document itself (forms often appear after load).
  new MutationObserver(add).observe(document, {childList: true, subtree: true});
  document.addEventListener('DOMContentLoaded', add);
})();
"""


def build(con=None, master=None) -> str:
    con = con or db.connect()
    me = personal(master or load_master())
    rows = con.execute("SELECT question, answer FROM answers WHERE status = 'approved' AND per_job = 0 "
                       "AND job_id IS NULL AND answer != ''").fetchall()
    return (SCRIPT.replace("__ME__", json.dumps(me))
            .replace("__FIELDS__", json.dumps(FIELDS))
            .replace("__AUTOCOMPLETE__", json.dumps(AUTOCOMPLETE))
            .replace("__ALIASES__", json.dumps(ALIASES))
            .replace("__OTHER__", json.dumps(OTHER_PERSON))
            .replace("__ANSWERS__", json.dumps([[r["question"], r["answer"]] for r in rows
                                                if not answers.is_per_job(r["question"]) and not DEMOGRAPHIC.search(r["question"])]))
            .replace("__SKIP__", json.dumps(DEMOGRAPHIC.pattern)))


def refresh(con=None) -> bool:
    """Rewrites the script if your profile or approved answers changed. True when it changed."""
    script = build(con)
    if OUT.exists() and OUT.read_text(encoding="utf-8") == script:
        return False
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(script, encoding="utf-8")
    return True


if __name__ == "__main__":
    refresh()
    print(f"Wrote {OUT}\nRe-run this after you change your profile or answer bank, then reinstall it in Violentmonkey.")
