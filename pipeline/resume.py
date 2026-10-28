"""Loads the master resume and renders a chosen subset of it to PDF.

Run: python -m pipeline.resume   (renders the whole master resume to output/master_full.pdf)
"""
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from pipeline.settings import DATA, ROOT

MASTER_PATH = DATA / "master_resume.yaml"
OUTPUT = ROOT / "output"
_env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())


def load_master(path: Path = MASTER_PATH) -> dict:
    master = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [b["id"] for entry in master["jobs"] + master["projects"] for b in entry["bullets"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate bullet IDs in master resume: {sorted(dupes)}")
    return master


def build_html(master: dict, summary_id: str | None = None,
               bullet_ids: list[str] | None = None, skills: list[str] | None = None,
               rewrites: dict[str, str] | None = None, summary_text: str | None = None) -> str:
    """None means "use everything". Unknown IDs or skills raise ValueError, so nothing
    that isn't in the master resume can reach the page. rewrites/summary_text carry reworded
    text that pipeline.tailor has already checked against the master resume."""
    summaries = {s["id"]: s for s in master["summaries"]}
    if summary_id is not None and summary_id not in summaries:
        raise ValueError(f"Unknown summary ID: {summary_id}")
    summary = summaries[summary_id] if summary_id else master["summaries"][0]
    if summary_text:
        summary = {**summary, "text": summary_text}

    all_bullets = {b["id"] for e in master["jobs"] + master["projects"] for b in e["bullets"]}
    unknown = [i for i in list(bullet_ids or []) + list(rewrites or {}) if i not in all_bullets]
    if unknown:
        raise ValueError(f"Unknown bullet IDs: {unknown}")
    order = {i: n for n, i in enumerate(bullet_ids or [])}

    def pick(entries):
        # Keep master order for entries; inside each, use the caller's bullet order.
        out = []
        for e in entries:
            chosen = [{**b, "text": (rewrites or {}).get(b["id"], b["text"])}
                      for b in e["bullets"] if bullet_ids is None or b["id"] in order]
            chosen.sort(key=lambda b: order.get(b["id"], 0))
            if chosen:
                out.append({**e, "bullets": chosen})
        return out

    all_skills = {s for group in master["skills"].values() for s in group}
    if skills is not None:
        unknown = [s for s in skills if s not in all_skills]
        if unknown:
            raise ValueError(f"Skills not in master resume: {unknown}")
    grouped = {g: [s for s in items if skills is None or s in skills] for g, items in master["skills"].items()}

    return _env.get_template("resume.html.j2").render(
        contact=master["contact"], summary=summary,
        jobs=pick(master["jobs"]), projects=pick(master["projects"]),
        education=master["education"], certifications=master.get("certifications") or [],
        skills={g: items for g, items in grouped.items() if items},
    )


def html_to_pdf(html: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out_path), prefer_css_page_size=True)
        browser.close()
    return out_path


def render_resume(master: dict, out_path: Path, **selection) -> Path:
    return html_to_pdf(build_html(master, **selection), out_path)


if __name__ == "__main__":
    pdf = render_resume(load_master(), OUTPUT / "master_full.pdf")
    print(f"Wrote {pdf}")
