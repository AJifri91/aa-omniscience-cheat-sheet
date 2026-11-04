"""Read AA-Omniscience results from Artificial Analysis (AA).

Everything that depends on *how AA serves its website* lives in this module.
The rest of the updater only sees the validated records returned by
`load_records()`. When AA changes its site, this is the one file to fix.

How AA serves the data (verified November 2026):

* The evaluation page is a Next.js App Router page. Its data streams in
  `self.__next_f.push([1, "<chunk>"])` script calls. Joined together, the
  chunks form a React Server Components ("flight") payload of `id:value` rows.
* One component's props hold `manifest: {"path": "/data/<id>.txt", "key": <hex>}`.
  The file at `path` is the full model feed: AES-256-GCM encrypted with `key`
  (nonce = SHA-256(key)[:12]), then gzip-compressed JSON `{"models": [...]}`.
* The page also embeds `modelsAndReleases`, a catalogue that links model
  variants (slug -> releaseSlug) to releases (creator, release date).

Design rules:

* Find data by its *shape* (which keys an object has), never by its position
  in the text. Key order, extra fields and nesting changes then stop mattering.
* Fail closed. If anything is missing or malformed, raise `SourceError` with a
  message that names what is missing; never fall back to partial data.
* Keep the raw bytes of everything fetched, so a failed run can be saved as a
  snapshot and replayed offline (`update_data.py --from-snapshot DIR`).
"""
import base64
import binascii
import gzip
import hashlib
import http.client
import json
import re
import time
from html.parser import HTMLParser
from numbers import Real
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SOURCE_URL = "https://artificialanalysis.ai/evaluations/omniscience"
SITE = "https://artificialanalysis.ai"
USER_AGENT = "Mozilla/5.0 AA-Omniscience-Cheat-Sheet/1.0"
PAGE_TIMEOUT = 45
FEED_TIMEOUT = 60

# Reasoning-effort annotations in model names, e.g. "GPT-6 Astra (max)".
EFFORT = {"minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
EFFORT_NOTE = re.compile(r"\((minimal|low|medium|high|xhigh|max)(?:\s+with fallback)?\)", re.I)


class SourceError(RuntimeError):
    """AA's data could not be read safely. The previous published data is kept."""


# --------------------------------------------------------------------------
# Fetching (with retries), plus capture and replay of raw responses
# --------------------------------------------------------------------------

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _describe(exc):
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    reason = getattr(exc, "reason", None)
    return f"{type(exc).__name__}: {reason or exc}"


def http_get(url, *, timeout, attempts=3, delays=(5, 20), opener=urlopen, sleep=time.sleep):
    """GET `url` and return the body, retrying transient failures with backoff.

    Retries network errors, timeouts and HTTP 408/425/429/5xx. Other HTTP
    errors (such as 404) fail immediately: retrying will not fix them.
    """
    last = None
    for attempt in range(1, attempts + 1):
        wait = delays[min(attempt - 1, len(delays) - 1)]
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code not in RETRY_STATUS:
                raise SourceError(f"{url} returned HTTP {exc.code}.") from exc
            last = exc
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            if str(retry_after).strip().isdigit():
                wait = int(retry_after)
        except (URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            last = exc
        if attempt < attempts:
            wait = min(wait, 60)
            print(f"Request to {url} failed ({_describe(last)}); retrying in {wait}s "
                  f"(attempt {attempt + 1} of {attempts}).")
            sleep(wait)
    raise SourceError(f"{url} failed after {attempts} attempts ({_describe(last)}).") from last


class Fetcher:
    """Fetches URLs and remembers the raw bytes, so a failed run can be saved."""

    def __init__(self, get=http_get):
        self._get = get
        self.responses = {}

    def get(self, url, *, timeout):
        body = self._get(url, timeout=timeout)
        self.responses[url] = body
        return body


class SnapshotFetcher(Fetcher):
    """Serves responses from a saved snapshot instead of contacting AA."""

    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        index_path = self.directory / "index.json"
        try:
            self.index = json.loads(index_path.read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(f"{index_path} is not a readable snapshot index ({exc}).") from exc

    def get(self, url, *, timeout):
        if url not in self.index:
            raise SourceError(f"The snapshot has no saved copy of {url}.")
        body = (self.directory / self.index[url]).read_bytes()
        self.responses[url] = body
        return body


def save_snapshot(fetcher, directory, report):
    """Write every raw response plus `report` to `directory`.

    The feed is saved exactly as AA serves it (still encrypted), so a snapshot
    holds nothing beyond what any visitor's browser downloads.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for number, (url, body) in enumerate(fetcher.responses.items()):
        name = f"{number:02d}-" + re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1])[:80]
        (directory / name).write_bytes(body)
        files[url] = name
    index = {"files": files, **report}
    (directory / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    return directory


# --------------------------------------------------------------------------
# Page parsing: HTML -> Next.js chunks -> flight rows -> objects
# --------------------------------------------------------------------------

class _ScriptCollector(HTMLParser):
    """Collects the raw text of every <script> element.

    Script bodies are passed through untouched: HTML entities are *not*
    decoded inside scripts, exactly as in a browser.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self._attrs = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._attrs = dict(attrs)
            self._parts = []

    def handle_data(self, data):
        if self._attrs is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._parts)))
            self._attrs = None


