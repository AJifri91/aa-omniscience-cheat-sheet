import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import aa_source
import fake_aa
import update_data
from update_data import (GuardError, StateError, UpdateError, build, check_changes, eligible_models,
                         load_config, load_previous, model_row)


def model(release, accuracy, *, effort=4, selected=False, date="2026-09-23"):
    return {
        "model": release,
        "releaseKey": release,
        "creatorSlug": "test",
        "rawAccuracy": accuracy,
        "reasoningRank": effort,
        "chartDefaultSelected": selected,
        "releaseDate": date,
    }


class AdmissionTests(unittest.TestCase):
    def test_unselected_new_release_is_admitted(self):
        previous = {"seenBenchmarkReleases": ["older-model"], "models": []}
        rows, admitted, seen, tracked = eligible_models(
            [model("older-model", 40), model("new-model", 35)], previous
        )
        self.assertEqual([row["releaseKey"] for row in rows], ["new-model"])
        self.assertIn("new-model", admitted)
        self.assertIn("new-model", seen)
        self.assertIn("new-model", tracked)

    def test_lower_reasoning_cannot_qualify_release(self):
        previous = {"seenBenchmarkReleases": [], "models": []}
        rows, admitted, _, tracked = eligible_models(
            [model("new-model", 40, effort=2), model("new-model", 34, effort=5)], previous
        )
        self.assertEqual(rows, [])
        self.assertEqual(admitted, set())
        self.assertEqual(tracked, {"new-model"})

    def test_tracked_release_can_qualify_later(self):
        previous = {"seenBenchmarkReleases": ["new-model"],
                    "trackedNewReleases": ["new-model"], "models": []}
        rows, admitted, _, _ = eligible_models([model("new-model", 35)], previous)
        self.assertEqual(len(rows), 1)
        self.assertEqual(admitted, {"new-model"})

    def test_new_family_release_replaces_older_only_when_qualified(self):
        previous = {"seenBenchmarkReleases": ["grok-4-6"],
                    "admittedNewReleases": [], "models": []}
        old = model("grok-4-6", 40, date="2026-09-01")
        new = model("grok-4-7", 34, date="2026-09-23")
        rows, _, _, tracked = eligible_models([old, new], previous)
        self.assertEqual([row["releaseKey"] for row in rows], ["grok-4-6"])
        self.assertIn("grok-4-7", tracked)
        new["rawAccuracy"] = 35
        rows, _, _, _ = eligible_models([old, new], previous)
        self.assertEqual([row["releaseKey"] for row in rows], ["grok-4-7"])

    def test_migration_does_not_import_historical_rows(self):
        rows, admitted, seen, tracked = eligible_models(
            [model("historical", 50), model("fresh", 35, selected=True)],
            {"models": []},
        )
        self.assertEqual([row["releaseKey"] for row in rows], ["fresh"])
        self.assertEqual(admitted, {"fresh"})
        self.assertEqual(seen, {"historical", "fresh"})
        self.assertEqual(tracked, {"fresh"})

    def test_thresholds_come_from_config(self):
        policy = dict(load_config()["admission"], newModelMinimumAccuracy=50)
        rows, _, _, _ = eligible_models([model("new-model", 45)],
                                        {"seenBenchmarkReleases": [], "models": []}, policy)
        self.assertEqual(rows, [])


class ConfigTests(unittest.TestCase):
    def test_repository_config_is_valid(self):
        config = load_config()
        self.assertEqual(config["admission"]["newModelMinimumAccuracy"], 35)
        self.assertIn("grok-4-6", config["admission"]["grandfatheredReleases"])

    def test_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"admission": {}, "guards": {}}))
            with self.assertRaisesRegex(UpdateError, "missing or invalid"):
                load_config(path)


class StateTests(unittest.TestCase):
    STATE = {"admittedNewReleases": ["a"], "seenBenchmarkReleases": ["a", "b"], "trackedNewReleases": ["a"]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, payload):
        Path(self.root, name).write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def test_state_file_is_preferred(self):
        self.write("data.json", {"models": []})
        self.write("state.json", self.STATE)
        _, state, source = load_previous(self.root)
        self.assertEqual(state, self.STATE)
        self.assertEqual(source, "state.json")

    def test_history_moves_out_of_old_style_data_json_once(self):
        self.write("data.json", {"models": [], **self.STATE})
        _, state, source = load_previous(self.root)
        self.assertEqual(state, self.STATE)
        self.assertIn("one-time move", source)

    def test_corrupt_state_fails_loudly_instead_of_resetting(self):
        self.write("data.json", {"models": []})
        self.write("state.json", "{not json")
        with self.assertRaisesRegex(StateError, "state.json exists but cannot be read"):
            load_previous(self.root)

    def test_corrupt_data_fails_loudly_instead_of_resetting(self):
        self.write("data.json", "<<<<<<< merge conflict")
        self.write("state.json", self.STATE)
        with self.assertRaisesRegex(StateError, "data.json exists but cannot be read"):
            load_previous(self.root)

    def test_incomplete_state_is_rejected(self):
        self.write("data.json", {"models": []})
        self.write("state.json", {"admittedNewReleases": []})
        with self.assertRaisesRegex(StateError, "incomplete"):
            load_previous(self.root)

    def test_missing_history_requires_explicit_bootstrap(self):
        self.write("data.json", {"models": []})
        with self.assertRaisesRegex(StateError, "state.json is missing"):
            load_previous(self.root)
        _, state, source = load_previous(self.root, bootstrap=True)
        self.assertEqual(state, {})
        self.assertIn("bootstrap", source)

    def test_missing_data_requires_explicit_bootstrap(self):
        with self.assertRaisesRegex(StateError, "data.json is missing"):
            load_previous(self.root)


