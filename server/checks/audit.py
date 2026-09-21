"""A standing audit of the app: classes of mistake that are invisible without a
browser but mechanical to find."""
import re, sys, pathlib, collections

# The server sits one level up from this folder, wherever the repo lives.
ROOT = pathlib.Path(__file__).resolve().parent.parent
src = (ROOT / "studio.py").read_text(encoding="utf-8")
html = re.search(r'INDEX_HTML = r"""(.*?)"""', src, re.S).group(1)
js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
markup = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", html, flags=re.S)
P = collections.defaultdict(list)

# --- CSS ------------------------------------------------------------------
used = set(re.findall(r"var\(\s*--([a-z0-9-]+)\s*\)", html))      # no fallback
defined = set(re.findall(r"--([a-z0-9-]+)\s*:", html))
P["undefined css variable"] += sorted(used - defined)

static_ids = re.findall(r'id="([\w-]+)"', markup)
P["duplicate id in static markup"] += sorted(
    i for i, n in collections.Counter(static_ids).items() if n > 1)

runtime_ids = set(re.findall(r'id=[\\"\']+([\w-]+)', js))
P["script addresses a missing id"] += sorted(
    set(re.findall(r'\$\("#([\w-]+)"\)', js)) - set(static_ids) - runtime_ids)

cls_css = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
cls_used = set()
for attr in re.findall(r'class="([^"{}+]*)"', markup):
    cls_used |= set(attr.split())
# The markup inside the script is written in JS string literals, so the quote
# may or may not be backslash-escaped. \? here was a literal question mark, so
# this matched nothing at all and the whole scan was dead.
for attr in re.findall(r'class=\\?["\']([a-zA-Z][\w \-]*)', js):
    cls_used |= set(attr.split())
for m in re.findall(r'classList\.(?:add|remove|toggle)\("([\w-]+)"', js):
    cls_used.add(m)
P["class used but never styled"] += sorted(cls_used - cls_css)


# --- CSS well-formedness --------------------------------------------------
# A rule inserted inside a multi-line declaration block makes the whole thing
# malformed and the browser silently drops it: the class is present in the DOM,
# the style simply never applies. Invisible by eye, trivial to test for.
# @media and @supports are the legal exception -- rules nest inside those.
SELECTOR = re.compile(r"^\s*[.#a-zA-Z\[][^{}]*\{")
stack = []                      # True for an at-rule block, False for a normal one
for line_no, raw in enumerate(css.splitlines(), 1):
    ln = re.sub(r"/\*.*?\*/", "", raw)
    inside_rule = any(kind is False for kind in stack)
    if inside_rule and SELECTOR.match(ln):
        P["css: rule opened inside an unclosed rule"].append(
            f"line {line_no}: {ln.strip()[:58]}")
    for pos, ch in enumerate(ln):
        if ch == "{":
            head = ln[:pos].strip()
            stack.append(head.startswith("@") or (not head and bool(stack) and stack[-1]))
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                P["css: stray closing brace"].append(f"line {line_no}")
if stack:
    P["css: a rule is never closed"].append(f"{len(stack)} block(s) left open")

# --- JS -------------------------------------------------------------------
for name in sorted(set(re.findall(r"function ([a-zA-Z_]\w*)\s*\(", js))):
    if len(re.findall(r"\b" + re.escape(name) + r"\b", js)) < 2:
        P["function defined but never called"].append(name)

# The whole app is one script in one scope, so a second `function foo` does not
# shadow the first -- it replaces it, everywhere, silently. Every earlier caller
# then passes the wrong arguments to a function it has never heard of.
top_level = re.findall(r"^(?:function|const|let) ([a-zA-Z_]\w*)\s*[=(]", js, re.M)
P["declared twice at the top level"] += sorted(
    n for n, count in collections.Counter(top_level).items() if count > 1)

# S.<prop> read somewhere but never written anywhere (typo detector)
state_block = re.search(r"const S=\{(.*?)\n\};", js, re.S)
declared = set(re.findall(r"^\s{2}(\w+)\s*:", state_block.group(1), re.M)) if state_block else set()
assigned = set(re.findall(r"(?<![A-Za-z0-9_])S\.(\w+)\s*=", js))
read = set(re.findall(r"(?<![A-Za-z0-9_])S\.(\w+)", js))
P["S.<prop> read but never declared or assigned"] += sorted(
    read - declared - assigned)
P["S.<prop> declared but never read"] += sorted(
    declared - {m for m in re.findall(r"(?<![A-Za-z0-9_])S\.(\w+)", js)})

# --- API surface ----------------------------------------------------------
routes = set(re.findall(r'u\.path == "(/api/[\w/]+)"', src)) | set(
    re.findall(r'u\.path\.startswith\("(/api/[\w/]+)"\)', src))
documented = set(re.findall(r'"(/api/[\w/]+)": \{', src))
P["route implemented but not in the openapi spec"] += sorted(routes - documented - {"/api/"})
P["route documented but not implemented"] += sorted(documented - routes)

# --- python ---------------------------------------------------------------
for mod in ("studio.py", "cv_map.py", "cv_render.py", "jobs.py", "mcp_server.py"):
    text = (ROOT / mod).read_text(encoding="utf-8")
    for fn in re.findall(r"^def ([a-z_]\w*)", text, re.M):
        if fn.startswith("_") or fn == "main":
            continue
        others = len(re.findall(r"\b" + fn + r"\b", text))
        cross = sum((ROOT / o).read_text(encoding="utf-8").count(fn)
                    for o in ("studio.py", "cv_map.py", "cv_render.py",
                              "jobs.py", "mcp_server.py") if o != mod)
        if others < 2 and cross == 0:
            P[f"{mod}: function never used"].append(fn)

bad = 0
for kind, items in P.items():
    if not items:
        continue
    bad += len(items)
    print(f"\n{kind} ({len(items)}):")
    for i in items:
        print("   ", i)
print("\nclean" if not bad else f"\n{bad} finding(s)")