def page_scripts(html_text):
    """Return (attributes, body) for every <script> in the page."""
    collector = _ScriptCollector()
    collector.feed(html_text)
    collector.close()
    return collector.scripts


_PUSH = re.compile(r"self\.__next_f\.push\(\s*")


def flight_stream(scripts):
    """Join the Next.js `self.__next_f.push(...)` chunks into one byte stream.

    Each call's argument is decoded with a real JSON decoder, so escaping is
    handled correctly. Chunk types follow Next.js: 1 = text, 3 = base64 binary;
    0 (bootstrap) and 2 (form state) carry no data.
    """
    decoder = json.JSONDecoder()
    parts = []
    calls = 0
    for _, body in scripts:
        for match in _PUSH.finditer(body):
            calls += 1
            try:
                value, _ = decoder.raw_decode(body, match.end())
            except ValueError as exc:
                raise SourceError(f"A Next.js data chunk could not be decoded ({exc}).") from exc
            if not isinstance(value, list) or not value:
                continue
            if value[0] == 1 and len(value) > 1 and isinstance(value[1], str):
                parts.append(value[1].encode("utf-8"))
            elif value[0] == 3 and len(value) > 1 and isinstance(value[1], str):
                try:
                    parts.append(base64.b64decode(value[1]))
                except (binascii.Error, ValueError) as exc:
                    raise SourceError(f"A binary Next.js data chunk is not valid base64 ({exc}).") from exc
    return b"".join(parts), calls


# Row ids are hex; hint rows (such as font preloads, ":HL[...]") have an empty id.
_ROW_ID = re.compile(rb"([0-9a-fA-F]*):")
_LENGTH_ROW = re.compile(rb"([A-Za-z])([0-9a-fA-F]+),")
_TAG = re.compile(rb"[A-Z]*")


def flight_rows(stream):
    """Parse a React Server Components payload into (row id, tag, value) tuples.

    JSON rows end at a newline; text ("T") and binary rows are length-prefixed
    in bytes, so they may themselves contain newlines. Rows that cannot be
    parsed are skipped and returned as short samples, never guessed at.
    """
    decoder = json.JSONDecoder()
    rows, errors = [], []
    position, size = 0, len(stream)

    def skip(reason, at):
        newline = stream.find(b"\n", at)
        end = size if newline == -1 else newline
        errors.append(f"{reason} at byte {at}: {stream[at:min(end, at + 80)].decode('utf-8', 'replace')!r}")
        return end + 1

    while position < size:
        if stream[position:position + 1] in (b"\n", b"\r"):
            position += 1
            continue
        head = _ROW_ID.match(stream, position)
        if not head:
            position = skip("no row id", position)
            continue
        row_id = head.group(1).decode()
        start = head.end()
        sized = _LENGTH_ROW.match(stream, start)
        if sized:
            tag = sized.group(1).decode()
            length = int(sized.group(2), 16)
            body = stream[sized.end():sized.end() + length]
            position = sized.end() + length
            if tag == "T":
                rows.append((row_id, "T", body.decode("utf-8", "replace")))
            continue  # Typed binary rows hold no data this updater needs.
        tag = _TAG.match(stream, start).group().decode()
        value_start = start + len(tag)
        newline = stream.find(b"\n", value_start)
        line_end = size if newline == -1 else newline
        line = stream[value_start:line_end].decode("utf-8", "replace")
        try:
            value, consumed = decoder.raw_decode(line)
        except ValueError:
            position = skip(f"row {row_id}{tag} is not JSON", position)
            continue
        rows.append((row_id, tag, value))
        if line[consumed:].strip():
            # Another row follows on the same line (no newline separator).
            position = value_start + len(line[:consumed].encode("utf-8"))
        else:
            position = line_end + 1
    return rows, errors


