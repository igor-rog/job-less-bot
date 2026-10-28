import pytest
import yaml

from pipeline.resume import build_html, load_master, render_resume
from pipeline.settings import ROOT

SAMPLE = ROOT / "samples" / "master_resume.example.yaml"


@pytest.fixture
def master():
    return load_master(SAMPLE)


def test_full_resume_includes_everything(master):
    html = build_html(master)
    for text in ["Alex Example", "Tested 40 sensor boards", "Home Weather Station", "Python"]:
        assert text in html


def test_selection_keeps_only_chosen_bullets_and_skills_in_order(master):
    html = build_html(master, bullet_ids=["acme_repair", "acme_test"], skills=["Python"])
    assert html.index("solder faults") < html.index("40 sensor boards")
    assert "Weather Station" not in html  # project with no chosen bullets is dropped
    assert "Oscilloscopes" not in html


def test_unknown_bullet_or_skill_is_rejected(master):
    with pytest.raises(ValueError):
        build_html(master, bullet_ids=["made_up"])
    with pytest.raises(ValueError):
        build_html(master, skills=["Kubernetes"])


def test_duplicate_bullet_ids_are_rejected(tmp_path):
    data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    data["projects"][0]["bullets"][0]["id"] = "acme_test"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="acme_test"):
        load_master(bad)


def test_renders_pdf(master, tmp_path):
    pdf = render_resume(master, tmp_path / "r.pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
