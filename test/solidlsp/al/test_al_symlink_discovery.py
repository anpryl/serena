"""
The AL server's own file discovery for the full symbol tree follows symbolic links only where its configuration does,
and its app.json, opened at startup, is used when linked from inside the repository, and when linked from outside it only
where the configuration follows links.
"""

import os
from pathlib import Path
from typing import cast

import pytest

from solidlsp import ls_config, ls_types
from solidlsp.language_servers.al_language_server import ALLanguageServer
from solidlsp.ls import DocumentSymbols, LSPFileBuffer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId


class _ALServerWithoutProcess(ALLanguageServer):
    """
    An AL server that is never started, whose document symbols are a single symbol per requested file, so that the full
    symbol tree shows which files the discovery found.
    """

    def request_document_symbols(self, relative_file_path: str, file_buffer: LSPFileBuffer | None = None) -> DocumentSymbols:
        symbol = {"name": f"symbol_of:{relative_file_path}", "kind": ls_types.SymbolKind.Class, "children": []}
        return DocumentSymbols([cast(ls_types.UnifiedSymbolInformation, symbol)])


def _create_server(repository_root: Path, config: LanguageServerConfig) -> ALLanguageServer:
    """Creates a server instance without starting it."""
    server = object.__new__(_ALServerWithoutProcess)
    server.config = config
    server.repository_root_path = str(repository_root)
    return server


def _discovered_files(nodes: list[ls_types.UnifiedSymbolInformation]) -> set[str]:
    files = set()
    for node in nodes:
        if node["kind"] == ls_types.SymbolKind.File:
            files.add(node["location"]["relativePath"])
        files.update(_discovered_files(node.get("children", [])))
    return files


@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_file_link_out_of_the_repository_is_discovered_only_where_links_are_followed(
    tmp_path: Path, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # an ordinary file and a link to a file outside the repository
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Main.al").write_text('codeunit 50100 "Main" { }\n', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.al").write_text('codeunit 50101 "Secret" { }\n', encoding="utf-8")
    try:
        (root / "src" / "Linked.al").symlink_to(outside / "Secret.al")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.AL, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    server = _create_server(root, config)

    expected = {"src/Main.al", "src/Linked.al"} if follows else {"src/Main.al"}
    assert _discovered_files(server.request_full_symbol_tree()) == expected
    assert _discovered_files(server.request_full_symbol_tree(within_relative_path="src")) == expected


def test_directory_link_inside_the_repository_given_as_the_scan_start_is_scanned(tmp_path: Path) -> None:
    """
    A directory requested explicitly may be a link that stays inside the repository, as Serena's untrusted rules allow;
    the scan covers it even where links are not followed, still leaving out file links below it.
    """
    root = tmp_path / "repo"
    (root / "inner").mkdir(parents=True)
    (root / "inner" / "Inner.al").write_text('codeunit 50102 "Inner" { }\n', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.al").write_text('codeunit 50101 "Secret" { }\n', encoding="utf-8")
    try:
        (root / "inner" / "Linked.al").symlink_to(outside / "Secret.al")
        (root / "alias").symlink_to(root / "inner", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.AL, follow_symlinks=False)
    server = _create_server(root, config)

    assert _discovered_files(server.request_full_symbol_tree(within_relative_path="alias")) == {"alias/Inner.al"}


@pytest.mark.parametrize("target", ["inside", "outside"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_app_json_linked_from_outside_the_repository_is_opened_only_where_links_are_followed(
    tmp_path: Path, target: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target_dir = root / "manifest" if target == "inside" else tmp_path / "outside"
    target_dir.mkdir()
    (target_dir / "app.json").write_text('{"name": "App"}\n', encoding="utf-8")
    try:
        (root / "app.json").symlink_to(target_dir / "app.json")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.AL, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    app_json = _create_server(root, config)._find_app_json()

    assert (app_json.path if app_json is not None else None) == (str(root / "app.json") if follows or target == "inside" else None)


def test_app_json_is_read_from_the_path_validated_when_its_link_is_swapped_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "manifest").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "manifest" / "app.json").write_text('{"name": "Inside"}\n', encoding="utf-8")
    (tmp_path / "outside" / "app.json").write_text('{"name": "Outside"}\n', encoding="utf-8")
    link = root / "app.json"
    link.symlink_to(root / "manifest" / "app.json")
    original_contains = ls_config.RealRoot.contains
    validated_target = os.path.realpath(root / "manifest" / "app.json")
    swapped: list[bool] = []

    # swap the link out of the repository right after its inside target has been validated
    def contains_then_swap(real_root: ls_config.RealRoot, real_path: str) -> bool:
        result = original_contains(real_root, real_path)
        if real_path == validated_target and not swapped:
            link.unlink()
            link.symlink_to(tmp_path / "outside" / "app.json")
            swapped.append(True)
        return result

    monkeypatch.setattr(ls_config.RealRoot, "contains", contains_then_swap)
    config = LanguageServerConfig(ls_id=LanguageServerId.AL, follow_symlinks=False)

    app_json = _create_server(root, config)._find_app_json()

    assert swapped
    assert app_json is not None
    assert Path(app_json.real_path).read_text(encoding="utf-8") == '{"name": "Inside"}\n'
