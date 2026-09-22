#!/usr/bin/env python3
import html
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SOURCE = "https://artificialanalysis.ai/evaluations/omniscience"
ROOT = Path(__file__).resolve().parents[1]
NEW_MODEL_ACCURACY = 35
# Models already displayed when the 35% admission rule was introduced.
# Their original 27% minimum remains in effect; new releases use 35%.
EXISTING_RELEASES = {
    "claude-fable-5-1", "gpt-6-astra", "claude-fable-5", "claude-opus-5",
    "grok-4-6", "gemini-3-8-flash", "muse-spark-1-3", "gpt-5-6-sol",
    "kimi-k3", "step-5-preview", "glm-5-3", "qwen3-8-max-0902",
    "glm-5-3-flash", "gemini-3-5-flash-lite", "inkling", "deepseek-v4-pro",
    "gpt-5-6-terra", "deepseek-v4-1-flash", "gpt-5-6-luna",
}


def fetch_page():
    req = Request(SOURCE, headers={"User-Agent": "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"})
    with urlopen(req, timeout=45) as response:
        return html.unescape(response.read().decode("utf-8"))


def source_models(page):
    """Read the public full-model feed used by AA's model selector.

    AA publishes its data path and decoding key in the page. This follows its
    public client loader: AES-GCM, SHA256(key)[:12] nonce, then gzip + JSON.
    Never fall back to the default selection: that silently omits new models.
    """
    chunks = re.finditer(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', page)
    payload = "".join(json.loads(match.group(1)) for match in chunks)
    decoder = json.JSONDecoder()
    metadata = {}
    for match in re.finditer(r'\{"slug":', payload):
        try:
            row, _ = decoder.raw_decode(payload, match.start())
        except ValueError:
            continue
        if isinstance(row.get("release"), dict):
            metadata[row["slug"]] = row
    match = re.search(r'"manifest":(\{[^}]+\})', payload)
    if not match:
        raise RuntimeError("Full-model feed missing; keeping previous data.")
    manifest = json.loads(match.group(1))
    if not re.fullmatch(r"/data/[a-zA-Z0-9._/-]+", manifest["path"]):
        raise RuntimeError("Unexpected model-feed path.")
    request = Request("https://artificialanalysis.ai" + manifest["path"],
                      headers={"User-Agent": "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"})
    with urlopen(request, timeout=60) as response:
        raw = response.read()
    if manifest.get("key"):
        key = bytes.fromhex(manifest["key"])
        raw = gzip.decompress(AESGCM(key).decrypt(hashlib.sha256(key).digest()[:12], raw, None))
    records = []
    for row in json.loads(raw)["models"]:
        breakdown = row.get("omniscienceBreakdown")
        if row.get("omniscience") is None or breakdown is None:
            continue  # AA has not published this model's benchmark yet.
        if not isinstance(breakdown, dict) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (row["omniscience"], breakdown.get("accuracy"), breakdown.get("hallucinationRate"))
        ):
            raise RuntimeError("Invalid benchmark record: " + row["slug"])
        meta = metadata[row["slug"]]
        row["releaseKey"] = meta["release"]["slug"]
        row["creatorSlug"] = (meta.get("creator") or {}).get("slug") or "unknown"
        effort_match = EFFORT_NOTE.search(row.get("shortName") or row.get("name") or "")
        row["reasoningRank"] = ((meta.get("effort") or {}).get("level") or
                                (EFFORT[effort_match.group(1).lower()] if effort_match else 0))
        row["releaseDate"] = meta.get("releaseDate") or ""
        records.append(row)
    if len(records) < 15:
        raise RuntimeError("Complete model records missing; keeping previous data.")
    return records


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
        key = row.get("releaseKey") or EFFORT_NOTE.sub("", name).strip().casefold()
        rank = row.get("reasoningRank", EFFORT[match.group(1).lower()] if match else 0)
        previous = selected.get(key)
        if previous is None or rank > previous[0]:
            selected[key] = (rank, row)
    return [entry[1] for entry in selected.values()]

