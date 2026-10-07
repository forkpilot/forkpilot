"""Static change-impact analysis of an ArduPilot diff, and what the scenarios cover of it.

  forkpilot impact --repo R --good G --bad B [--vehicle copter|plane]

Rule-based: no build, no flight, no LLM. It reads git objects only (`git diff`, `git show`,
`git grep`; never a checkout), so it is safe on a tree with unrelated work in it. Steps:

  1. changed classes   C++ classes defined in the changed files under libraries/ and the vehicle dir
  2. vehicle objects   members of the vehicle whose type is such a class (or derives from one),
                       plus AP::name() / Class::get_singleton() accessors
  3. affected modes    mode_*.cpp files that use those objects; an object that other vehicle code
                       (Attitude.cpp, motors.cpp, ...) uses too is "core": every mode is affected
  4. affected params   identifiers on changed lines that are variables of the class's var_info table,
                       with the name prefix from the vehicle's Parameters.cpp and the doc comment

The coverage part compares that with the modes and parameters the shipped scenarios use.
It narrows what to flight-test; a mode or parameter it does not list is not proven unaffected.
All git access goes through a small object (`RepoGit`), so tests feed it fixtures instead.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .build import resolve
from .i18n import t

VEHICLE_DIRS = {"copter": "ArduCopter", "plane": "ArduPlane"}
DOC_VEHICLE = {"copter": "copter", "plane": "plane"}

# Vehicle files whose use of an object does not make it core: parameter tables, telemetry, logging,
# RC aux functions, tuning knobs, and the Mode base class (reached only through the modes anyway)
NONCORE_FILES = ("Parameters.cpp", "mode.cpp", "tuning.cpp", "Log.cpp", "afs_", "version.h")
NONCORE_PREFIXES = ("GCS", "RC_Channel", "Parameters", "AP_Arming", "AP_Vehicle")
# A line that only sets the object up is not a use of it
SETUP_RE = re.compile(r"NEW_NOTHROW|\bnew\b|load_object_from_eeprom|nullptr|allocation_error|\w+\(\w+\.\w+\)\s*,?\s*$")
CODE_SUFFIX = (".cpp", ".h", ".hpp")


# --- git access ---------------------------------------------------------------------------------

class RepoGit:
    """The three read-only git operations the analysis needs, on a real repository."""

    def __init__(self, repo):
        self.repo = str(repo)

    def _run(self, *args):
        return subprocess.run(["git", "-C", self.repo, "--no-pager", *args], capture_output=True)

    def diff(self, good: str, bad: str) -> str:
        r = self._run("diff", "-U0", "--no-color", "--no-ext-diff", good, bad)
        if r.returncode:
            raise ImpactError(r.stderr.decode(errors="replace").strip() or "git diff failed")
        return r.stdout.decode(errors="replace")

    def show(self, rev: str, path: str) -> str | None:
        r = self._run("show", f"{rev}:{path}")
        return r.stdout.decode(errors="replace") if r.returncode == 0 else None

    def grep(self, pattern: str, rev: str, paths) -> list[tuple[str, int, str]]:
        """(path, line number, text) of every match of the extended regex in `rev`."""
        r = self._run("grep", "-n", "-I", "-E", "-e", pattern, rev, "--", *paths)
        out = []
        for line in r.stdout.decode(errors="replace").splitlines():
            path, _, rest = line[len(rev) + 1:].partition(":")
            no, _, text = rest.partition(":")
            if no.isdigit():
                out.append((path, int(no), text))
        return out


class ImpactError(RuntimeError):
    pass


# --- the diff -----------------------------------------------------------------------------------

def parse_diff(text: str) -> dict[str, dict]:
    """{path: {"added": [...], "removed": [...], "lines": {numbers}}} of a unified diff (any context
    size). `lines` are the changed line numbers in the new file; a deletion counts at the line
    that follows it."""
    files: dict[str, dict] = {}
    cur = None
    no = 0

    def entry(path):
        return files.setdefault(path, {"added": [], "removed": [], "lines": set()})

    for line in text.splitlines():
        if line.startswith("--- "):
            old = line[4:].strip()
            cur = None if old == "/dev/null" else old[2:]
            if cur:
                entry(cur)
        elif line.startswith("+++ "):
            new = line[4:].strip()
            if new != "/dev/null":
                cur = new[2:]
                entry(cur)
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            no = int(m.group(1)) if m else 0
        elif cur and line.startswith("+"):
            files[cur]["added"].append(line[1:])
            files[cur]["lines"].add(no)
            no += 1
        elif cur and line.startswith("-"):
            files[cur]["removed"].append(line[1:])
            files[cur]["lines"].add(no)
        elif cur and line.startswith(" "):
            no += 1
    return files


def _code(line: str) -> str:
    """A source line without its string literals and `//` comment; "" for a comment-only line."""
    s = line.strip()
    if s.startswith(("*", "/*", "//")):
        return ""
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    return s.split("//")[0]


def idents(lines) -> set[str]:
    return {w for line in lines for w in re.findall(r"[A-Za-z_]\w*", _code(line))}


def _under(path: str, *dirs) -> bool:
    return any(path.startswith(d + "/") for d in dirs)


# --- 1. classes ---------------------------------------------------------------------------------

DEF_CLASS = re.compile(r"^[A-Za-z_][^;=]*?\b(\w+)::~?\w+\s*\(|^[^;]*?\b(\w+)::var_info\[")
NOT_CLASSES = {"AP", "std", "AP_HAL", "GCS_MAVLINK", "MAVLink"}
BRACE = re.compile(r"(?<!enum )\b(?:class|struct)\s+([A-Za-z_]\w+)\b|[{};]")


def class_spans(text: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) of the class/struct bodies of a header, by brace counting."""
    spans, stack, pending = [], [], None
    for i, line in enumerate(text.splitlines(), 1):
        for m in BRACE.finditer(_code(line)):
            tok = m.group(0)
            if m.group(1):
                pending = (m.group(1), i)
            elif tok == "{":
                stack.append(pending or (None, i))
                pending = None
            elif tok == "}" and stack:
                name, start = stack.pop()
                if name:
                    spans.append((name, start, i))
            elif tok == ";":
                pending = None
    return spans


