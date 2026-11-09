"""
Provides Haskell specific instantiation of the LanguageServer class. Contains various configurations and settings specific to Haskell.
"""
# SPDX-License-Identifier: MIT

import bisect
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable, Collection, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NamedTuple, cast

from overrides import override

from solidlsp import ls_types
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_utils import PathUtils
from solidlsp.lsp_protocol_handler import lsp_types
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)

# A cabal component stanza ("library", "executable foo", "test-suite foo-test", ...), a `common` stanza whose
# fields components pull in with `import:`, a conditional, and an indented field.
_CABAL_COMPONENT_RE = re.compile(r"^(?:library|executable|test-suite|benchmark|foreign-library)\b", re.IGNORECASE)
_CABAL_COMMON_RE = re.compile(r"^common\s+(\S+)", re.IGNORECASE)
_CABAL_CONDITIONAL_RE = re.compile(r"^\s+(?:if|elif|else)\b", re.IGNORECASE)
_CABAL_FIELD_RE = re.compile(r"^(\s+)([A-Za-z-]+)\s*:\s*(.*)$")
_CABAL_VALUE_RE = re.compile(r'"([^"]*)"|([^,\s]+)')
_CABAL_FIELDS = ("hs-source-dirs", "main-is", "test-module", "exposed-modules", "other-modules", "import")
_HASKELL_SOURCE_SUFFIXES = (".hs", ".lhs")


def _cabal_components(cabal_text: str) -> list[tuple[list[str], list[str]]]:
    """
    The source directories of each component of a .cabal file, and the files that may hold its modules, each in
    the order to search them. The fields a component imports from `common` stanzas count.

    Which `if`/`else` branch is active is not known here. Opening files from two branches makes HLS index
    whichever it typechecks last, so what lies outside any conditional, directly or through an import, comes
    first (for directories, `.` when there are none, as when every condition is false), then what lies inside
    one, latest first, putting `else` first. The main module, the one file no other component has, comes before
    the listed modules, wherever it is declared.
    """
    commons: dict[str, dict[str, list[str]]] = {}
    components: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    conditionals: list[int] = []  # the indentation of each enclosing `if`/`else`
    lines = cabal_text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("--"):
            continue
        if not line[0].isspace():
            current = {key: [] for field in _CABAL_FIELDS for key in (field, "conditional-" + field)}
            conditionals = []
            if _CABAL_COMPONENT_RE.match(line):
                components.append(current)
            elif common := _CABAL_COMMON_RE.match(line):
                commons[common.group(1)] = current
            else:
                current = None  # a flag, source-repository or other stanza, or a top-level field
            continue
        indent = len(line) - len(line.lstrip())
        while conditionals and conditionals[-1] >= indent:
            conditionals.pop()
        if _CABAL_CONDITIONAL_RE.match(line):
            conditionals.append(indent)
            continue
        field = _CABAL_FIELD_RE.match(line)
        key = field.group(2).lower() if field else ""
        if conditionals:
            key = "conditional-" + key
        if current is None or field is None or key not in current:
            continue
        value = field.group(3)
        for continuation in lines[i + 1 :]:  # continuation lines are indented deeper and are not fields
            if not continuation.strip() or continuation.lstrip().startswith("--"):
                continue
            if len(continuation) - len(continuation.lstrip()) <= indent:  # deeper ones continue it, `C:/src` too
                break
            value += " " + continuation.strip()
        current[key] += [quoted or bare for quoted, bare in _CABAL_VALUE_RE.findall(value) if quoted or bare]

    def collect(stanza: dict[str, list[str]], field: str, seen: frozenset[str]) -> tuple[list[str], list[str]]:
        """A field's values outside any conditional, and inside one, with those of the stanzas imported."""
        plain, conditional = list(stanza[field]), list(stanza["conditional-" + field])
        for name, in_conditional in [(n, False) for n in stanza["import"]] + [(n, True) for n in stanza["conditional-import"]]:
            if name in commons and name not in seen:
                imported_plain, imported_conditional = collect(commons[name], field, seen | {name})
                (conditional if in_conditional else plain).extend(imported_plain)
                conditional += imported_conditional
        return plain, conditional

    def in_order(component: dict[str, list[str]], field: str) -> tuple[list[str], list[str]]:
        plain, conditional = collect(component, field, frozenset())
        return plain, conditional[::-1]

    def module_files(component: dict[str, list[str]]) -> list[str]:
        main_plain, main_conditional = in_order(component, "main-is")
        test_plain, test_conditional = in_order(component, "test-module")  # a detailed-0.9 test suite's main module
        exposed_plain, exposed_conditional = in_order(component, "exposed-modules")
        other_plain, other_conditional = in_order(component, "other-modules")

        def files(modules: list[str]) -> list[str]:
            return [m.replace(".", "/") + s for m in modules for s in _HASKELL_SOURCE_SUFFIXES]

        mains = main_plain + main_conditional + files(test_plain + test_conditional)
        return mains + files(exposed_plain + other_plain) + files(exposed_conditional + other_conditional)

    def source_dirs(component: dict[str, list[str]]) -> list[str]:
        plain, conditional = in_order(component, "hs-source-dirs")
        return list(dict.fromkeys((plain or ["."]) + conditional))

    return [(source_dirs(c), module_files(c)) for c in components]


