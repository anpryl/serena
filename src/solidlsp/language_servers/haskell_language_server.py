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
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from overrides import override

from solidlsp import ls_types
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.lsp_protocol_handler import lsp_types
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)

# A cabal component stanza, e.g. "library", "executable foo", "test-suite foo-test".
_CABAL_STANZA_RE = re.compile(r"^(library|executable|test-suite|benchmark|foreign-library)\b", re.IGNORECASE)
_CABAL_FIELD_RE = re.compile(r"^(\s+)([A-Za-z-]+)\s*:\s*(.*)$")
# A plain (non-operator) Haskell identifier; rename supports only these.
_HS_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_HS_RESERVED_WORDS = frozenset(
    {
        "case",
        "class",
        "data",
        "default",
        "deriving",
        "do",
        "else",
        "foreign",
        "if",
        "import",
        "in",
        "infix",
        "infixl",
        "infixr",
        "instance",
        "let",
        "module",
        "newtype",
        "of",
        "then",
        "type",
        "where",
        "_",
    }
)


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
        # HLS's default "singleComponent" loads each cabal component (library, executable, test
        # suite) into its own session, so references and renames never cross a component boundary.
        self._hls_session_loading: str = self._custom_settings.get("session_loading", "multipleComponents")
        self._hls_progress_lock = threading.Lock()
        self._hls_active_progress: set[str] = set()
        self._hls_last_progress = time.monotonic()
        self._hls_progress_begins = 0
        self._hls_components_lock = threading.Lock()
        self._hls_components_loaded = False
        # True only when every component's file opened and HLS then went idle; renames depend on it.
        self._hls_components_ready = False

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in ["dist", "dist-newstyle", ".stack-work", ".cabal-sandbox"]

    def _on_hls_progress(self, params: dict) -> None:
        token = str(params.get("token"))
        kind = (params.get("value") or {}).get("kind")
        with self._hls_progress_lock:
            if kind == "begin":
                self._hls_active_progress.add(token)
                self._hls_progress_begins += 1
            elif kind == "end":
                self._hls_active_progress.discard(token)
            self._hls_last_progress = time.monotonic()

    def _hls_wait_idle(self, quiet_seconds: float = 2.0, timeout_seconds: float = 300.0) -> bool:
        """Waits until HLS reports no work in progress and has been silent for `quiet_seconds`."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._hls_progress_lock:
                busy = bool(self._hls_active_progress)
                silent_for = time.monotonic() - self._hls_last_progress
            if not busy and silent_for >= quiet_seconds:
                return True
            time.sleep(0.2)
        log.warning("HLS still busy after %.0fs: %s", timeout_seconds, sorted(self._hls_active_progress))
        return False

    def _hls_is_ignored_path(self, path: Path, root: Path) -> bool:
        return any(part.startswith(".") or self.is_ignored_dirname(part) for part in path.relative_to(root).parts[:-1])

    def _hls_first_source_file(self, base: Path, root: Path) -> Path | None:
        if not base.is_dir():
            return None
        files = sorted(p for p in base.rglob("*.hs") if not self._hls_is_ignored_path(p, root))
        return files[0] if files else None

    def _hls_component_files(self) -> list[str]:
        """
        One source file per cabal component (library, executable, test suite, benchmark), relative to the
        repository root. Opening one is what makes HLS load that component into its session.
        Without any parsable .cabal file (e.g. an hpack-only project), one file per top-level source directory.
        """
        root = Path(self.repository_root_path)
        chosen: list[Path] = []
        for cabal_file in sorted(root.rglob("*.cabal")):
            if self._hls_is_ignored_path(cabal_file, root):
                continue
            package_dir = cabal_file.parent
            stanzas: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            lines = cabal_file.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if _CABAL_STANZA_RE.match(line):
                    current = {"hs-source-dirs": [], "main-is": None}
                    stanzas.append(current)
                    continue
                if line and not line[0].isspace():
                    current = None  # another top-level stanza (common, flag, source-repository) or field
                    continue
                field = _CABAL_FIELD_RE.match(line)
                if current is None or field is None:
                    continue
                indent, key, value = len(field.group(1)), field.group(2).lower(), field.group(3)
                for cont in lines[i + 1 :]:  # continuation lines are indented deeper and are not fields
                    if not cont.strip() or len(cont) - len(cont.lstrip()) <= indent or _CABAL_FIELD_RE.match(cont):
                        break
                    value += " " + cont.strip()
                if key == "hs-source-dirs":
                    current["hs-source-dirs"] += [d for d in re.split(r"[,\s]+", value) if d]
                elif key == "main-is":
                    current["main-is"] = value.strip()
            for stanza in stanzas:
                dirs = stanza["hs-source-dirs"] or ["."]
                found: Path | None = None
                if stanza["main-is"]:
                    found = next(
                        (package_dir / d / stanza["main-is"] for d in dirs if (package_dir / d / stanza["main-is"]).is_file()), None
                    )
                for d in dirs:
                    if found is None:
                        found = self._hls_first_source_file(package_dir / d, root)
                if found is not None:
                    chosen.append(found)
        if not chosen:
            for top in sorted(p for p in root.iterdir() if p.is_dir() and not (p.name.startswith(".") or self.is_ignored_dirname(p.name))):
                if (found := self._hls_first_source_file(top, root)) is not None:
                    chosen.append(found)
        unique = dict.fromkeys(p.resolve().relative_to(root.resolve()).as_posix() for p in chosen)
        return list(unique)

    def _ensure_hls_components_loaded(self) -> bool:
        """
        Opens one file of every cabal component once, then waits for HLS to finish loading and indexing.
        In multi-component mode HLS adds a component to its session only when one of its files is opened,
        so without this, references and renames see only the components the agent happened to touch.

        :return: whether every component loaded and HLS went idle afterwards. Only then are references
            complete enough to rename from. Loading is attempted once per server.
        """
        if self._hls_session_loading != "multipleComponents":
            return False
        with self._hls_components_lock:
            if self._hls_components_loaded:
                return self._hls_components_ready
            files = self._hls_component_files()
            log.info("Loading every cabal component into HLS by opening: %s", files)
            all_opened = bool(files)
            for relative_path in files:
                try:
                    with self.open_file(relative_path):
                        self.server.send.document_symbol({"textDocument": {"uri": self._resolve_file_uri(relative_path)}})
                except Exception as e:  # one unloadable component must not break references for the others
                    log.warning("HLS could not load %s: %s", relative_path, e)
                    all_opened = False
            with self._hls_progress_lock:
                self._hls_last_progress = time.monotonic()  # wait at least the quiet period after the last open
            idle = self._hls_wait_idle()
            self._hls_components_loaded = True
            self._hls_components_ready = all_opened and idle
            log.info(
                "HLS component loading: %d files, all opened=%s, idle=%s, progress reports seen=%d",
                len(files),
                all_opened,
                idle,
                self._hls_progress_begins,
            )
            return self._hls_components_ready

    @override
    def _send_references_request(self, relative_file_path: str, line: int, column: int) -> list[lsp_types.Location] | None:
        self._ensure_hls_components_loaded()
        self._hls_wait_idle(timeout_seconds=60)  # let HLS re-index after edits made earlier in the session
        return super()._send_references_request(relative_file_path, line, column)

    def _hls_project_mentions(self, name: str) -> list[str]:
        """Project .hs files in which `name` occurs as an identifier (qualified or not)."""
        root = Path(self.repository_root_path)
        pattern = re.compile(rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])")
        hits = []
        for path in sorted(root.rglob("*.hs")):
            if not self._hls_is_ignored_path(path, root) and pattern.search(path.read_text(errors="replace")):
                hits.append(path.relative_to(root).as_posix())
        return hits

    @staticmethod
    def _hls_is_record_pun(line: str, start: int, end: int) -> bool:
        """Whether line[start:end] is a field pun (`R {field}`): renaming it would also rename the bound variable."""
        before, after = line[:start].rstrip(), line[end:].lstrip()
        opener = max(before.rfind("{"), before.rfind("("), before.rfind("["))
        return opener >= 0 and before[opener] == "{" and before[-1:] in ("{", ",") and after[:1] in ("}", ",")

    @override
    def request_rename_symbol_edit(
        self,
        relative_file_path: str,
        line: int,
        column: int,
        new_name: str,
    ) -> ls_types.WorkspaceEdit | None:
        """
        Renames from HLS's references instead of HLS's own rename. HLS refuses to rename exported names, and
        with its experimental crossModule option it renames only inside the component holding the definition
        while reporting success. Its references, once every component is loaded, include the export list, the
        type signature, every equation head and qualified uses, so each one becomes an edit of exactly the name.

        Every condition under which those references could be incomplete, or the edits could produce invalid
        code, refuses the whole rename instead of returning a partial or broken one: components not all loaded
        (or single-component loading), HLS still busy, a reserved or already-used new name, a reference that
        is not the name, lies outside the project, or is a record field pun.
        """
        with self.open_file(relative_file_path) as file_buffer:
            symbol_line = file_buffer.contents.splitlines()[line]
        old_name = next((m.group(0) for m in _HS_IDENT_RE.finditer(symbol_line) if m.start() <= column < m.end()), None)
        if old_name is None or not _HS_IDENT_RE.fullmatch(new_name) or self._hls_session_loading != "multipleComponents":
            # operators, or single-component loading: HLS's own rename, which refuses rather than stops short
            return super().request_rename_symbol_edit(relative_file_path, line, column, new_name)

        def refuse(reason: str) -> ValueError:
            return ValueError(f"Refusing to rename '{old_name}' to '{new_name}': {reason}")

        if old_name[0].isupper() != new_name[0].isupper():
            raise refuse("Haskell names keep their case class")
        if new_name in _HS_RESERVED_WORDS:
            raise refuse("the new name is a reserved word")
        if mentions := self._hls_project_mentions(new_name):
            raise refuse(f"the new name already occurs in {', '.join(mentions[:5])}, so the result could clash or be ambiguous")
        if not self._ensure_hls_components_loaded():
            raise refuse("HLS did not load every cabal component, so references may be incomplete")
        if not self._hls_wait_idle(timeout_seconds=120):
            raise refuse("HLS is still indexing, so references may be incomplete")

        with self.open_file(relative_file_path):
            locations = self.server.send.references(
                {
                    "textDocument": {"uri": self._resolve_file_uri(relative_file_path)},
                    "position": {"line": line, "character": column},
                    "context": {"includeDeclaration": True},
                }
            )
        if not locations:
            raise refuse("HLS returned no references, not even the definition")

        root = Path(self.repository_root_path).resolve()
        file_lines: dict[str, list[str]] = {}
        changes: dict[str, list[ls_types.TextEdit]] = {}
        seen: set[tuple[str, int, int]] = set()
        for location in locations:
            uri, start, end = location["uri"], location["range"]["start"], location["range"]["end"]
            path = Path(unquote(urlparse(uri).path)).resolve()
            where = f"{path}:{start['line'] + 1}"
            if not path.is_relative_to(root):
                raise refuse(f"a reference lies outside the project, at {where}")
            if start["line"] != end["line"]:
                raise refuse(f"multi-line reference at {where}")
            if uri not in file_lines:
                file_lines[uri] = path.read_text().splitlines()
            source_line = file_lines[uri][start["line"]]
            text = source_line[start["character"] : end["character"]]
            if text == old_name:
                name_start = start["character"]
            elif text.endswith("." + old_name) or text == f"`{old_name}`":
                name_start = end["character"] - len(old_name) - (1 if text.startswith("`") else 0)
            else:
                raise refuse(f"the reference text {text!r} at {where} is not the name (stale index, CPP, Template Haskell?)")
            if self._hls_is_record_pun(source_line, start["character"], end["character"]):
                raise refuse(f"the field pun at {where} would also rename the variable it binds")
            if (uri, start["line"], name_start) in seen:
                continue
            seen.add((uri, start["line"], name_start))
            changes.setdefault(uri, []).append(
                {
                    "range": {
                        "start": {"line": start["line"], "character": name_start},
                        "end": {"line": start["line"], "character": name_start + len(old_name)},
                    },
                    "newText": new_name,
                }
            )
        log.info("Haskell rename %s -> %s: %d edits in %d files", old_name, new_name, len(seen), len(changes))
        return {"changes": changes}

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
                    "sessionLoading": self._hls_session_loading,
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
                "sessionLoading": self._hls_session_loading,
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
        # HLS reports loading and indexing as work-done progress, but only after the client accepts
        # its window/workDoneProgress/create request; unanswered, that request fails with
        # MethodNotFound and the waits in _hls_wait_idle would see no work at all.
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
