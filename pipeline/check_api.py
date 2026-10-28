"""Makes one tiny Claude API call to confirm the key in .env works.

Run: python -m pipeline.check_api
"""
import os
import sys

import anthropic

from pipeline.settings import CONFIG, claude


def main() -> int:
    if not os.environ.get("PIPELINE_ANTHROPIC_KEY", "").startswith("sk-ant-"):
        print("PIPELINE_ANTHROPIC_KEY is missing from .env (copy .env.example to .env and paste your key).")
        return 1
    model = CONFIG["models"]["scoring"]
    try:
        reply = claude().messages.create(
            model=model, max_tokens=5, messages=[{"role": "user", "content": "Reply with: ok"}]
        )
    except anthropic.APIError as e:
        print(f"API call failed: {e}")
        return 1
    print(f"Key works. {model} replied: {reply.content[0].text!r} "
          f"({reply.usage.input_tokens} in / {reply.usage.output_tokens} out tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