def classes_in(path: str, text: str, lines=None) -> set[str]:
    """Classes of a changed file: in a header those whose body holds a changed line (all of them
    when `lines` is None); in a .cpp those it defines members of."""
    out = set()
    if path.endswith((".h", ".hpp")):
        for name, a, b in class_spans(text):
            if lines is None or any(a <= n <= b for n in lines):
                out.add(name)
    else:
        for line in text.splitlines():
            m = DEF_CLASS.match(line)
            if m and not line.startswith(("#", "//", "/*")):
                out.add(m.group(1) or m.group(2))
    return out - NOT_CLASSES


def changed_classes(git, good, bad, diff, vdir) -> dict[str, list[str]]:
    """{class: [files]} for the code files under libraries/ and vdir/ that changed."""
    out: dict[str, list[str]] = {}
    for path in sorted(diff):
        if not (path.endswith(CODE_SUFFIX) and _under(path, "libraries", vdir)):
            continue
        text = git.show(bad, path) or git.show(good, path) or ""
        for c in classes_in(path, text, diff[path]["lines"]):
            out.setdefault(c, []).append(path)
    return out


def derived_classes(git, bad, names) -> set[str]:
    """Classes under libraries/ that name one of `names` as a base (one level)."""
    if not names:
        return set()
    pat = r"^\s*(class|struct)\s+\w+\s*:\s*(public|protected|private)\s+(" + "|".join(sorted(names)) + r")\b"
    out = set()
    for _, _, text in git.grep(pat, bad, ["libraries"]):
        m = re.match(r"^\s*(?:class|struct)\s+(\w+)", text)
        if m:
            out.add(m.group(1))
    return out - set(names)


# --- 2. vehicle objects ---------------------------------------------------------------------------

DECL = re.compile(r"^\s*(?:(?:static|const|mutable|inline)\s+)*(\w+)(?:\s*[*&]+\s*|\s+)(?:const\s+)?[*&]*\s*(\w+)\s*(?:[;{=\[]|$)")


@dataclass
class Obj:
    name: str
    type: str
    kind: str                  # member | singleton
    decl: str                  # file of the declaration
    pattern: str = ""          # regex that finds a use of it
    modes: set = None
    core_files: set = None
    other_files: set = None

    def __post_init__(self):
        self.modes, self.core_files, self.other_files = set(), set(), set()


