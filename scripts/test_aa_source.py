import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import aa_source
import fake_aa
from aa_source import (SourceError, decode_feed, find_manifests, flight_rows, http_get, load_records,
                       model_metadata, page_identities, page_objects, scored_rows)

PAGE_URL = aa_source.SOURCE_URL
FEED_URL = "https://artificialanalysis.ai" + fake_aa.FEED_PATH


class FlightParsingTests(unittest.TestCase):
    def test_rows_survive_chunk_splits_text_rows_and_multibyte_text(self):
        text = "line one\nCafé — Ω"
        payload = (f'1:I[1,["a"],"Nav"]\n2:T{len(text.encode()):x},{text}'
                   '3:{"x":1}\n4:["$","div",null,{"slug":"s"}]\n').encode("utf-8")
        rows, errors = flight_rows(payload)
        self.assertEqual(errors, [])
        self.assertEqual([(row_id, tag) for row_id, tag, _ in rows],
                         [("1", "I"), ("2", "T"), ("3", ""), ("4", "")])
        self.assertEqual(rows[1][2], text)
        self.assertEqual(rows[2][2], {"x": 1})

    def test_hint_rows_with_an_empty_id_are_understood(self):
        rows, errors = flight_rows(b':HL["/_next/static/media/a.woff2","font",{"crossOrigin":""}]\n'
                                   b'1:{"x":1}\n')
        self.assertEqual(errors, [])
        self.assertEqual([(row_id, tag) for row_id, tag, _ in rows], [("", "HL"), ("1", "")])

    def test_rows_glued_without_newline_are_still_separated(self):
        rows, errors = flight_rows(b'7:{"slug":"a"}8:{"slug":"b"}')
        self.assertEqual(errors, [])
        self.assertEqual([value["slug"] for _, _, value in rows], ["a", "b"])

    def test_unparseable_rows_are_counted_not_guessed(self):
        rows, errors = flight_rows(b'1:{"ok":true}\nnot a row\n2:{broken\n3:[1]\n')
        self.assertEqual([value for _, _, value in rows], [{"ok": True}, [1]])
        self.assertEqual(len(errors), 2)
        self.assertIn("'not a row'", errors[0])
        self.assertIn("row 2 is not JSON", errors[1])

    def test_html_entities_inside_scripts_are_not_decoded(self):
        flight = fake_aa.flight([], [{"slug": "r-and-d", "name": "R&amp;D", "releaseSlug": "r"}],
                                {"path": fake_aa.FEED_PATH, "key": fake_aa.TEST_KEY})
        objects, _ = page_objects(fake_aa.page(flight))
        names = [obj.get("name") for obj in objects if obj.get("slug") == "r-and-d"]
        self.assertEqual(names, ["R&amp;D"])

    def test_page_without_embedded_data_fails_clearly(self):
        with self.assertRaisesRegex(SourceError, "no embedded data"):
            page_objects(b"<html><body><p>Hello</p><script>var a=1;</script></body></html>")


