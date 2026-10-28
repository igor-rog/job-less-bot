"""Loads config.yaml and .env once; every module imports from here."""
import os
import re
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"  # gitignored: master resume, database, generated files

load_dotenv(ROOT / ".env")
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def claude() -> anthropic.Anthropic:
    # PIPELINE_ANTHROPIC_KEY, not ANTHROPIC_API_KEY: Claude's own tools reserve that name.
    key = os.environ.get("PIPELINE_ANTHROPIC_KEY")
    if not key:
        raise SystemExit("PIPELINE_ANTHROPIC_KEY isn't set. Copy .env.example to .env in the project folder "
                         "and paste your Claude API key after PIPELINE_ANTHROPIC_KEY=")
    return anthropic.Anthropic(api_key=key)


def set_config_value(text: str, key: str, value) -> str:
    """Changes one `key: value` line of config.yaml text, keeping comments and layout. `key` includes its
    indentation ("  scoring"), so a section header with the same name (top-level "scoring:") isn't touched."""
    return re.sub(rf"^({key}:[ \t]*)[^#\n]*?([ \t]*(#.*)?)$", lambda m: f"{m.group(1)}{value}{m.group(2)}", text,
                  count=1, flags=re.M)
