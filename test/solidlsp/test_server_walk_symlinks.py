"""
Language servers' own walks of the repository whose files are opened or read follow symbolic links only where the
configuration does: the Vue, Svelte and Astro companion indexing, the Angular and TypeScript seed files, and Clojure's
project descriptors; configured workspace folders (indexed and additional) are reached through a link out of the
repository only where it does; Elixir's mix.exs, Zig's build.zig and clangd's compilation database are used when
linked from inside the repository, and when linked from outside it only where it does; and the Vue, Svelte and Astro
companion TypeScript servers keep that rule.
"""

import builtins
import gc
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pathspec
import pytest

from solidlsp import ls_config
from solidlsp.language_servers.al_language_server import ALLanguageServer
from solidlsp.language_servers.angular_language_server import AngularLanguageServer
from solidlsp.language_servers.astro_language_server import AstroLanguageServer
from solidlsp.language_servers.clangd_language_server import ClangdLanguageServer
from solidlsp.language_servers.clojure_lsp import ClojureLSP
from solidlsp.language_servers.elixir_tools.elixir_tools import ElixirTools
from solidlsp.language_servers.svelte_language_server import SvelteLanguageServer
from solidlsp.language_servers.typescript_language_server import TypeScriptLanguageServer
from solidlsp.language_servers.vue_language_server import VueLanguageServer
from solidlsp.language_servers.zls import ZigLanguageServer
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId, RealRoot
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings


@dataclass
class _CompanionDiscovery:
    server_class: type[SolidLanguageServer]
    ls_id: LanguageServerId
    extension: str
    find_files: Callable[[Any], list[str]]
    """the server's discovery of the files to open on its companion TypeScript server, as paths relative to the root"""

    def create_server(self, repository_root: Path, config: LanguageServerConfig) -> Any:
        """Creates a server instance without starting it (or its companion)."""
        server: Any = object.__new__(self.server_class)
        server.config = config
        server.ls_id = self.ls_id
        server.repository_root_path = str(repository_root)
        server.repo_path = str(repository_root)
        server._ignore_spec = pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, [])
        return server


_DISCOVERIES = [
    _CompanionDiscovery(VueLanguageServer, LanguageServerId.VUE, ".vue", lambda server: server._find_all_vue_files()),
    _CompanionDiscovery(SvelteLanguageServer, LanguageServerId.SVELTE, ".svelte", lambda server: server._find_all_svelte_files()),
    _CompanionDiscovery(AstroLanguageServer, LanguageServerId.ASTRO, ".astro", lambda server: server._find_all_astro_files()),
]