def vehicle_objects(git, bad, vdir, classes, headers: dict[str, str]) -> list[Obj]:
    objs: dict[str, Obj] = {}
    if classes:
        for path, _, text in git.grep(r"\b(" + "|".join(sorted(classes)) + r")\b", bad, [f"{vdir}/*.h"]):
            m = DECL.match(text)
            if m and m.group(1) in classes and m.group(2) not in objs:
                objs[m.group(2)] = Obj(m.group(2), m.group(1), "member", path, rf"\b{m.group(2)}\b")
    for text in headers.values():
        if "namespace AP" not in text:
            continue
        for m in re.finditer(r"^\s*(\w+)\s*[*&]\s*(\w+)\(\)\s*;", text, re.M):
            if m.group(1) in classes:
                name = f"AP::{m.group(2)}()"
                objs[name] = Obj(name, m.group(1), "singleton", "", rf"AP::{m.group(2)}\(\)")
    for c in sorted(classes):
        name = f"{c}::get_singleton()"
        objs.setdefault(name, Obj(name, c, "singleton", "", rf"\b{c}::get_singleton\b"))
    return list(objs.values())


# --- 3. modes -------------------------------------------------------------------------------------

MODE_NAME_FIX = {"LOITER TO QLAND": "LOITER_ALT_QLAND", "SMARTRTL": "SMART_RTL"}
NOT_MODES = {"INITIALISING", "INITIALIZING"}


def _mode_name(literals: list[str]) -> str:
    lit = next((x for x in literals if " " not in x), literals[0])
    up = lit.upper()
    return MODE_NAME_FIX.get(up, up.replace(" ", "_"))


def mode_table(git, bad, vdir):
    """({class: mode name}, [(line, class)]) from the vehicle's mode.h."""
    path = f"{vdir}/mode.h"
    hits = git.grep(r"^class\s+Mode\w*|name\(\) const override", bad, [path])
    names: dict[str, str] = {}
    spans = []
    cur = None
    for _, no, text in sorted(hits, key=lambda h: h[1]):
        m = re.match(r"^class\s+(Mode\w*)", text)
        if m:
            cur = m.group(1)
            spans.append((no, cur))
        elif cur:
            lits = re.findall(r'"([^"]+)"', text)
            if lits:
                names[cur] = _mode_name(lits)
    names.pop("Mode", None)
    return {c: n for c, n in names.items() if n not in NOT_MODES}, spans


def mode_of_file(path: str, table: dict[str, str]) -> str:
    stem = Path(path).stem[len("mode_"):]
    key = "mode" + stem.replace("_", "").lower()
    for cls, name in table.items():
        if cls.replace("_", "").lower() == key:
            return name
    return stem.upper()


def _is_mode_file(path: str) -> bool:
    base = Path(path).name
    return base.startswith("mode_") and base.endswith(".cpp")


def _noncore(path: str) -> bool:
    base = Path(path).name
    return base in NONCORE_FILES or base.startswith(NONCORE_PREFIXES)


# A function that only runs at boot: an object used only there (system.cpp's init_ardupilot calls
# mode_auto.mission.init()) does not make it core
SETUP_FN = re.compile(r"^(init|setup)|_init$|^load_parameters$")
FN_DEF = re.compile(r"^(?!(?:if|for|while|switch|return|else|do)\b)[A-Za-z_][\w\s*&:<>,]*?\b(\w+)\s*\([^;]*$")


def enclosing_function(text: str, no: int) -> str | None:
    """Name of the function whose body holds line `no` (1-based), from the nearest definition
    that starts in column 0 above it; None at file scope."""
    for line in reversed(text.splitlines()[:max(no - 1, 0)]):
        if line.startswith("}"):
            return None
        m = FN_DEF.match(line)
        if m:
            return m.group(1)
    return None


