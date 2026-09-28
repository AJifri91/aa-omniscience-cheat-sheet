import unittest

from update_data import eligible_models, model_metadata, page_model_metadata


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


class MetadataRecoveryTests(unittest.TestCase):
    def test_separate_page_model_and_release_records_are_joined(self):
        payload = ('7:{"slug":"new-2026","name":"New 2026",'
                   '"releaseDate":"2026-09-28","creator":{"slug":"lab"}}'
                   '8:{"slug":"new-2026-high","name":"New 2026 (high)",'
                   '"releaseSlug":"new-2026"}')
        metadata = page_model_metadata(payload)
        self.assertEqual(metadata["new-2026-high"]["release"]["slug"], "new-2026")
        self.assertEqual(metadata["new-2026-high"]["creator"]["slug"], "lab")
        self.assertEqual(metadata["new-2026-high"]["releaseDate"], "2026-09-28")

    def test_missing_page_entry_recovers_exact_previous_release(self):
        metadata = {
            "claude-opus-5": {
                "release": {"slug": "claude-opus-5"},
                "creator": {"slug": "anthropic"},
            }
        }
        prior = {"releaseKey": "claude-opus-5-5", "reasoningRank": 6,
                 "releaseDate": "2026-09-22"}
        recovered = model_metadata({"slug": "claude-opus-5-5"}, metadata, prior)
        self.assertEqual(recovered["release"]["slug"], "claude-opus-5-5")
        self.assertEqual(recovered["creator"]["slug"], "anthropic")
        self.assertEqual(recovered["reasoningRank"], 6)

    def test_new_row_uses_feed_identity_if_page_metadata_is_missing(self):
        row = {"slug": "new-model", "release": {"slug": "new-model"},
               "creator": {"slug": "new-lab"}}
        self.assertEqual(model_metadata(row, {})["release"]["slug"], "new-model")

    def test_unknown_new_row_is_deferred_without_guessing_family(self):
        self.assertIsNone(model_metadata({"slug": "new-model"}, {}))

    def test_ambiguous_creator_does_not_replace_prior_release(self):
        metadata = {
            "one": {"release": {"slug": "nova-1"}, "creator": {"slug": "lab-one"}},
            "two": {"release": {"slug": "nova-2"}, "creator": {"slug": "lab-two"}},
        }
        prior = {"releaseKey": "nova-3", "reasoningRank": 4}
        self.assertIsNone(model_metadata({"slug": "nova-3"}, metadata, prior))


if __name__ == "__main__":
    unittest.main()
