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


def metric_rows(page, key):
    pattern = re.compile(
        r'\{"label":"(?P<label>(?:\\.|[^"])*)","' + re.escape(key) +
        r'":(?P<value>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?),"detailsUrl":"(?P<url>[^"]+)"\}'
    )
    rows = {}
    for match in pattern.finditer(page):
        label = json.loads('"' + match.group("label") + '"')
        rows[match.group("url")] = {"model": label, "value": float(match.group("value"))}
    return rows


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


def build():
    page = fetch_page()
    indexes = metric_rows(page, "omniscienceIndex")
    accuracies = metric_rows(page, "omniscienceAccuracy")
    hallucinations = metric_rows(page, "omniscienceHallucinationRate")
    urls = sorted(set(indexes) | set(accuracies) | set(hallucinations))

    models = []
    for url in urls:
        present = sum(url in group for group in (indexes, accuracies, hallucinations))
        if present < 2:
            continue
        acc = accuracies.get(url, {}).get("value")
        oi = indexes.get(url, {}).get("value")
        hr = hallucinations.get(url, {}).get("value")
        if acc is not None and oi is not None:
            hr = (acc * 100 - oi) / (100 - acc * 100)
        elif acc is not None and hr is not None:
            oi = acc * 100 - hr * (100 - acc * 100)
        elif oi is not None and hr is not None:
            acc = ((oi + 100 * hr) / (1 + hr)) / 100
        correct = acc * 100
        incorrect = correct - oi
        partial_abstain = 100 - correct - incorrect
        models.append({
            "model": (indexes.get(url) or accuracies.get(url) or hallucinations[url])["model"],
            "detailsUrl": "https://artificialanalysis.ai" + url,
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
