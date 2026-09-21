#!/usr/bin/env python3
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = "https://artificialanalysis.ai/evaluations/omniscience"
ROOT = Path(__file__).resolve().parents[1]


def fetch_page():
    req = Request(SOURCE, headers={"User-Agent": "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"})
    with urlopen(req, timeout=45) as response:
        return html.unescape(response.read().decode("utf-8"))


def source_models(page):
    """Read complete default-selected model records, not truncated chart summaries."""
    chunks = re.finditer(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', page)
    payload = "".join(json.loads(match.group(1)) for match in chunks)
    decoder = json.JSONDecoder()
    records = {}
    for match in re.finditer(r'\{"id":"[^"\n]+","slug":', payload):
        try:
            row, _ = decoder.raw_decode(payload, match.start())
        except ValueError:
            continue
        breakdown = row.get("omniscienceBreakdown")
        if isinstance(row.get("omniscience"), (int, float)) and isinstance(breakdown, dict):
            if all(isinstance(breakdown.get(key), (int, float)) for key in ("accuracy", "hallucinationRate")):
                records[row["slug"]] = row
    if len(records) < 15:
        raise RuntimeError("Complete model records missing; keeping previous data.")
    return list(records.values())


def profile(acc, oi, hr):
    if oi >= 35 and hr <= 0.5:
        return "Frontier reliability: high knowledge with comparatively controlled guessing."
    if acc >= 0.6 and hr > 0.6:
        return "High raw knowledge, but aggressive guessing raises hallucination risk."
    if oi >= 20 and hr <= 0.4:
        return "Well calibrated: solid knowledge and relatively honest abstention."
    if oi >= 20:
        return "Strong knowledge, with meaningful hallucination risk on misses."
    if oi >= 0 and hr <= 0.35:
        return "Conservative and well calibrated, though factual coverage is more limited."
    if oi >= 0:
        return "Positive reliability, but mistakes remain frequent when uncertain."
    return "Negative reliability: more confidently wrong answers than correct answers."


# Only effort annotations are removed; model versions, sizes and dates stay distinct.
EFFORT = {"minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
EFFORT_NOTE = re.compile(r"\((minimal|low|medium|high|xhigh|max)(?:\s+with fallback)?\)", re.I)


def highest_reasoning(models):
    selected = {}
    for row in models:
        name = row["model"]
        match = EFFORT_NOTE.search(name)
        key = EFFORT_NOTE.sub("", name).strip().casefold()
        rank = EFFORT[match.group(1).lower()] if match else 0
        previous = selected.get(key)
        if previous is None or rank > previous[0]:
            selected[key] = (rank, row)
    return [entry[1] for entry in selected.values()]


def build():
    page = fetch_page()
    records = source_models(page)
    models = []
    for row in records:
        acc = row["omniscienceBreakdown"]["accuracy"]
        oi = row["omniscience"]
        hr = row["omniscienceBreakdown"]["hallucinationRate"]
        if not (0 <= acc <= 1 and -100 <= oi <= 100 and 0 <= hr <= 1):
            raise RuntimeError("Invalid benchmark values for " + row["slug"])
        if abs((acc * 100 - hr * (100 - acc * 100)) - oi) > 0.1:
            raise RuntimeError("Inconsistent benchmark metrics for " + row["slug"])
        correct = acc * 100
        incorrect = correct - oi
        partial_abstain = 100 - correct - incorrect
        models.append({
            "model": row.get("shortName") or row["name"],
            "detailsUrl": "https://artificialanalysis.ai/models/" + row["slug"],
            "correct": round(correct, 2),
            "incorrect": round(incorrect, 2),
            "partialAbstain": round(partial_abstain, 2),
            "accuracy": round(acc * 100, 2),
            "omniscienceIndex": round(oi, 2),
            "hallucinationRate": round(hr * 100, 2),
            "standardPercent": round((oi / 2) + 50, 2),
            "profile": profile(acc, oi, hr),
        })

    if len(models) < 15:
        raise RuntimeError(f"Only {len(models)} usable model records found; source format may have changed.")
    models = highest_reasoning(models)
    models = [row for row in models if row["correct"] >= 27]
    models.sort(key=lambda row: row["omniscienceIndex"], reverse=True)
    return {
        "source": SOURCE,
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "modelCount": len(models),
        "models": models,
    }


if __name__ == "__main__":
    payload = build()
    (ROOT / "data.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {len(payload['models'])} models at {payload['updatedAt']}")