@pytest.mark.parametrize("discovery", _DISCOVERIES, ids=lambda discovery: discovery.ls_id.value)
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_links_out_of_the_repository_are_discovered_only_where_links_are_followed(
    tmp_path: Path, discovery: _CompanionDiscovery, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # an ordinary file, a link to a file outside the repository and a link to a directory outside it
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / f"App{discovery.extension}").write_text("<template></template>\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"Secret{discovery.extension}").write_text("<template>secret</template>\n", encoding="utf-8")
    try:
        (root / "src" / f"Linked{discovery.extension}").symlink_to(outside / f"Secret{discovery.extension}")
        (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=discovery.ls_id, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    found = {Path(path).as_posix() for path in discovery.find_files(discovery.create_server(root, config))}

    # whether a link to a directory is descended into where links are followed depends on the Python version's rglob
    if follows:
        assert {f"src/App{discovery.extension}", f"src/Linked{discovery.extension}"} <= found
    else:
        assert found == {f"src/App{discovery.extension}"}


def _create_unstarted_server(server_class: type[SolidLanguageServer], repository_root: Path, config: LanguageServerConfig) -> Any:
    """Creates a server instance without starting it."""
    server: Any = object.__new__(server_class)
    server.config = config
    server.repository_root_path = str(repository_root)
    return server


@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_angular_seed_is_reached_through_a_link_only_where_links_are_followed(
    tmp_path: Path, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # a plain source file, and a component (the preferred seed) linked from outside the repository
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "src" / "main.ts").write_text("export const main = 1;\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.component.ts").write_text("export const secret = 1;\n", encoding="utf-8")
    try:
        (root / "src" / "linked.component.ts").symlink_to(outside / "secret.component.ts")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.ANGULAR, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    seed = _create_unstarted_server(AngularLanguageServer, root, config)._find_representative_source_file(str(root))

    assert seed == str(root / "src" / ("linked.component.ts" if follows else "main.ts"))


def test_angular_seed_of_an_additional_workspace_folder_outside_the_repository_is_found(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    additional = tmp_path / "additional"
    additional.mkdir()
    (additional / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (additional / "app.component.ts").write_text("export const app = 1;\n", encoding="utf-8")

    config = LanguageServerConfig(ls_id=LanguageServerId.ANGULAR, follow_symlinks=False)
    seed = _create_unstarted_server(AngularLanguageServer, root, config)._find_representative_source_file(str(additional))

    assert seed == str(additional / "app.component.ts")


@pytest.mark.parametrize("link_kind", ["file", "src_directory"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_typescript_seed_is_reached_through_a_link_only_where_links_are_followed(
    tmp_path: Path, link_kind: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # a file beside tsconfig.json, and a preferred seed under src reached through a link out of the repository
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "main.ts").write_text("export const main = 1;\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.ts").write_text("export const secret = 1;\n", encoding="utf-8")
    try:
        if link_kind == "file":
            (root / "src").mkdir()
            (root / "src" / "linked.ts").symlink_to(outside / "secret.ts")
            linked_seed = root / "src" / "linked.ts"
        else:
            (root / "src").symlink_to(outside, target_is_directory=True)
            linked_seed = root / "src" / "secret.ts"
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.TYPESCRIPT, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    seed = _create_unstarted_server(TypeScriptLanguageServer, root, config)._find_representative_source_file(str(root))

    assert seed == str(linked_seed if follows else root / "main.ts")


def test_typescript_seed_of_an_additional_workspace_folder_outside_the_repository_is_found(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    additional = tmp_path / "additional"
    additional.mkdir()
    (additional / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (additional / "index.ts").write_text("export const index = 1;\n", encoding="utf-8")

    config = LanguageServerConfig(ls_id=LanguageServerId.TYPESCRIPT, follow_symlinks=False)
    seed = _create_unstarted_server(TypeScriptLanguageServer, root, config)._find_representative_source_file(str(additional))

    assert seed == str(additional / "index.ts")


@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_clojure_descriptor_reached_through_a_link_is_read_only_where_links_are_followed(
    tmp_path: Path, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # a module's descriptor, and another module's descriptor that is a link out of the repository
    root = tmp_path / "repo"
    (root / "linked_module").mkdir(parents=True)
    (root / "deps.edn").write_text('{:paths ["src"]}\n', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deps.edn").write_text('{:paths ["secret"]}\n', encoding="utf-8")
    try:
        (root / "linked_module" / "deps.edn").symlink_to(outside / "deps.edn")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.CLOJURE, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    source_paths = _create_unstarted_server(ClojureLSP, root, config)._collect_source_paths()

    assert set(source_paths) == ({"src", "linked_module/secret"} if follows else {"src"})


@dataclass
class _ConfiguredFolder:
    path: str
    """the folder as configured"""
    resolved: Path
    """the directory it resolves to"""
    leaves_through_link: bool
    """whether it is reached through a link that lies inside the repository and leads out of it"""


@pytest.mark.parametrize("kind", ["indexed", "additional"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_workspace_folder_reached_through_a_link_out_of_the_repository_is_used_only_where_links_are_followed(
    tmp_path: Path, kind: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # the repository, with a folder, a link to that folder and a link out of the repository
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def secret():\n    return 1\n", encoding="utf-8")
    # where `linked/../src` resolves to (through the link), unlike its lexical normalisation `src`
    (tmp_path / "src").mkdir()
    # folders outside the repository, one of them named through a link that lies outside it too
    (tmp_path / "plain").mkdir()
    (tmp_path / "shared").mkdir()
    try:
        (root / "in_link").symlink_to(root / "src", target_is_directory=True)
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (tmp_path / "shared-link").symlink_to(tmp_path / "shared", target_is_directory=True)
        (tmp_path / "ancestor-link").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    folders = [
        _ConfiguredFolder("src", root / "src", False),
        _ConfiguredFolder("in_link", root / "src", False),
        _ConfiguredFolder("linked", outside, True),
        _ConfiguredFolder("linked/../src", tmp_path / "src", True),
        _ConfiguredFolder("nonexist/../linked", outside, True),
        _ConfiguredFolder(str(root / "nonexist" / ".." / "linked"), outside, True),
        _ConfiguredFolder(str(tmp_path / "nope" / ".." / "repo" / "linked"), outside, True),
        _ConfiguredFolder(os.path.join("..", "plain"), tmp_path / "plain", False),
        _ConfiguredFolder(os.path.join("..", "shared-link"), tmp_path / "shared", False),
        _ConfiguredFolder(str(tmp_path / "shared-link"), tmp_path / "shared", False),
    ]
    # other spellings of the root, where the file system has them: another letter case, and macOS's firmlinked data volume
    if (tmp_path / "REPO").exists():
        folders.append(_ConfiguredFolder(str(tmp_path / "REPO" / "linked"), outside, True))
        folders.append(_ConfiguredFolder(os.path.join("..", "REPO", "linked"), outside, True))
    firmlinked_root = Path("/System/Volumes/Data" + os.path.realpath(root))
    if firmlinked_root.exists():
        folders.append(_ConfiguredFolder(str(firmlinked_root / "linked"), outside, True))
    paths = [folder.path for folder in folders]
    settings = {"workspace_folders": paths} if kind == "indexed" else {"additional_workspace_folders": paths}
    config = LanguageServerConfig(
        ls_id=LanguageServerId.PYTHON, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks, **settings
    )

    def resolve(project_root: Path) -> list[str]:
        if kind == "indexed":
            return config.get_absolute_workspace_folders(str(project_root))
        return config.get_absolute_additional_workspace_folders(str(project_root))

    # folders resolving to the same directory are used once
    expected: list[str] = []
    for folder in folders:
        resolved = os.path.realpath(folder.resolved)
        if (follows or not folder.leaves_through_link) and resolved not in expected:
            expected.append(resolved)
    assert resolve(root) == expected

    # the root spelled through a linked ancestor behaves as its plain spelling
    assert resolve(tmp_path / "ancestor-link" / "repo") == expected


class _CompanionStopped(Exception):
    pass


@pytest.mark.parametrize(
    "server_class,companion_class_name",
    [
        (VueLanguageServer, "VueTypeScriptServer"),
        (SvelteLanguageServer, "SvelteTypeScriptServer"),
        (AstroLanguageServer, "AstroTypeScriptServer"),
    ],
    ids=["vue", "svelte", "astro"],
)
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks",
    [(True, True), (True, False), (False, True)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_companion_typescript_server_keeps_the_symlink_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_class: type[SolidLanguageServer],
    companion_class_name: str,
    is_project_trusted: bool,
    follow_symlinks: bool,
) -> None:
    # a companion that records the configuration it is created with, then stops the start-up
    companion_configs: list[LanguageServerConfig] = []

    def create_companion(*_args: Any, config: LanguageServerConfig, **_kwargs: Any) -> Any:
        companion_configs.append(config)
        raise _CompanionStopped

    module = sys.modules[server_class.__module__]
    monkeypatch.setattr(module, companion_class_name, create_companion)
    for helper in ("_get_svelte_ts_plugin_path", "_get_astro_ts_plugin_path", "_get_ts_ls_executable"):
        if hasattr(server_class, helper):
            monkeypatch.setattr(server_class, helper, lambda _self: str(tmp_path))

    config = LanguageServerConfig(ls_id=LanguageServerId.TYPESCRIPT, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    server = _create_unstarted_server(server_class, tmp_path, config)
    server.repo_path = str(tmp_path)
    server._solidlsp_settings = None
    server.tsdk_path = str(tmp_path)
    server._vue_ls_dir = str(tmp_path)
    server._ts_ls_cmd = str(tmp_path)
    # a failed companion start fails the Vue server's start, while Svelte and Astro degrade without it
    try:
        server._start_typescript_server()
    except _CompanionStopped:
        pass

    assert len(companion_configs) == 1
    assert companion_configs[0].is_following_symlinks() == config.is_following_symlinks()


@pytest.mark.parametrize("link_kind", ["file", "directory"])
@pytest.mark.parametrize("target", ["inside", "outside"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_elixir_mix_exs_linked_from_outside_the_repository_is_picked_only_where_links_are_followed(
    tmp_path: Path, link_kind: str, target: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # a mix.exs reached through a link (the root mix.exs itself, or a linked subdirectory), whose target lies inside the
    # repository (below a directory holding no mix.exs of its own) or outside it
    root = tmp_path / "repo"
    root.mkdir()
    target_dir = root / "nested" / "real_app" if target == "inside" else tmp_path / "outside"
    target_dir.mkdir(parents=True)
    (target_dir / "mix.exs").write_text("defmodule App.MixProject do\nend\n", encoding="utf-8")
    try:
        if link_kind == "file":
            (root / "mix.exs").symlink_to(target_dir / "mix.exs")
            linked_mix_exs = root / "mix.exs"
        else:
            (root / "app").symlink_to(target_dir, target_is_directory=True)
            linked_mix_exs = root / "app" / "mix.exs"
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.ELIXIR, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    mix_exs = _create_unstarted_server(ElixirTools, root, config)._find_mix_exs()

    assert (mix_exs.path if mix_exs is not None else None) == (str(linked_mix_exs) if follows or target == "inside" else None)


@pytest.mark.parametrize("target", ["inside", "outside"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_zig_build_file_linked_from_outside_the_repository_is_opened_only_where_links_are_followed(
    tmp_path: Path, target: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target_dir = root / "build" if target == "inside" else tmp_path / "outside"
    target_dir.mkdir()
    (target_dir / "build.zig").write_text("// build\n", encoding="utf-8")
    try:
        (root / "build.zig").symlink_to(target_dir / "build.zig")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.ZIG, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    build_zig = _create_unstarted_server(ZigLanguageServer, root, config)._find_build_zig()

    assert (build_zig.path if build_zig is not None else None) == (str(root / "build.zig") if follows or target == "inside" else None)


@pytest.mark.parametrize("target", ["inside", "outside"])
@pytest.mark.parametrize(
    "is_project_trusted,follow_symlinks,follows",
    [(True, True, True), (True, False, False), (False, True, False)],
    ids=["trusted_following", "trusted_not_following", "untrusted"],
)
def test_clangd_compilation_database_linked_from_outside_the_repository_is_rewritten_only_where_links_are_followed(
    tmp_path: Path, target: str, is_project_trusted: bool, follow_symlinks: bool, follows: bool
) -> None:
    # a compilation database with a relative directory (which Serena rewrites), linked from inside the repository (the
    # usual CMake layout, build/compile_commands.json) or from outside it
    root = tmp_path / "repo"
    root.mkdir()
    target_dir = root / "build" if target == "inside" else tmp_path / "outside"
    target_dir.mkdir()
    (target_dir / "compile_commands.json").write_text(
        json.dumps([{"directory": "src", "command": "cc -c main.c", "file": "main.c"}]), encoding="utf-8"
    )
    try:
        (root / "compile_commands.json").symlink_to(target_dir / "compile_commands.json")
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    config = LanguageServerConfig(ls_id=LanguageServerId.CPP, is_project_trusted=is_project_trusted, follow_symlinks=follow_symlinks)
    server = _create_unstarted_server(ClangdLanguageServer, root, config)
    server._solidlsp_settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"), project_data_path=str(root / ".serena"))
    server._custom_settings = {}
    compile_commands_dir = server._prepare_compile_commands()

    rewritten = root / ".serena" / "compile_commands.json"
    if follows or target == "inside":
        assert compile_commands_dir == str(root / ".serena")
        assert "main.c" in rewritten.read_text(encoding="utf-8")
    else:
        assert compile_commands_dir is not None
        _assert_empty_directory_outside(compile_commands_dir, root)
        assert not rewritten.exists()
        server._remove_ignored_compile_commands_dir()


def test_root_on_a_file_system_without_inode_numbers_is_recognised_by_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Where the file system reports an inode number of 0 for every entry, identity says nothing: a path outside the root,
    on the same device, is not taken for one inside it.
    """
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    real_stat = os.stat

    def stat_without_inode_numbers(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        fields = list(real_stat(path, *args, **kwargs))
        fields[1] = 0  # st_ino
        return os.stat_result(fields)

    monkeypatch.setattr(ls_config.os, "stat", stat_without_inode_numbers)
    real_root = RealRoot(str(root))

    assert not real_root.contains(os.path.realpath(tmp_path / "outside"))
    assert real_root.contains(os.path.realpath(root / "src"))


@dataclass
class _LinkSwap:
    """
    A link inside the repository that leads to a target inside it, swapped to lead out of the repository right after the
    inside target has been validated (as a concurrent change between a check and the use of its result would).
    """

    link: Path
    inside_target: Path
    outside_target: Path
    swapped: bool = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.link.symlink_to(self.inside_target, target_is_directory=self.inside_target.is_dir())
        original_contains = ls_config.RealRoot.contains
        validated_target = os.path.realpath(self.inside_target)

        def contains_then_swap(real_root: ls_config.RealRoot, real_path: str) -> bool:
            result = original_contains(real_root, real_path)
            if real_path == validated_target and not self.swapped:
                self.link.unlink()
                self.link.symlink_to(self.outside_target, target_is_directory=self.outside_target.is_dir())
                self.swapped = True
            return result

        monkeypatch.setattr(ls_config.RealRoot, "contains", contains_then_swap)


def _not_following_config(ls_id: LanguageServerId, **settings: Any) -> LanguageServerConfig:
    return LanguageServerConfig(ls_id=ls_id, is_project_trusted=True, follow_symlinks=False, **settings)


def test_workspace_folder_is_the_path_validated_when_its_link_is_swapped_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    swap = _LinkSwap(link=root / "inner", inside_target=root / "src", outside_target=outside)
    swap.install(monkeypatch)

    folders = _not_following_config(LanguageServerId.PYTHON, workspace_folders=["inner"]).get_absolute_workspace_folders(str(root))

    assert swap.swapped
    assert folders == [os.path.realpath(root / "src")]


def _swap_fixed_path_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Path, _LinkSwap]:
    """
    Sets up a repository whose `name` is a link to a file inside it, swapped to a file outside it right after validation.

    :return: the repository root and the swap
    """
    root = tmp_path / "repo"
    (root / "inside").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "inside" / name).write_text("inside\n", encoding="utf-8")
    (tmp_path / "outside" / name).write_text("outside\n", encoding="utf-8")
    swap = _LinkSwap(link=root / name, inside_target=root / "inside" / name, outside_target=tmp_path / "outside" / name)
    swap.install(monkeypatch)
    return root, swap


@pytest.mark.parametrize(
    "server_class,ls_id,name,find",
    [
        (ZigLanguageServer, LanguageServerId.ZIG, "build.zig", lambda server: server._find_build_zig()),
        (ElixirTools, LanguageServerId.ELIXIR, "mix.exs", lambda server: server._find_mix_exs()),
    ],
    ids=["zig", "elixir"],
)
def test_fixed_path_startup_file_is_read_from_the_path_validated_when_its_link_is_swapped_afterwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_class: type[SolidLanguageServer],
    ls_id: LanguageServerId,
    name: str,
    find: Callable[[Any], Any],
) -> None:
    root, swap = _swap_fixed_path_file(tmp_path, monkeypatch, name)

    found = find(_create_unstarted_server(server_class, root, _not_following_config(ls_id)))

    assert swap.swapped
    assert found.path == str(root / name)
    assert Path(found.real_path).read_text(encoding="utf-8") == "inside\n"


def test_clangd_compilation_database_is_read_from_the_path_validated_when_its_link_is_swapped_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "build").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    for directory, source in ((root / "build", "inside.c"), (tmp_path / "outside", "outside.c")):
        (directory / "compile_commands.json").write_text(
            json.dumps([{"directory": "src", "command": f"cc -c {source}", "file": source}]), encoding="utf-8"
        )
    swap = _LinkSwap(
        link=root / "compile_commands.json",
        inside_target=root / "build" / "compile_commands.json",
        outside_target=tmp_path / "outside" / "compile_commands.json",
    )
    swap.install(monkeypatch)
    server = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))
    server._solidlsp_settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"), project_data_path=str(root / ".serena"))
    server._custom_settings = {}

    server._prepare_compile_commands()

    assert swap.swapped
    rewritten = (root / ".serena" / "compile_commands.json").read_text(encoding="utf-8")
    assert "inside.c" in rewritten
    assert "outside.c" not in rewritten


def test_workspace_folder_is_the_path_its_walk_checked_when_a_component_is_swapped_for_a_link_after_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The folder admitted is the path the walk checked, not the outside directory a component swapped for a link after its
    check leads to; that the path itself is still followed by later uses is the stated check-then-use window (W7).
    """
    # a real directory of the repository, replaced by a link out of it right after the walk saw it is not a link
    root = tmp_path / "repo"
    (root / "inner").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    inner = os.path.join(os.path.realpath(root), "inner")
    original_islink = os.path.islink
    swapped: list[bool] = []

    def islink_then_swap(path: Any) -> bool:
        result = original_islink(path)
        if os.fspath(path) == inner and not result and not swapped:
            os.rmdir(inner)
            os.symlink(outside, inner, target_is_directory=True)
            swapped.append(True)
        return result

    monkeypatch.setattr(ls_config.os.path, "islink", islink_then_swap)

    folders = _not_following_config(LanguageServerId.PYTHON, workspace_folders=["inner"]).get_absolute_workspace_folders(str(root))

    assert swapped
    assert folders == [inner]


def test_workspace_folder_whose_missing_component_is_created_as_a_link_after_its_check_is_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `missing/sub`, where `missing` is created as a link out of the repository right after it was found missing
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside" / "sub").mkdir(parents=True)
    missing = os.path.join(os.path.realpath(root), "missing")
    original_exists = os.path.exists
    created: list[bool] = []

    def exists_then_create(path: Any) -> bool:
        result = original_exists(path)
        if os.fspath(path) == missing and not result and not created:
            os.symlink(tmp_path / "outside", missing, target_is_directory=True)
            created.append(True)
        return result

    monkeypatch.setattr(ls_config.os.path, "exists", exists_then_create)

    folders = _not_following_config(LanguageServerId.PYTHON, workspace_folders=["missing/sub"]).get_absolute_workspace_folders(str(root))

    assert created
    assert folders == []


def _clangd_with_database_linked_from_outside(tmp_path: Path) -> tuple[Any, Path]:
    """
    :return: an unstarted clangd server, not following links, whose repository's compile_commands.json is a link to a
        database outside the repository, and that database
    """
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    outside_db = tmp_path / "outside" / "compile_commands.json"
    outside_db.write_text(json.dumps([{"directory": "src", "command": "cc -c secret.c", "file": "secret.c"}]), encoding="utf-8")
    (root / "compile_commands.json").symlink_to(outside_db)
    server = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))
    server._solidlsp_settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"), project_data_path=str(root / ".serena"))
    server._custom_settings = {}
    return server, outside_db


def _assert_empty_directory_outside(directory: str, root: Path) -> None:
    assert os.path.isdir(directory)
    assert os.listdir(directory) == []
    assert os.path.commonpath([os.path.realpath(directory), os.path.realpath(root)]) != os.path.realpath(root)


@pytest.mark.parametrize("planted", ["none", "folder_link", "database_link"])
def test_clangd_is_launched_without_a_database_linked_from_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    """
    The clangd server is pointed at an empty temporary directory outside the repository, whatever the repository holds
    at the place of an earlier fixed folder (a link out of it, or a folder holding a link to the outside database).
    """
    server, outside_db = _clangd_with_database_linked_from_outside(tmp_path)
    root = tmp_path / "repo"
    planted_dir = root / ".serena" / "clangd-ignored-compile-commands"
    planted_dir.parent.mkdir(parents=True)
    if planted == "folder_link":
        planted_dir.symlink_to(outside_db.parent, target_is_directory=True)
    elif planted == "database_link":
        planted_dir.mkdir()
        (planted_dir / "compile_commands.json").symlink_to(outside_db)
    monkeypatch.setattr(SolidLanguageServer, "_create_process_launch_info", lambda _self: ProcessLaunchInfo(cmd=["clangd", "--log=error"]))

    cmd = server._create_process_launch_info().cmd

    assert cmd[0] == "clangd" and cmd[2:] == ["--log=error"]
    compile_commands_dir = cmd[1].removeprefix("--compile-commands-dir=")
    _assert_empty_directory_outside(compile_commands_dir, root)
    server.stop()
    assert not os.path.exists(compile_commands_dir)


def test_clangd_fingerprint_does_not_read_a_database_linked_from_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, outside_db = _clangd_with_database_linked_from_outside(tmp_path)
    opened: list[str] = []
    original_open = builtins.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.path.realpath(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)

    server._compile_commands_fingerprint()

    assert os.path.realpath(outside_db) not in opened


def test_clangd_fingerprint_of_a_database_linked_from_outside_the_repository_is_ignored(tmp_path: Path) -> None:
    server, _ = _clangd_with_database_linked_from_outside(tmp_path)

    assert server._compile_commands_fingerprint() == "ignored"


def test_clangd_fingerprint_hashes_the_whole_database_from_its_validated_path(tmp_path: Path) -> None:
    # a database linked from inside the repository, larger than one read chunk
    root = tmp_path / "repo"
    (root / "build").mkdir(parents=True)
    content = json.dumps([{"directory": "/abs", "command": "cc -c main.c", "file": "main.c"}]).encode() + b" " * (3 * 1024 * 1024)
    (root / "build" / "compile_commands.json").write_bytes(content)
    (root / "compile_commands.json").symlink_to(root / "build" / "compile_commands.json")
    server = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))

    fingerprint = server._compile_commands_fingerprint()

    assert fingerprint == hashlib.md5(content).hexdigest()
    (root / "build" / "compile_commands.json").write_bytes(content[:-1] + b"x")
    assert server._compile_commands_fingerprint() != fingerprint


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_fixed_path_files_that_are_fifos_are_treated_as_absent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("compile_commands.json", "build.zig", "app.json", "mix.exs"):
        os.mkfifo(root / name)

    clangd = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))
    clangd._custom_settings = {}
    zls = _create_unstarted_server(ZigLanguageServer, root, _not_following_config(LanguageServerId.ZIG))
    al = _create_unstarted_server(ALLanguageServer, root, _not_following_config(LanguageServerId.AL))
    elixir = _create_unstarted_server(ElixirTools, root, _not_following_config(LanguageServerId.ELIXIR))

    assert clangd._compile_commands_fingerprint() is None
    assert clangd._prepare_compile_commands() is None
    assert zls._find_build_zig() is None
    assert al._find_app_json() is None
    assert elixir._find_mix_exs() is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_clangd_compilation_database_that_is_a_fifo_fails_an_unreal_project_as_an_absent_one_does(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Game.uproject").write_text("{}\n", encoding="utf-8")
    os.mkfifo(root / "compile_commands.json")
    clangd = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))
    clangd._custom_settings = {}

    with pytest.raises(SolidLSPException, match="compile_commands.json"):
        clangd._prepare_compile_commands()


def test_clangd_ignored_database_folder_is_removed_when_the_server_is_discarded_without_stopping(tmp_path: Path) -> None:
    server, _ = _clangd_with_database_linked_from_outside(tmp_path)
    compile_commands_dir = server._prepare_compile_commands()
    assert compile_commands_dir is not None and os.path.isdir(compile_commands_dir)

    del server
    gc.collect()

    assert not os.path.exists(compile_commands_dir)


def test_clangd_database_linked_from_outside_fails_an_unreal_project_as_an_absent_one_does(tmp_path: Path) -> None:
    server, _ = _clangd_with_database_linked_from_outside(tmp_path)
    (tmp_path / "repo" / "Game.uproject").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SolidLSPException, match="compile_commands.json"):
        server._prepare_compile_commands()
    assert server._ignored_compile_commands_dir is None


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_clangd_database_that_is_a_dangling_link(tmp_path: Path, target: str) -> None:
    """
    A dangling link out of the repository is ignored (clangd gets the empty folder, and nothing is fingerprinted), as one
    whose target exists would be; a dangling link inside the repository has nothing to read, and counts as absent.
    """
    root = tmp_path / "repo"
    root.mkdir()
    missing_target = (tmp_path / "outside" if target == "outside" else root / "build") / "compile_commands.json"
    (root / "compile_commands.json").symlink_to(missing_target)
    server = _create_unstarted_server(ClangdLanguageServer, root, _not_following_config(LanguageServerId.CPP))
    server._custom_settings = {}

    compile_commands_dir = server._prepare_compile_commands()

    if target == "outside":
        assert compile_commands_dir is not None
        _assert_empty_directory_outside(compile_commands_dir, root)
        assert server._compile_commands_fingerprint() == "ignored"
        server._remove_ignored_compile_commands_dir()
    else:
        assert compile_commands_dir is None
        assert server._compile_commands_fingerprint() is None
