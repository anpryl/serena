# SPDX-License-Identifier: GPL-3.0-or-later
"""
The ``--follow-cwd`` router: an MCP server that holds no project and forwards each tool call to a child Serena
launched on the project root of the call's folder.
"""