def published(name, release, creator, accuracy, oi, url=None):
    return {"model": name, "releaseKey": release, "creatorSlug": creator, "accuracy": accuracy,
            "omniscienceIndex": oi, "detailsUrl": url or f"https://artificialanalysis.ai/models/{release}"}


class GuardTests(unittest.TestCase):
    guards = load_config()["guards"]

    def baseline(self):
        return [published(f"M{n}", f"model-{n}", f"lab{n}", 40.0, 10.0) for n in range(10)]

    def test_normal_churn_passes(self):
        old = self.baseline()
        new = old[1:] + [published("Fresh", "fresh-1", "newlab", 45.0, 12.0)]
        summary, problems = check_changes(old, new, self.guards)
        self.assertEqual(problems, [])
        self.assertTrue(any(line.startswith("Added (1): Fresh") for line in summary))
        self.assertTrue(any(line.startswith("Removed (1): M0") for line in summary))

    def test_newer_release_in_same_product_line_is_a_replacement_not_a_removal(self):
        old = self.baseline()
        new = old[:-1] + [published("M9 v2", "model-10", "lab9", 41.0, 11.0)]
        summary, problems = check_changes(old, new, self.guards)
        self.assertEqual(problems, [])
        self.assertIn("Replaced by a newer release (1): M9 → M9 v2", summary)

    def test_mass_removal_is_blocked(self):
        old = self.baseline()
        _, problems = check_changes(old, old[4:], self.guards)
        self.assertEqual(problems, ["4 models would be removed at once (limit 3)"])

    def test_mass_additions_are_blocked(self):
        old = self.baseline()
        new = old + [published(f"N{n}", f"new-{n}", f"other{n}", 40.0, 5.0) for n in range(6)]
        _, problems = check_changes(old, new, self.guards)
        self.assertEqual(problems, ["6 models would be added at once (limit 5)"])

    def test_mass_rescoring_is_blocked_but_small_moves_are_not(self):
        old = self.baseline()
        new = [dict(m, accuracy=m["accuracy"] + 2) for m in old]
        self.assertEqual(check_changes(old, new, self.guards)[1], [])
        new = [dict(m, omniscienceIndex=m["omniscienceIndex"] + 9) if i < 4 else m for i, m in enumerate(old)]
        _, problems = check_changes(old, new, self.guards)
        self.assertEqual(problems, ["4 models would be re-scored at once (limit 3)"])

    def test_first_publication_is_not_guarded(self):
        self.assertEqual(check_changes([], self.baseline(), self.guards)[1], [])