def find_uses(git, bad, vdir, objs: list[Obj], table, spans) -> None:
    """Fill each object's modes / core_files / other_files from where the vehicle code uses it."""
    if not objs:
        return
    hits = git.grep("|".join(o.pattern for o in objs), bad, [vdir])
    texts: dict[str, str] = {}

    def in_setup(path, no):
        if path not in texts:
            texts[path] = git.show(bad, path) or ""
        fn = enclosing_function(texts[path], no)
        return bool(fn and SETUP_FN.search(fn))
    for path, no, text in hits:
        code = _code(text)
        if not code or not path.endswith(CODE_SUFFIX):
            continue
        for o in objs:
            if not re.search(o.pattern, code):
                continue
            d = DECL.match(code)
            if (d and d.group(2) == o.name and d.group(1) == o.type) or SETUP_RE.search(code):
                continue
            if _is_mode_file(path) and path.endswith(".cpp"):
                o.modes.add(mode_of_file(path, table))
            elif path.endswith("/mode.h"):
                cls = max((s for s in spans if s[0] <= no), default=(0, None), key=lambda s: s[0])[1]
                if cls in table:
                    o.modes.add(table[cls])
            elif path.endswith(".cpp") and not _noncore(path) and not in_setup(path, no):
                o.core_files.add(Path(path).name)
            elif path.endswith(".cpp") and not Path(path).name.startswith("Parameters"):
                o.other_files.add(Path(path).name)


# --- 4. parameters ---------------------------------------------------------------------------------

GROUPINFO = re.compile(r'AP_GROUPINFO\w*\(\s*"([^"]*)"\s*,\s*(\d+)\s*,\s*(\w+)\s*,\s*([^,]+?)\s*,(?:\s*([^,)]+))?')
SUBGROUP = re.compile(r'AP_SUBGROUP\w*\(\s*(\w+)\s*,\s*"([^"]*)"\s*,\s*\d+\s*,\s*(\w+)\s*,\s*(\w+)')
NESTED = re.compile(r"AP_NESTEDGROUPINFO\(\s*(\w+)\s*,")
GSCALAR = re.compile(r'\bGSCALAR\(\s*(\w+)\s*,\s*"([^"]+)"(?:\s*,\s*([^,)]+))?')
GOBJ = re.compile(r"\bG(?:OBJECT\w*|GROUP\w*)\(([^)]*)\)")


@dataclass
class Entry:
    name: str                   # the name inside its table
    var: str
    cls: str
    doc: dict
    path: str = ""
    default: str | None = None  # as written: a number or a macro


def parse_doc(lines: list[str], vehicle: str) -> dict:
    """The @Param block (last contiguous `//` lines) as a dict; vehicle variants like
    `@Bitmask{Copter}` win over the plain tag."""
    fields: dict[str, list] = {}
    for line in lines:
        m = re.match(r"\s*//\s*@(\w+)(?:\{([^}]*)\})?\s*:\s*(.*)$", line)
        if m:
            vs = {v.strip().lower() for v in m.group(2).split(",")} if m.group(2) else None
            fields.setdefault(m.group(1), []).append((vs, m.group(3).strip()))
    doc = {}
    for key, variants in fields.items():
        own = [v for vs, v in variants if vs and DOC_VEHICLE[vehicle] in vs]
        plain = [v for vs, v in variants if vs is None]
        pick = own or plain
        if pick:
            doc[key.lower()] = pick[0]
    out = {}
    for key in ("values", "bitmask"):
        if key in doc:
            out[key] = _pairs(doc[key])
    if "range" in doc:
        out["range"] = doc["range"].split()
    for key in ("units", "displayname"):
        if key in doc:
            out[key] = doc[key]
    return out


def _pairs(text: str) -> list[list]:
    out = []
    for part in text.split(","):
        k, sep, label = part.partition(":")
        if sep and k.strip().lstrip("-").isdigit():
            out.append([int(k), label.strip()])
    return out


def _doc_above(lines: list[str], i: int) -> list[str]:
    j = i
    while j > 0 and lines[j - 1].lstrip().startswith("//"):
        j -= 1
    return lines[j:i]


def table_entries(text: str, cls: str | None, vehicle: str) -> list[Entry]:
    """The AP_GROUPINFO rows of `cls::var_info` (cls None: every row of the text), with docs."""
    lines = text.splitlines()
    out = []
    inside = cls is None
    for i, line in enumerate(lines):
        if cls is not None:
            m = re.search(r"(\w+)::var_info\[\]", line)
            if m:
                inside = m.group(1) == cls
        if inside and line.lstrip().startswith("AP_GROUPEND") and cls is not None:
            inside = False
        m = GROUPINFO.search(line) if inside else None
        if m and not line.lstrip().startswith("//"):
            out.append(Entry(m.group(1), m.group(4), m.group(3), parse_doc(_doc_above(lines, i), vehicle),
                             default=(m.group(5) or "").strip() or None))
    return out