def iter_objects(values):
    """Yield every JSON object (dict) nested anywhere in `values`, in document order.

    Strings that hold JSON (for example a text row or a serialised prop) are
    parsed and searched too.
    """
    stack = list(reversed(values))
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            yield value
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))
        elif (isinstance(value, str) and len(value) > 20 and value[0] in "{["
              and value.rstrip()[-1:] in ("}", "]")):
            try:
                stack.append(json.loads(value))
            except ValueError:
                pass


def page_objects(html_bytes):
    """Return (all objects in the page's embedded data, parsing stats)."""
    try:
        html_text = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(f"The page is not valid UTF-8 ({exc}).") from exc
    scripts = page_scripts(html_text)
    stream, calls = flight_stream(scripts)
    rows, row_errors = flight_rows(stream)
    values = [value for _, _, value in rows]
    # Other common ways frameworks embed data, searched in case AA moves to them.
    json_scripts = 0
    for attrs, body in scripts:
        if attrs.get("type") in ("application/json", "application/ld+json") or attrs.get("id") == "__NEXT_DATA__":
            try:
                values.append(json.loads(body))
                json_scripts += 1
            except ValueError:
                pass
    stats = {"pageBytes": len(html_bytes), "scripts": len(scripts), "nextChunks": calls,
             "flightRows": len(rows), "unparsedFlightRows": len(row_errors), "jsonScripts": json_scripts,
             "unparsedSamples": row_errors[:5]}
    if not values:
        raise SourceError("The page has no embedded data: no Next.js data chunks or JSON scripts "
                          f"were found ({len(scripts)} scripts). AA has likely changed how the page is built.")
    return list(iter_objects(values)), stats


_FEED_PATH = re.compile(r"/data/[A-Za-z0-9._/-]+")
_HEX_KEY = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{48}|[0-9a-fA-F]{64})")


def _valid_feed_path(path):
    return isinstance(path, str) and _FEED_PATH.fullmatch(path) is not None and ".." not in path


def find_manifests(objects):
    """Return candidate feed manifests, most likely first.

    First choice: an object stored under a "manifest" key whose "path" is a
    /data/ file. Second choice: any object shaped like {path: /data/..., key:
    <hex AES key>}, which still finds the manifest if AA renames the prop or
    moves it to its own row.
    """
    named, shaped = [], []
    for obj in objects:
        manifest = obj.get("manifest")
        if isinstance(manifest, dict) and _valid_feed_path(manifest.get("path")):
            named.append(manifest)
        if (_valid_feed_path(obj.get("path")) and isinstance(obj.get("key"), str)
                and _HEX_KEY.fullmatch(obj["key"])):
            shaped.append(obj)
    unique, seen = [], set()
    for manifest in named + shaped:
        identity = (manifest["path"], manifest.get("key"))
        if identity not in seen:
            seen.add(identity)
            unique.append({"path": manifest["path"], "key": manifest.get("key")})
    return unique


def page_identities(objects):
    """Join the page's model variants to its release catalogue.

    Variants look like {slug, releaseSlug}; releases look like
    {slug, creator: {...}, releaseDate}. Returns {variant slug: identity}.
    """
    releases, variants = {}, {}
    for obj in objects:
        slug = obj.get("slug")
        if not isinstance(slug, str):
            continue
        if isinstance(obj.get("releaseSlug"), str):
            variants[slug] = obj
        elif isinstance(obj.get("creator"), dict) and obj.get("releaseDate"):
            releases[slug] = obj
    identities = {}
    for slug, variant in variants.items():
        release_slug = variant["releaseSlug"]
        release = releases.get(release_slug, {})
        creator = variant.get("creator") or release.get("creator")
        if not isinstance(creator, dict) or not creator.get("slug"):
            continue
        identities[slug] = {
            "release": {"slug": release_slug},
            "creator": creator,
            "releaseDate": variant.get("releaseDate") or release.get("releaseDate") or "",
            "effort": variant.get("effort"),
        }
    return identities


