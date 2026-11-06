"""
Provides Haskell specific instantiation of the LanguageServer class. Contains various configurations and settings specific to Haskell.
"""
# SPDX-License-Identifier: MIT

import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from overrides import override

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
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
            if len(continuation) - len(continuation.lstrip()) <= indent or _CABAL_FIELD_RE.match(continuation):
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
        except TimeoutError:  # given up on, while HLS is still setting the component up
            timed_out.set()
        except Exception as e:  # a component HLS cannot load must not stop the others
            log.warning("HLS could not load %s: %s", relative_path, e)

    def _ensure_hls_components_loaded(self) -> bool:
        """
        Once per server, opens a file of every cabal component in turn, then, with time left, all of them again
        until HLS reports no more work.

        HLS answers references from an index of the files it has typechecked, and it typechecks a component only
        once one of its files has been opened. Without this, references from a library symbol never included
        callers in an executable or test suite the agent had not touched.

        :return: whether this call already waited for work HLS reports, as long as a query would
        """
        with self._hls_components_lock:
            if self._hls_components_loaded:
                return False
            try:
                files = _haskell_component_files(Path(self.repository_root_path), self.is_ignored_dirname)
            except Exception as e:  # references still work for the components that happen to be loaded
                log.warning("Could not determine the cabal components to load into HLS: %s", e)
                return False
            # Once only, even if it does not finish: every file opened or closed restarts HLS's work, so
            # trying again would abort what is still running, such as a slow Template Haskell splice.
            self._hls_components_loaded = True
            log.info("Loading every cabal component into HLS by opening: %s", files)
            deadline = time.monotonic() + self._HLS_LOAD_TIMEOUT_SECONDS
            # HLS sets up components one after another and reports progress for only some of them, so its
            # silence does not mean they are loaded; its answer to a request on a file does. One request at
            # a time: HLS answers a references request only once every component it was asked about is set
            # up, so at the deadline at most one may still be setting up. Each file is closed once answered,
            # since every file opened restarts HLS's work on all the open ones.
            complete, loaded, timed_out = True, [], threading.Event()
            for relative_path in files:
                if time.monotonic() >= deadline or timed_out.is_set():
                    complete = False
                    break
                try:
                    with self.open_file(relative_path):
                        request = threading.Thread(target=self._hls_request_symbols, args=(relative_path, timed_out), daemon=True)
                        request.start()
                        request.join(timeout=max(0.0, deadline - time.monotonic()))  # one still running is left to finish
                except Exception as e:  # one unreadable file must not stop the other components
                    log.warning("Could not open %s for HLS: %s", relative_path, e)
                    continue
                loaded.append(relative_path)
            # With time left, the files are opened again until HLS is idle: while one of a component's files is
            # open, HLS reports typechecking the rest of that component as progress, and once it is closed that
            # work carries on unreported, so a wait would end before it does. The query wait is spent here too,
            # since closing the files restarts the work being waited for, and unreported.
            reopened = complete and not timed_out.is_set() and time.monotonic() < deadline
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
                    remaining = max(0.0, deadline - time.monotonic()) + self._HLS_QUERY_WAIT_SECONDS
                    complete = self._hls_wait_idle(timeout_seconds=remaining) and complete
            if not complete:
                log.warning("HLS was still loading after %.0fs; references may be incomplete", self._HLS_LOAD_TIMEOUT_SECONDS)
            return reopened

    @override
    def _send_references_request(self, relative_file_path: str, line: int, column: int) -> list[lsp_types.Location] | None:
        if not self._ensure_hls_components_loaded():
            self._hls_wait_idle(timeout_seconds=self._HLS_QUERY_WAIT_SECONDS)
        return super()._send_references_request(relative_file_path, line, column)

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