def vehicle_prefixes(text: str) -> dict[str, list[str]]:
    """{class: [prefix]} from the GOBJECT/GOBJECTN/GOBJECTPTR/GGROUP lines of Parameters.cpp."""
    out: dict[str, list[str]] = {}
    for m in GOBJ.finditer(text or ""):
        args = [a.strip() for a in m.group(1).split(",")]
        pfx = next((a[1:-1] for a in args if len(a) >= 2 and a[0] == a[-1] == '"'), None)
        if pfx is not None and args[-1].isidentifier():
            out.setdefault(args[-1], []).append(pfx)
    return out


def table_files(git, rev, vdir, cls) -> list[str]:
    hits = git.grep(rf"\b{cls}::var_info\[", rev, ["libraries", vdir])
    return sorted({p for p, _, _ in hits})


def parents(git, rev, vdir, cls) -> list[tuple[str, str]]:
    """[(parent class, prefix added)] of the tables that embed `cls` as a sub-group or nest it."""
    out = []
    hits = git.grep(rf"AP_SUBGROUP\w*\(.*\b{cls}\)|AP_NESTEDGROUPINFO\(\s*{cls}\s*,", rev, ["libraries", vdir])
    for path, no, text in hits:
        m = SUBGROUP.search(text)
        if m and m.group(4) == cls:
            out.append((m.group(3), m.group(2)))
        elif NESTED.search(text):
            src = git.show(rev, path) or ""
            owner = None
            for line in src.splitlines()[:no]:
                mm = re.search(r"(\w+)::var_info\[\]", line)
                owner = mm.group(1) if mm else owner
            if owner:
                out.append((owner, ""))
    return out


def prefixes_of(git, rev, vdir, cls, top: dict[str, list[str]]) -> list[str] | None:
    """Full-name prefixes of class `cls`, or None when they cannot be told (one nesting level)."""
    if cls in top:
        return top[cls]
    found = []
    for parent, add in parents(git, rev, vdir, cls):
        found += [p + add for p in top.get(parent, [])]
    return sorted(set(found)) or None


@dataclass
class Param:
    name: str | None            # full name, None when the prefix is unknown
    key: str                    # name inside its table
    var: str
    cls: str
    status: str                 # changed | new | removed
    scope: str                  # library | vehicle
    prefix_known: bool
    doc: dict
    default: int | float | None = None     # None: not a number, or a macro not found

    def as_dict(self):
        return dict(self.__dict__)


def _number(text: str):
    t = text.strip().strip("()").rstrip("fFuUlL")
    try:
        return int(t, 0)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return None


def default_value(src, rev, raw: str | None, paths):
    """The number a parameter's default (as written in its table) stands for: a literal, or a
    `#define` of that name in `paths`; None otherwise."""
    if not raw:
        return None
    n = _number(raw)
    if n is not None or not re.fullmatch(r"[A-Za-z_]\w*", raw):
        return n
    for _, _, text in src.grep(rf"^\s*#\s*define\s+{raw}\s", rev, paths):
        m = re.match(rf"\s*#\s*define\s+{raw}\s+(\S+)", text)
        if m and _number(m.group(1)) is not None:
            return _number(m.group(1))
    return None


def _hits_var(var: str, lines, words: set[str]) -> bool:
    if "." in var or "[" in var or "-" in var:
        return any(re.search(r"(?<![\w.])" + re.escape(var) + r"(?!\w)", _code(x)) for x in lines)
    return var in words


def library_params(git, good, bad, vdir, vehicle, classes, diff, top) -> list[Param]:
    out: dict[tuple, Param] = {}
    for cls in sorted(classes):
        files = table_files(git, bad, vdir, cls) or table_files(git, good, vdir, cls)
        for path in files:
            now = {e.name: e for e in table_entries(git.show(bad, path) or "", cls, vehicle)}
            before = {e.name: e for e in table_entries(git.show(good, path) or "", cls, vehicle)}
            folder = path.rsplit("/", 1)[0] + "/"
            lines = [x for p, d in diff.items() if p.startswith(folder) for x in d["added"] + d["removed"]]
            words = idents(lines)
            pfx = prefixes_of(git, bad, vdir, cls, top) or prefixes_of(git, good, vdir, cls, top)
            for name, e in {**before, **now}.items():
                if not _hits_var(e.var, lines, words):
                    continue
                status = "new" if name not in before else "removed" if name not in now else "changed"
                doc = (now.get(name) or before[name]).doc
                default = default_value(git, bad if name in now else good, (now.get(name) or before[name]).default,
                                        [folder.rstrip("/"), vdir])
                for p in pfx or [None]:
                    full = None if p is None else p + name
                    out[(cls, name, p)] = Param(full, name, e.var, cls, status, "library", p is not None, doc,
                                                default)
    return list(out.values())


