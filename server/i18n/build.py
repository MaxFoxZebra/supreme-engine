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
from views import VC, VRX  # noqa: E402

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

# The views: the catalogue and the code that applies it, written into each
# view between its markers, so the view stays one file a client can show.
views = ["window.CVS_I18N = {"]
for lang, i in LANGS.items():
    views.append(f"{lang}: {{" + ",".join(f"{dump(k)}:{dump(VC[k][i])}" for k in sorted(VC)) + "},")
views.append("};")
views.append("window.CVS_I18N_RX = {")
for lang, i in LANGS.items():
    views.append(f"{lang}: [" + ",".join(f"[new RegExp({dump(r[0])}),{dump(r[i + 1])}]" for r in VRX) + "],")
views.append("};")
views.append((HERE.parent / "static" / "mcp-i18n-run.js").read_text(encoding="utf-8"))
block = "\n".join(views).replace("</", "<\\/")
import re  # noqa: E402
for name in ("mcp-page.html", "mcp-jobs.html"):
    f = HERE.parent / "static" / name
    html = f.read_text(encoding="utf-8")
    new = re.sub(r"/\*cvs-i18n:start\*/.*?/\*cvs-i18n:end\*/",
                 lambda m: "/*cvs-i18n:start*/\n" + block + "\n/*cvs-i18n:end*/", html, count=1, flags=re.S)
    if new == html and "/*cvs-i18n:start*/" not in html:
        raise SystemExit(f"{name} has no cvs-i18n markers")
    f.write_text(new, encoding="utf-8")
print(len(VC), "view strings,", len(VRX), "view patterns")
