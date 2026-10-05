#!/usr/bin/env python3
"""Generate the web and agent translation bundles from i18n/strings.json.

Outputs:
  public/translations.js   ->  window.I18N_STRINGS = { ...all languages... }
  agent/translations.py    ->  TRANSLATIONS = { ...all languages... }
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

with open(os.path.join(HERE, "strings.json"), encoding="utf-8") as f:
    data = json.load(f)

# Stable ordering: English first, then the rest alphabetically.
langs = ["en"] + sorted(k for k in data if k != "en")
ordered = {k: data[k] for k in langs if k in data}

compact = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))

web = os.path.join(ROOT, "public", "translations.js")
with open(web, "w", encoding="utf-8", newline="\n") as f:
    f.write("// Auto-generated from i18n/strings.json. Do not edit by hand.\n")
    f.write("window.I18N_STRINGS = " + compact + ";\n")

pretty = json.dumps(ordered, ensure_ascii=False, indent=1)
agent = os.path.join(ROOT, "agent", "translations.py")
with open(agent, "w", encoding="utf-8", newline="\n") as f:
    f.write("# Auto-generated from i18n/strings.json. Do not edit by hand.\n")
    f.write("import json\n\n")
    f.write("TRANSLATIONS = json.loads(r'''" + json.dumps(ordered, ensure_ascii=False) + "''')\n")

print(f"Languages: {', '.join(langs)}")
print(f"Keys: {len(data['en'])}")
print(f"Wrote {web}")
print(f"Wrote {agent}")
