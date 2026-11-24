# Trusted Projects

- `trusted_project_path_patterns` matches project roots, not individual files.
- Trust enables specific features; it is not a sandbox.

Symbolic links:
- Under the untrusted symlink rules (untrusted projects, and worktree projects, see below)
  - symbolic links are not followed when walking project subtrees
  - explicitly requested targets that resolve to locations outside the project root are rejected
- Under the trusted symlink rules (`are_symlinks_trusted`), links (that aren't ignored) are followed and can redirect
  to arbitrary locations
- The symlink rules are decided by `Project.are_symlinks_trusted`, not `is_trusted`: projects for calls routed to a
  git worktree (`route_worktree_paths`) get the untrusted symlink rules while keeping their trust for everything
  else (`ls_specific_settings`, project-local language servers). SolidLSP receives it as
  `LanguageServerConfig.follow_symlinks`, beside `is_project_trusted` (unchanged)
- SolidLSP walks guarded by `LanguageServerConfig.is_following_symlinks()` (links followed only where it holds):
  - configured (indexed and additional) workspace folders (`LanguageServerConfig.get_absolute_*workspace_folders`):
    resolved component by component, one passing through a link that lies inside the root and leads out of it is
    left out; links leading back inside the root, links lying outside it and folders named plainly outside it are kept;
    a component missing when checked leaves the folder out; the folder admitted is the path that one pass checked, never
    a second resolution (later uses still resolve it, so an entry swapped after its check is W7's window)
  - the base symbol-tree scan (per entry below the start)
  - via `SolidLanguageServer._is_reached_through_unfollowed_link`: AL's symbol tree (below its start), the
    Vue/Svelte/Astro companion indexing, the Angular and TypeScript seed files (inside the repository; additional
    workspace folders outside it are not checked), Clojure's project descriptors
  - fixed-path startup files whose content is sent in a `didOpen` or rewritten, via
    `SolidLanguageServer._resolve_fixed_path_file` (left out only when the real path lies outside the root,
    recognised by identity through `ls_config.RealRoot`; a link staying inside is used; the file is read from the
    validated real path, `FixedPathFile.real_path`, and named by its path; `_resolve_readable_fixed_path_file` also
    treats a file that is not a regular one, e.g. a FIFO, as absent): Elixir's `mix.exs`, ZLS's `build.zig`, AL's
    `app.json`, clangd's `compile_commands.json` (found with `lexists`, so a dangling link out of the repository is
    rejected too; a rejected one is ignored: neither read, fingerprinted nor rewritten, an Unreal Engine project fails
    startup as with a missing one, and otherwise clangd gets `--compile-commands-dir` set to a fresh empty
    `tempfile.mkdtemp` directory outside the repository, removed once when the server is stopped, collected or at
    exit, so it does not fall back to the linked file; an accepted regular one is fingerprinted from its validated
    path, the whole file in bounded chunks)
  - companion servers: the Vue, Svelte and Astro companion TypeScript servers get this server's
    `is_following_symlinks()` as their `follow_symlinks`; Angular's copies this server's configuration
- Unguarded reads and walks (limits); in each, only a path or a derived fact (a version, a hash, a yes/no) leaves
  Serena, and the language server reads the files itself:
  - paths: msl's `.mrc` reads (run by that process), Scala's build roots (directory links followed as Metals does),
    C#'s, VB.NET's and OmniSharp's `breadth_first_file_scan` (solution and project paths), Haxe's `.hxml`
  - derived facts: Scala's `project/build.properties` (`sbt.version` present), Godot's `project.godot` (config
    version), Ruby LSP's and Solargraph's `Gemfile`/`Gemfile.lock` (gem present), Ruby LSP's `.ruby-version` (the
    version, in the launch command), GraphQL's config (a hash), Angular's `package.json` probe (`workspaces` present),
    Solidity's `**/*.sol` glob (a count, for the indexing wait), `is_unreal_engine_project`'s `*.uproject` glob (yes/no)
  - the language servers' own discovery: clangd's own database search (ancestors and `build/`) when Serena passes no
    `--compile-commands-dir`; the ZLS, AL and Elixir server processes finding a root `build.zig`, `app.json` or
    `mix.exs` themselves after Serena left it out (ZLS's build-on-save runs `zig build`)
- A server walk whose files are opened or read should use the helper; a file read at a fixed path and sent in a
  `didOpen`, or rewritten, should use `_resolve_readable_fixed_path_file` (or `_resolve_fixed_path_file`) and read
  `real_path`
- Writes Serena itself makes for routed calls are also checked by the worktree project's write guard against the
  worktree (or its Serena data folder). Stated limits: the check-then-write window (W7; likewise a path component
  swapped after a validated workspace folder or startup file is opened), hard links, some build-time
  writes, and writes the language-server processes make themselves
- Ignore rules match unresolved paths, not the resolved targets of symbolic links.
- Containment checks: Use `Project.validate_relative_path` in Serena's APIs;
  keep trust validation separate from ignore matching (which must be as fast as possible).

Other restrictions in untrusted projects:
- Do not run `activation_command`.
- Ignore project-level `ls_specific_settings`; global settings still apply.
- Language servers should not run project-local binaries (e.g. `tsserver` will use the version
  managed by Serena instead of a locally provided version in `node_modules`)