def vehicle_params(git, good, bad, vdir, vehicle, diff) -> list[Param]:
    """GSCALAR parameters of Parameters.cpp (and the vehicle's g2 table) whose g.<var> changed."""
    path = f"{vdir}/Parameters.cpp"
    texts = {"now": git.show(bad, path) or "", "before": git.show(good, path) or ""}
    lines = [x for p, d in diff.items() if p.startswith(vdir + "/") for x in d["added"] + d["removed"]]
    words = idents(lines)
    used = set(re.findall(r"\bg2?\.(\w+)", "\n".join(_code(x) for x in lines)))

    def scalars(text):
        src = text.splitlines()
        out = {}
        for i, line in enumerate(src):
            m = GSCALAR.search(line)
            if m and not line.lstrip().startswith("//"):
                out[m.group(2)] = Entry(m.group(2), m.group(1), "g",
                                        parse_doc(_doc_above(src, i), vehicle),
                                        default=(m.group(3) or "").strip() or None)
        return out

    now, before = scalars(texts["now"]), scalars(texts["before"])
    g2_now = {e.name: e for e in table_entries(texts["now"], "ParametersG2", vehicle)}
    g2_before = {e.name: e for e in table_entries(texts["before"], "ParametersG2", vehicle)}
    out = []
    for scope_now, scope_before, kind in ((now, before, "g"), (g2_now, g2_before, "g2")):
        for name, e in {**scope_before, **scope_now}.items():
            if e.var not in used and not (path in diff and e.var in words and kind == "g"):
                continue
            status = "new" if name not in scope_before else "removed" if name not in scope_now else "changed"
            doc = (scope_now.get(name) or scope_before[name]).doc
            raw = (scope_now.get(name) or scope_before[name]).default
            default = default_value(git, bad if name in scope_now else good, raw, [vdir])
            out.append(Param(name, name, e.var, kind, status, "vehicle", True, doc, default))
    return out


def _files(names, keep: int = 5) -> str:
    names = sorted(names)
    return ", ".join(names[:keep]) + (f" (+{len(names) - keep} more)" if len(names) > keep else "")


# --- the whole analysis --------------------------------------------------------------------------------

def analyse(git, good: str, bad: str, vehicle: str = "copter") -> dict:
    """The impact of good..bad on `vehicle`; a plain dict (JSON-ready)."""
    if vehicle not in VEHICLE_DIRS:
        raise ImpactError(f"vehicle must be one of {', '.join(VEHICLE_DIRS)}")
    vdir = VEHICLE_DIRS[vehicle]
    diff = parse_diff(git.diff(good, bad))
    files = sorted(diff)
    classes = changed_classes(git, good, bad, diff, vdir)
    lib_classes = {c for c, fs in classes.items() if any(_under(f, "libraries") for f in fs)}
    derived = derived_classes(git, bad, lib_classes)
    names = lib_classes | derived
    headers = {f: git.show(bad, f) or "" for f in files if f.endswith((".h", ".hpp")) and _under(f, "libraries")}
    table, spans = mode_table(git, bad, vdir)
    objs = vehicle_objects(git, bad, vdir, names, headers)
    find_uses(git, bad, vdir, objs, table, spans)

    modes: dict[str, list[str]] = {}
    core: list[dict] = []
    for o in objs:
        for m in o.modes:
            modes.setdefault(m, []).append(o.name)
        if o.core_files:
            core.append({"object": o.name, "reason": f"{o.name} is used in {_files(o.core_files)}"})
    for path in files:
        if not _under(path, vdir):
            continue
        base = Path(path).name
        if _is_mode_file(path):
            modes.setdefault(mode_of_file(path, table), []).append(path)
        elif base == "mode.h":
            for cls in classes:
                if path in classes[cls] and cls in table:
                    modes.setdefault(table[cls], []).append(f"{path} ({cls})")
                elif path in classes[cls] and cls == "Mode":
                    core.append({"object": path, "reason": f"{path}: the Mode base class changed"})
        elif base == "mode.cpp":
            core.append({"object": path, "reason": f"{path} (the Mode base class) changed"})
        elif base.endswith(".cpp") and not _noncore(path):
            core.append({"object": path, "reason": f"{path} (vehicle core code) changed"})

    top = vehicle_prefixes(git.show(bad, f"{vdir}/Parameters.cpp") or git.show(good, f"{vdir}/Parameters.cpp"))
    params = library_params(git, good, bad, vdir, vehicle, names, diff, top) + \
        vehicle_params(git, good, bad, vdir, vehicle, diff)
    return {
        "vehicle": vehicle, "good": good, "bad": bad, "files": files,
        "classes": sorted(classes), "derived_classes": sorted(derived),
        "objects": [{"name": o.name, "type": o.type, "kind": o.kind, "decl": o.decl,
                     "modes": sorted(o.modes), "core_files": sorted(o.core_files),
                     "other_files": sorted(o.other_files)} for o in objs],
        "core": core,
        "modes": {m: sorted(set(v)) for m, v in sorted(modes.items())},
        "all_modes": sorted(set(table.values())),
        "params": sorted((p.as_dict() for p in params), key=lambda p: (p["name"] or "~", p["key"])),
    }


