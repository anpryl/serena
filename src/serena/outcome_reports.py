# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass

from serena.util.path_renderer import PathRenderer


@dataclass(frozen=True)
class RenameOutcome:
    """
    What a rename did, as facts, and the message that reports them.
    """

    name_path: str
    new_name: str
    changed_files: tuple[str, ...]
    """the files touched by the rename, relative to the project root"""
    n_changes: int
    """the number of edit operations applied"""
    declaration_file: str
    """the file that declares the renamed symbol, relative to the project root"""
    n_other_uses: int
    """the number of uses of the symbol outside its declaration (references the language server reports, or other files that mention the name); 0 if it was not determined, which it is only when the rename changed a file other than the declaration file"""

    @property
    def only_declaration_edited(self) -> bool:
        return self.n_other_uses > 0 and self.changed_files == (self.declaration_file,) and self.n_changes == 1

    def message(self) -> str:
        """
        :return: the report of the rename, naming the changed files and warning if only the declaration was
            edited although the symbol is referenced elsewhere
        """
        renderer = PathRenderer.current()
        files = ", ".join(renderer.render(f) for f in self.changed_files)
        msg = f"Successfully renamed '{self.name_path}' to '{self.new_name}' ({self.n_changes} changes applied in: {files})"
        if self.only_declaration_edited:
            msg += (
                f"\nWarning: only the declaration was renamed, but {self.n_other_uses} "
                "other use(s) of the name exist and were left unchanged. The language server may not know the "
                "project's other files (e.g. a JavaScript project without jsconfig.json or tsconfig.json). "
                "Search the project for the name and rename the remaining uses by hand."
            )
        return msg


@dataclass(frozen=True)
class SymbolLookupOutcome:
    """
    What a symbol lookup found, as facts, and the hint that follows from them.
    """

    name_path_pattern: str
    relative_path: str
    """the file or directory the lookup was restricted to; empty if it was not"""
    n_matches: int

    GO_SUFFIX = ".go"

    @property
    def is_empty_go_lookup_of_nested_name_path(self) -> bool:
        return self.n_matches == 0 and self.relative_path.endswith(self.GO_SUFFIX) and "/" in self.name_path_pattern.strip("/")

    def hint(self) -> str:
        """
        :return: text to append to the lookup result; empty if there is nothing to add
        """
        if self.is_empty_go_lookup_of_nested_name_path:
            last_segment = self.name_path_pattern.strip("/").rsplit("/", 1)[-1]
            return f"\nGo methods are top-level symbols: try `{last_segment}`."
        return ""
