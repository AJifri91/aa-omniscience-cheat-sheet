import unittest

from update_data import eligible_models


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


if __name__ == "__main__":
    unittest.main()
