#!/usr/bin/env python3
"""Temporary: run the old and new pipelines on the *same* live AA responses and compare."""
import importlib.util
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import aa_source  # noqa: E402
import update_data as new  # noqa: E402

lines = []


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line)
    lines.append(line)


spec = importlib.util.spec_from_file_location("old_update_data", HERE / "_old_update_data.py")
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

config = new.load_config()
say("config grandfathered == old EXISTING_RELEASES:",
    set(config["admission"]["grandfatheredReleases"]) == old.EXISTING_RELEASES)
previous_data, state, source = new.load_previous()
say("state source:", source)

fetcher = aa_source.Fetcher()
records, stats = aa_source.load_records(fetcher, previous_data.get("models", []))
say("new stats:", json.dumps(stats)[:1500])
payload_new, state_new = new.build(records, previous_data, state, config)


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=None):
    return Reply(fetcher.responses[request.full_url])


old.urlopen = fake_urlopen
payload_old = old.build()

old_models = payload_old["models"]
new_models = payload_new["models"]
say("model counts old/new:", len(old_models), len(new_models))
say("models identical (all fields, same order):", old_models == new_models)
if old_models != new_models:
    for a, b in zip(old_models, new_models):
        if a != b:
            say("  first difference:", json.dumps(a)[:400], "||", json.dumps(b)[:400])
            break
for key in new.STATE_KEYS:
    same = sorted(payload_old[key]) == state_new[key]
    say(f"{key} identical:", same, "| counts", len(payload_old[key]), len(state_new[key]))
    if not same:
        say("   only old:", sorted(set(payload_old[key]) - set(state_new[key]))[:20],
            "only new:", sorted(set(state_new[key]) - set(payload_old[key]))[:20])
say("admissionPolicy identical:", payload_old["admissionPolicy"] == payload_new["admissionPolicy"])

# Page identity extraction: old regex scan vs new structural walk.
page = fetcher.responses[aa_source.SOURCE_URL]
import html as _html, re as _re  # noqa: E401,E402
text = _html.unescape(page.decode("utf-8"))
chunks = _re.finditer(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', text)
flight_old = "".join(json.loads(m.group(1)) for m in chunks)
meta_old = old.page_model_metadata(flight_old)
objects, _ = aa_source.page_objects(page)
meta_new = aa_source.page_identities(objects)
say("page identities old/new:", len(meta_old), len(meta_new), "| identical:", meta_old == meta_new)
if meta_old != meta_new:
    diff = [k for k in set(meta_old) | set(meta_new) if meta_old.get(k) != meta_new.get(k)]
    say("   differing slugs:", diff[:10])
    for k in diff[:3]:
        say("   ", k, "old:", json.dumps(meta_old.get(k))[:300], "new:", json.dumps(meta_new.get(k))[:300])
stream, _ = aa_source.flight_stream(aa_source.page_scripts(page.decode("utf-8")))
say("flight payload identical to old (unescaped) text:", stream.decode("utf-8") == flight_old)
summary, problems = new.check_changes(previous_data.get("models", []), new_models, config["guards"])
say("guard summary:", summary, "| problems:", problems)

ok = old_models == new_models and all(sorted(payload_old[k]) == state_new[k] for k in new.STATE_KEYS)
say("RESULT:", "IDENTICAL" if ok else "DIFFERENT")
Path(sys.argv[1] if len(sys.argv) > 1 else "compare.md").write_text("```\n" + "\n".join(lines)[:60000] + "\n```\n")
sys.exit(0 if ok else 1)
