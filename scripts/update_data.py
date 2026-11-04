#!/usr/bin/env python3
"""Refresh data.json with the latest AA-Omniscience results.

Pipeline:
  aa_source.load_records()   read and validate AA's data (the only AA-specific code)
  build()                    derive metrics, apply admission rules
  check_changes()            block suspicious mass changes (sanity guard)
  write                      data.json (what the site shows) + state.json (admission history)

Run `python scripts/update_data.py --help` for options such as --dry-run and
--from-snapshot. See README.md for the full picture.
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import aa_source
from aa_source import EFFORT, EFFORT_NOTE, SourceError, product_key

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = "config.json"
DATA_FILE = "data.json"
STATE_FILE = "state.json"
STATE_KEYS = ("admittedNewReleases", "seenBenchmarkReleases", "trackedNewReleases")


class UpdateError(RuntimeError):
    """The update cannot proceed safely. Nothing is written."""


class StateError(UpdateError):
    """Stored data or admission history is missing or unreadable."""


class GuardError(UpdateError):
    """The new data differs from the published data more than the guard allows."""


# --------------------------------------------------------------------------
# Configuration and stored state
# --------------------------------------------------------------------------

def load_config(path=None):
    path = Path(path) if path else ROOT / CONFIG_FILE
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        admission, guards = config["admission"], config["guards"]
        int(admission["newModelMinimumAccuracy"]), int(admission["grandfatheredMinimumAccuracy"])
        if not all(isinstance(item, str) for item in admission["grandfatheredReleases"]):
            raise TypeError("grandfatheredReleases must be a list of release slugs")
        for key in ("minimumModels", "maxRemovedModels", "maxAddedModels", "maxChangedModels",
                    "changedAccuracyPoints", "changedIndexPoints"):
            float(guards[key])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError(f"{path} is missing or invalid ({exc}).") from exc
    return config


def _read_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"{path.name} exists but cannot be read ({exc}). Restore it from git "
                         f"history; refusing to continue, because a silent reset would {what}.") from exc


def load_previous(root=None, bootstrap=False):
    """Return (published data, admission state, where the state came from).

    Fails loudly instead of silently starting over: an empty history would
    re-run the one-time migration, dropping admitted models for good.
    """
    root = Path(root) if root else ROOT
    data_path, state_path = root / DATA_FILE, root / STATE_FILE
    data = {}
    if data_path.exists():
        data = _read_json(data_path, "change which models are shown")
        if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
            raise StateError(f"{DATA_FILE} does not have the expected shape (an object with a 'models' list).")
    elif not bootstrap:
        raise StateError(f"{DATA_FILE} is missing. Restore it from git history, or run with --bootstrap "
                         "to start a brand-new tracker.")

    if state_path.exists():
        state = _read_json(state_path, "drop admitted models permanently")
        source = STATE_FILE
    elif "seenBenchmarkReleases" in data:
        state = {key: data.get(key, []) for key in STATE_KEYS}
        source = f"{DATA_FILE} (one-time move to {STATE_FILE})"
    elif bootstrap:
        return data, {}, "bootstrap (empty history)"
    else:
        raise StateError(f"{STATE_FILE} is missing and {DATA_FILE} holds no admission history. "
                         f"Restore {STATE_FILE} from git history; use --bootstrap only for a brand-new tracker.")

    if not isinstance(state, dict) or any(
            not isinstance(state.get(key), list) or not all(isinstance(item, str) for item in state[key])
            for key in STATE_KEYS):
        raise StateError(f"Admission history from {source} is incomplete: it needs lists of release "
                         f"slugs for {', '.join(STATE_KEYS)}.")
    return data, {key: state[key] for key in STATE_KEYS}, source


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------
# Metrics and admission rules
# --------------------------------------------------------------------------

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


def model_row(record):
    """Derive the published metrics for one validated AA record."""
    acc, oi, hr = record["accuracy"], record["omniscience"], record["hallucinationRate"]
    # OI = c - i and HR = i / (i + p + a) = i / (100 - c), so OI = c - HR * (100 - c).
    if abs((acc * 100 - hr * (100 - acc * 100)) - oi) > 0.1:
        raise UpdateError(f"Inconsistent benchmark metrics for {record['slug']}: accuracy, hallucination "
                          "rate and Omniscience Index no longer agree. AA may have changed what a field means.")
    correct = acc * 100
    incorrect = correct - oi
    partial_abstain = 100 - correct - incorrect
    return {
        "model": record["name"],
        "releaseKey": record["releaseKey"],
        "creatorSlug": record["creatorSlug"],
        "reasoningRank": record["reasoningRank"],
        "releaseDate": record["releaseDate"],
        "chartDefaultSelected": record["chartDefaultSelected"],
        "rawAccuracy": correct,
        "detailsUrl": "https://artificialanalysis.ai/models/" + record["slug"],
        "correct": round(correct, 2),
        "incorrect": round(incorrect, 2),
        "partialAbstain": round(partial_abstain, 2),
        "accuracy": round(acc * 100, 2),
        "omniscienceIndex": round(oi, 2),
        "hallucinationRate": round(hr * 100, 2),
        "standardPercent": round((oi / 2) + 50, 2),
        "profile": profile(acc, oi, hr),
    }


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
    return f'{row.get("creatorSlug", "unknown")}:{product_key(row["releaseKey"])}'


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


def previous_admitted_releases(previous, grandfathered):
    admitted = set(previous.get("admittedNewReleases", []))
    # Anything already displayed (and not grandfathered) was admitted earlier.
    admitted.update(row.get("releaseKey") for row in previous.get("models", [])
                    if row.get("releaseKey") not in grandfathered)
    admitted.discard(None)
    return admitted - grandfathered


def eligible_models(models, previous=None, policy=None):
    """Admit newly scored releases, while keeping historical non-baseline rows out.

    The first run snapshots AA's complete scored catalogue. After that, any
    release newly appearing in the full feed is tracked even when AA does not
    mark it as default-selected. Tracked releases can qualify on a later run if
    their highest-reasoning score crosses the threshold.
    """
    policy = policy or load_config()["admission"]
    grandfathered = set(policy["grandfatheredReleases"])
    new_minimum = policy["newModelMinimumAccuracy"]
    previous = previous or {}
    previously_admitted = previous_admitted_releases(previous, grandfathered)
    selected = highest_reasoning(models)
    current_releases = {row["releaseKey"] for row in selected}
    known_releases = set(previous.get("seenBenchmarkReleases", []))
    tracked = set(previous.get("trackedNewReleases", []))
    if "seenBenchmarkReleases" in previous:
        tracked.update(current_releases - known_releases - grandfathered)
    else:
        # First run of a brand-new tracker (--bootstrap only): don't admit
        # hundreds of older scored models. Start from AA's default chart
        # selection, then use the full catalogue for every future discovery.
        tracked.update(row["releaseKey"] for row in selected
                       if row["releaseKey"] not in grandfathered and
                       row.get("chartDefaultSelected") and
                       displayed_accuracy(row["rawAccuracy"]) >= new_minimum)
    newly_qualified = {row["releaseKey"] for row in selected
                       if row["releaseKey"] in tracked and
                       displayed_accuracy(row["rawAccuracy"]) >= new_minimum}
    admitted = previously_admitted | newly_qualified
    eligible = [row for row in selected if
                (row["releaseKey"] in grandfathered and
                 row["rawAccuracy"] >= policy["grandfatheredMinimumAccuracy"]) or
                row["releaseKey"] in admitted]
    # A new release replaces an older release only after passing admission.
    # This prevents an unqualified launch from removing a qualifying incumbent.
    eligible = newest_product_lines(eligible)
    return eligible, admitted, known_releases | current_releases, tracked


def admission_policy(policy):
    return {
        "newModelMinimumAccuracy": policy["newModelMinimumAccuracy"],
        "accuracyBasis": "Artificial Analysis whole-percent display (round half up)",
        "newModelSignal": "New scored releases in the complete AA model feed, tracked across runs",
        "existingModelMinimumAccuracy": policy["grandfatheredMinimumAccuracy"],
        "familyReplacement": "Newest eligible AA release date per creator and product line",
    }


def build(records, previous_data, state, config, now=None):
    """Return (data.json payload, state.json payload) for validated AA records."""
    policy, guards = config["admission"], config["guards"]
    models = [model_row(record) for record in records]
    if len(models) < guards["minimumModels"]:
        raise UpdateError(f"Only {len(models)} usable model records found; source format may have changed.")
    previous = {**state, "models": previous_data.get("models", [])}
    models, admitted, seen, tracked = eligible_models(models, previous, policy)
    for row in models:
        del row["rawAccuracy"]
    models.sort(key=lambda row: row["omniscienceIndex"], reverse=True)
    now = now or datetime.now(timezone.utc)
    payload = {
        "source": aa_source.SOURCE_URL,
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "modelCount": len(models),
        "admissionPolicy": admission_policy(policy),
        "models": models,
    }
    unchanged = ({k: v for k, v in payload.items() if k != "updatedAt"} ==
                 {k: v for k, v in previous_data.items() if k != "updatedAt"})
    if unchanged and previous_data.get("updatedAt"):
        # updatedAt means "data last changed", so a quiet run changes nothing.
        payload["updatedAt"] = previous_data["updatedAt"]
    new_state = {
        "admittedNewReleases": sorted(admitted),
        "seenBenchmarkReleases": sorted(seen),
        "trackedNewReleases": sorted(tracked),
    }
    return payload, new_state


# --------------------------------------------------------------------------
# Sanity guard
# --------------------------------------------------------------------------

def check_changes(previous_models, new_models, guards):
    """Compare published and new models. Return (summary lines, guard problems)."""
    old_lines = {lineage_key(m): m for m in previous_models if m.get("releaseKey")}
    new_lines = {lineage_key(m): m for m in new_models}
    removed = [m["model"] for key, m in old_lines.items() if key not in new_lines]
    added = [m["model"] for key, m in new_lines.items() if key not in old_lines]
    replaced = [f'{old_lines[key]["model"]} → {m["model"]}' for key, m in new_lines.items()
                if key in old_lines and old_lines[key]["detailsUrl"] != m["detailsUrl"]]
    old_by_url = {m["detailsUrl"]: m for m in previous_models if m.get("detailsUrl")}
    rescored = []
    for m in new_models:
        old = old_by_url.get(m["detailsUrl"])
        if old is None:
            continue
        accuracy_move = abs(m["accuracy"] - old.get("accuracy", m["accuracy"]))
        index_move = abs(m["omniscienceIndex"] - old.get("omniscienceIndex", m["omniscienceIndex"]))
        if accuracy_move > guards["changedAccuracyPoints"] or index_move > guards["changedIndexPoints"]:
            rescored.append(f'{m["model"]} (accuracy {old.get("accuracy")} → {m["accuracy"]}, '
                            f'OI {old.get("omniscienceIndex")} → {m["omniscienceIndex"]})')

    summary = [f"Models: {len(previous_models)} → {len(new_models)}"]
    for label, items in (("Added", added), ("Removed", removed), ("Replaced by a newer release", replaced),
                         ("Re-scored beyond guard thresholds", rescored)):
        if items:
            summary.append(f"{label} ({len(items)}): " + "; ".join(items))
    if len(summary) == 1:
        summary.append("No model changes.")

    problems = []
    if previous_models:
        for label, items, limit in (("removed", removed, guards["maxRemovedModels"]),
                                    ("added", added, guards["maxAddedModels"]),
                                    ("re-scored", rescored, guards["maxChangedModels"])):
            if len(items) > limit:
                problems.append(f"{len(items)} models would be {label} at once (limit {limit:g})")
    return summary, problems


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Refresh data.json from AA-Omniscience results.")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate everything and show what would change, but write nothing")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="where to write data.json and state.json (default: repository root)")
    parser.add_argument("--snapshot-dir", type=Path,
                        help="on failure, save AA's raw page and feed here for debugging")
    parser.add_argument("--keep-snapshot", action="store_true",
                        help="save the snapshot even when the run succeeds (needs --snapshot-dir)")
    parser.add_argument("--from-snapshot", type=Path, metavar="DIR",
                        help="replay a saved snapshot instead of contacting AA")
    parser.add_argument("--allow-large-changes", action="store_true",
                        help="publish even if the sanity guard flags a mass change "
                             "(same as ALLOW_LARGE_CHANGES=true)")
    parser.add_argument("--bootstrap", action="store_true",
                        help="start a brand-new tracker with empty admission history")
    return parser.parse_args(argv)


def _annotate(level, message):
    """Surface a message in the GitHub Actions UI when running there."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title=AA-Omniscience update::{escaped}")