# --------------------------------------------------------------------------
# Feed decoding and the record contract
# --------------------------------------------------------------------------

def _key_bytes(key):
    if _HEX_KEY.fullmatch(key):
        return bytes.fromhex(key)
    try:
        decoded = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) in (16, 24, 32):
        return decoded
    raise SourceError("The feed key is neither a hex nor a base64 AES key.")


def decode_feed(raw, key=None):
    """Decrypt (when keyed), decompress (when gzipped) and parse the model feed.

    AES-GCM authenticates the data, so trying a second nonce layout can never
    produce garbage: a wrong layout simply fails to authenticate.
    """
    data = raw
    if key:
        secret = _key_bytes(key)
        cipher = AESGCM(secret)
        layouts = (
            ("nonce = SHA-256(key)[:12]", hashlib.sha256(secret).digest()[:12], raw),
            ("nonce prefixed to the data", raw[:12], raw[12:]),
        )
        for _, nonce, body in layouts:
            try:
                data = cipher.decrypt(nonce, body, None)
                break
            except (InvalidTag, ValueError):
                continue
        else:
            raise SourceError("The model feed could not be decrypted with the page's key. "
                              "AA has likely changed its encryption scheme.")
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise SourceError(f"The model feed is not valid gzip ({exc}).") from exc
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError(f"The model feed is not valid JSON ({exc}).") from exc


def _number(value):
    return isinstance(value, Real) and not isinstance(value, bool)


def scored_rows(feed):
    """Return feed rows that have AA-Omniscience scores, checking each one's shape.

    Raises SourceError listing every problem (up to 10), each naming the model
    and the field, so a format change is obvious from the error alone.
    """
    if not isinstance(feed, dict) or not isinstance(feed.get("models"), list):
        keys = sorted(feed)[:20] if isinstance(feed, dict) else type(feed).__name__
        raise SourceError(f"The model feed has no 'models' list (top level: {keys}).")
    problems, scored = [], []
    for number, row in enumerate(feed["models"]):
        if not isinstance(row, dict):
            problems.append(f"models[{number}] is not an object")
            continue
        if row.get("omniscience") is None or row.get("omniscienceBreakdown") is None:
            continue  # AA has not published this model's AA-Omniscience result yet.
        label = f"models[{number}] ({row.get('slug')!r})"
        breakdown = row["omniscienceBreakdown"]
        checks = [
            ("slug", row.get("slug"), lambda v: isinstance(v, str) and v.strip() != "", "a non-empty string"),
            ("shortName/name", row.get("shortName") or row.get("name"),
             lambda v: isinstance(v, str) and v.strip() != "", "a non-empty string"),
            ("omniscience", row.get("omniscience"), lambda v: _number(v) and -100 <= v <= 100,
             "a number from -100 to 100"),
            ("omniscienceBreakdown", breakdown, lambda v: isinstance(v, dict), "an object"),
        ]
        if isinstance(breakdown, dict):
            checks += [
                ("omniscienceBreakdown.accuracy", breakdown.get("accuracy"),
                 lambda v: _number(v) and 0 <= v <= 1, "a number from 0 to 1"),
                ("omniscienceBreakdown.hallucinationRate", breakdown.get("hallucinationRate"),
                 lambda v: _number(v) and 0 <= v <= 1, "a number from 0 to 1"),
            ]
        failed = [f"{label}: {field} must be {expected}, got {value!r}"
                  for field, value, valid, expected in checks if not valid(value)]
        problems.extend(failed)
        if not failed:
            scored.append(row)
    if problems:
        more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
        raise SourceError("The model feed does not have the expected shape: "
                          + "; ".join(problems[:10]) + more)
    return scored


# --------------------------------------------------------------------------
# Model identity
# --------------------------------------------------------------------------

def product_key(release_key):
    """Remove version-like tokens while retaining product descriptors."""
    parts = []
    for token in release_key.casefold().split("-"):
        token = re.sub(r"\d.*$", "", token)
        if token and token != "v":
            parts.append(token)
    return "-".join(parts) or release_key.casefold()