def _haskell_component_files(root: Path, is_ignored_dirname: Callable[[str], bool]) -> list[str]:
    """
    Files that, once opened, make HLS load every component of the project. HLS typechecks a whole component
    once one of its files is open, so one file per component of every .cabal file is enough: its main module,
    else one of its listed modules, else the first source file in its source directories. A package with only
    a package.yaml gets one source file per directory next to it, and a project with neither, one per
    top-level directory.

    :return: paths relative to `root`, as found under it (symlinks are not resolved)
    """

    def wanted(name: str) -> bool:
        return not name.startswith(".") and not is_ignored_dirname(name)

    def walk(directory: Path) -> Iterator[tuple[Path, list[str]]]:
        for parent, dirnames, filenames in os.walk(directory):
            dirnames[:] = sorted(d for d in dirnames if wanted(d))
            yield Path(parent), sorted(filenames)

    def first_source(directory: Path, taken: Collection[Path] = ()) -> Path | None:
        sources = (parent / f for parent, files in walk(directory) for f in files if f.endswith(_HASKELL_SOURCE_SUFFIXES))
        return next((p for p in sources if p not in taken), None)

    def subdirectories(directory: Path) -> list[Path]:
        return [d for d in sorted(directory.iterdir()) if d.is_dir() and wanted(d.name)]

    cabal_files: list[Path] = []
    hpack_packages: list[Path] = []
    for parent, files in walk(root):
        cabal_files += [parent / f for f in files if f.endswith(".cabal")]
        if "package.yaml" in files and not any(f.endswith(".cabal") for f in files):
            hpack_packages.append(parent)
    chosen: list[Path] = []
    for cabal_file in cabal_files:
        package = cabal_file.parent
        try:
            components = _cabal_components(cabal_file.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:  # e.g. a dangling symlink; the other packages still load
            log.warning("Could not read %s: %s", cabal_file, e)
            continue
        for source_dirs, module_files in components:
            # normalised without resolving symlinks; one outside the project is skipped, as HLS's paths must be
            # under the root
            directories = [d for d in (Path(os.path.normpath(package / s)) for s in source_dirs) if d.is_relative_to(root)]
            # not a file another component already has: HLS would load only one of the two components for it
            listed = (d / f for f in module_files for d in directories if d / f not in chosen)
            found = next((p for p in listed if p.is_file()), None)
            found = found or next((s for d in directories if (s := first_source(d, chosen)) is not None), None)
            if found is not None:
                chosen.append(found)
    for package in hpack_packages:
        chosen += [f for d in subdirectories(package) if (f := first_source(d)) is not None]
        chosen += sorted(package.glob("*.hs"))[:1]
    if not chosen:
        chosen += [f for d in subdirectories(root) if (f := first_source(d)) is not None]
    return list(dict.fromkeys(p.relative_to(root).as_posix() for p in chosen))


_HASKELL_ASCII_SYMBOLS = "!#$%&*+./<=>?@\\^|~:-"
_HASKELL_IDENTIFIER_RE = re.compile(r"[^\W\d][\w']*")
_HASKELL_CHAR_LITERAL_RE = re.compile(r"'(?:\\.[^'\n]*|[^'\\\n])'")
_HASKELL_MODULE_NAME = r"[^\W\d_][\w']*(?:\.[^\W\d_][\w']*)*"
# a module's header or an import may follow a comment on its line, which `_haskell_lex` turns into spaces
_HASKELL_MODULE_RE = re.compile(rf"^[ \t]*module\s+({_HASKELL_MODULE_NAME})", re.MULTILINE)
# an import starts a line, or follows `{` or `;` (explicit layout); `qualified` after the module name
# (ImportQualifiedPost) may continue on an indented line
_HASKELL_IMPORT_RE = re.compile(
    rf"(?:^|[{{;])[ \t]*import\s+(?:safe\s+)?(qualified\s+)?(?:\"[^\"\n]*\"\s+)?({_HASKELL_MODULE_NAME})((?:[ \t]+|\n[ \t]+)qualified\b)?(?:\s+as\s+({_HASKELL_MODULE_NAME}))?",
    re.MULTILINE,
)
_HASKELL_WILDCARD_EXPORT_RE = re.compile(r"(?:\(([^()\s]+)\)|([^\W\d_][\w']*))\s*\(\s*\.\.")
_HASKELL_CPP_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b")


def _is_identifier_char(c: str) -> bool:
    return c.isalnum() or c in "_'"


def _is_symbol_char(c: str) -> bool:
    """A character Haskell operators are made of: an ASCII symbol, or a non-ASCII Unicode symbol or punctuation."""
    return c in _HASKELL_ASCII_SYMBOLS if c.isascii() else unicodedata.category(c)[0] in "SP"


def _utf16_to_index(line: str, column: int) -> int:
    """The string index of the UTF-16 `column` LSP gives for `line`."""
    units = 0
    for index, c in enumerate(line):
        if units >= column:
            return index
        units += 2 if ord(c) > 0xFFFF else 1
    return len(line)


def _haskell_lex(text: str) -> tuple[str, list[tuple[int, int]]]:
    """
    The text with every comment and pragma replaced by spaces, keeping line breaks, so positions are unchanged,
    and the spans of its string literals. Strings are kept: a name built from one (`mkName "f"`) is a use too.
    """
    out = list(text)
    strings = []

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    i, depth = 0, 0
    while i < len(text):
        if text.startswith("{-", i) or (depth and text.startswith("-}", i)):
            depth += 1 if text[i] == "{" else -1
            blank(i, i + 2)
            i += 2
        elif depth:
            blank(i, i + 1)
            i += 1
        elif text[i] == '"':
            start = i
            i += 1
            while i < len(text) and text[i] not in '"\n':
                if text[i] == "\\" and text[i + 1 : i + 2].isspace():
                    # a string gap: white space, line breaks included, up to the next backslash
                    i += 1
                    while i < len(text) and text[i].isspace():
                        i += 1
                    i += 1
                else:
                    i += 2 if text[i] == "\\" else 1
            i += 1
            strings.append((start, i))
        elif text[i] == "'" and not (i and _is_identifier_char(text[i - 1])) and (m := _HASKELL_CHAR_LITERAL_RE.match(text, i)):
            i = m.end()
        elif text.startswith("--", i):
            dashes_end = i
            while dashes_end < len(text) and text[dashes_end] == "-":
                dashes_end += 1
            # the dashes start a comment unless they are part of an operator such as `-->`, `|--` or `--⊕`
            after, before = text[dashes_end : dashes_end + 1], text[i - 1 : i]
            if (after and _is_symbol_char(after)) or (before and _is_symbol_char(before)):
                i = dashes_end
            else:
                line_end = text.find("\n", i)
                line_end = len(text) if line_end < 0 else line_end
                blank(i, line_end)
                i = line_end
        else:
            i += 1
    return "".join(out), strings


def _haskell_code(text: str) -> str:
    return _haskell_lex(text)[0]


def _haskell_name_spans(code: str, name: str) -> Iterator[tuple[int, int]]:
    """The spans where `name` occurs in `code` as a whole identifier or operator."""
    identifier = _HASKELL_IDENTIFIER_RE.fullmatch(name) is not None
    for m in re.finditer(re.escape(name), code):
        start, end = m.span()
        after = code[end : end + 1]
        if identifier:
            primes = start
            while primes and code[primes - 1] == "'":
                primes -= 1
            # Template Haskell quotes a name as 'name (''Type for a type), unlike a prime inside an identifier
            if (primes and _is_identifier_char(code[primes - 1])) or (after and _is_identifier_char(after)):
                continue
        else:
            before = code[start - 1 : start]
            # qualified, an operator follows its qualifier's dot (L.<+>)
            qualified = before == "." and start > 1 and _is_identifier_char(code[start - 2])
            if (before and _is_symbol_char(before) and not qualified) or (after and _is_symbol_char(after)):
                continue
        yield start, end


def _haskell_name_at(line: str, index: int) -> str | None:
    """The identifier or operator at string `index` of `line`, if there is one."""
    for m in _HASKELL_IDENTIFIER_RE.finditer(line):
        if m.start() <= index < m.end():
            return m.group()
    if not (0 <= index < len(line) and _is_symbol_char(line[index])):
        return None
    start, end = index, index + 1
    while start and _is_symbol_char(line[start - 1]):
        start -= 1
    while end < len(line) and _is_symbol_char(line[end]):
        end += 1
    if line[start] == "." and start and _is_identifier_char(line[start - 1]) and start < index:
        start += 1  # the dot of a qualified operator (L.<+>)
    return line[start:end]


def _haskell_header(code: str) -> tuple[str, str | None, tuple[int, int]] | None:
    """The module's name, the text of its export list (None without one) and the name's span; None without a header."""
    m = _HASKELL_MODULE_RE.search(code)
    if m is None:
        return None
    start = m.end()
    while start < len(code) and code[start].isspace():
        start += 1
    if not code.startswith("(", start):
        return m.group(1), None, m.span(1)
    depth = 0
    for i in range(start, len(code)):
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        if depth == 0:
            return m.group(1), code[start + 1 : i], m.span(1)
    return m.group(1), code[start + 1 :], m.span(1)


def _haskell_mentions(text: str, name: str) -> bool:
    return next(_haskell_name_spans(text, name), None) is not None


def _haskell_declaration(code: str, type_name: str) -> str:
    """The `data`, `newtype` or `class` declaration of `type_name`: its first line and the more indented ones after it."""
    lines = code.split("\n")
    for k, line in enumerate(lines):
        m = re.match(r"([ \t]*)(?:data|newtype|class)\b(.*)", line)
        if not m:
            continue
        # the declared type comes before `=` or `where`, and after a class's context (`C a => T a`)
        head = re.split(r"\bwhere\b|=(?!>)", m.group(2), maxsplit=1)[0].split("=>")[-1]
        if _haskell_mentions(head, type_name):
            indent = len(m.group(1))
            end = next(
                (e for e in range(k + 1, len(lines)) if lines[e].strip() and len(lines[e]) - len(lines[e].lstrip()) <= indent), len(lines)
            )
            return "\n".join(lines[k:end])
    return ""


def _haskell_exports(code: str, name: str, defining_code: str, through: Collection[str], declares: bool) -> bool:
    """
    Whether a module (comments removed) exports `name`, which `defining_code` declares: by naming it (`T (x, y)`
    and `pattern P` included), as a constructor, field or method of a `T (..)` it exports, or through a
    `module M` export where M is one of `through`. Without an export list a module exports what it declares,
    and nothing it imports.

    :param declares: whether `code` is the module declaring the name
    """
    header = _haskell_header(code)
    if header is None:  # implicitly `module Main (main) where`
        return declares and name == "main"
    exports = header[1]
    if exports is None:
        return declares
    if _haskell_mentions(exports, name):
        return True
    modules = "|".join(re.escape(m) for m in through)
    if modules and re.search(rf"(?<![\w'.])module\s+(?:{modules})(?![\w'.])", exports):
        return True
    parents = (operator or identifier for operator, identifier in _HASKELL_WILDCARD_EXPORT_RE.findall(exports))
    return any(_haskell_mentions(_haskell_declaration(defining_code, parent), name) for parent in parents)


class _HaskellImport(NamedTuple):
    qualified: bool
    qualifier: str  # what its uses are qualified with: the alias, else the module's name
    names: list[tuple[int, int]]  # spans of the module's name and alias, which are not uses of anything
    items: tuple[int, int] | None  # span of its import list, the parentheses included
    hiding: bool


def _haskell_imports(code: str, module: str) -> list[_HaskellImport]:
    """Every import of `module` in a file (comments removed)."""
    imports = []
    for m in _HASKELL_IMPORT_RE.finditer(code):
        if m.group(2) != module:
            continue
        i = m.end()
        while i < len(code) and code[i].isspace():
            i += 1
        hiding = code.startswith("hiding", i) and not _is_identifier_char(code[i + 6 : i + 7])
        if hiding:
            i += 6
            while i < len(code) and code[i].isspace():
                i += 1
        items = None
        if code.startswith("(", i):
            depth, j = 0, i
            while j < len(code):
                depth += {"(": 1, ")": -1}.get(code[j], 0)
                j += 1
                if depth == 0:
                    break
            items = (i, j)
        names = [m.span(2), *([m.span(4)] if m.group(4) else [])]
        imports.append(_HaskellImport(bool(m.group(1) or m.group(3)), m.group(4) or module, names, items, hiding))
    return imports


def _haskell_import_brings(code: str, imp: _HaskellImport, name: str) -> bool:
    """Whether an import can bring `name` into scope: it lists it, or a `T (..)` that may hold it, or hides nothing of it."""
    if imp.items is None:
        return True
    items = code[imp.items[0] : imp.items[1]]
    if imp.hiding:
        return not _haskell_mentions(items, name)
    return _haskell_mentions(items, name) or "(..)" in re.sub(r"\s", "", items)


# where to search one file for uses of a name, worked out on the text before the rename: whether it is the
# declaring module, whether an unqualified import of it brings the name, the qualifiers of the imports that
# bring it, and the modules whose imports bring it
class _HaskellScope(NamedTuple):
    declaring: bool
    bare: bool
    qualifiers: frozenset[str]
    modules: frozenset[str]


def _haskell_scopes(sources: dict[str, str], defining: str, name: str) -> dict[str, _HaskellScope]:
    """
    The files where a rename of `name`, declared in `sources[defining]`, must edit uses that HLS can miss:
    - the declaring module, for uses inside a CPP conditional (HLS sees only the active branch), record dot
      accesses (which HLS does not index), and Template Haskell name quotes and strings;
    - if the module exports the name, each project module importing it, directly or through modules
      re-exporting it, since HLS edits only modules it has loaded, never one in an inactive `if flag` branch
      or an unbuilt stack component. A module can import it through several of them.

    :param sources: project-relative path -> file text, for every project module
    """
    codes = {defining: _haskell_code(sources[defining])}

    def code_of(path: str) -> str:
        if path not in codes:
            codes[path] = _haskell_code(sources[path])
        return codes[path]

    scopes = {defining: _HaskellScope(True, False, frozenset(), frozenset())}
    header = _haskell_header(codes[defining])
    module = header[0] if header else "Main"
    if not _haskell_exports(codes[defining], name, codes[defining], [module], declares=True):
        return scopes
    # the modules the name is visible through: the declaring one, then every importer re-exporting it
    queue, reached, visited = [module], {module}, set()
    while queue:
        current = queue.pop()
        for path, text in sources.items():
            if path == defining or (path, current) in visited or current not in text:
                continue
            imports = _haskell_imports(code_of(path), current)
            if not imports:
                continue
            visited.add((path, current))
            bringing = [imp for imp in imports if _haskell_import_brings(code_of(path), imp, name)]
            old = scopes.get(path, _HaskellScope(False, False, frozenset(), frozenset()))
            scopes[path] = _HaskellScope(
                False,
                old.bare or any(not imp.qualified for imp in bringing),
                old.qualifiers | {imp.qualifier for imp in bringing},
                old.modules | {current},
            )
            importer = _haskell_header(code_of(path))
            through = [current, *(imp.qualifier for imp in imports)]
            if importer and importer[0] not in reached and _haskell_exports(code_of(path), name, codes[defining], through, declares=False):
                reached.add(importer[0])
                queue.append(importer[0])
    return scopes


def _haskell_uses_in(text: str, name: str, scope: _HaskellScope) -> list[tuple[int, int, int]]:
    """
    The uses of `name` in a file that a rename must edit, given where it is in scope: unqualified where an
    unqualified import brings it, `Q.name` for the qualifiers of the imports bringing it, in those imports' lists,
    and record dot accesses; in the declaring module, also those inside a CPP conditional and Template Haskell
    name quotes and strings. Module names (`import T`, `T.x`) are not uses of a type T.

    :return: (0-based line, start index, end index) of each use
    """
    code, strings = _haskell_lex(text)
    imports = [imp for module in scope.modules for imp in _haskell_imports(code, module)]
    header = _haskell_header(code)
    not_uses = [span for imp in imports for span in imp.names] + ([header[2]] if header else [])
    lists = [imp.items for imp in imports if imp.items]
    cpp = _haskell_cpp_lines(code) if scope.declaring else set()
    line_starts = [0, *(i + 1 for i, c in enumerate(code) if c == "\n")]

    def within(spans: list[tuple[int, int]], start: int) -> bool:
        return any(s <= start < e for s, e in spans)

    uses = []
    for start, end in _haskell_name_spans(code, name):
        if within(not_uses, start) or (code[end : end + 1] == "." and _is_identifier_char(code[end + 1 : end + 2])):
            continue  # a module name, or a module qualifier (T.x)
        line = bisect.bisect_right(line_starts, start) - 1
        qualifier = _haskell_qualifier(code, start)
        wanted = (
            line in cpp
            or _haskell_record_dot_use(code, start)
            or within(lists, start)
            or (qualifier in scope.qualifiers if qualifier else scope.bare)
            or (scope.declaring and (code[start - 1 : start] == "'" or within(strings, start)))
        )
        if wanted:
            uses.append((line, start - line_starts[line], end - line_starts[line]))
    return uses


def _haskell_qualifier(code: str, start: int) -> str | None:
    """The module qualifier the name at `start` is written with (`Data.Map` in `Data.Map.lookup`), if any."""
    if start < 2 or code[start - 1] != ".":
        return None
    begin = start - 1
    while begin and (
        _is_identifier_char(code[begin - 1]) or (code[begin - 1] == "." and begin > 1 and _is_identifier_char(code[begin - 2]))
    ):
        begin -= 1
    qualifier = code[begin : start - 1]
    return qualifier if qualifier[:1].isupper() and all(part[:1].isupper() for part in qualifier.split(".")) else None


def _haskell_record_dot_use(code: str, start: int) -> bool:
    """Whether the name at `start` is a record dot access (`c.field`, `(f x).field`, `(.field)`), not a qualified name."""
    if start < 2 or code[start - 1] != ".":
        return False
    if code[start - 2] == "(":  # a projection, `(.field)`
        return True
    if not (_is_identifier_char(code[start - 2]) or code[start - 2] in ")]}"):
        return False
    return _haskell_qualifier(code, start) is None


def _haskell_cpp_lines(code: str) -> set[int]:
    """The 0-based numbers of the lines inside a CPP conditional (`#if` ... `#endif`)."""
    inside, depth = set(), 0
    for number, line in enumerate(code.split("\n")):
        directive = _HASKELL_CPP_RE.match(line)
        if directive and directive.group(1).startswith("if"):
            depth += 1
        elif directive and directive.group(1) == "endif":
            depth = max(0, depth - 1)
        elif depth and not directive:
            inside.add(number)
    return inside


def _haskell_uses_to_check(sources: dict[str, str], defining: str, name: str) -> list[tuple[str, int, int, int]]:
    """(path, 0-based line, start index, end index) of each use a rename of `name` must edit; see `_haskell_scopes`."""
    return sorted(
        (path, *use)
        for path, scope in _haskell_scopes(sources, defining, name).items()
        for use in _haskell_uses_in(sources[path], name, scope)
    )


_HaskellTextEdit = tuple[int, int, int, int, str]  # start line, start column, end line, end column (UTF-16), new text


def _haskell_text_edits(edit: ls_types.WorkspaceEdit, root: Path) -> dict[str, list[_HaskellTextEdit]]:
    """The text edits of a WorkspaceEdit, by project-relative path, in their order."""
    per_uri: dict[str, list[Any]] = {uri: list(edits) for uri, edits in (edit.get("changes") or {}).items()}
    for change in edit.get("documentChanges") or []:
        document_change = cast(dict[str, Any], change)  # file creations, renames and deletions have no edits
        if "edits" in document_change:
            per_uri.setdefault(document_change["textDocument"]["uri"], []).extend(document_change["edits"])
    text_edits: dict[str, list[_HaskellTextEdit]] = {}
    for uri, edits in per_uri.items():
        path = Path(PathUtils.uri_to_path(uri)).resolve()
        if path.is_relative_to(root):
            text_edits.setdefault(path.relative_to(root).as_posix(), []).extend(
                (
                    e["range"]["start"]["line"],
                    e["range"]["start"]["character"],
                    e["range"]["end"]["line"],
                    e["range"]["end"]["character"],
                    e["newText"],
                )
                for e in edits
            )
    return text_edits


def _haskell_apply_edits(text: str, edits: list[_HaskellTextEdit]) -> str:
    """
    The text with the edits applied, as LSP defines it: positions refer to the original text, in UTF-16 columns,
    and edits starting at the same position apply in their order (an insertion before a replacement).
    """
    lines = text.split("\n")
    line_starts = [0, *(i + 1 for i, c in enumerate(text) if c == "\n")]

    def offset(line: int, column: int) -> int:
        return len(text) if line >= len(lines) else line_starts[line] + _utf16_to_index(lines[line], column)

    spans = sorted(
        ((offset(sl, sc), offset(el, ec), k, new) for k, (sl, sc, el, ec, new) in enumerate(edits)), key=lambda s: (s[0], s[1], s[2])
    )
    pieces, position = [], 0
    for start, end, _, new_text in spans:
        pieces += [text[position:start], new_text]
        position = max(position, end)
    return "".join([*pieces, text[position:]])


def _haskell_astral_edits(sources: dict[str, str], text_edits: dict[str, list[_HaskellTextEdit]]) -> list[tuple[str, int, str]]:
    """
    The edited lines where a character outside the Basic Multilingual Plane comes before an edited column. Serena
    applies an edit's UTF-16 columns as string indices, so on such a line it would write the edit in the wrong
    place.
    """
    lines_of = {path: sources[path].split("\n") for path in text_edits if path in sources}
    astral = set()
    for path, edits in text_edits.items():
        for start_line, start_column, end_line, end_column, _ in edits:
            for line, column in ((start_line, start_column), (end_line, end_column)):
                text = lines_of.get(path, [])[line] if line < len(lines_of.get(path, [])) else ""
                if any(ord(c) > 0xFFFF for c in text[: _utf16_to_index(text, column)]):
                    astral.add((path, line, text.strip()))
    return sorted(astral)


def _haskell_rename_misses(
    sources: dict[str, str], defining: str, name: str, edit: ls_types.WorkspaceEdit, root: Path
) -> list[tuple[str, int, str]]:
    """
    The uses of `name` a rename edit leaves: each file that must not use the old name any more is searched once
    the edit is applied, so a use is caught whether the edit skips its file or rewrites its line without it
    (HLS replaces whole lines).

    :return: (path, 0-based line, line text) of each, the line as it is after the edit
    """
    text_edits = _haskell_text_edits(edit, root)
    misses = []
    for path, scope in _haskell_scopes(sources, defining, name).items():
        after = _haskell_apply_edits(sources[path], text_edits.get(path, []))
        lines = after.split("\n")
        misses += [(path, line, lines[line].strip()) for line in sorted({line for line, _, _ in _haskell_uses_in(after, name, scope)})]
    return misses


class HaskellLanguageServer(SolidLanguageServer):
    """
    Provides Haskell specific instantiation of the LanguageServer class. Contains various configurations and settings specific to Haskell.
    Uses Haskell Language Server (HLS) for LSP functionality.
    """

    @staticmethod
    def _ensure_hls_installed() -> str:
        """Ensure haskell-language-server-wrapper is available."""
        # Try common locations
        common_paths = [
            shutil.which("haskell-language-server-wrapper"),
            "/opt/homebrew/bin/haskell-language-server-wrapper",
            "/usr/local/bin/haskell-language-server-wrapper",
            os.path.expanduser("~/.ghcup/bin/haskell-language-server-wrapper"),
            os.path.expanduser("~/.cabal/bin/haskell-language-server-wrapper"),
            os.path.expanduser("~/.local/bin/haskell-language-server-wrapper"),
        ]

        # Check Stack programs directory
        stack_programs = os.path.expanduser("~/.local/share/stack/programs")
        if os.path.exists(stack_programs):
            try:
                for arch_dir in os.listdir(stack_programs):
                    arch_path = os.path.join(stack_programs, arch_dir)
                    if os.path.isdir(arch_path):
                        try:
                            for ghc_dir in os.listdir(arch_path):
                                hls_path = os.path.join(arch_path, ghc_dir, "bin", "haskell-language-server-wrapper")
                                if os.path.isfile(hls_path) and os.access(hls_path, os.X_OK):
                                    common_paths.append(hls_path)
                        except (PermissionError, OSError):
                            # Skip directories we can't read
                            continue
            except (PermissionError, OSError):
                # Stack programs directory not accessible
                pass

        for path in common_paths:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        raise RuntimeError(
            "haskell-language-server-wrapper is not installed or not in PATH.\n"
            "Searched locations:\n" + "\n".join(f"  - {p}" for p in common_paths if p) + "\n"
            "Please install HLS via:\n"
            "  - GHCup: https://www.haskell.org/ghcup/\n"
            "  - Stack: stack install haskell-language-server\n"
            "  - Cabal: cabal install haskell-language-server\n"
            "  - Homebrew (macOS): brew install haskell-language-server"
        )

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """
        Creates a HaskellLanguageServer instance. This class is not meant to be instantiated directly. Use LanguageServer.create() instead.
        """
        hls_executable_path = self._ensure_hls_installed()
        log.info(f"Using haskell-language-server at: {hls_executable_path}")

        # Check if there's a haskell subdirectory with Stack/Cabal project
        haskell_subdir = os.path.join(repository_root_path, "haskell")
        if os.path.exists(haskell_subdir) and (
            os.path.exists(os.path.join(haskell_subdir, "stack.yaml")) or os.path.exists(os.path.join(haskell_subdir, "cabal.project"))
        ):
            working_dir = haskell_subdir
            log.info(f"Using Haskell project directory: {working_dir}")
        else:
            working_dir = repository_root_path

        # Set up environment with GHCup bin in PATH
        env = dict(os.environ)
        ghcup_bin = os.path.expanduser("~/.ghcup/bin")
        if ghcup_bin not in env.get("PATH", ""):
            env["PATH"] = f"{ghcup_bin}{os.pathsep}{env.get('PATH', '')}"

        super().__init__(
            config,
            repository_root_path,
            ProcessLaunchInfo(cmd=[hls_executable_path, "--lsp", "--cwd", working_dir], cwd=working_dir, env=env),
            "haskell",
            solidlsp_settings,
        )
        self._hls_progress_lock = threading.Lock()
        self._hls_active_progress: set[str] = set()
        self._hls_last_progress = time.monotonic()
        self._hls_progress_begins = 0
        self._hls_components_lock = threading.Lock()
        self._hls_components_loaded = False
        # components a load did not reach, and the request still running when it stopped
        self._hls_unvisited: list[str] = []
        self._hls_pending: threading.Thread | None = None
        self._hls_given_up: str | None = None  # a file whose request timed out while HLS set its component up

    # Upper bounds on blocking a references request: the first one, for loading every component; each one,
    # including the first, for work HLS still reports. HLS keeps working past either; references are best-effort.
    _HLS_LOAD_TIMEOUT_SECONDS = 120.0
    _HLS_QUERY_WAIT_SECONDS = 30.0
    # HLS reports nothing for about a second between finishing one piece of work and starting the next
    _HLS_QUIET_SECONDS = 2.0

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in ["dist", "dist-newstyle", ".stack-work", ".cabal-sandbox"]

    def _on_hls_progress(self, params: dict) -> None:
        token = str(params.get("token"))
        value = params.get("value") or {}
        kind = value.get("kind")
        with self._hls_progress_lock:
            if kind == "begin":
                self._hls_progress_begins += 1
            # HLS can leave a token open at 100% for good, which would make every later request wait its bound
            if kind == "end" or (value.get("percentage") or 0) >= 100:
                self._hls_active_progress.discard(token)
            elif kind in ("begin", "report"):
                self._hls_active_progress.add(token)
            self._hls_last_progress = time.monotonic()

    def _hls_wait_idle(self, timeout_seconds: float) -> bool:
        """Waits until HLS reports no work in progress and has been silent for `_HLS_QUIET_SECONDS`."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._hls_progress_lock:
                busy = bool(self._hls_active_progress)
                silent_for = time.monotonic() - self._hls_last_progress
            if not busy and silent_for >= self._HLS_QUIET_SECONDS:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.2, remaining))

    def _hls_request_symbols(self, relative_path: str, timed_out: threading.Event) -> None:
        try:
            self.server.send.document_symbol({"textDocument": {"uri": self._resolve_file_uri(relative_path)}})
        except TimeoutError:  # given up on, while HLS is still setting the component up: asked again later
            self._hls_given_up = relative_path
            timed_out.set()
        except Exception as e:  # a component HLS cannot load must not stop the others
            log.warning("HLS could not load %s: %s", relative_path, e)

    def _ensure_hls_components_loaded(self) -> bool:
        """
        Once per server, opens a file of every cabal component in turn, then, with time left, all of them again
        until HLS reports no more work. Components not reached in time are loaded by later calls, once the
        setup still running has finished.

        HLS answers references from an index of the files it has typechecked, and it typechecks a component only
        once one of its files has been opened. Without this, references from a library symbol never included
        callers in an executable or test suite the agent had not touched.

        :return: whether this call already waited for work HLS reports, as long as a query would
        """
        with self._hls_components_lock:
            if self._hls_components_loaded:
                if not (self._hls_unvisited or self._hls_given_up) or (self._hls_pending is not None and self._hls_pending.is_alive()):
                    return False
                deadline = time.monotonic() + self._HLS_QUERY_WAIT_SECONDS
                given_up, self._hls_given_up = self._hls_given_up, None
                if given_up is not None:
                    # asked again with the file left closed, as opening it would restart that component's setup;
                    # the answer waits for the setup
                    timed_out = threading.Event()
                    request = threading.Thread(target=self._hls_request_symbols, args=(given_up, timed_out), daemon=True)
                    request.start()
                    self._hls_pending = request
                    request.join(timeout=max(0.0, deadline - time.monotonic()))
                # Only files never opened: opening one again would restart HLS's work on its component, such as
                # a slow Template Haskell splice.
                still_pending = self._hls_pending is not None and self._hls_pending.is_alive()
                if self._hls_unvisited and not (self._hls_given_up or still_pending):
                    files, self._hls_unvisited = self._hls_unvisited, []
                    log.info("Loading the cabal components HLS has not reached yet by opening: %s", files)
                    self._hls_load(files, timeout_seconds=max(0.0, deadline - time.monotonic()), query_wait_seconds=0.0)
                self._hls_wait_idle(timeout_seconds=max(0.0, deadline - time.monotonic()))
                return True
            try:
                files = _haskell_component_files(Path(self.repository_root_path), self.is_ignored_dirname)
            except Exception as e:  # references still work for the components that happen to be loaded
                log.warning("Could not determine the cabal components to load into HLS: %s", e)
                return False
            self._hls_components_loaded = True
            log.info("Loading every cabal component into HLS by opening: %s", files)
            return self._hls_load(files, timeout_seconds=self._HLS_LOAD_TIMEOUT_SECONDS, query_wait_seconds=self._HLS_QUERY_WAIT_SECONDS)

    def _hls_load(self, files: list[str], timeout_seconds: float, query_wait_seconds: float) -> bool:
        """
        Asks HLS about each file in turn until the deadline, recording what it did not reach, then reopens them
        all until HLS is idle if time remains, for at most `query_wait_seconds` past the deadline.

        :return: whether the files were reopened, so the query wait has been spent
        """
        started = time.monotonic()
        deadline = started + timeout_seconds
        # HLS sets up components one after another and reports progress for only some of them, so its silence
        # does not mean they are loaded; its answer to a request on a file does. One request at a time: HLS
        # answers a references request only once every component it was asked about is set up, so at the
        # deadline at most one may still be setting up. Each file is closed once answered, since every file
        # opened restarts HLS's work on all the open ones.
        complete, loaded, timed_out = True, [], threading.Event()
        for index, relative_path in enumerate(files):
            if time.monotonic() >= deadline or timed_out.is_set():
                self._hls_unvisited = files[index:]
                complete = False
                break
            try:
                with self.open_file(relative_path):
                    request = threading.Thread(target=self._hls_request_symbols, args=(relative_path, timed_out), daemon=True)
                    request.start()
                    self._hls_pending = request
                    request.join(timeout=max(0.0, deadline - time.monotonic()))  # one still running is left to finish
            except Exception as e:  # one unreadable file must not stop the other components
                log.warning("Could not open %s for HLS: %s", relative_path, e)
                continue
            loaded.append(relative_path)
        if timed_out.is_set():  # the last request was given up on
            complete = False
        # With time left, the files are opened again until HLS is idle: while one of a component's files is
        # open, HLS reports typechecking the rest of that component as progress, and once it is closed that
        # work carries on unreported, so a wait would end before it does. The query wait is spent here too,
        # since closing the files restarts the work being waited for, and unreported.
        reopened = complete and time.monotonic() < deadline
        complete = reopened
        if reopened:
            with ExitStack() as opened:
                for relative_path in loaded:
                    if time.monotonic() >= deadline:
                        complete = False
                        break
                    try:
                        opened.enter_context(self.open_file(relative_path))
                    except Exception as e:
                        log.warning("Could not open %s for HLS: %s", relative_path, e)
                with self._hls_progress_lock:
                    self._hls_last_progress = time.monotonic()  # wait at least the quiet period after opening
                remaining = max(0.0, deadline - time.monotonic()) + query_wait_seconds
                complete = self._hls_wait_idle(timeout_seconds=remaining) and complete
        if not complete:
            log.warning("HLS was still loading after %.0fs; references may be incomplete", time.monotonic() - started)
        return reopened

    @override
    def _send_references_request(self, relative_file_path: str, line: int, column: int) -> list[lsp_types.Location] | None:
        if not self._ensure_hls_components_loaded():
            self._hls_wait_idle(timeout_seconds=self._HLS_QUERY_WAIT_SECONDS)
        return super()._send_references_request(relative_file_path, line, column)

    @override
    def request_rename_symbol_edit(self, relative_file_path: str, line: int, column: int, new_name: str) -> ls_types.WorkspaceEdit | None:
        """
        HLS's rename, refused when its edit leaves a use of the name that a text search finds. HLS renames from
        its index of the modules it has typechecked, so it misses uses in code it never loads (an inactive
        `if flag` or CPP branch, an unbuilt stack component), record dot accesses, and Template Haskell name
        quotes (`'f`) and names built from strings (`mkName "f"`), and Serena would apply the rest as a
        successful rename.
        """
        # as for references: HLS knows only the importers in components it has loaded
        if not self._ensure_hls_components_loaded():
            self._hls_wait_idle(timeout_seconds=self._HLS_QUERY_WAIT_SECONDS)
        edit = super().request_rename_symbol_edit(relative_file_path, line, column, new_name)
        if not edit:
            return edit
        root = Path(self.repository_root_path).resolve()
        defining = Path(relative_file_path).as_posix()
        text = (root / defining).read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        name = _haskell_name_at(lines[line], _utf16_to_index(lines[line], column)) if line < len(lines) else None
        if name is None:
            return edit
        sources = {**self._haskell_sources(root), defining: text}
        astral = _haskell_astral_edits(sources, _haskell_text_edits(edit, root))
        if astral:
            shown = "; ".join(f"{path}:{number + 1}: {line_text}" for path, number, line_text in astral[:5])
            raise SolidLSPException(
                f"Not renaming '{name}': Serena would write HLS's edit at the wrong column on {len(astral)} line(s) with a "
                f"character outside the Basic Multilingual Plane, such as an emoji, before the edit. Rename it by hand and "
                f"build. Lines: {shown}"
            )
        missed = _haskell_rename_misses(sources, defining, name, edit, root)
        if missed:
            shown = "; ".join(f"{path}:{number + 1}: {line_text}" for path, number, line_text in missed[:5])
            raise SolidLSPException(
                f"Not renaming '{name}': HLS's edit leaves it on {len(missed)} line(s) where a text search finds it, such as "
                f"code HLS has not loaded (an inactive `if flag` or CPP branch, an unbuilt stack component), a record dot "
                f"access, or a Template Haskell name quote or `mkName` string. Rename it by hand and build. Left: {shown}"
            )
        return edit

    def _haskell_sources(self, root: Path) -> dict[str, str]:
        """
        Every project .hs file outside ignored directories, by relative path; all of them, as a module re-exporting
        the name need not mention it. A symlink is left out, so a file is never counted under two paths (HLS's
        edit names the target), and so is a file that cannot be read.
        """
        sources = {}
        for parent, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not self.is_ignored_dirname(d)]
            for filename in filenames:
                path = Path(parent) / filename
                relative = path.relative_to(root).as_posix()
                if filename.endswith(".hs") and not path.is_symlink() and not self.is_ignored_path(relative):
                    try:
                        sources[relative] = path.read_text(encoding="utf-8", errors="replace")
                    except OSError as e:
                        log.warning("Not checking %s for uses of the renamed name: %s", relative, e)
        return sources

    def _create_base_initialize_params(self) -> dict:
        """
        Returns the initialize params for the Haskell Language Server.
        """
        initialize_params = {
            "locale": "en",
            "capabilities": {
                "workspace": {
                    "applyEdit": True,
                    "workspaceEdit": {
                        "documentChanges": True,
                        "resourceOperations": ["create", "rename", "delete"],
                        "failureHandling": "textOnlyTransactional",
                        "normalizesLineEndings": True,
                        "changeAnnotationSupport": {"groupsOnLabel": True},
                    },
                    "configuration": True,
                    "didChangeWatchedFiles": {"dynamicRegistration": True, "relativePatternSupport": True},
                    "symbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                        "tagSupport": {"valueSet": [1]},
                        "resolveSupport": {"properties": ["location.range"]},
                    },
                    "executeCommand": {"dynamicRegistration": True},
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "workspaceFolders": True,
                    "semanticTokens": {"refreshSupport": True},
                },
                "textDocument": {
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "versionSupport": False,
                        "tagSupport": {"valueSet": [1, 2]},
                        "codeDescriptionSupport": True,
                        "dataSupport": True,
                    },
                    "synchronization": {"dynamicRegistration": True, "willSave": True, "willSaveWaitUntil": True, "didSave": True},
                    "completion": {
                        "dynamicRegistration": True,
                        "contextSupport": True,
                        "completionItem": {
                            "snippetSupport": True,
                            "commitCharactersSupport": True,
                            "documentationFormat": ["markdown", "plaintext"],
                            "deprecatedSupport": True,
                            "preselectSupport": True,
                            "tagSupport": {"valueSet": [1]},
                            "insertReplaceSupport": True,
                            "resolveSupport": {"properties": ["documentation", "detail", "additionalTextEdits"]},
                            "insertTextModeSupport": {"valueSet": [1, 2]},
                            "labelDetailsSupport": True,
                        },
                        "insertTextMode": 2,
                        "completionItemKind": {
                            "valueSet": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
                        },
                    },
                    "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                    "signatureHelp": {
                        "dynamicRegistration": True,
                        "signatureInformation": {
                            "documentationFormat": ["markdown", "plaintext"],
                            "parameterInformation": {"labelOffsetSupport": True},
                            "activeParameterSupport": True,
                        },
                        "contextSupport": True,
                    },
                    "definition": {"dynamicRegistration": True, "linkSupport": True},
                    "references": {"dynamicRegistration": True},
                    "documentHighlight": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                        "hierarchicalDocumentSymbolSupport": True,
                        "tagSupport": {"valueSet": [1]},
                        "labelSupport": True,
                    },
                    "codeAction": {
                        "dynamicRegistration": True,
                        "isPreferredSupport": True,
                        "disabledSupport": True,
                        "dataSupport": True,
                        "resolveSupport": {"properties": ["edit"]},
                        "codeActionLiteralSupport": {
                            "codeActionKind": {
                                "valueSet": [
                                    "",
                                    "quickfix",
                                    "refactor",
                                    "refactor.extract",
                                    "refactor.inline",
                                    "refactor.rewrite",
                                    "source",
                                    "source.organizeImports",
                                ]
                            }
                        },
                        "honorsChangeAnnotations": False,
                    },
                    "formatting": {"dynamicRegistration": True},
                    "rangeFormatting": {"dynamicRegistration": True},
                    "onTypeFormatting": {"dynamicRegistration": True},
                    "rename": {
                        "dynamicRegistration": True,
                        "prepareSupport": True,
                        "prepareSupportDefaultBehavior": 1,
                        "honorsChangeAnnotations": True,
                    },
                    "documentLink": {"dynamicRegistration": True, "tooltipSupport": True},
                    "typeDefinition": {"dynamicRegistration": True, "linkSupport": True},
                    "implementation": {"dynamicRegistration": True, "linkSupport": True},
                    "colorProvider": {"dynamicRegistration": True},
                    "foldingRange": {
                        "dynamicRegistration": True,
                        "rangeLimit": 5000,
                        "lineFoldingOnly": True,
                        "foldingRangeKind": {"valueSet": ["comment", "imports", "region"]},
                    },
                    "declaration": {"dynamicRegistration": True, "linkSupport": True},
                    "selectionRange": {"dynamicRegistration": True},
                    "callHierarchy": {"dynamicRegistration": True},
                    "semanticTokens": {
                        "dynamicRegistration": True,
                        "tokenTypes": [
                            "namespace",
                            "type",
                            "class",
                            "enum",
                            "interface",
                            "struct",
                            "typeParameter",
                            "parameter",
                            "variable",
                            "property",
                            "enumMember",
                            "event",
                            "function",
                            "method",
                            "macro",
                            "keyword",
                            "modifier",
                            "comment",
                            "string",
                            "number",
                            "regexp",
                            "operator",
                        ],
                        "tokenModifiers": [
                            "declaration",
                            "definition",
                            "readonly",
                            "static",
                            "deprecated",
                            "abstract",
                            "async",
                            "modification",
                            "documentation",
                            "defaultLibrary",
                        ],
                        "formats": ["relative"],
                        "requests": {"range": True, "full": {"delta": True}},
                        "multilineTokenSupport": False,
                        "overlappingTokenSupport": False,
                    },
                    "linkedEditingRange": {"dynamicRegistration": True},
                },
                "window": {
                    "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
                    "showDocument": {"support": True},
                    "workDoneProgress": True,
                },
                "general": {
                    "staleRequestSupport": {"cancel": True, "retryOnContentModified": []},
                    "regularExpressions": {"engine": "ECMAScript", "version": "ES2020"},
                    "markdown": {
                        "parser": "marked",
                        "version": "1.1.0",
                    },
                    "positionEncodings": ["utf-16"],
                },
            },
            "initializationOptions": {
                "haskell": {
                    "formattingProvider": "ormolu",
                    "checkProject": True,
                }
            },
            "trace": "verbose",
        }
        return initialize_params

    def _start_server(self) -> None:
        """
        Starts the Haskell Language Server
        """

        def do_nothing(params: Any) -> None:
            return

        def window_log_message(msg: dict) -> None:
            log.info(f"LSP: window/logMessage: {msg}")

        def register_capability_handler(params: dict) -> None:
            """Handle dynamic capability registration from HLS"""
            if "registrations" in params:
                for registration in params.get("registrations", []):
                    method = registration.get("method", "")
                    log.info(f"HLS registered capability: {method}")
            return

        def workspace_configuration_handler(params: dict) -> Any:
            """Handle workspace/configuration requests from HLS"""
            log.info(f"HLS requesting configuration: {params}")

            # Configuration matching VS Code settings and initialization options
            haskell_config = {
                "formattingProvider": "ormolu",
                "checkProject": True,
                "plugin": {"importLens": {"codeActionsOn": False, "codeLensOn": False}, "hlint": {"codeActionsOn": False}},
            }

            # HLS expects array of config items matching requested sections
            if isinstance(params, dict) and "items" in params:
                result = []
                for item in params["items"]:
                    section = item.get("section", "")
                    if section == "haskell":
                        result.append(haskell_config)
                    else:
                        result.append({})
                log.info(f"Returning configuration: {result}")
                return result

            # Fallback: return single config
            return [haskell_config]

        self.server.on_notification("window/logMessage", window_log_message)
        # HLS reports loading and indexing as work-done progress, but only after the client accepts its
        # window/workDoneProgress/create request; unanswered, that request fails with MethodNotFound, and
        # _hls_wait_idle would see no work at all.
        self.server.on_request("window/workDoneProgress/create", lambda params: None)
        self.server.on_notification("$/progress", self._on_hls_progress)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)
        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_request("workspace/configuration", workspace_configuration_handler)

        log.info("Starting Haskell Language Server process")
        self.server.start()
        initialize_params = self._create_initialize_params()

        log.info("Sending initialize request from LSP client to LSP server and awaiting response")
        init_response = self.server.send.initialize(initialize_params)

        # Log capabilities returned by HLS
        capabilities = init_response.get("capabilities", {})
        log.info(f"HLS capabilities: {list(capabilities.keys())}")

        self.server.notify.initialized({})

        # Give HLS time to index the project
        # HLS can be slow to index, especially on first run
        log.info("Waiting for HLS to index project...")
        time.sleep(5)

        log.info("Haskell Language Server initialized successfully")