class BuildTests(unittest.TestCase):
    config = load_config()

    def records(self, count=16):
        records, _ = aa_source.load_records(fake_aa.FakeFetcher(fake_aa.site(fake_aa.standard_feed(count))))
        return records

    def test_metrics_are_derived_from_records(self):
        row = model_row({"slug": "x", "name": "X", "releaseKey": "x", "creatorSlug": "lab",
                         "reasoningRank": 4, "releaseDate": "2026-10-01", "chartDefaultSelected": True,
                         "accuracy": 0.5826666666666667, "hallucinationRate": 0.11421725239616613,
                         "omniscience": 53.5})
        self.assertEqual((row["correct"], row["incorrect"], row["partialAbstain"]), (58.27, 4.77, 36.97))
        self.assertEqual((row["hallucinationRate"], row["standardPercent"]), (11.42, 76.75))
        self.assertEqual(row["profile"],
                         "Frontier reliability: high knowledge with comparatively controlled guessing.")

    def test_inconsistent_metrics_are_rejected_with_the_model_named(self):
        with self.assertRaisesRegex(UpdateError, "Inconsistent benchmark metrics for broken"):
            model_row({"slug": "broken", "accuracy": 0.5, "hallucinationRate": 0.5, "omniscience": 40})

    def test_payload_and_state_are_separate(self):
        state = {"admittedNewReleases": [], "seenBenchmarkReleases": [], "trackedNewReleases": []}
        payload, new_state = build(self.records(), {"models": []}, state, self.config,
                                   now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertEqual(set(payload), {"source", "updatedAt", "modelCount", "admissionPolicy", "models"})
        self.assertEqual(payload["modelCount"], 16)
        self.assertEqual(payload["updatedAt"], "2026-11-04T00:00:00Z")
        self.assertEqual(len(new_state["admittedNewReleases"]), 16)
        self.assertEqual(payload["models"][0]["releaseKey"], "model-15")  # sorted by OI

    def test_updated_at_only_changes_when_data_changes(self):
        state = {"admittedNewReleases": [], "seenBenchmarkReleases": [], "trackedNewReleases": []}
        first, first_state = build(self.records(), {"models": []}, state, self.config,
                                   now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        second, _ = build(self.records(), first, first_state, self.config,
                          now=datetime(2026, 11, 5, tzinfo=timezone.utc))
        self.assertEqual(second["updatedAt"], "2026-11-04T00:00:00Z")


class CommandLineTests(unittest.TestCase):
    """End-to-end runs of update_data.main() against a fake AA snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "repo")
        self.root.mkdir()
        Path(self.root, "config.json").write_text(Path(update_data.ROOT, "config.json").read_text())
        self.snapshot = Path(self.tmp.name, "snapshot")
        self.save_site(fake_aa.site())
        patcher = mock.patch.object(update_data, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def save_site(self, responses, directory=None):
        fetcher = fake_aa.FakeFetcher(responses)
        for url in responses:
            fetcher.get(url, timeout=1)
        aa_source.save_snapshot(fetcher, directory or self.snapshot, {"result": "fixture"})

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        quiet_ci = {"GITHUB_ACTIONS": "", "GITHUB_STEP_SUMMARY": "", "ALLOW_LARGE_CHANGES": ""}
        with redirect_stdout(out), redirect_stderr(err), mock.patch.dict("os.environ", quiet_ci):
            code = update_data.main(["--from-snapshot", str(self.snapshot), *args])
        return code, out.getvalue(), err.getvalue()

    def read(self, name):
        return json.loads(Path(self.root, name).read_text())

    def test_bootstrap_then_quiet_rerun(self):
        code, out, err = self.run_main("--bootstrap")
        self.assertEqual(code, 0, err)
        data, state = self.read("data.json"), self.read("state.json")
        self.assertEqual(data["modelCount"], 4)  # a brand-new tracker starts from AA's default selection
        self.assertEqual(len(state["seenBenchmarkReleases"]), 16)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("Admission history: state.json", out)
        self.assertEqual(self.read("data.json")["updatedAt"], data["updatedAt"])

    def test_missing_history_stops_the_run_without_writing(self):
        Path(self.root, "data.json").write_text(json.dumps({"models": []}))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("state.json is missing", err)
        self.assertFalse(Path(self.root, "state.json").exists())

    def test_new_release_is_published_and_dry_run_writes_nothing(self):
        state = {"admittedNewReleases": [], "seenBenchmarkReleases": [], "trackedNewReleases": []}
        Path(self.root, "data.json").write_text(json.dumps({"models": []}))
        Path(self.root, "state.json").write_text(json.dumps(state))
        code, out, err = self.run_main("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Dry run: nothing written.", out)
        self.assertEqual(self.read("state.json"), state)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("data.json")["modelCount"], 16)

    def test_guard_blocks_mass_removal_and_override_allows_it(self):
        state = {"admittedNewReleases": [], "seenBenchmarkReleases": [], "trackedNewReleases": []}
        Path(self.root, "data.json").write_text(json.dumps({"models": []}))
        Path(self.root, "state.json").write_text(json.dumps(state))
        self.save_site(fake_aa.site(fake_aa.standard_feed(20)))
        self.assertEqual(self.run_main()[0], 0)
        published_before = self.read("data.json")
        self.assertEqual(published_before["modelCount"], 20)
        feed = fake_aa.standard_feed(20)
        feed["models"] = feed["models"][:16] + feed["models"][20:]  # four models vanish
        self.save_site(fake_aa.site(feed))
        failure_dir = Path(self.tmp.name, "failure")
        code, _, err = self.run_main("--snapshot-dir", str(failure_dir))
        self.assertEqual(code, 1)
        self.assertIn("4 models would be removed at once", err)
        self.assertEqual(self.read("data.json"), published_before)
        report = json.loads(Path(failure_dir, "index.json").read_text())
        self.assertEqual(report["result"], "failed")
        self.assertEqual(len(report["files"]), 2)
        code, _, err = self.run_main("--allow-large-changes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("data.json")["modelCount"], 16)

    def test_unreadable_snapshot_is_a_clean_failure(self):
        Path(self.root, "data.json").write_text(json.dumps({"models": []}))
        Path(self.root, "state.json").write_text(json.dumps(
            {"admittedNewReleases": [], "seenBenchmarkReleases": [], "trackedNewReleases": []}))
        Path(self.snapshot, "index.json").write_text("{")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("is not a readable snapshot index", err)


if __name__ == "__main__":
    unittest.main()