# --- coverage (what the scenarios fly) --------------------------------------------------------------------

# Failsafe scenarios do not name the mode the failsafe enters; it follows from the parameter that
# configures the reaction (set by the scenario, else its default). Small on purpose: the first
# column is the fault the scenario injects, then (parameter, default, {value: mode}).
FAILSAFE_MODES = {
    "copter": {
        "SIM_RC_FAIL": ("FS_THR_ENABLE", 1, {1: "RTL", 3: "LAND", 4: "SMART_RTL", 5: "SMART_RTL"}),
        "SIM_BATT_VOLTAGE": ("BATT_FS_LOW_ACT", 0, {1: "LAND", 2: "RTL", 3: "SMART_RTL", 4: "SMART_RTL"}),
        "SIM_GPS1_ENABLE": ("FS_EKF_ACTION", 1, {1: "LAND", 3: "LAND"}),
    },
    "plane": {
        "SIM_RC_FAIL": ("FS_LONG_ACTN", 0, {1: "RTL"}),
        "SIM_BATT_VOLTAGE": ("BATT_FS_LOW_ACT", 0, {1: "RTL", 2: "LAND"}),
    },
}


def scenario_facts(path: Path, vehicle: str) -> dict:
    """{"name", "modes": set, "params": {NAME: value}} of one scenario file."""
    from . import scenario
    data, _ = scenario.load(path)
    steps = [next(iter(s.items())) for s in data.get("steps") or [] if isinstance(s, dict) and len(s) == 1]
    params = {str(k): v for k, v in (data.get("params") or {}).items()}
    quad = data.get("frame") == "quadplane"
    modes: set[str] = set()
    for name, arg in steps:
        if name == "mode" and isinstance(arg, str):
            modes.add(arg)
        elif name == "takeoff":
            if vehicle == "copter":
                modes.add("GUIDED")
            else:
                modes.add(scenario._plane_takeoff_mode(arg, "quadplane" if quad else "plane"))
        elif name == "set_param" and isinstance(arg, dict):
            _failsafe(modes, vehicle, arg, params)
            params.update({str(k): v for k, v in arg.items()})
    if quad and params.get("Q_RTL_MODE") in (1, 2) and "RTL" in modes:
        modes.add("QRTL")
    return {"name": data.get("name") or Path(path).stem, "modes": modes, "params": params}


def _failsafe(modes: set, vehicle: str, arg: dict, params: dict) -> None:
    for fault, (param, default, table) in FAILSAFE_MODES.get(vehicle, {}).items():
        if fault in arg:
            mode = table.get(params.get(param, default))
            if mode:
                modes.add(mode)


