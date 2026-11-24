# SPDX-License-Identifier: GPL-3.0-or-later

from collections.abc import Iterator
from pathlib import Path


def find_project_root(root: str | Path | None = None) -> str | None:
    """Find project root by walking up from CWD.

    Returns the nearest ancestor that is either an explicit Serena project
    (contains .serena/project.yml) or a git root (contains .git, which may be a
    directory or, for git worktrees and submodules, a pointer file). The nearest
    such directory wins; a .serena/project.yml at the same level takes priority
    over .git only because they resolve to the same directory.

    Walking up with a single pass (rather than searching all levels for
    .serena/project.yml first and only then for .git) ensures a git worktree
    nested under another Serena project resolves to the worktree itself, instead
    of being hijacked by the ancestor project's .serena/project.yml.

    :param root: If provided, constrains the search to this directory and below
                 (acts as a virtual filesystem root). Search stops at this boundary.
    :return: absolute path to project root or None if not suitable root is found
    """
    current = Path.cwd().resolve()
    boundary = Path(root).resolve() if root is not None else None

    def ancestors() -> Iterator[Path]:
        """Yield current directory and ancestors up to boundary."""
        yield current
        for parent in current.parents:
            yield parent
            if boundary is not None and parent == boundary:
                return

    # Single pass: the nearest project boundary wins, whether it is an explicit
    # Serena project (.serena/project.yml) or a git root (.git, a dir or a worktree
    # pointer file). This keeps a nested git worktree from being hijacked by an
    # ancestor's .serena/project.yml.
    for directory in ancestors():
        if (directory / ".serena" / "project.yml").is_file() or (directory / ".git").exists():
            return str(directory)

    return None
