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
from collections.abc import Callable
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
# fields components pull in with `import:`, and an indented field.
_CABAL_COMPONENT_RE = re.compile(r"^(?:library|executable|test-suite|benchmark|foreign-library)\b", re.IGNORECASE)
_CABAL_COMMON_RE = re.compile(r"^common\s+(\S+)", re.IGNORECASE)
_CABAL_FIELD_RE = re.compile(r"^(\s+)([A-Za-z-]+)\s*:\s*(.*)$")
_CABAL_FIELDS = ("hs-source-dirs", "main-is", "import")


def _cabal_components(cabal_text: str) -> list[tuple[list[str], list[str]]]:
    """
    The source directories and main modules of each component of a .cabal file. The fields of every `if`/`else`
    branch count, since which branch is active is not known here, and so do the fields a component imports
    from `common` stanzas.
    """
    commons: dict[str, dict[str, list[str]]] = {}
    components: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    lines = cabal_text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("--"):
            continue
        if not line[0].isspace():
            current = {key: [] for key in _CABAL_FIELDS}
            if _CABAL_COMPONENT_RE.match(line):
                components.append(current)
            elif common := _CABAL_COMMON_RE.match(line):
                commons[common.group(1)] = current
            else:
                current = None  # a flag, source-repository or other stanza, or a top-level field
            continue
        field = _CABAL_FIELD_RE.match(line)
        if current is None or field is None or field.group(2).lower() not in current:
            continue
        indent, value = len(field.group(1)), field.group(3)
        for continuation in lines[i + 1 :]:  # continuation lines are indented deeper and are not fields
            if continuation.lstrip().startswith("--"):
                continue
            if not continuation.strip() or len(continuation) - len(continuation.lstrip()) <= indent:
                break
            if _CABAL_FIELD_RE.match(continuation):
                break
            value += " " + continuation.strip()
        current[field.group(2).lower()] += [v.strip('"') for v in re.split(r"[,\s]+", value) if v.strip('"')]

    def collect(stanza: dict[str, list[str]], key: str, seen: frozenset[str]) -> list[str]:
        values = list(stanza[key])
        for name in stanza["import"]:
            if name in commons and name not in seen:
                values += collect(commons[name], key, seen | {name})
        return values

    return [(collect(c, "hs-source-dirs", frozenset()) or ["."], collect(c, "main-is", frozenset())) for c in components]


def _haskell_component_files(root: Path, is_ignored_dirname: Callable[[str], bool]) -> list[str]:
    """
    Files that, once opened, make HLS load every component of the project: for each component of every
    .cabal file, its main module and one source file per source directory. A package with only a package.yaml
    gets one source file per directory next to it, and a project with neither, one per top-level directory.

    :return: paths relative to `root`, as found under it (symlinks are not resolved)
    """

    def ignored(path: Path) -> bool:
        return any(part.startswith(".") or is_ignored_dirname(part) for part in path.relative_to(root).parts[:-1])

    def first_source(directory: Path) -> Path | None:
        if not directory.is_dir():
            return None
        return next((p for p in sorted(directory.rglob("*.hs")) if not ignored(p)), None)

    def subdirectories(directory: Path) -> list[Path]:
        return [d for d in sorted(directory.iterdir()) if d.is_dir() and not d.name.startswith(".") and not is_ignored_dirname(d.name)]

    chosen: list[Path] = []
    for cabal_file in sorted(root.rglob("*.cabal")):
        if ignored(cabal_file):
            continue
        package = cabal_file.parent
        for source_dirs, main_modules in _cabal_components(cabal_file.read_text(errors="replace")):
            chosen += [package / d / m for m in main_modules for d in source_dirs if (package / d / m).is_file()]
            chosen += [f for d in source_dirs if (f := first_source(package / d)) is not None]
    for package_yaml in sorted(root.rglob("package.yaml")):
        package = package_yaml.parent
        if ignored(package_yaml) or any(package.glob("*.cabal")):
            continue
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

    # Upper bounds on blocking a references request: once for loading every component, then per request for
    # indexing in progress (e.g. after an edit). HLS keeps indexing past either; references are best-effort.
    _HLS_LOAD_TIMEOUT_SECONDS = 120.0
    _HLS_QUERY_WAIT_SECONDS = 30.0

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

    def _hls_wait_idle(self, timeout_seconds: float, quiet_seconds: float = 2.0) -> bool:
        """Waits until HLS reports no work in progress and has been silent for `quiet_seconds`."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._hls_progress_lock:
                busy = bool(self._hls_active_progress)
                silent_for = time.monotonic() - self._hls_last_progress
            if not busy and silent_for >= quiet_seconds:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def _ensure_hls_components_loaded(self) -> None:
        """
        Opens files of every cabal component once, then waits for HLS to finish loading and indexing them.

        HLS answers references from an index of the files it has typechecked, and it typechecks a component only
        once one of its files has been opened. Without this, references from a library symbol never included
        callers in an executable or test suite the agent had not touched.
        """
        with self._hls_components_lock:
            if self._hls_components_loaded:
                return
            self._hls_components_loaded = True
            deadline = time.monotonic() + self._HLS_LOAD_TIMEOUT_SECONDS
            try:
                files = _haskell_component_files(Path(self.repository_root_path), self.is_ignored_dirname)
            except Exception as e:  # references still work for the components that happen to be loaded
                log.warning("Could not determine the cabal components to load into HLS: %s", e)
                return
            log.info("Loading every cabal component into HLS by opening: %s", files)
            for index, relative_path in enumerate(files):
                if time.monotonic() >= deadline:
                    log.warning("HLS component loading exceeded %.0fs; not opening %s", self._HLS_LOAD_TIMEOUT_SECONDS, files[index:])
                    break
                try:
                    with self.open_file(relative_path):
                        self.server.send.document_symbol({"textDocument": {"uri": self._resolve_file_uri(relative_path)}})
                except Exception as e:  # one unloadable component must not stop the others
                    log.warning("HLS could not load %s: %s", relative_path, e)
            with self._hls_progress_lock:
                self._hls_last_progress = time.monotonic()  # wait at least the quiet period after the last open
            if not self._hls_wait_idle(timeout_seconds=max(0.0, deadline - time.monotonic())):
                log.warning("HLS is still indexing; references may be incomplete until it finishes")

    @override
    def _send_references_request(self, relative_file_path: str, line: int, column: int) -> list[lsp_types.Location] | None:
        self._ensure_hls_components_loaded()
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
