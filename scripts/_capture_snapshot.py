#!/usr/bin/env python3
"""Temporary: report the *shape* of AA's page and feed (no bulk data) for parser work."""
import gzip
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parent))
UA = {"User-Agent": "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"}
SOURCE = "https://artificialanalysis.ai/evaluations/omniscience"
report = []


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line)
    report.append(line)


def shape(value, depth=0, limit=60):
    """Small structural preview: keys + types + truncated scalars."""
    if isinstance(value, dict):
        if depth > 2:
            return "{…%d keys}" % len(value)
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if depth > 2:
            return "[…%d]" % len(value)
        return [shape(v, depth + 1) for v in value[:2]] + (["…%d more" % (len(value) - 2)] if len(value) > 2 else [])
    if isinstance(value, str):
        value = re.sub(r"[0-9a-f]{24,}", "<hex>", value)
        return value[:limit] + ("…" if len(value) > limit else "")
    return value


with urlopen(Request(SOURCE, headers=UA), timeout=45) as response:
    raw = response.read()
    say("page status", response.status, "bytes", len(raw), "type", response.headers.get("content-type"))
text = raw.decode("utf-8")
say("unescape changes page:", __import__("html").unescape(text) != text)

decoder = json.JSONDecoder()
types = Counter()
payload = []
scripts_with_push = Counter()
for m in re.finditer(r"self\.__next_f\.push\(", text):
    try:
        value, _ = decoder.raw_decode(text, text.index("(", m.start()) + 1)
    except ValueError:
        types["undecodable"] += 1
        continue
    types[str(value[0]) if isinstance(value, list) and value else "other"] += 1
    if isinstance(value, list) and len(value) > 1 and value[0] == 1:
        payload.append(value[1])
say("chunk types:", dict(types), "| __NEXT_DATA__:", "__NEXT_DATA__" in text,
    "| json scripts:", text.count('type="application/json"'))
flight = "".join(payload)
say("flight chars:", len(flight), "| lines:", flight.count("\n"))
tags = Counter()
for line in flight.split("\n"):
    m = re.match(r"([0-9a-f]+):([A-Z]*)(.)?", line)
    if m:
        tags[(m.group(2) or "json") + ":" + (m.group(3) or "")] += 1
    elif line:
        tags["<continuation>"] += 1
say("row heads (tag:first char):", dict(tags.most_common(20)))

# Old-style discovery, to learn real key orders and containers.
slug_objs = []
for m in re.finditer(r'\{"slug":', flight):
    try:
        entry, end = decoder.raw_decode(flight, m.start())
        slug_objs.append((m.start(), entry))
    except ValueError:
        pass
any_slug = Counter(re.findall(r'\{"([A-Za-z_]+)":[^{}]{0,400}?"slug":', flight))
say('{"slug": objects decodable:', len(slug_objs), '| dicts where slug is NOT first key (first key: count):', dict(any_slug.most_common(10)))
variants = [(p, e) for p, e in slug_objs if isinstance(e.get("releaseSlug"), str)]
releases = [(p, e) for p, e in slug_objs if not isinstance(e.get("releaseSlug"), str)
            and isinstance(e.get("creator"), dict) and e.get("releaseDate")]
say("variants:", len(variants), "releases:", len(releases),
    "| distinct variant slugs:", len({e["slug"] for _, e in variants}),
    "| distinct release slugs:", len({e["slug"] for _, e in releases}))
dupes = Counter(e["slug"] for _, e in variants)
dup_slugs = [s for s, c in dupes.items() if c > 1]
say("duplicate variant slugs:", len(dup_slugs), "| identical duplicates:",
    sum(1 for s in dup_slugs if len({json.dumps(e, sort_keys=True) for _, e in variants if e["slug"] == s}) == 1))
for label, items in (("VARIANT", variants[:2]), ("RELEASE", releases[:2])):
    for pos, entry in items:
        say(label, "keys:", list(entry), "\n  preview:", json.dumps(shape(entry))[:900])
        say("  context before:", re.sub(r"[0-9a-f]{24,}", "<hex>", flight[max(0, pos - 220):pos]).replace("\n", "⏎"))

m = re.search(r'"manifest":(\{[^}]+\})', flight)
say("manifest via regex:", bool(m), "| '\"manifest\"' occurrences:", flight.count('"manifest"'))
if m:
    ctx = flight[max(0, m.start() - 400): m.end() + 200]
    say("manifest context:", re.sub(r"[0-9a-f]{24,}", "<hex>", ctx).replace("\n", "⏎"))
    manifest = json.loads(m.group(1))
    say("manifest keys:", list(manifest), "path:", manifest.get("path"), "key len:", len(manifest.get("key") or ""))
    with urlopen(Request("https://artificialanalysis.ai" + manifest["path"], headers=UA), timeout=60) as response:
        feed = response.read()
        say("feed bytes", len(feed), "type", response.headers.get("content-type"), "first bytes", feed[:4].hex())
    key = bytes.fromhex(manifest["key"])
    plain = AESGCM(key).decrypt(hashlib.sha256(key).digest()[:12], feed, None)
    say("decrypted first bytes:", plain[:4].hex(), "(1f8b = gzip)")
    data = json.loads(gzip.decompress(plain))
    say("feed top-level:", {k: type(v).__name__ + (f"[{len(v)}]" if isinstance(v, (list, dict)) else "") for k, v in data.items()})
    rows = data["models"]
    scored = [r for r in rows if r.get("omniscience") is not None]
    key_freq = Counter(k for r in scored for k in r)
    say("rows:", len(rows), "scored:", len(scored), "| keys present in all scored rows:",
        sorted(k for k, c in key_freq.items() if c == len(scored))[:80])
    say("keys present in some scored rows:", sorted((k, c) for k, c in key_freq.items() if c < len(scored))[:60])
    for r in scored[:2]:
        say("SCORED ROW preview:", json.dumps(shape({k: r[k] for k in r if k in (
            "slug", "name", "shortName", "omniscience", "omniscienceBreakdown", "release", "creator",
            "effort", "releaseDate", "chartDefaultSelected", "releaseSlug", "model_creator", "reasoning")}))[:1500])
    say("rows with release dict:", sum(isinstance(r.get("release"), dict) for r in scored),
        "| creator dict:", sum(isinstance(r.get("creator"), dict) for r in scored),
        "| effort dict:", sum(isinstance(r.get("effort"), dict) for r in scored),
        "| chartDefaultSelected true:", sum(bool(r.get("chartDefaultSelected")) for r in scored))
    say("breakdown keys:", sorted(Counter(k for r in scored if isinstance(r.get("omniscienceBreakdown"), dict)
                                           for k in r["omniscienceBreakdown"]).items()))

# Old pipeline end-to-end (no writes): does it still match the committed data?
import update_data as old
payload_new = old.build()
committed = json.loads(Path(old.ROOT / "data.json").read_text())
say("old pipeline models:", payload_new["modelCount"], "| committed:", committed["modelCount"],
    "| same model list:", [m["model"] for m in payload_new["models"]] == [m["model"] for m in committed["models"]])
say("added vs committed:", sorted({m["model"] for m in payload_new["models"]} - {m["model"] for m in committed["models"]}),
    "| removed:", sorted({m["model"] for m in committed["models"]} - {m["model"] for m in payload_new["models"]}))

Path(sys.argv[1] if len(sys.argv) > 1 else "report.md").write_text("```\n" + "\n".join(report)[:60000] + "\n```\n")
