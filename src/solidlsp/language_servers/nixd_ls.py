# type: ignore
"""
Provides Nix specific instantiation of the LanguageServer class using nixd (Nix Language Server).

Note: Windows is not supported as Nix itself doesn't support Windows natively.
"""
# SPDX-License-Identifier: MIT

import bisect
import functools
import itertools
import json
import logging
import platform
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Hashable
from copy import deepcopy
from pathlib import Path
from time import sleep
from typing import Any

from overrides import override

from solidlsp import ls_types
from solidlsp.dependency_provider import LanguageServerDependencyProvider, LanguageServerDependencyProviderSinglePath
from solidlsp.ls import DocumentSymbols, LSPFileBuffer, SolidLanguageServer, SymbolBodyFactory
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.settings import SolidLSPSettings
from solidlsp.util.subprocess_util import subprocess_run

log = logging.getLogger(__name__)

# What may come between a binding's name and its `=`: whitespace and comments. `==` is a comparison, not a
# binding. The group is atomic so a comment is consumed whole: backtracking into one could otherwise expose
# an `=` written inside it, or let `/* */` run past its own end, and made long comments take exponential time.
# nixd puts a binding's range on the last component of an attribute path, so no path follows the name.
_NIX_BINDING_TAIL = re.compile(r"(?>(?:\s|#[^\n]*|/\*.*?\*/)*)=(?!=)", re.DOTALL)
# what may follow a binding that ends its line: whitespace and comments
_NIX_LINE_END = re.compile(r"(?:\s|#.*|/\*(?:[^*]|\*(?!/))*\*/)*")
_NIX_INDENT = re.compile(r"\s*")


@functools.lru_cache(maxsize=16)
def _utf16_offsets(line: str) -> tuple[int, ...]:
    """The UTF-16 code-unit offset at which each character of `line` starts, followed by the line's length in units."""
    return tuple(itertools.accumulate((2 if ord(char) > 0xFFFF else 1 for char in line), initial=0))


def _utf16_column_to_index(line: str, column: int) -> int:
    """
    The Python string index of an LSP column, which counts UTF-16 code units, on `line`. A column inside an
    astral character rounds to after it. This runs twice per symbol, so a long line is measured once (cached),
    not walked again for each symbol on it.
    """
    if line.isascii():
        return min(column, len(line))
    return min(bisect.bisect_left(_utf16_offsets(line), column), len(line))