def displayed_accuracy(value):
    """Match AA's whole-percent chart label (round half up)."""
    return int(value + 0.5)


def lineage_key(row):
    """Return a conservative product-line identity for release replacement.

    AA does not publish a family ID. Its release slugs do consistently separate
    version/size/date tokens from product descriptors, so remove only tokens
    that contain version-like digits. Descriptors remain: GPT Sol/Terra/Luna,
    Gemini Flash/Flash-Lite, GLM/GLM-Flash, and DeepSeek Pro/Flash cannot merge.
    Creator is included to prevent similarly named products from different labs
    colliding.
    """
    parts = []
    for token in row["releaseKey"].casefold().split("-"):
        # qwen3 -> qwen, k3 -> k; pure versions/dates/sizes disappear.
        token = re.sub(r"\d.*$", "", token)
        if token and token != "v":
            parts.append(token)
    product = "-".join(parts) or row["releaseKey"].casefold()
    return f'{row.get("creatorSlug", "unknown")}:{product}'


def newest_product_lines(models):
    """Keep only the newest eligible release in each product line."""
    selected = {}
    for row in models:
        family = lineage_key(row)
        candidate = (row.get("releaseDate") or "", row["releaseKey"])
        previous = selected.get(family)
        if previous is None or candidate > previous[0]:
            selected[family] = (candidate, row)
    return [entry[1] for entry in selected.values()]

def previous_admitted_releases():
    try:
        previous = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    admitted = set(previous.get("admittedNewReleases", []))
    # Migration path from data written before this state field existed.
    admitted.update(row.get("releaseKey") for row in previous.get("models", [])
                    if row.get("releaseKey") not in EXISTING_RELEASES)
    admitted.discard(None)
    return admitted - EXISTING_RELEASES

def eligible_models(models, previously_admitted=None):
    # AA's current default selection identifies newly surfaced benchmark models.
    # Select reasoning first; do not substitute a lower effort just to pass.
    previously_admitted = set(previously_admitted or ())
    newly_selected_releases = {row["releaseKey"] for row in models
                               if row.get("chartDefaultSelected")}
    selected = highest_reasoning(models)
    newly_qualified = {row["releaseKey"] for row in selected
                       if row["releaseKey"] not in EXISTING_RELEASES and
                       row["releaseKey"] in newly_selected_releases and
                       displayed_accuracy(row["rawAccuracy"]) >= NEW_MODEL_ACCURACY}
    admitted = previously_admitted | newly_qualified
    eligible = [row for row in selected if
                (row["releaseKey"] in EXISTING_RELEASES and row["rawAccuracy"] >= 27) or
                row["releaseKey"] in admitted]
    # A new release replaces an older release only after passing admission.
    # This prevents an unqualified launch from removing a qualifying incumbent.
    eligible = newest_product_lines(eligible)
    return eligible, admitted


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
            "releaseKey": row["releaseKey"],
            "creatorSlug": row["creatorSlug"],
            "reasoningRank": row["reasoningRank"],
            "releaseDate": row["releaseDate"],
            "chartDefaultSelected": bool(row.get("chartDefaultSelected")),
            "rawAccuracy": correct,
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
    models, admitted = eligible_models(models, previous_admitted_releases())
    for row in models:
        del row["rawAccuracy"]
        del row["creatorSlug"]
    models.sort(key=lambda row: row["omniscienceIndex"], reverse=True)
    return {
        "source": SOURCE,
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "modelCount": len(models),
        "admittedNewReleases": sorted(admitted),
        "admissionPolicy": {
            "newModelMinimumAccuracy": NEW_MODEL_ACCURACY,
            "accuracyBasis": "Artificial Analysis whole-percent display (round half up)",
            "newModelSignal": "Newly present in Artificial Analysis default selection",
            "existingModelMinimumAccuracy": 27,
            "familyReplacement": "Newest eligible AA release date per creator and product line",
        },
        "models": models,
    }


if __name__ == "__main__":
    payload = build()
    (ROOT / "data.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {len(payload['models'])} models at {payload['updatedAt']}")