def coverage(result: dict, facts: list[dict]) -> dict:
    """Compare an `analyse` result with the scenarios' modes and parameters."""
    core = bool(result["core"])
    affected = list(result["all_modes"]) if core else sorted(result["modes"])
    flown: dict[str, list[str]] = {}
    for f in facts:
        for m in f["modes"]:
            flown.setdefault(m, []).append(f["name"])
    set_by: dict[str, list[dict]] = {}
    for f in facts:
        for p, v in f["params"].items():
            set_by.setdefault(p, []).append({"scenario": f["name"], "value": v})
    named = [p["name"] for p in result["params"] if p["name"]]
    return {
        "scenarios": [f["name"] for f in facts],
        "core": core,
        "modes_flown": {m: sorted(flown[m]) for m in affected if m in flown},
        "modes_not_flown": [m for m in affected if m not in flown],
        "params_default": [p for p in named if p not in set_by],
        "params_set": {p: set_by[p] for p in named if p in set_by},
    }


def scenario_facts_for(vehicle: str, dirs=None) -> list[dict]:
    from . import battery
    return [scenario_facts(p, vehicle) for p in battery.scenario_paths(dirs=dirs, vehicle=vehicle)]


def impact(repo, good: str, bad: str, vehicle: str = "copter", git=None, facts=None) -> dict:
    """Analysis plus coverage: {"impact": ..., "coverage": ...}. `git` and `facts` are injectable."""
    if git is None:
        git = RepoGit(repo)
        good, bad = resolve(repo, good), resolve(repo, bad)    # "abc^" must not print as "abc"
    res = analyse(git, good, bad, vehicle)
    facts = scenario_facts_for(vehicle) if facts is None else facts
    return {"impact": res, "coverage": coverage(res, facts)}


# --- text ------------------------------------------------------------------------------------------

def _param_line(p: dict) -> str:
    name = p["name"] or t("impact.unknown_prefix", param=p["key"])
    bits = [t("impact.status." + p["status"])] if p["status"] != "changed" else []
    doc = p["doc"]
    if doc.get("bitmask"):
        bits.append(t("impact.bitmask") + " " + ", ".join(f"{k}:{v}" for k, v in doc["bitmask"]))
    if doc.get("values"):
        bits.append(t("impact.values") + " " + ", ".join(f"{k}:{v}" for k, v in doc["values"]))
    if doc.get("range"):
        bits.append(t("impact.range") + " " + " ".join(doc["range"]))
    if doc.get("units"):
        bits.append(doc["units"])
    return f"  {name}" + (f" ({'; '.join(bits)})" if bits else "")


def lines(report: dict) -> list[str]:
    """Human text of `impact()`'s result (English or the chosen language)."""
    r, c = report["impact"], report["coverage"]
    out = [t("impact.title", good=r["good"][:10], bad=r["bad"][:10], vehicle=r["vehicle"]),
           t("impact.files", n=len(r["files"])),
           t("impact.classes", names=", ".join(r["classes"] + r["derived_classes"]) or t("impact.none"))]
    objs = [o for o in r["objects"] if o["modes"] or o["core_files"]]
    out.append(t("impact.objects", names=", ".join(f"{o['name']} ({o['type']})" for o in objs) or t("impact.none")))
    if c["core"]:
        out.append(t("impact.modes_core"))
        out += [f"  {x['reason']}" for x in r["core"]]
        if r["modes"]:
            out.append(t("impact.modes_direct", names=", ".join(r["modes"])))
    else:
        out.append(t("impact.modes", names=", ".join(r["modes"]) or t("impact.none")))
    for o in objs:
        if o["other_files"]:
            out.append(t("impact.other_files", obj=o["name"], files=", ".join(o["other_files"])))
    out.append(t("impact.params"))
    out += [_param_line(p) for p in r["params"]] or ["  " + t("impact.none")]
    out += ["", t("impact.coverage", n=len(c["scenarios"]))]
    flown = ", ".join(f"{m} ({', '.join(s)})" for m, s in c["modes_flown"].items())
    out.append(t("impact.flown", names=flown or t("impact.none")))
    out.append(t("impact.not_flown", names=", ".join(c["modes_not_flown"]) or t("impact.none")))
    out.append(t("impact.param_default", names=", ".join(c["params_default"]) or t("impact.none")))
    for p, who in c["params_set"].items():
        out.append(t("impact.param_set", name=p, who=", ".join(f"{w['scenario']}={w['value']}" for w in who)))
    out += ["", t("impact.caveat")]
    return out


def markdown(report: dict) -> list[str]:
    """The same as a section for evidence.md."""
    return [t("impact.section"), "", "```", *lines(report), "```", ""]
