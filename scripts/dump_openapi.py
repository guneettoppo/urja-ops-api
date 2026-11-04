"""Write openapi.json from the live app definition.

Run after changing routes or schemas so the committed spec never drifts:
    python -m scripts.dump_openapi
"""

import json
from pathlib import Path

from urja.main import app

OUT = Path(__file__).resolve().parent.parent / "openapi.json"


def main() -> None:
    spec = app.openapi()
    spec["info"]["description"] = app.description
    spec["servers"] = [{"url": "http://localhost:8080", "description": "Local development"}]
    OUT.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = len(spec.get("paths", {}))
    print(f"wrote {OUT} ({paths} paths, {len(spec.get('components', {}).get('schemas', {}))} schemas)")


if __name__ == "__main__":
    main()