class NixLanguageServer(SolidLanguageServer):
    """
    Provides Nix specific instantiation of the LanguageServer class using nixd.
    """

    _HOVER_RETRY_COUNT = 5
    _HOVER_RETRY_DELAY_SECONDS = 0.1

    class DependencyProvider(LanguageServerDependencyProviderSinglePath):
        """Provides the nixd launch command and managed dependency fallback."""

        @staticmethod
        def _get_nixd_path() -> str | None:
            """Return an existing nixd executable path, if one can be found."""
            nixd_path = shutil.which("nixd")
            if nixd_path:
                return nixd_path

            home = Path.home()
            possible_paths = [
                home / ".local" / "bin" / "nixd",
                home / ".serena" / "language_servers" / "nixd" / "nixd",
                home / ".nix-profile" / "bin" / "nixd",
                Path("/usr/local/bin/nixd"),
                Path("/run/current-system/sw/bin/nixd"),
                Path("/opt/homebrew/bin/nixd"),
                Path("/usr/local/opt/nixd/bin/nixd"),
            ]

            if platform.system() == "Windows":
                possible_paths.extend(
                    [
                        home / "AppData" / "Local" / "nixd" / "nixd.exe",
                        home / ".serena" / "language_servers" / "nixd" / "nixd.exe",
                    ]
                )

            for path in possible_paths:
                if path.exists():
                    return str(path)

            return None

        @staticmethod
        def _install_nixd_with_nix() -> str | None:
            """Install nixd through Nix and return the resulting executable path."""
            if not shutil.which("nix"):
                return None

            log.info("Installing nixd using nix... This may take a few minutes.")
            try:
                result = subprocess_run(
                    ["nix", "profile", "install", "github:nix-community/nixd"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=600,
                )

                if result.returncode == 0:
                    nixd_path = shutil.which("nixd")
                    if nixd_path:
                        log.info("Successfully installed nixd at: %s", nixd_path)
                        return nixd_path
                else:
                    result = subprocess_run(
                        ["nix-env", "-iA", "nixpkgs.nixd"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=600,
                    )
                    if result.returncode == 0:
                        nixd_path = shutil.which("nixd")
                        if nixd_path:
                            log.info("Successfully installed nixd at: %s", nixd_path)
                            return nixd_path
                    log.error("Failed to install nixd: %s", result.stderr)

            except subprocess.TimeoutExpired:
                log.error("Nix install timed out after 10 minutes")
            except Exception:
                log.exception("Error installing nixd with nix")

            return None

        def _get_or_install_core_dependency(self) -> str:
            """Return a working nixd path, installing nixd when necessary."""
            if not shutil.which("nix"):
                log.error("Nix is not installed. nixd requires Nix to function properly.")
                raise RuntimeError("Nix is required for nixd. Please install Nix from https://nixos.org/download.html")

            nixd_path = self._get_nixd_path()
            if not nixd_path:
                log.info("nixd not found. Attempting to install...")
                nixd_path = self._install_nixd_with_nix()

            if not nixd_path:
                raise RuntimeError(
                    "nixd (Nix Language Server) is not installed.\n"
                    "Please install nixd using one of the following methods:\n"
                    "  - Using Nix flakes: nix profile install github:nix-community/nixd\n"
                    "  - From nixpkgs: nix-env -iA nixpkgs.nixd\n"
                    "  - On macOS with Homebrew: brew install nixd\n\n"
                    "After installation, make sure 'nixd' is in your PATH."
                )

            try:
                result = subprocess_run([nixd_path, "--version"], capture_output=True, text=True, check=False, timeout=5)
                if result.returncode != 0:
                    raise RuntimeError(f"nixd failed to run: {result.stderr}")
            except Exception as exc:
                raise RuntimeError(f"Failed to verify nixd installation: {exc}") from exc

            return nixd_path

        def _create_launch_command(self, core_path: str) -> list[str]:
            """Return the nixd stdio launch command."""
            return [core_path]

    @staticmethod
    def _is_nix_binding(symbol: ls_types.UnifiedSymbolInformation, lines: list[str]) -> bool:
        """
        Whether the symbol is a binding (`name = value`), judged by what follows its name: nixd's selectionRange
        covers the name as written (`plain`, `"quoted"`, `${dynamic}`), and in a binding only whitespace and
        comments come between it and the `=`. The displayed symbol name is no use for this, since nixd strips
        the quotes from it and labels dynamic names generically.
        """
        selection = symbol.get("selectionRange")
        if selection is None:
            return False
        name_end, symbol_end = selection["end"], symbol["range"]["end"]
        if symbol_end["line"] >= len(lines) or name_end["line"] > symbol_end["line"]:
            return False
        name_line, end_line = lines[name_end["line"]], lines[symbol_end["line"]]
        name_end_index = _utf16_column_to_index(name_line, name_end["character"])
        symbol_end_index = _utf16_column_to_index(end_line, symbol_end["character"])
        if name_end["line"] == symbol_end["line"]:
            tail = name_line[name_end_index:symbol_end_index]
        else:
            tail = "\n".join([name_line[name_end_index:], *lines[name_end["line"] + 1 : symbol_end["line"]], end_line[:symbol_end_index]])
        return _NIX_BINDING_TAIL.match(tail) is not None

    def _extend_nix_symbol_range_to_include_semicolon(
        self, symbol: ls_types.UnifiedSymbolInformation, lines: list[str]
    ) -> ls_types.UnifiedSymbolInformation:
        """
        Extend symbol range to include trailing semicolon for Nix attribute symbols.

        nixd provides ranges that exclude semicolons (expression-level), but serena needs
        statement-level ranges that include semicolons for proper replacement.

        Only bindings (`name = value;`, `"quoted" = value;`, `a.b = value;`) are extended. An inner
        expression that merely ends right before the binding's semicolon (a lambda body, the last
        identifier in `a + b;`, an `inherit` name) does not own it, and extending it would make an edit of
        that expression delete the semicolon. The extended symbol carries no precomputed body, so the
        caller must create one from the new range.
        """
        range_info = symbol["range"]
        end_line = range_info["end"]["line"]
        end_char = range_info["end"]["character"]

        if end_line >= len(lines):
            return symbol

        if not self._is_nix_binding(symbol, lines):
            return symbol

        line = lines[end_line]

        # Check if there's a semicolon immediately after the current range end. The column is used as a string
        # index, as Serena's symbol bodies and editor use it; converted from UTF-16 here alone, a write-back of a
        # body after an astral character would delete the character after the semicolon.
        if end_char < len(line) and line[end_char] == ";":
            # Extend range to include the semicolon
            new_range = {"start": range_info["start"], "end": {"line": end_line, "character": end_char + 1}}

            # Create modified symbol with extended range. The body the parent computed covers the old
            # range; left in place, find_symbol would show it without the semicolon while
            # replace_symbol_body overwrites the extended range, deleting the semicolon.
            extended_symbol = symbol.copy()
            extended_symbol.pop("body", None)
            extended_symbol["range"] = new_range

            # CRITICAL: Also update the location.range if it exists
            if extended_symbol.get("location"):
                location = extended_symbol["location"].copy()
                if "range" in location:
                    location["range"] = new_range.copy()
                extended_symbol["location"] = location

            return extended_symbol

        return symbol

    # nixd's document symbols are its syntax tree: an attribute (Field, `let` bindings included) or a lambda
    # (Function) is a definition, but every variable use (Constant, Variable, Event, Boolean, Null), attribute
    # name in a selection (Property), literal (String, Number) and list (Array) is a symbol too. Taken as symbols,
    # uses share the names of what they use, so `find_symbol` of a binding matched its uses as well; and a use
    # starts where nixd's reference to it starts, so `find_referencing_symbols` took each reference for the symbol
    # itself and dropped it.
    # A lambda without an `@` name is `(anonymous lambda)`: nearly every module and package file is one, which hid
    # the file's bindings below it in the overview and put `(anonymous lambda)/` in front of every name path.
    _NIX_DEFINITION_KINDS = frozenset({ls_types.SymbolKind.Field, ls_types.SymbolKind.Function})
    _NIX_ANONYMOUS_LAMBDA = "(anonymous lambda)"

    @classmethod
    def _nix_definitions(cls, symbols: list[ls_types.UnifiedSymbolInformation]) -> list[ls_types.UnifiedSymbolInformation]:
        """
        The definitions among nixd's symbols, each with only definitions below it. A definition inside a dropped
        symbol, such as an attribute set in a list or the body of an anonymous lambda, takes the dropped symbol's
        place. Each is marked as structure: a Field's kind would leave it out of overviews.
        """
        definitions = []
        for symbol in symbols:
            children = cls._nix_definitions(symbol.get("children") or [])
            anonymous_lambda = symbol.get("kind") == ls_types.SymbolKind.Function and symbol["name"] == cls._NIX_ANONYMOUS_LAMBDA
            if symbol.get("kind") in cls._NIX_DEFINITION_KINDS and not anonymous_lambda:
                definitions.append({**symbol, "children": children, "low_level": False})
            else:
                definitions.extend(children)
        # the overload indices were given among nixd's siblings, uses and lifted definitions included
        counts = Counter(symbol["name"] for symbol in definitions)
        seen: Counter[str] = Counter()
        for symbol in definitions:
            symbol.pop("overload_idx", None)
            if counts[symbol["name"]] > 1:
                symbol["overload_idx"] = seen[symbol["name"]]
            seen[symbol["name"]] += 1
        return definitions

    @classmethod
    def _is_nix_range_shifted(cls, symbol: ls_types.UnifiedSymbolInformation, edited: ls_types.Range, lines: list[str]) -> bool:
        """
        Whether Serena's editor, which takes columns as string indices, would edit other text than the symbol's: LSP
        counts UTF-16 units, two for a character outside the Basic Multilingual Plane. One such character before a
        binding's end still lands on its `;`, which the range then does not take; more, or one before its start, do not.
        """
        start, end = symbol["range"]["start"], symbol["range"]["end"]
        if end["line"] >= len(lines):
            return False
        start_line, end_line = lines[start["line"]], lines[end["line"]]
        if start_line.isascii() and end_line.isascii():
            return False
        true_start = _utf16_column_to_index(start_line, start["character"])
        true_end = _utf16_column_to_index(end_line, end["character"])
        if cls._is_nix_binding(symbol, lines) and end_line[true_end : true_end + 1] == ";":
            true_end += 1
        return (edited["start"]["character"], edited["end"]["character"]) != (true_start, true_end)

    @classmethod
    def _join_attrpath_segments(
        cls,
        symbol: ls_types.UnifiedSymbolInformation,
        body_factory: SymbolBodyFactory,
        lines: list[str],
        path_start: ls_types.UnifiedSymbolInformation | None = None,
    ) -> tuple[tuple[int, int], bool]:
        """
        Gives each segment of a dotted key the statement it begins, and refuses the edits that would break the file.

        nixd gives the segments of an attribute path before its last only their name: in `a.b.c = 1;`, `a` covers
        `a`, so a new `a.b.c = 2;` replaced the name alone. A segment with one child now ends where its child does:
        `a` covers `a.b.c = 1;` and `b` covers `b.c = 1;`. nixd merges the statements that share a prefix, so
        `x.a = 1; y = 2; x.b = 3;` has one `x` and `z = { }; z.extra = 2;` one `z`, and neither has a single body.
        A later statement that adds no attribute (`x.a = 1; x = { };`) leaves no trace in the tree, and goes unseen.

        Serena deletes a symbol's range and inserts at the line before or after it, which is right only for a whole
        statement on lines of its own: deleting `b` of `a.b = 1;` leaves `a.` to take the next binding, deleting
        `x: x` of `f = x: x;` leaves `f = ;`, and inserting after `a = 1;` in `a = 1; b = {` lands inside `b`.

        :param path_start: the first segment of the attribute path that the symbol continues, if it continues one
        :return: the end of the last statement defining the symbol, as (line, character), and whether the editor's
            columns are shifted from the symbol's text
        """
        shifted = symbol.pop("_nix_shifted", False)
        children = symbol.get("children") or []
        symbol_range = symbol["range"]
        end = (symbol_range["end"]["line"], symbol_range["end"]["character"])
        starts_after = [(c["range"]["start"]["line"], c["range"]["start"]["character"]) >= end for c in children]
        name_only = symbol_range == symbol.get("selectionRange")
        # nixd also lists the attributes of `inherit ({ x = 1; }) x;`'s set below the name, before it
        is_segment = bool(children) and name_only and all(starts_after)
        child_results = [
            # a child after a binding's range is in a later statement continuing it, as `a` is in `x = { }; x.a = 1;`
            cls._join_attrpath_segments(child, body_factory, lines, (path_start or symbol) if after else None)
            for child, after in zip(children, starts_after, strict=True)
        ]
        child_ends = [child_end for child_end, _ in child_results]
        if is_segment:
            pieced = len(children) > 1 or child_ends[0] > (children[0]["range"]["end"]["line"], children[0]["range"]["end"]["character"])
            if not pieced:
                joined = {"start": symbol_range["start"], "end": {**children[0]["range"]["end"]}}
                symbol["range"] = joined
                symbol["location"] = {**symbol["location"], "range": {**joined}}
                symbol.pop("body", None)  # the factory would return the name's body
                symbol["body"] = body_factory.create_symbol_body(symbol)
                end = child_ends[0]
                shifted = shifted or child_results[0][1]
        else:
            pieced = any(child_end > end for child_end in child_ends)

        name = symbol["name"]
        # positions, not slices: a generated line can hold thousands of symbols
        start_line, end_line = lines[symbol["range"]["start"]["line"]], lines[symbol["range"]["end"]["line"]]
        begins_line = _utf16_column_to_index(start_line, symbol["range"]["start"]["character"]) <= _NIX_INDENT.match(start_line).end()
        end_index = _utf16_column_to_index(end_line, symbol["range"]["end"]["character"])
        refusals: dict[str, str] = {}
        if pieced:
            refusals["replace_body"] = refusals["delete"] = (
                f"`{name}` is defined by more than one statement (as `x` is in `x.a = 1; x.b = 2;`), so it has no single "
                "body to replace or delete. Edit the attributes below it one by one, or the text with replace_content."
            )
        if shifted:
            refusals["replace_body"] = refusals["delete"] = (
                f"`{name}` follows a character outside the Basic Multilingual Plane (such as an emoji) on its line, where "
                "Serena's columns differ from the language server's, so it would edit other text. Use replace_content."
            )
        if path_start is not None:
            refusals.setdefault(
                "delete",
                f"`{name}` continues the attribute path of its statement, so deleting it alone would leave the path's start "
                "behind (`a.` of `a.b = 1;`). Delete the statement through its first segment, or the text with replace_content.",
            )
            refusals["insert_before"] = (
                f"`{name}` continues an attribute path, so code inserted before its line could land inside the path. Insert "
                "before the path's first segment or after the binding above, or with replace_content."
            )
        whole = end_index > 0 and end_line[end_index - 1] == ";"
        # what starts a binding: not a lambda, an inherited name (its name alone, no segment) or a path's continuation
        begins_binding = symbol.get("kind") == ls_types.SymbolKind.Field and path_start is None and (is_segment or not name_only)
        part = f"`{name}` is not a whole binding (as `x: x` is not, of `f = x: x;`, nor a name of an `inherit`)"
        if not whole:  # only a binding's range takes its `;`
            refusals.setdefault(
                "delete", f"{part}, so deleting it would leave its binding broken. Edit the binding, or the text with replace_content."
            )
        if not begins_binding:
            refusals.setdefault(
                "insert_before",
                f"{part}, so code inserted before its line could land inside its binding. Insert relative to a binding that "
                "begins and ends its lines, or with replace_content.",
            )
        if not whole:
            refusals.setdefault(
                "insert_after",
                f"`{name}` does not end with the `;` of a whole binding, so code inserted after its line could land inside a "
                "statement. Insert relative to a binding that begins and ends its lines, or with replace_content.",
            )
        if not begins_line:
            refusals.setdefault(
                "insert_before",
                f"`{name}` does not begin its line, so code inserted before the line would go before what precedes it there "
                "(a `{` or another binding). Insert relative to a binding on its own line, or with replace_content.",
            )
        if not _NIX_LINE_END.fullmatch(end_line, end_index):
            refusals.setdefault(
                "insert_after",
                f"`{name}` does not end its line, so code inserted after the line would go after what follows it there "
                "(a `}` or the start of another binding). Insert relative to a binding on its own line, or with replace_content.",
            )
        if refusals:
            symbol["edit_refusals"] = refusals
        return max([end, *child_ends]), shifted

    @override
    def _document_symbols_cache_fingerprint(self) -> Hashable | None:
        build_document_symbols_version = 7
        return build_document_symbols_version

    @override
    def _build_document_symbols_from_raw_symbols(self, relative_file_path: str, file_buffer: LSPFileBuffer) -> DocumentSymbols:
        # Override to extend Nix symbol ranges to include trailing semicolons.
        # nixd provides expression-level ranges (excluding semicolons) but serena needs
        # statement-level ranges (including semicolons) for proper symbol replacement.
        # IMPORTANT: Update _document_symbols_cache_fingerprint() when changing this method.

        # Get symbols from parent implementation
        document_symbols = super()._build_document_symbols_from_raw_symbols(relative_file_path, file_buffer=file_buffer)

        # Split once per document: a fresh copy of a long line per symbol would be rehashed for each lookup
        lines = file_buffer.split_lines()
        body_factory = SymbolBodyFactory(file_buffer)

        # Extend ranges for all symbols recursively
        def extend_symbol_and_children(symbol: ls_types.UnifiedSymbolInformation) -> ls_types.UnifiedSymbolInformation:
            # Extend this symbol's range
            extended = self._extend_nix_symbol_range_to_include_semicolon(symbol, lines)
            extended["_nix_shifted"] = self._is_nix_range_shifted(symbol, extended["range"], lines)
            # every body from one factory, so that the document keeps a single copy of its lines
            extended.pop("body", None)
            extended["body"] = body_factory.create_symbol_body(extended)

            # Extend children recursively
            if extended.get("children"):
                extended["children"] = [extend_symbol_and_children(child) for child in extended["children"]]
                for child in extended["children"]:  # the container lookup returns the parent, with its body
                    child["parent"] = extended

            return extended

        # Apply range extension to all symbols
        try:
            extended_root_symbols = [extend_symbol_and_children(sym) for sym in self._nix_definitions(document_symbols.root_symbols)]
            for root in extended_root_symbols:  # a definition lifted out of a dropped symbol would still name it
                root["parent"] = None
                self._join_attrpath_segments(root, body_factory, lines)
        finally:
            _utf16_offsets.cache_clear()  # its tables are several times their lines' size

        return DocumentSymbols(extended_root_symbols)

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        # For Nix projects, we should ignore:
        # - result: nix build output symlinks
        # - result-*: multiple build outputs
        # - .direnv: direnv cache
        return super().is_ignored_dirname(dirname) or dirname in ["result", ".direnv"] or dirname.startswith("result-")

    @staticmethod
    def _create_default_nixd_settings() -> dict[str, Any]:
        """Return the default settings sent to nixd."""
        return {
            "nixpkgs": {"expr": "import <nixpkgs> { }"},
            "formatting": {"command": ["nixpkgs-fmt"]},
            "options": {},
        }

    @classmethod
    def _load_nixd_settings(cls, custom_settings: SolidLSPSettings.CustomLSSettings) -> dict[str, Any]:
        """Load nixd settings from ``config_path`` or return the built-in defaults.

        :param custom_settings: Nix-specific language-server settings.
        :return: The value of the nixd configuration section.
        :raises ValueError: If ``config_path`` or its JSON document has an invalid shape.
        :raises RuntimeError: If the configuration file cannot be read.
        """
        config_path_value = custom_settings.get("config_path")
        if config_path_value is None:
            return cls._create_default_nixd_settings()
        if not isinstance(config_path_value, str) or not config_path_value.strip():
            raise ValueError("ls_specific_settings.nix.config_path must be a non-empty absolute path")

        config_path = Path(config_path_value).expanduser()
        if not config_path.is_absolute():
            raise ValueError(f"ls_specific_settings.nix.config_path must be absolute: {config_path_value!r}")

        try:
            with config_path.open(encoding="utf-8") as config_file:
                settings = json.load(config_file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in nixd configuration file '{config_path}': {exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read nixd configuration file '{config_path}': {exc}") from exc

        if not isinstance(settings, dict):
            raise ValueError(
                f"Invalid nixd configuration file '{config_path}': expected a JSON object containing the value of the 'nixd' section"
            )
        return settings

    @staticmethod
    def _resolve_nixd_configuration_section(settings: dict[str, Any], section: object) -> Any:
        """Resolve a ``nixd`` configuration section from the effective settings."""
        if section == "nixd":
            return deepcopy(settings)
        if not isinstance(section, str) or not section.startswith("nixd."):
            return {}

        value: Any = settings
        for key in section.removeprefix("nixd.").split("."):
            if not key or not isinstance(value, dict) or key not in value:
                return {}
            value = value[key]
        return deepcopy(value)

    @classmethod
    def _get_workspace_configuration(cls, params: object, settings: dict[str, Any]) -> list[Any]:
        """Return configuration values matching each requested item in order."""
        if not isinstance(params, dict):
            return []

        items = params.get("items", [])
        if not isinstance(items, list):
            return []

        return [
            cls._resolve_nixd_configuration_section(settings, item.get("section") if isinstance(item, dict) else None) for item in items
        ]

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        custom_settings = solidlsp_settings.get_ls_specific_settings(config.ls_id)
        self._nixd_settings = self._load_nixd_settings(custom_settings)

        super().__init__(config, repository_root_path, None, "nix", solidlsp_settings)
        self.request_id = 0

    def _create_dependency_provider(self) -> LanguageServerDependencyProvider:
        """Create the provider that resolves the nixd launch command."""
        return self.DependencyProvider(self._custom_settings, self._ls_resources_dir)

    def _create_base_initialize_params(self) -> dict:
        """
        Returns the initialize params for nixd.
        """
        initialize_params = {
            "locale": "en",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": True},
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "completion": {
                        "dynamicRegistration": True,
                        "completionItem": {
                            "snippetSupport": True,
                            "commitCharactersSupport": True,
                            "documentationFormat": ["markdown", "plaintext"],
                            "deprecatedSupport": True,
                            "preselectSupport": True,
                        },
                    },
                    "hover": {
                        "dynamicRegistration": True,
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    "signatureHelp": {
                        "dynamicRegistration": True,
                        "signatureInformation": {
                            "documentationFormat": ["markdown", "plaintext"],
                            "parameterInformation": {"labelOffsetSupport": True},
                        },
                    },
                    "codeAction": {
                        "dynamicRegistration": True,
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
                    },
                    "rename": {"dynamicRegistration": True, "prepareSupport": True},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "configuration": True,
                    "symbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
            },
            "initializationOptions": deepcopy(self._nixd_settings),
        }
        return initialize_params

    @override
    def request_definition(self, relative_file_path: str, line: int, column: int) -> list[ls_types.Location]:
        # nixd answers a name that `with` brings into scope with that `with` keyword, as Nix looks the name up only
        # when evaluating. Taken for a definition, it made the binding around the `with` the name's declaration.
        definitions = super().request_definition(relative_file_path, line, column)
        if not definitions:
            return definitions
        lines = self.retrieve_full_file_content(relative_file_path).split("\n")

        def is_with_keyword(location: ls_types.Location) -> bool:
            start, end = location["range"]["start"], location["range"]["end"]
            if location.get("relativePath") != relative_file_path or start["line"] != end["line"] or start["line"] >= len(lines):
                return False
            text = lines[start["line"]]
            return text[_utf16_column_to_index(text, start["character"]) : _utf16_column_to_index(text, end["character"])] == "with"

        return [definition for definition in definitions if not is_with_keyword(definition)]

    @override
    def _request_hover(self, file_buffer: LSPFileBuffer, line: int, column: int) -> ls_types.Hover | None:
        """Retry an empty hover while nixd finishes its initial analysis.

        nixd can answer the first hover request with ``null`` immediately after
        ``textDocument/didOpen`` even though the same position becomes available
        shortly afterwards. Keep the retry bounded so a genuinely unsupported
        hover still returns promptly.
        """
        result = super()._request_hover(file_buffer, line, column)
        for _ in range(self._HOVER_RETRY_COUNT):
            if result is not None:
                return result
            sleep(self._HOVER_RETRY_DELAY_SECONDS)
            result = super()._request_hover(file_buffer, line, column)
        return result

    @override
    def _supports_pull_diagnostics(self) -> bool:
        # nixd publishes diagnostics after textDocument/didOpen but terminates when it receives
        # textDocument/diagnostic. Force the published-diagnostics path instead.
        return False

    def _start_server(self):
        """Start nixd server process"""
        initialize_params = self._create_initialize_params()
        nixd_settings = initialize_params.get("initializationOptions", {})
        if not isinstance(nixd_settings, dict):
            nixd_settings = {}

        def register_capability_handler(params):
            return

        def workspace_configuration_handler(params):
            return self._get_workspace_configuration(params, nixd_settings)

        def window_log_message(msg):
            log.info(f"LSP: window/logMessage: {msg}")

        def do_nothing(params):
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_request("workspace/configuration", workspace_configuration_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        log.info("Starting nixd server process")
        self.server.start()

        log.info("Sending initialize request from LSP client to LSP server and awaiting response")
        init_response = self.server.send.initialize(initialize_params)

        # Verify server capabilities
        assert "textDocumentSync" in init_response["capabilities"]
        assert "definitionProvider" in init_response["capabilities"]
        assert "documentSymbolProvider" in init_response["capabilities"]
        assert "referencesProvider" in init_response["capabilities"]

        self.server.notify.initialized({})

        # nixd server is typically ready immediately after initialization