def model_metadata(row, metadata, prior=None):
    """Use page metadata, then the feed's own identity, then a previously shown exact model.

    A new release without enough identity is deferred until AA publishes its
    metadata. It is never guessed from a slug or marked as seen.
    """
    meta = metadata.get(row["slug"])
    if meta and (meta.get("release") or {}).get("slug"):
        return meta
    if isinstance(row.get("release"), dict) and row["release"].get("slug"):
        return row
    if not prior:
        return None
    release_key = prior["releaseKey"]
    creator = ((row.get("creator") or {}).get("slug") or prior.get("creatorSlug"))
    if not creator:
        # AA's page can omit one scored model while retaining metadata for
        # other versions of the same product line.
        candidates = {
            (entry.get("creator") or {}).get("slug")
            for entry in metadata.values()
            if (entry.get("release") or {}).get("slug") and
            product_key(entry["release"]["slug"]) == product_key(release_key)
        }
        candidates.discard(None)
        if len(candidates) == 1:
            creator = candidates.pop()
    if not creator:
        return None
    return {"release": {"slug": release_key}, "creator": {"slug": creator},
            "reasoningRank": prior.get("reasoningRank", 0),
            "releaseDate": prior.get("releaseDate", "")}


def reasoning_rank(row, meta):
    name = row.get("shortName") or row.get("name") or ""
    note = EFFORT_NOTE.search(name)
    return ((meta.get("effort") or {}).get("level") or
            (EFFORT[note.group(1).lower()] if note else 0) or
            meta.get("reasoningRank") or 0)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def load_records(fetcher, previous_models=(), minimum_records=15):
    """Fetch AA's page and feed and return (validated model records, stats).

    Each record is a plain dict with: slug, name, releaseKey, creatorSlug,
    reasoningRank, releaseDate, chartDefaultSelected, accuracy (0-1),
    hallucinationRate (0-1) and omniscience (-100 to 100).
    """
    page = fetcher.get(SOURCE_URL, timeout=PAGE_TIMEOUT)
    objects, stats = page_objects(page)
    identities = page_identities(objects)
    manifests = find_manifests(objects)
    stats.update(pageObjects=len(objects), pageIdentities=len(identities), manifests=len(manifests))
    if not manifests:
        raise SourceError("No model-feed manifest was found in the page. AA has likely moved or "
                          "renamed it; never fall back to the page's default chart selection, "
                          "which silently omits new models.")

    problems, rows = [], None
    for manifest in manifests:
        url = SITE + manifest["path"]
        try:
            feed = decode_feed(fetcher.get(url, timeout=FEED_TIMEOUT), manifest.get("key"))
            rows = scored_rows(feed)
            stats.update(feedPath=manifest["path"], feedRows=len(feed["models"]), scoredRows=len(rows))
            break
        except SourceError as exc:
            problems.append(f"{manifest['path']}: {exc}")
    if rows is None:
        raise SourceError("No usable model feed: " + " | ".join(problems))

    previous_by_slug = {
        prior["detailsUrl"].rsplit("/", 1)[-1]: prior
        for prior in previous_models
        if prior.get("detailsUrl") and prior.get("releaseKey")
    }
    records, skipped, disagreements = [], [], 0
    for row in rows:
        meta = model_metadata(row, identities, previous_by_slug.get(row["slug"]))
        if meta is None:
            # An unidentifiable row must not enter the seen-release snapshot.
            # AA may supply its metadata in a later run.
            skipped.append(row["slug"])
            continue
        feed_release = (row.get("release") or {}).get("slug")
        if feed_release and feed_release != meta["release"]["slug"]:
            disagreements += 1
        breakdown = row["omniscienceBreakdown"]
        records.append({
            "slug": row["slug"],
            "name": row.get("shortName") or row["name"],
            "releaseKey": meta["release"]["slug"],
            "creatorSlug": (meta.get("creator") or {}).get("slug") or "unknown",
            "reasoningRank": reasoning_rank(row, meta),
            "releaseDate": meta.get("releaseDate") or "",
            "chartDefaultSelected": bool(row.get("chartDefaultSelected")),
            "accuracy": breakdown["accuracy"],
            "hallucinationRate": breakdown["hallucinationRate"],
            "omniscience": row["omniscience"],
        })
    stats.update(records=len(records), skippedWithoutIdentity=skipped,
                 pageFeedReleaseDisagreements=disagreements)
    if skipped:
        print(f"Skipped {len(skipped)} scored models lacking release identity; sample: {skipped[:10]}")
    if len(records) < minimum_records:
        raise SourceError(f"Only {len(records)} complete model records were found (minimum "
                          f"{minimum_records}); AA's format may have changed.")
    return records, stats
