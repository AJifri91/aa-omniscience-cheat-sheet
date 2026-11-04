#!/usr/bin/env python3
"""Temporary: capture AA's page and raw (still encrypted) feed for parser work."""
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"}
SOURCE = "https://artificialanalysis.ai/evaluations/omniscience"
out = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshot")
out.mkdir(parents=True, exist_ok=True)

with urlopen(Request(SOURCE, headers=UA), timeout=45) as response:
    raw = response.read()
    print("status", response.status, "bytes", len(raw), "content-type", response.headers.get("content-type"))
(out / "page.html").write_bytes(raw)
text = raw.decode("utf-8")

pushes = [m.start() for m in re.finditer(r"self\.__next_f\.push\(", text)]
print("next_f pushes:", len(pushes))
decoder = json.JSONDecoder()
types = Counter()
payload = []
for start in pushes:
    try:
        value, _ = decoder.raw_decode(text, text.index("(", start) + 1)
    except ValueError as exc:
        types["undecodable"] += 1
        continue
    types[value[0] if isinstance(value, list) and value else "other"] += 1
    if isinstance(value, list) and len(value) > 1 and value[0] == 1:
        payload.append(value[1])
print("chunk types:", dict(types))
print("__NEXT_DATA__ present:", "__NEXT_DATA__" in text)
print("application/json scripts:", text.count('type="application/json"'))
flight = "".join(payload)
print("flight chars:", len(flight))
row_tags = Counter(re.findall(r"(?:^|\n)[0-9a-f]+:([A-Z]{0,2})", flight))
print("flight row tags:", dict(row_tags))
print('{"slug": occurrences:', flight.count('{"slug":'), ' "slug": occurrences:', flight.count('"slug":'))
match = re.search(r'"manifest":(\{[^}]+\})', flight)
print("manifest found:", bool(match))
if match:
    manifest = json.loads(match.group(1))
    print("manifest keys:", sorted(manifest), "path:", manifest.get("path"))
    ctx_start = max(0, match.start() - 300)
    print("manifest context (keys only):", re.sub(r'"[0-9a-f]{32,}"', '"<hex>"', flight[ctx_start:match.end() + 100]))
    (out / "manifest.json").write_text(json.dumps(manifest))
    with urlopen(Request("https://artificialanalysis.ai" + manifest["path"], headers=UA), timeout=60) as response:
        feed = response.read()
        print("feed bytes", len(feed), "content-type", response.headers.get("content-type"))
    (out / "feed.bin").write_bytes(feed)
for needle in ('"releaseSlug":', '"omniscienceBreakdown"', '"chartDefaultSelected"', '"effort":'):
    i = flight.find(needle)
    print(needle, "count", flight.count(needle), "first context:", flight[max(0, i - 200): i + 200] if i >= 0 else None)
