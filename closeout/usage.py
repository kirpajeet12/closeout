"""What Closeout's reading and answering has cost, from the run records. Shown on the office's home page as weekly and
monthly totals; never on the screen where a reading is started. Voice is billed elsewhere and is not counted here."""
from __future__ import annotations

import json

from .drawings import cost_usd
from .store import Store

KIND_NAMES = {"project": "Reading a project folder", "review": "Field reviews", "batch": "Evidence from contractors",
              "documents": "Folder checks", "drawings": "Drawings readings", "ask": "Questions answered"}


def summary(store: Store, keep_days: int = 92) -> dict:
    """Every run priced by its model, with the recent ones listed so the screen can sum them by week and month in the
    office's own time zone."""
    rows = store.runs()
    total = 0.0
    recent: list[dict] = []
    by_kind: dict[str, dict] = {}
    for r in rows:
        try:
            usage = json.loads(r.get("usage_json") or "{}")
        except ValueError:
            usage = {}
        usd = cost_usd(usage, r.get("model_id") or "")
        total += usd
        k = by_kind.setdefault(r.get("kind") or "batch", {"usd": 0.0, "runs": 0})
        k["usd"] += usd
        k["runs"] += 1
        recent.append({"at": r["started_at"], "usd": usd, "kind": r.get("kind") or "batch", "project_id": r.get("project_id") or "",
                       "status": r.get("status")})
    recent = recent[-400:]
    return {"total_usd": round(total, 2), "runs": len(rows),
            "by_kind": {k: {"name": KIND_NAMES.get(k, k), "usd": round(v["usd"], 2), "runs": v["runs"]} for k, v in by_kind.items()},
            "recent": recent}
