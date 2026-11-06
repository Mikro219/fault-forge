"""Generates schemas/report.schema.json from the pydantic models."""

import json
from pathlib import Path

from faultforge.models import Report

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "report.schema.json"


def render_report_schema() -> str:
    return json.dumps(Report.model_json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.parent.mkdir(exist_ok=True)
    SCHEMA_PATH.write_text(render_report_schema(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
