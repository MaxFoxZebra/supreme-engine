"""A CV out to Word and plain text, and a Word CV in.

    python checks/docxtest.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import importer  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def word(paragraphs: list[str], header: list[str] = (), table: list[list[str]] = ()) -> bytes:
    """A minimal .docx the way Word lays one out: paragraphs, a header part,
    and a table, which CV templates use for their two columns."""
    p = lambda t: f"<w:p><w:r><w:t xml:space=\"preserve\">{t}</w:t></w:r></w:p>"
    rows = "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in r) + "</w:tr>" for r in table)
    body = "".join(p(t) for t in paragraphs) + (f"<w:tbl>{rows}</w:tbl>" if rows else "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
        if header:
            z.writestr("word/header1.xml", f"<w:hdr {W}>{''.join(p(t) for t in header)}</w:hdr>")
    return buf.getvalue()


def main() -> int:
    studio.open_sample()
    cv = next(d["path"] for d in studio.list_documents() if d["path"].startswith("profile/"))
    data = studio.to_plain(studio.yaml_rt.load(studio.safe_path(cv).read_text(encoding="utf-8")))

    blob, ctype, name = studio.cv_export(studio.safe_path(cv), "docx")
    z = zipfile.ZipFile(io.BytesIO(blob))
    doc = z.read("word/document.xml").decode()
    check("a CV exports as a Word file", "wordprocessingml" in ctype and name.endswith("_CV.docx"), name)
    check("with every bullet in it", all(
        h.split("**")[0][:30] in doc for e in data["cv"]["sections"]["experience"]
        for h in e.get("highlights") or []))
    text = studio.cv_export(studio.safe_path(cv), "txt")[0].decode()
    check("and as plain text, the section titles as headings", "\nEXPERIENCE\n" in text, text[:80])
    check("with no Markdown left in it", "**" not in text)

    back = importer.import_file(name, blob)
    found = {f["what"]: f["count"] for f in back["found"]}
    check("the Word file imports back", back["cv"].get("name") == data["cv"]["name"]
          and "Experience" in found and back["source"] == "Word document", str(found))
    check("with as many roles as it had",
          found.get("Experience", "").startswith(f"{len(data['cv']['sections']['experience'])} role"),
          found.get("Experience"))

    made = word(["Experience", "Senior Engineer, Acme Corp", "Jan 2020 – present",
                 "Led the platform team of six engineers across two sites", "Education",
                 "MSc Computer Science, University of Lyon", "2014 – 2016"],
                header=["Camille Durand", "camille@example.com"],
                table=[["Skills", "Python, Go, Kubernetes"]])
    got = importer.import_file("cv.docx", made)
    check("a Word CV's header gives the name and email",
          got["cv"].get("name") == "Camille Durand" and got["cv"].get("email") == "camille@example.com",
          str({k: got["cv"].get(k) for k in ("name", "email")}))
    check("and its body the sections", "experience" in (got["cv"].get("sections") or {}),
          str(list((got["cv"].get("sections") or {}))))
    check("text in a table is read too", "Kubernetes" in str(got["cv"]))
    for bad, why in ((b"PK\x03\x04junk", "a broken zip"), (word(["Hi"]), "an empty one")):
        try:
            importer.import_file("x.docx", bad)
            check(f"{why} is refused, with a reason", False)
        except importer.ImportError_ as exc:
            check(f"{why} is refused, with a reason", "Word file" in str(exc), str(exc))
    try:
        importer.import_file("old.doc", b"\xd0\xcf\x11\xe0")
        check("an old .doc says to save it as .docx", False)
    except importer.ImportError_ as exc:
        check("an old .doc says to save it as .docx", ".docx" in str(exc))

    print(f"\n{fails} failure(s)" if fails else "\nevery Word check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
