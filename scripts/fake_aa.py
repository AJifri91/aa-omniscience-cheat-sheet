"""A small, structure-faithful imitation of AA's page and feed, for tests.

The layout mirrors what AA served in November 2026 (see aa_source.py):
Next.js push chunks split at arbitrary points, module-import ("I") rows, a
length-prefixed text ("T") row containing newlines and multi-byte characters,
and JSON rows whose component props hold `modelsAndReleases` and `manifest`.
The model numbers are invented and the feed uses a throwaway test key, so no
AA data is copied into the repository.
"""
import gzip
import hashlib
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TEST_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
FEED_PATH = "/data/0123456789abcdef.txt"


def scored(slug, *, release, creator, accuracy, hallucination, name=None, default=False,
           release_date="2026-10-01"):
    """One scored feed row, shaped like AA's (OI is derived so the metrics agree)."""
    oi = accuracy * 100 - hallucination * (100 - accuracy * 100)
    return {
        "id": f"id-{slug}", "slug": slug, "name": name or slug, "shortName": name or slug,
        "release": {"slug": release, "name": release}, "chartDefaultSelected": default,
        "releaseDate": release_date, "deprecated": False,
        "creator": {"id": f"id-{creator}", "slug": creator, "name": creator.title()},
        "omniscience": oi,
        "omniscienceBreakdown": {"accuracy": accuracy, "hallucinationRate": hallucination,
                                 "attemptRate": 0.8, "byDomain": {"Law": 50.0}},
        "intelligenceIndex": 50,
    }


def unscored(slug):
    return {"id": f"id-{slug}", "slug": slug, "name": slug, "omniscience": None,
            "omniscienceBreakdown": None}


def standard_feed(count=16):
    """`count` scored models from distinct creators plus one unscored model.

    The first four are on AA's default chart selection.
    """
    rows = [scored(f"model-{n}", release=f"model-{n}", creator=f"lab{n}",
                   accuracy=0.40 + n / 100, hallucination=0.30, name=f"Model {n} (high)",
                   default=n < 4)
            for n in range(count)]
    return {"models": rows + [unscored("not-yet-scored")], "priceFallbackByModelId": {}}


def catalogue(feed):
    """The page's modelsAndReleases catalogue for every scored row in `feed`."""
    releases, variants = [], []
    for row in feed["models"]:
        if row.get("omniscience") is None:
            continue
        variants.append({"slug": row["slug"], "name": row["name"], "releaseSlug": row["release"]["slug"]})
        releases.append({"slug": row["release"]["slug"], "name": row["release"]["name"], "deprecated": False,
                         "releaseDate": row["releaseDate"],
                         "creator": {"slug": row["creator"]["slug"], "name": row["creator"]["name"]}})
    return releases, variants


def encrypt(feed, key=TEST_KEY, layout="hashed-nonce", compress=True):
    data = json.dumps(feed).encode("utf-8")
    if compress:
        data = gzip.compress(data, mtime=0)
    if key is None:
        return data
    secret = bytes.fromhex(key)
    if layout == "hashed-nonce":
        return AESGCM(secret).encrypt(hashlib.sha256(secret).digest()[:12], data, None)
    nonce = bytes(range(12))
    return nonce + AESGCM(secret).encrypt(nonce, data, None)


def flight(releases, variants, manifest, *, manifest_prop="manifest"):
    """A React Server Components payload laid out like AA's page."""
    text = "Methodology notes\nCafé — Ω résumé\nsecond line"
    catalogue_props = {"modelsAndReleases": {"releases": releases, "models": variants}}
    tool_props = {"rateByEval": {"omniscience": {"input": 0.01}}, manifest_prop: manifest,
                  "children": "$L2d"}
    rows = [
        '0:{"P":null,"b":"build","c":["",""]}',
        '1:I[39088,["70728","static/chunks/6e810e1c.js"],"Nav"]',
        ':HL["/_next/static/media/09d69b6d-s.p.woff2","font",{"crossOrigin":"","type":"font/woff2"}]',
        f'2:T{len(text.encode("utf-8")):x},{text}',
        '3:["$","$L6",null,{"locale":"en","children":["$","main",null,{"children":[["$","$L88",null,'
        + json.dumps(catalogue_props) + "]]}]}]",
        '4:["$","$L9",null,' + json.dumps(tool_props) + "]",
        '5:"$Sreact.suspense"',
        "6:null",
    ]
    # Text rows are length-delimited, so the next row follows without a newline.
    return "\n".join(rows[:3]) + "\n" + rows[3] + "\n".join(rows[4:]) + "\n"


def page(flight_text, *, chunk_size=97):
    """HTML that streams `flight_text` through self.__next_f.push chunks."""
    scripts = ["<script>(self.__next_f=self.__next_f||[]).push([0])</script>"]
    for start in range(0, len(flight_text), chunk_size):
        chunk = json.dumps(flight_text[start:start + chunk_size], ensure_ascii=False).replace("<", "\\u003c")
        scripts.append(f"<script>self.__next_f.push([1,{chunk}])</script>")
    body = ("<p>R&amp;D notes &copy; 2026 &lt;not a tag&gt;</p>"
            '<script type="text/javascript">var x = "a &amp; b";</script>')
    return ("<!DOCTYPE html><html><head><title>AA</title></head><body>" + body
            + "".join(scripts) + "</body></html>").encode("utf-8")


def site(feed=None, *, key=TEST_KEY, layout="hashed-nonce", manifest_prop="manifest", include_catalogue=True):
    """Return {url: bytes} for a complete fake AA site."""
    feed = feed if feed is not None else standard_feed()
    releases, variants = catalogue(feed) if include_catalogue else ([], [])
    manifest = {"path": FEED_PATH, "key": key} if key else {"path": FEED_PATH}
    return {
        "https://artificialanalysis.ai/evaluations/omniscience":
            page(flight(releases, variants, manifest, manifest_prop=manifest_prop)),
        "https://artificialanalysis.ai" + FEED_PATH: encrypt(feed, key=key, layout=layout),
    }


class FakeFetcher:
    """A Fetcher stand-in that serves a fake site and records what was asked for."""

    def __init__(self, responses):
        self.site = dict(responses)
        self.responses = {}

    def get(self, url, *, timeout):
        from aa_source import SourceError
        if url not in self.site:
            raise SourceError(f"{url} returned HTTP 404.")
        self.responses[url] = self.site[url]
        return self.site[url]