def _step_summary(lines):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    allow_large = args.allow_large_changes or os.environ.get("ALLOW_LARGE_CHANGES", "").lower() in ("1", "true", "yes")
    fetcher = None
    stats = {}
    try:
        config = load_config()
        previous_data, state, state_source = load_previous(bootstrap=args.bootstrap)
        print(f"Admission history: {state_source}")
        fetcher = (aa_source.SnapshotFetcher(args.from_snapshot) if args.from_snapshot
                   else aa_source.Fetcher())
        records, stats = aa_source.load_records(fetcher, previous_data.get("models", []),
                                                minimum_records=config["guards"]["minimumModels"])
        print("Source: " + ", ".join(f"{k}={v}" for k, v in stats.items()
                                     if not isinstance(v, list)))
        payload, new_state = build(records, previous_data, state, config)
        summary, problems = check_changes(previous_data.get("models", []), payload["models"], config["guards"])
        print("\n".join(summary))
        _step_summary(["### AA-Omniscience update", *[f"- {line}" for line in summary]])
        if problems:
            message = ("Sanity guard: " + "; ".join(problems) + ". This usually means the parser broke "
                       "rather than the benchmark changing. If the change is genuine (for example AA "
                       "re-scored every model), re-run the update workflow manually with 'Allow large "
                       "changes' ticked.")
            if not allow_large:
                raise GuardError(message)
            print("Allowed by override: " + message)
    except Exception as exc:  # noqa: BLE001 - every failure must leave evidence and a clear message
        known = isinstance(exc, (UpdateError, SourceError))
        message = str(exc) if known else f"Unexpected {type(exc).__name__}: {exc}"
        if not known:
            traceback.print_exc()
        if args.snapshot_dir and fetcher is not None and fetcher.responses:
            where = aa_source.save_snapshot(fetcher, args.snapshot_dir, {
                "result": "failed", "error": message, "stats": stats,
                "savedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            print(f"Saved AA's raw responses to {where} for debugging.")
        print(f"ERROR: {message}", file=sys.stderr)
        _annotate("error", message)
        _step_summary(["### AA-Omniscience update failed", message])
        return 1

    if args.snapshot_dir and args.keep_snapshot:
        aa_source.save_snapshot(fetcher, args.snapshot_dir, {
            "result": "ok", "stats": stats, "summary": summary,
            "savedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    output_dir = args.output_dir or ROOT
    write_json(output_dir / STATE_FILE, new_state)
    write_json(output_dir / DATA_FILE, payload)
    print(f"Updated {len(payload['models'])} models; data last changed {payload['updatedAt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