class ShapeDiscoveryTests(unittest.TestCase):
    def objects(self, payload):
        rows, _ = flight_rows(payload.encode("utf-8"))
        return [value for _, _, value in rows]

    def test_manifest_found_regardless_of_key_order_or_nested_fields(self):
        objects = list(aa_source.iter_objects(self.objects(
            '1:["$","$L9",null,{"children":"$L2","manifest":{"meta":{"v":2},"key":"'
            + fake_aa.TEST_KEY + '","path":"/data/abc.txt"}}]\n')))
        self.assertEqual(find_manifests(objects), [{"path": "/data/abc.txt", "key": fake_aa.TEST_KEY}])

    def test_manifest_found_by_shape_when_renamed(self):
        objects = list(aa_source.iter_objects(self.objects(
            '1:{"feedConfig":{"key":"' + fake_aa.TEST_KEY + '","path":"/data/abc.txt"}}\n')))
        self.assertEqual(find_manifests(objects)[0]["path"], "/data/abc.txt")

    def test_manifest_inside_a_json_string_is_found(self):
        inner = json.dumps({"manifest": {"path": "/data/abc.txt", "key": fake_aa.TEST_KEY}})
        objects = list(aa_source.iter_objects([{"props": inner}]))
        self.assertEqual(find_manifests(objects)[0]["path"], "/data/abc.txt")

    def test_unsafe_feed_paths_are_rejected(self):
        objects = [{"manifest": {"path": "//evil.example/x", "key": fake_aa.TEST_KEY}},
                   {"manifest": {"path": "/data/../secret", "key": fake_aa.TEST_KEY}}]
        self.assertEqual(find_manifests(objects), [])

    def test_identities_do_not_depend_on_key_order(self):
        objects = list(aa_source.iter_objects(self.objects(
            '1:{"modelsAndReleases":{"releases":[{"name":"New","creator":{"slug":"lab"},'
            '"releaseDate":"2026-09-28","slug":"new-2026"}],'
            '"models":[{"name":"New (high)","releaseSlug":"new-2026","slug":"new-2026-high"}]}}\n')))
        identity = page_identities(objects)["new-2026-high"]
        self.assertEqual(identity["release"]["slug"], "new-2026")
        self.assertEqual(identity["creator"]["slug"], "lab")
        self.assertEqual(identity["releaseDate"], "2026-09-28")

    def test_separate_page_model_and_release_records_are_joined(self):
        objects = self.objects('7:{"slug":"new-2026","name":"New 2026",'
                               '"releaseDate":"2026-09-28","creator":{"slug":"lab"}}'
                               '8:{"slug":"new-2026-high","name":"New 2026 (high)",'
                               '"releaseSlug":"new-2026"}')
        metadata = page_identities(objects)
        self.assertEqual(metadata["new-2026-high"]["release"]["slug"], "new-2026")
        self.assertEqual(metadata["new-2026-high"]["creator"]["slug"], "lab")
        self.assertEqual(metadata["new-2026-high"]["releaseDate"], "2026-09-28")


class FeedDecodingTests(unittest.TestCase):
    feed = {"models": [{"slug": "a"}]}

    def test_hashed_nonce_layout(self):
        self.assertEqual(decode_feed(fake_aa.encrypt(self.feed), fake_aa.TEST_KEY), self.feed)

    def test_prefixed_nonce_layout(self):
        raw = fake_aa.encrypt(self.feed, layout="prefixed-nonce")
        self.assertEqual(decode_feed(raw, fake_aa.TEST_KEY), self.feed)

    def test_unencrypted_gzip_and_plain_json(self):
        self.assertEqual(decode_feed(fake_aa.encrypt(self.feed, key=None)), self.feed)
        self.assertEqual(decode_feed(json.dumps(self.feed).encode()), self.feed)

    def test_wrong_key_is_reported_as_a_scheme_change(self):
        wrong = "ff" * 32
        with self.assertRaisesRegex(SourceError, "could not be decrypted"):
            decode_feed(fake_aa.encrypt(self.feed), wrong)


class ContractTests(unittest.TestCase):
    def test_valid_rows_pass_and_unscored_rows_are_ignored(self):
        rows = scored_rows(fake_aa.standard_feed(3))
        self.assertEqual([row["slug"] for row in rows], ["model-0", "model-1", "model-2"])

    def test_errors_name_the_model_and_the_field(self):
        feed = fake_aa.standard_feed(2)
        feed["models"][1]["omniscienceBreakdown"]["accuracy"] = "58%"
        feed["models"][0]["omniscienceBreakdown"]["hallucinationRate"] = True
        with self.assertRaises(SourceError) as caught:
            scored_rows(feed)
        message = str(caught.exception)
        self.assertIn("'model-1'", message)
        self.assertIn("omniscienceBreakdown.accuracy must be a number from 0 to 1, got '58%'", message)
        self.assertIn("omniscienceBreakdown.hallucinationRate", message)

    def test_out_of_range_values_are_rejected(self):
        feed = fake_aa.standard_feed(1)
        feed["models"][0]["omniscience"] = 140
        with self.assertRaisesRegex(SourceError, "omniscience must be a number from -100 to 100"):
            scored_rows(feed)

    def test_missing_models_list_lists_the_keys_found(self):
        with self.assertRaisesRegex(SourceError, r"no 'models' list \(top level: \['rows'\]\)"):
            scored_rows({"rows": []})


class RetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("builtins.print")  # keep retry notices out of the test output
        patcher.start()
        self.addCleanup(patcher.stop)

    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    def opener(self, outcomes):
        calls = []

        def open_(request, timeout):
            calls.append(request.full_url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return self.Reply(outcome)
        return open_, calls

    @staticmethod
    def http_error(code, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return HTTPError("https://x", code, "error", headers, None)

    def test_transient_failures_are_retried_with_backoff(self):
        open_, calls = self.opener([URLError("reset"), self.http_error(503), b"ok"])
        waits = []
        self.assertEqual(http_get("https://x", timeout=1, opener=open_, sleep=waits.append), b"ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [5, 20])

    def test_retry_after_header_is_respected_with_a_cap(self):
        open_, _ = self.opener([self.http_error(429, retry_after=7), self.http_error(429, 999), b"ok"])
        waits = []
        http_get("https://x", timeout=1, opener=open_, sleep=waits.append)
        self.assertEqual(waits, [7, 60])

    def test_permanent_http_errors_fail_immediately(self):
        open_, calls = self.opener([self.http_error(404)])
        with self.assertRaisesRegex(SourceError, "HTTP 404"):
            http_get("https://x", timeout=1, opener=open_, sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_the_last_attempt(self):
        open_, _ = self.opener([TimeoutError("slow")] * 3)
        with self.assertRaisesRegex(SourceError, "failed after 3 attempts"):
            http_get("https://x", timeout=1, opener=open_, sleep=lambda _: None)


class LoadRecordsTests(unittest.TestCase):
    def test_full_fake_site_produces_validated_records(self):
        fetcher = fake_aa.FakeFetcher(fake_aa.site())
        records, stats = load_records(fetcher)
        self.assertEqual(len(records), 16)
        first = records[0]
        self.assertEqual(first["slug"], "model-0")
        self.assertEqual(first["name"], "Model 0 (high)")
        self.assertEqual(first["releaseKey"], "model-0")
        self.assertEqual(first["creatorSlug"], "lab0")
        self.assertEqual(first["reasoningRank"], 4)
        self.assertEqual(stats["pageIdentities"], 16)
        self.assertEqual(stats["scoredRows"], 16)
        self.assertEqual(stats["unparsedFlightRows"], 0)

    def test_feed_identity_is_used_when_the_page_catalogue_is_gone(self):
        records, stats = load_records(fake_aa.FakeFetcher(fake_aa.site(include_catalogue=False)))
        self.assertEqual(stats["pageIdentities"], 0)
        self.assertEqual(records[3]["creatorSlug"], "lab3")

    def test_renamed_manifest_prop_still_works(self):
        records, _ = load_records(fake_aa.FakeFetcher(fake_aa.site(manifest_prop="feedSource")))
        self.assertEqual(len(records), 16)

    def test_missing_manifest_fails_closed(self):
        responses = fake_aa.site()
        responses[PAGE_URL] = fake_aa.page(fake_aa.flight([], [], {"path": "/elsewhere"}))
        with self.assertRaisesRegex(SourceError, "No model-feed manifest"):
            load_records(fake_aa.FakeFetcher(responses))

    def test_too_few_records_fails_closed(self):
        responses = fake_aa.site(fake_aa.standard_feed(5))
        with self.assertRaisesRegex(SourceError, "Only 5 complete model records"):
            load_records(fake_aa.FakeFetcher(responses))

    def test_broken_feed_reports_every_manifest_tried(self):
        responses = fake_aa.site()
        responses[FEED_URL] = b"not encrypted data"
        with self.assertRaisesRegex(SourceError, "No usable model feed: /data/0123456789abcdef.txt: "):
            load_records(fake_aa.FakeFetcher(responses))

    def test_snapshot_round_trip_replays_the_same_records(self):
        fetcher = fake_aa.FakeFetcher(fake_aa.site())
        records, _ = load_records(fetcher)
        with tempfile.TemporaryDirectory() as tmp:
            aa_source.save_snapshot(fetcher, tmp, {"result": "ok"})
            index = json.loads((Path(tmp) / "index.json").read_text())
            self.assertEqual(set(index["files"]), {PAGE_URL, FEED_URL})
            replayed, _ = load_records(aa_source.SnapshotFetcher(tmp))
        self.assertEqual(replayed, records)

    def test_replaying_an_incomplete_snapshot_says_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "page.html").write_bytes(fake_aa.site()[PAGE_URL])
            Path(tmp, "index.json").write_text(json.dumps({"files": {PAGE_URL: "page.html"}}))
            with self.assertRaisesRegex(SourceError, "no saved copy of " + FEED_URL):
                load_records(aa_source.SnapshotFetcher(tmp))


class MetadataRecoveryTests(unittest.TestCase):
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
