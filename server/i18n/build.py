"""Write static/i18n.js from catalogue.py.

    python i18n/build.py

Run it after changing the catalogue, and commit both.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from catalogue import C, RX  # noqa: E402

LANGS = {"fr": 0, "es": 1, "pt": 2}
dump = lambda s: json.dumps(s, ensure_ascii=False)

out = ["/* The interface in French, Spanish and Brazilian Portuguese.\n\n"
       "   Keyed by the English text exactly as the interface shows it (whitespace\n"
       "   collapsed), so a string is translated wherever it appears. I18N_RX holds\n"
       "   the ones with a number or a name in them, as patterns, tried in order.\n"
       "   Your documents, postings and notes are never run through this: see\n"
       "   I18N_SKIP in studio.py.\n\n"
       "   Written by i18n/build.py from i18n/catalogue.py: edit that, not this. */",
       "window.I18N = {"]
for lang, i in LANGS.items():
    out.append(f"  {lang}: {{")
    for k in sorted(C):
        out.append(f"    {dump(k)}: {dump(C[k][i])},")
    out.append("  },")
out.append("};")
out.append("window.I18N_RX = {")
for lang, i in LANGS.items():
    out.append(f"  {lang}: [")
    for r in RX:
        out.append(f"    [new RegExp({dump(r[0])}), {dump(r[i + 1])}],")
    out.append("  ],")
out.append("};")
(HERE.parent / "static" / "i18n.js").write_text("\n".join(out) + "\n", encoding="utf-8")
print(len(C), "strings,", len(RX), "patterns")
