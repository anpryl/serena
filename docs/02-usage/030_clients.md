# Connecting Your MCP Client

In the following, we provide general instructions on how to connect Serena to your MCP-enabled client,
as well as specific instructions for popular clients.

(clients-general-instructions)=
## General Instructions

In general, Serena can be used with any MCP-enabled client.
To connect Serena to your favourite client, simply

1. determine how to add a custom MCP server to your client (refer to the client's documentation).
2. add a new MCP server entry by specifying either
    * a [run command](start-mcp-server) that allows the client to start the MCP server in stdio mode as a subprocess, or
    * the URL of the HTTP/SSE endpoint, having started the [Serena MCP server in HTTP/SSE mode](streamable-http) beforehand.

Find concrete examples for popular clients below.

Depending on your needs, you might want to further customize Serena's behaviour by
* [adding command-line arguments](mcp-args)
* [adjusting configuration](050_configuration).

**Mode of Operation**.
Note that some clients have a per-workspace MCP configuration (e.g, VSCode and Claude Code),
while others have a global MCP configuration (e.g. Codex and Claude Desktop).

- In the per-workspace case, you typically want to start Serena with your workspace directory as the project directory 
  and never switch to a different project. This is achieved by specifying the
  `--project <path>` argument with a single-project [context](#contexts) (e.g. `ide` or `claude-code`).
- In the global configuration case, you must first activate the project you want to work on, which you can do by asking
  the LLM to do so (e.g., "Activate the current dir as project using serena"). In such settings, the `activate_project`
  tool is required.

**Tool Selection**.
While you may be able to turn off tools through your client's interface (e.g., in VSCode or Claude Desktop),
we recommend selecting your base tool set through Serena's configuration, as Serena's prompts automatically
adjust based on which tools are enabled/disabled.  
A key mechanism for this is to use the appropriate [context](#contexts) when starting Serena.

(clients-common-pitfalls)=
### Common Pitfalls

**Discoverability of the `serena` command**.
Your client may not find the `serena` CLI command, even if it is on your system PATH.
In this case, a workaround is to provide the full path to the `serena` executable.

**Serena's tools not being used**.
With some clients, you may experience that Serena's tools are not being used.
First, ask the agent to activate your project and read Serena's initial instructions. If you want to enforce
Serena usage more rigorously, optional hooks can provide reminders and other controls. Hooks are not generally
recommended as part of the standard setup; see the client-specific subsections below for configuration examples.

**Environment Variables**.
Some language servers may require additional environment variables to be set (e.g. F# on macOS with Homebrew),
which you may need to explicitly add to the MCP server configuration.
Note that for some clients (e.g. Claude Desktop), the spawned MCP server process may not inherit environment variables that
are only configured in your shell profile (e.g. `.bashrc`, `.zshrc`, etc.); they would need to be set system-wide instead.
An easy fix is to add them explicitly to the MCP server entry.
For example, in Claude Desktop and other clients, you can simply add an `env` key to the `serena`
object, e.g.

```
"env": {
    "DOTNET_ROOT": "/opt/homebrew/Cellar/dotnet/9.0.8/libexec"
}
```

## Copilot in JetBrains

Open the settings of your JetBrains IDE and go to Tools / GitHub Copilot / Model Context Protocol (MCP). Then click
on the Configure button. This will open your global `mcp.json` file, where you can add the following entry for Serena:

```json
{
    "servers": {
      "serena": {
        "type": "stdio",
        "command": "serena",
        "args": [
          "start-mcp-server",
          "--context=jb-copilot-plugin"
        ]
      }
    }
}
```

**Verification.**
Open Copilot, switch to Agent mode, and click on the configure tools button. You should see Serena's tools in the list and be able to start
the Serena server there (you do not generally have to start Serena in the future, Copilot will start the server by itself). If the server is shown as running, Copilot is successfully connected to Serena. Most models will understand how to use Serena's tools out of the box, but for some models you may have to prompt "Activate the current project with Serena and read initial instructions" in the beginning of the chat.

**Recommended Configuration**.
The `jb-copilot-plugin` context (see above) comes with our recommended subset of Serena's tools for Copilot in JetBrains IDEs. We also 
recommend *disabling* the following built-in tools for optimal performance: 
replace_string_in_file, apply_patch, list_dir, file_search, grep_search. Note that running subagents may not use MCP servers, consider deactivating the run_subagent tool as well.

Serena offers better alternatives to these basic tools. If you do prefer to use the built-in tools instead,
you should disable corresponding Serena tools instead to prevent context bloat.

We also recommend marking Serena's tools as approved so you don't have to manually approve them in agent sessions. 
You can do this in Tools / GitHub Copilot / Chat, where at the bottom you can click on the Configure button for MCP tool auto-approval.

## Claude Code

Serena is a great way to make Claude Code both more efficient and more powerful!
To set up the Serena MCP server for Claude Code, you can simply run this command: 

    serena setup claude-code

### Manual Setup

**Global Configuration**. To add the Serena MCP server for all your projects, use the user-level configuration of claude code and the `--project-from-cwd` flag:

```shell
claude mcp add --scope user serena -- serena start-mcp-server --context claude-code --project-from-cwd
```

**Per-Project Configuration.** Alternatively, to add Serena only for the current project in the current directory, 
use the command:

```shell
claude mcp add serena -- serena start-mcp-server --context claude-code --project "$(pwd)"
```

**Verification.**
Confirm that Claude Code is connected to Serena by running the `/mcp` command and by reconnecting, if necessary.
If Serena fails to start fast enough, you should set `MCP_TIMEOUT` to a sufficiently high value
(e.g. by adding `export MCP_TIMEOUT=60000` to your shell profile)

### Optional Hooks

Hooks let you enforce Serena usage more rigorously, for example by reminding the agent to use Serena's
symbolic tools during long sessions. They are optional and are not generally recommended for standard use.

:::{note}
Hooks are an **alpha feature**. Provide feedback via the [GitHub issue tracker](https://github.com/oraios/serena/issues) if you encounter any issues.
:::

Include only the hooks you want. Add the following to your Claude Code settings file
(`.claude/settings.json` in your project directory, or `~/.claude/settings.json` globally):

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks remind --client=claude-code"
                    }
                ]
            },
            {
                "matcher": "mcp__serena__*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks auto-approve --client=claude-code"
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks activate --client=claude-code"
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks cleanup --client=claude-code"
                    }
                ]
            }
        ]
    }
}
```

The hooks will:

- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  `grep` or `read_file` calls without using any Serena tools in between. Searches and source-file reads
  run via the `Bash` tool (as native Claude Code installations do) are counted as well.
- **`activate`**: Prompt the agent to activate the project and read Serena's instructions at session start.
- **`cleanup`**: Clean up hook session data when the session ends.
- **`auto-approve`**: Auto-approve Serena tool calls whenever Claude Code is in a permissive
  permission mode (`acceptEdits` or `auto`), so blanket approvals cover Serena's destructive
  tools (e.g. `replace_symbol_body`, `rename_symbol`) instead of prompting on every call.

For more details on Claude Code's hook system, see the
[Claude Code hooks documentation](https://code.claude.com/docs/en/hooks).

(claude-code-follow-cwd)=
### Following the Session's Folder (`--follow-cwd`)

With `--project-from-cwd`, Serena picks one project when Claude Code starts and keeps it, even after Claude Code
moves to another folder or worktree. With `--follow-cwd`, Serena starts a router that holds no project. The router
sends each tool call to a child Serena started on the project root of the call's folder. A call therefore behaves
as it would in a Serena freshly launched in that folder, with that project's configuration, trust, language servers,
memories and instructions.

**Setup.** Run:

```shell
serena setup claude-code --follow-cwd
```

The command does the following:

- If `claude mcp get serena` finds a server, it runs `claude mcp remove -s user serena`.
- It registers the router with
  `claude mcp add --scope user serena -- serena start-mcp-server --context=claude-code --follow-cwd`.
- It adds the hooks below to `~/.claude/settings.json`. Other keys and hooks stay as they are. An existing
  `serena-hooks activate` entry is replaced by `serena-hooks activate --follow-cwd`. The file is written atomically,
  and a second run changes nothing.
- It prints every change it made.

Restart Claude Code afterwards. To set it up by hand, register the router with the `claude mcp add` command above
and add these hooks to `~/.claude/settings.json`:

```json
{
    "hooks": {
        "PreToolUse": [
            {"matcher": "mcp__serena__.*", "hooks": [{"type": "command", "command": "serena-hooks check-cwd"}]},
            {"matcher": "Read|Bash", "hooks": [{"type": "command", "command": "serena-hooks code-guard"}]}
        ],
        "PostToolUse": [
            {"matcher": "mcp__serena__.*", "hooks": [{"type": "command", "command": "serena-hooks code-guard-record"}]}
        ],
        "SubagentStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": "serena-hooks brief-subagent"}]}
        ],
        "SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "serena-hooks activate --follow-cwd"}]},
            {"matcher": "clear|compact", "hooks": [{"type": "command", "command": "serena-hooks reorient"}]}
        ]
    }
}
```

`--follow-cwd` requires the stdio transport and cannot be combined with `--project`, `--project-from-cwd` or a
project argument. The router and every child run with `project_registry: false`, `read_only_config: true` and the
`tools` agent interface, whatever `serena_config.yml` says. So visited projects are not registered, and Serena does
not write `serena_config.yml`. Children run without the dashboard and the GUI log window.

**The `cwd` argument.** Every tool takes a required `cwd`, the caller's working directory as an absolute path.
The agent passes the folder it is in, and after a `cd`, the new folder.

- A call without `cwd`, with a relative `cwd` or with a `cwd` that is not a directory fails, and the error names
  the argument.
- The project is the nearest folder above `cwd` that holds `.serena/project.yml` or `.git`. A git worktree, even one
  nested in another project, is a project of its own.
- An absolute path argument selects the project it lies in, in place of `cwd`. A call whose absolute paths lie in
  different projects is refused.
- Relative paths in the other arguments are relative to the project root, not to `cwd`.
- Paths in results are relative to the project root when `cwd` lies inside the project, and absolute otherwise.
- The first result from each project carries that project's instructions. Results from a project other than the
  launch folder's begin with a `[serena project <name> at <root>]` line.
- Calls to different projects can run at the same time.

The router marks `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, `replace_symbol_body` and
`replace_content` to load upfront (`anthropic/alwaysLoad`). Every tool declares `anthropic/maxResultSizeChars` of
200,000, so Claude Code shows results up to that size inline. The router cuts a longer result and appends a notice
that names the arguments for a narrower call.

**Children.** The router starts the launch folder's child when Claude Code connects, if that folder is in a project.
Other children start on their project's first call. A child that no call has used for 60 minutes stops. When 8
children run, starting another one first stops the least recently used idle child. The next call to a stopped or
crashed child's project starts it again.

**The hooks.** Each hook is optional. Without them, every call still goes to the project of its `cwd`.

| Hook | Event and matcher | What it does | What it never does |
| --- | --- | --- | --- |
| `check-cwd` | PreToolUse, `mcp__serena__.*` | Adds a notice when the project of the call's `cwd` differs from the project of the shell's folder | Block or change the call |
| `code-guard` | PreToolUse, `Read\|Bash` | Denies the first `Read`, shell read (`cat`, `head`, `tail`, `sed`, `awk`, `less`, `more`, `bat`) or shell write (such as `>`, `tee` or `sed -i`) of a code file that Serena has not covered in this session. The reason names the Serena tools to use | Deny a search (`grep`, `rg`), a second attempt at the same file, a shell write that creates a file, or a file in a project whose child has not started in this session |
| `code-guard-record` | PostToolUse, `mcp__serena__.*` | Records the files named in a Serena call and in its result as covered | Change the result |
| `brief-subagent` | SubagentStart | Tells the subagent its folder and project, to pass that folder as `cwd`, and to call `initial_instructions` | Send the instructions themselves |
| `activate --follow-cwd` | SessionStart, `startup` | Tells the agent to pass `cwd` on every call and to call `initial_instructions` | Ask the agent to activate a project |
| `reorient` | SessionStart, `clear\|compact` | Names the project and its root, and tells the agent to call `initial_instructions` with that `cwd` | Send the instructions themselves |

A code file is a file whose extension one of the project's language servers handles. When a child starts, the
router writes that list to `<SERENA_HOME>/follow_cwd/`, and `code-guard` reads it. A project with the JetBrains
backend gets no list, so `code-guard` passes its files. `code-guard` and `code-guard-record` work together, so
install both or neither. Setup installs neither `remind`, which `code-guard` replaces, nor `auto-approve`. If you add
`auto-approve` by hand, note that in `auto` mode it allows Serena's edit tools without auto mode's own check.

**Trust.** A project inside a Claude Code working directory (the launch folder, or a folder added with `/add-dir`)
is trusted as a fresh launch there would be, so `trusted_project_path_patterns` decides. A project counts as inside
when its root lies inside a working directory or a working directory lies inside its root. A project outside
every working directory, which a call reaches by an absolute path or a `cwd` elsewhere (for example a sibling
worktree), runs in Serena's [untrusted mode](trusted-projects-config). Its `activation_command` and its
project-level `ls_specific_settings` are ignored. A setting that such a project needs, such as `ls_path`, must go in
the global `serena_config.yml`. The router reads the working directories from the client's MCP roots. Without
roots, only the launch folder counts. With `--untrusted` on the router, every child runs untrusted.

**One language server per language.** Claude Code's code-intelligence plugins start their own language server for
each language they support. For a language that Serena also serves, two servers then run, which costs memory and
gives two sources of diagnostics. Disable the plugin for each language Serena serves, or accept the second server.
Each child also has its own language servers, and two Claude Code sessions on one project start two children.

**Where it differs from a fresh launch.**

- **A wrong `cwd`.** A call goes to the project of the folder the agent declares. `check-cwd` reports a mismatch
  with the shell's folder, which is reliable in the main session. A subagent that ran `cd` has no second source to
  check against.
- **The tool list is the router's.** The router lists the tools of the `claude-code` context and the global
  configuration, so it lists the same tools in every project. A tool that one project excludes is still listed, and
  a call to it in that project fails. An optional tool that a project includes is not listed, unless the global
  configuration includes it as well.
- **Outside the working directories,** a project runs untrusted (see **Trust** above).
- **`.serena/` in a followed folder.** A child on a project without `.serena/project.yml` creates it, along with
  a cache, as a fresh launch does. In a git worktree, `git worktree remove` then needs `--force`.
- **`code-guard` covers only projects opened in this session.** The first `Read` in a project that has no child yet
  passes.

**Status line.** Claude Code passes no MCP state to a status line, so each router writes
`<SERENA_HOME>/follow_cwd/state/<router pid>.json` (`SERENA_HOME` defaults to `~/.serena`):

```json
{"pid": 4242, "launch_root": "/home/me/app", "children": [{"root": "/home/me/app", "name": "app", "state": "idle"}]}
```

`launch_root` is the project root of the folder the router started in, or that folder if it is in no project.
`state` is `starting`, `ready` (a call is running) or `idle`. The router rewrites the file when a child starts,
stops or changes state, and removes it on exit. A router that was killed leaves its file behind. This script, which
needs `jq`, prints the children of the routers that belong to the session. It keeps the files whose `launch_root`
is the session's launch folder (`workspace.project_dir`) or contains it, and whose process is still alive:

```bash
#!/bin/bash
project_dir=$(cd "$(jq -r '.workspace.project_dir')" 2>/dev/null && pwd -P) || exit 0
for file in "${SERENA_HOME:-$HOME/.serena}"/follow_cwd/state/*.json; do
    [ -f "$file" ] || continue
    pid=$(jq -r '.pid' "$file")
    launch_root=$(jq -r '.launch_root' "$file")
    kill -0 "$pid" 2>/dev/null || continue
    case "$project_dir/" in
        "$launch_root"/*) ;;
        *) continue ;;
    esac
    jq -r 'select(.children | length > 0) | "serena: " + ([.children[] | "\(.name) (\(.state))"] | join(", "))' "$file"
done
```

Save it as `~/.claude/serena-statusline.sh`, make it executable, and add it to `~/.claude/settings.json`:

```json
{"statusLine": {"type": "command", "command": "~/.claude/serena-statusline.sh"}}
```

Two sessions launched in the same project both match, so the script then prints a line for each router.

## DeepSeek Harness (DSH)

Connect Serena as an MCP server using the [general instructions](#clients-general-instructions).

### Optional Hooks

To enforce Serena usage more rigorously, DSH can run Serena's hooks through its Claude Code-compatible
hooks bridge. Install and enable
`@deepseek-ai/dsh-hooks-claude-code`, then configure the bridge with the path to the hooks file. For example,
add the following plugin entry to the DSH Cordis configuration (the exact file location depends on your DSH setup):

```yaml
- dsh-hooks-claude-code:
    # Use an absolute path; relative paths resolve against DSH's process launch directory.
    configPath: /absolute/path/to/.dsh/hooks.json
```

The bridge accepts either a bare event map or a settings file with a top-level `hooks` key. Create the file at the
absolute path used above with the following content:

```json
{
  "SessionStart": [{
    "matcher": "",
    "hooks": [{ "type": "command", "command": "serena-hooks activate --client dsh" }]
  }],
  "PreToolUse": [{
    "matcher": "",
    "hooks": [{ "type": "command", "command": "serena-hooks remind --client dsh" }]
  }],
  "Stop": [{
    "matcher": "",
    "hooks": [{ "type": "command", "command": "serena-hooks cleanup --client dsh" }]
  }]
}
```

The DSH bridge uses `SessionStart`, `PreToolUse`, and `Stop`; therefore Serena maps cleanup to `Stop` because
DSH does not currently expose a separate `SessionEnd` event. Serena's DSH output follows the same
`hookSpecificOutput` shape as Claude Code, so the bridge can consume `permissionDecision` and
`permissionDecisionReason` without a client-specific wrapper. The bridge supports deny/ask responses for
`PreToolUse`, but does not use Claude Code's `allow` response for pre-approval, so Serena's `auto-approve`
hook is intentionally not configured for DSH. Native `Bash` search/read commands are classified the same
way as Claude Code's Grep/Read tools (gated on the `Bash` tool name so unrelated MCP tools with a
`command` parameter are not counted).

## VSCode

You can add Serena to VSCode by running the MCP: Add Server command.
In that dialogue, select the Command (stdio) option. You can decide between installing it globally
or in the workspace (only for the currently open project), and the command you should enter depends on that choice.
(You will be asked to choose after entering the mcp run command.)

**Global.** (Recommended)
Enter `serena start-mcp-server --context=vscode`. Unfortunately, due to a [bug in VSCode](https://github.com/microsoft/vscode/issues/245905),
in this setting Serena won't be able to activate the project automatically. You will have to remember to prompt
"Activate the current dir as project using serena" at the start of each session.

**Workspace.**
Enter `serena start-mcp-server --context=vscode --project ${workspaceFolder}`. This will allow Serena to automatically activate the project,
with the downside that you will have to add Serena to each project you want to use it in.

In both cases, proceed to enter Serena as the name, then select either global or workspace.

**Verification.**
You should be able to see Serena in the tools overview in the AI Chat window.

(vscode-hooks)=
### Optional Hooks

If you want to enforce Serena usage more rigorously, you can configure hooks to remind the agent to use
Serena's symbolic tools and prompt project activation. These hooks are optional.

The hooks will:

- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  `grep` or `read_file` calls without using any Serena tools in between.
- **`activate`**: Prompt the agent to activate the project and read Serena's instructions at session start.
- **`cleanup`**: Clean up hook session data when the session ends.

To set this up, create the file `~/.copilot/hooks/serena-hooks.json` with the following content:

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "type": "command",
                "command": "serena-hooks remind --client=vscode"
            }
        ],
        "SessionStart": [
            {
                "type": "command",
                "command": "serena-hooks activate --client=vscode"
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": "serena-hooks cleanup --client=vscode"
            }
        ]
    }
}
```

The `SessionStart` hook also addresses the global configuration limitation mentioned above — it will
automatically prompt the agent to activate the project directory, so you no longer need to do this manually.

## Copilot CLI

Use the interactive `/mcp add` slash command, choose Serena as the name, STDIO as the server type, and
`serena start-mcp-server --context=copilot-cli --project-from-cwd` as command. Copilot CLI will immediately notify you
that Serena is running if everything is set up correctly or display an error otherwise.

### Optional Hooks

For stricter Serena usage, you can use the same [optional hooks as in VSCode](#vscode-hooks)
if Copilot CLI did not pick them up automatically.


## Codex (CLI and via ChatGPT App)

You can simply run `serena setup codex`.

Alternatively, you can manually add the following to `~/.codex/config.toml` (create the file if it does not exist):

```toml
[mcp_servers.serena]
startup_timeout_sec = 15
command = "serena"
args = ["start-mcp-server", "--project-from-cwd", "--context=codex"]
```

**Verification.**
Run the `/mcp` command and verify that Serena is connected.
The Codex app does not start a session in the project's directory, so when using the app, we recommend
asking Codex to "Activate the current dir as project using serena" at the start of each session (though Codex might
do this automatically).

### SessionStart Hook

For Codex users who consistently work with Serena, we recommend the `SessionStart` hook to bootstrap Serena: it prompts the agent to activate
its project and read Serena's instructions when a session starts or resumes. This handles the manual
activation step described above.

See the [Codex hooks documentation](https://developers.openai.com/codex/hooks) for details. Create
`~/.codex/hooks.json` with the following content, or merge this entry into your existing hooks configuration:

```json
{
    "hooks": {
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks activate --client=codex",
                        "statusMessage": "Activating Serena project",
                        "timeout": 5
                    }
                ]
            }
        ]
    }
}
```

### Optional Full Hook Configuration

If you want to enforce Serena usage more rigorously, the configuration below adds tool-use reminders,
reminder counter resets, plan-mode controls, and cleanup. These additional hooks are optional and are
not generally recommended for standard use. Use this configuration in place of the minimal configuration
above; it already includes the `SessionStart` hook.

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks remind --client=codex",
                        "statusMessage": "Checking Serena tool usage",
                        "timeout": 5
                    }
                ]
            },
            {
                "matcher": "^mcp__serena__",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks plan-guard --client=codex",
                        "statusMessage": "Checking Serena plan-mode permissions",
                        "timeout": 5
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "^mcp__serena__.*$",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks reset --client=codex"
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks activate --client=codex",
                        "statusMessage": "Activating Serena project",
                        "timeout": 5
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks plan-context --client=codex",
                        "statusMessage": "Applying Serena plan context",
                        "timeout": 5
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks cleanup --client=codex",
                        "statusMessage": "Cleaning up Serena hook state",
                        "timeout": 3
                    }
                ]
            }
        ]
    }
}
```

These timeout values are specific to Codex. Without an explicit timeout, Codex allows most hooks
to run for 600 seconds, so the five-second reminder and activation timeouts prevent a stuck hook
from blocking a `Bash` call or session startup for that entire default. `SessionEnd` instead
defaults to one second and supports at most three seconds, so the cleanup hook uses that maximum.
Codex reports timed-out commands as hook failures. Other clients may have different timeout
behaviour; in particular, the Grok cleanup example below intentionally remains at five seconds.

The `SessionEnd` cleanup hook requires Codex 0.145.0 or newer. Older Codex versions only support
`Stop` for cleanup, which currently has a [known compatibility issue](https://github.com/oraios/serena/issues/1533).
If you still configure it, replace `SessionEnd` with `Stop` in the example above. Configure cleanup
under exactly one of these events, never both: `Stop` runs after every turn, while `SessionEnd` runs
when Codex tears down the root thread.

Each Codex event maps to one Serena hook command:

| Codex event | Serena command | When it runs |
| --- | --- | --- |
| `PreToolUse` with matcher `Bash` | `serena-hooks remind` | Before a shell command, to detect drift from Serena's symbolic tools |
| `PreToolUse` with matcher `^mcp__serena__` | `serena-hooks plan-guard` | Before a Serena MCP tool call, to block edit-capable tools in plan mode |
| `SessionStart` with matcher `startup\|resume` | `serena-hooks activate` | When a session starts or resumes, to prompt project activation |
| `UserPromptSubmit` | `serena-hooks plan-context` | Before each prompt, to add read-only Serena guidance in plan mode |
| `SessionEnd` | `serena-hooks cleanup` | When Codex tears down the root thread, to clean up hook session data |

The distinct `statusMessage` values make the five kinds of hook activity distinguishable while
they run.

The reminder hook's `PreToolUse` matcher is intentionally restricted to `Bash`. It tracks
shell-based grep and code-file reads, so running it for every tool call is unnecessary. The separate
plan guard targets Serena MCP calls. While Codex reports `permission_mode: "plan"`, it blocks every
Serena tool marked as edit-capable, including memory-writing and shell-command tools, while leaving
read-only tools available. Both plan hooks evaluate the current event without retaining mode state,
so they become silent and editing is available again as soon as Codex leaves plan mode; Serena's MCP
server does not need to restart.

- **`activate`**: Prompt the agent to activate the current project and read Serena's instructions
  when a Codex session starts or resumes.
- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  code-search or code-file-read calls without using Serena tools in between.
- **`reset`**: Clear the reminder counters after a successful Serena symbolic tool call, so using
  Serena's tools starts a fresh count instead of leaving the prior grep/read streak in place.
- **`cleanup`**: Clean up hook session data when the session ends.

The `PreToolUse` matcher is intentionally restricted to `Bash`: the reminder hook tracks shell-based
grep and code-file reads, so running it for every tool call is unnecessary. That matcher never sees
`mcp__serena__*` tool names, though, so it cannot also perform the counter reset on Serena tool use
the way it does for clients whose `PreToolUse` hook observes every tool call. The separate `reset`
hook above, matched to `PostToolUse` on Serena's own tools, covers that case for Codex instead.

#### Diagnosing opaque Codex hook failures

Codex may report that a hook failed without identifying the configured command or showing enough
of its output to diagnose the cause. [Codex issue #27052](https://github.com/openai/codex/issues/27052)
tracks richer hook failure diagnostics. Use Codex's `/hooks` command first to inspect hook sources,
trust state, and enabled hooks.

While that issue remains open, an advanced local wrapper can add an inner timeout and capture a
bounded, redacted tail of stdout and stderr. Keep wrappers out of the standard setup: they add
another script and executable path, platform-specific timeout behaviour, log rotation and redaction
concerns, and another hook definition hash to review and trust. Such wrappers work around Codex's
diagnostic limitations; Serena does not require them.

## Grok

Serena provides native support for xAI's Grok Build CLI. To set up the Serena MCP server for Grok,
simply run:

    serena setup grok

### Manual Setup

**Global Configuration**. To add the Serena MCP server for all your projects, use Grok's user-level configuration and the `--project-from-cwd` flag:

```bash
grok mcp add --scope user serena -- serena start-mcp-server --context=grok --project-from-cwd
```

Alternatively, add the following to `~/.grok/config.toml`:

```toml
[mcp_servers.serena]
command = "serena"
args = ["start-mcp-server", "--project-from-cwd", "--context=grok"]
```

**Project-Level Configuration**. To add the Serena MCP server for a single project only:

```bash
grok mcp add --scope project serena -- serena start-mcp-server --context=grok --project "$(pwd)"
```

**Verification.**
Run `grok inspect` and verify that Serena is listed as an MCP server. You can also use Grok's `/mcps`
modal to inspect, refresh, enable, or disable configured MCP servers.

Grok can also load existing Claude Code MCP configuration. If you previously ran `serena setup claude-code`,
Serena may already appear in Grok, but it will use the `claude-code` context instead of the dedicated `grok` context.

### Optional Hooks

For stricter Serena usage, you can optionally configure Grok's lifecycle hooks; see Grok's bundled
hooks documentation for details. To enable Serena's hooks
for Grok globally, create `~/.grok/hooks/serena-hooks.json` with the following content. For a single project,
create `.grok/hooks/serena-hooks.json` in that project instead; note that Grok loads project-scoped hooks
only after you have trusted the project for hook execution (via Grok's `/hooks-trust` command).

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "grep|read_file|run_terminal_command",
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks remind --client=grok",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks cleanup --client=grok",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The hooks will:

- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  code-search or code-file-read calls without using Serena tools in between.
- **`cleanup`**: Clean up hook session data when the agent turn ends.

Grok ignores stdout from passive hooks such as `SessionStart`, so the Grok hook setup intentionally
uses only `PreToolUse` for reminders and `Stop` for cleanup.

## Claude Desktop

On Windows and macOS, there are official [Claude Desktop applications by Anthropic](https://claude.ai/download); for Linux, there is an [open-source
community version](https://github.com/aaddrick/claude-desktop-debian).

To configure MCP server settings, go to File / Settings / Developer / MCP Servers / Edit Config,
which will let you open the JSON file `claude_desktop_config.json`.

Add the `serena` MCP server configuration

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=desktop-app"
      ]
    }
  }
}
```

If your language server requires specific environment variables to be set (e.g. F# on macOS with Homebrew),
you can add them via an `env` key (see [above](#clients-common-pitfalls)).

**Verification.**
Once you have created the new MCP server entry, save the config and then restart Claude Desktop.

:::{attention}
Be sure to fully quit the Claude Desktop application via File / Exit, as regularly closing the application will just
minimize it.
:::

After restarting, you should see Serena's tools in your chat interface (notice the small hammer icon).

## Copilot CLI

In the interactive mode, you can call `/mcp add` from within the copilot CLI. There, use serena as name, 
STDIO as the server type, and `serena start-mcp-server --context=copilot-cli --project-from-cwd` as command.

Alternatively, add the following to `~/.copilot/mcp-config.json` (create the file if it does not exist):

```json
{
  "mcpServers": {
    "serena": {
      "type": "stdio",
      "command": "serena",
      "tools": [
        "*"
      ],
      "args": [
        "start-mcp-server",
        "--context=copilot-cli",
        "--project-from-cwd"
      ]
    }
  }
}
```

**Verification.**
Copilot should now show that Serena is running, though you may have to restart it.


## JetBrains Junie

For the Junie plugin in JetBrains IDEs you can add Serena either to the global configuration in `~/.junie/mcp/mcp.json` 
or to the project configuration in `<project>/.junie/mcp/mcp.json`. Important, don't add both!
In both cases the entry should be:


```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=junie",
        "--project-from-cwd"
      ]
    }
  }
}
```

With the global configuration, Serena will be available in all projects. However,
within the Junie plugin, projects will not be automatically activated in Serena. 
You may thus have to prompt 
Junie to "Activate the current project using serena's activation tool" at the start of each session (though some models are
smart enough to activate the project automatically).

With the project-scoped configuration, Serena will be available only in that project, and the project will automatically
be recognized as active by Serena.


## JetBrains AI Assistant

Go to Settings / Tools / AI Assistant / MCP and enter the following configuration:

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=jb-ai-assistant",
        "--project-from-cwd"
      ]
    }
  }
}
```

Like for Junie, you have the choice between the global and the project-scoped configuration, 
with the same trade-off.

## Antigravity

Add this configuration:

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=antigravity"
      ]
    }
  }
}
```

You will have to prompt Antigravity's agent to "Activate the current project using serena's activation tool" after starting Antigravity in the project directory (once in the first chat enough, all other chat sessions will continue using the same Serena session).


Unlike VSCode, Antigravity does not currently support including the working directory in the MCP configuration.
Also, the current client will be shown as `none` in Serena's dashboard (Antigravity currently does not fully support the MCP specifications). This is not a problem, all tools will work as expected.

## CodeBuddy

Serena provides native support for CodeBuddy, a CLI coding agent that shares a similar architecture with Claude Code.
To set up the Serena MCP server for CodeBuddy, simply run:

    serena setup codebuddy

### Manual Setup

**Global Configuration**. To add the Serena MCP server for all your projects, use the user-level configuration of CodeBuddy and the `--project-from-cwd` flag:

```bash
codebuddy mcp add --scope user serena -- serena start-mcp-server --context codebuddy --project-from-cwd
```

**Project-Level Configuration**. To add the Serena MCP server for a single project only:

```bash
codebuddy mcp add serena -- serena start-mcp-server --context codebuddy --project "$(pwd)"
```

Confirm that CodeBuddy is connected to Serena by running the `/mcp` command and reconnecting if necessary.

### Optional Hooks

CodeBuddy supports the same hook system as Claude Code. If you want to enforce Serena usage more rigorously, add the following to your CodeBuddy settings file (`.codebuddy/settings.json` in your project directory, or `~/.codebuddy/settings.json` globally):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks remind --client=codebuddy"
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks auto-approve --client=codebuddy"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks activate --client=codebuddy"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "serena-hooks cleanup --client=codebuddy"
          }
        ]
      }
    ]
  }
}
```

#### Hook Descriptions

- **`remind`**: Remind the agent to use Serena's tools instead of built-in `grep` and `read` tools.
- **`activate`**: Prompt the agent to activate the project at session start and read Serena's instructions.
- **`cleanup`**: Clean up hook session data when the session ends.
- **`auto-approve`**: Auto-approve Serena tool calls whenever CodeBuddy is in a permissive
  permission mode (`acceptEdits` or `auto`), so blanket approvals cover Serena's destructive
  tools (e.g. `replace_symbol_body`, `rename_symbol`) instead of prompting on every call.

## ZCode

Serena provides `serena-hooks` support for [ZCode](https://zcode.z.ai), Z.ai's coding agent harness. There is
currently no `serena setup zcode` command (unlike Claude Code, CodeBuddy, Codex and Grok, ZCode does not
document a CLI subcommand for registering an MCP server), so the MCP server has to be added manually.

### Manual Setup

Add Serena as an MCP server in ZCode's configuration (`~/.zcode/cli/config.json` for all workspaces, or
`.zcode/config.json` in a project directory for that project only):

```json
{
  "mcpServers": {
    "serena": {
      "enabled": true,
      "type": "http",
      "url": "http://127.0.0.1:9121/mcp"
    }
  }
}
```

This assumes Serena is already running in [HTTP/SSE mode](streamable-http) on the given port; start it with
`--context=zcode --project-from-cwd` for a single-project setup, or `--context=zcode` if you intend to
activate a project from within the chat.

### Optional Hooks

ZCode supports lifecycle hooks with the same event names as Claude Code (`SessionStart`, `PreToolUse`,
`Stop`, ...). If you want to enforce Serena usage more rigorously, add the following to your ZCode hooks configuration
(`~/.zcode/cli/config.json` globally, or `.zcode/config.json` for a single project), under
`hooks.events`, with `hooks.enabled` set to `true`:

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [
        { "hooks": [ { "type": "process", "command": "serena-hooks", "args": ["activate", "--client", "zcode"] } ] }
      ],
      "PreToolUse": [
        { "matcher": "Read|Bash", "hooks": [ { "type": "process", "command": "serena-hooks", "args": ["remind", "--client", "zcode"] } ] }
      ],
      "Stop": [
        { "hooks": [ { "type": "process", "command": "serena-hooks", "args": ["cleanup", "--client", "zcode"] } ] }
      ]
    }
  }
}
```

#### Hook Descriptions

- **`activate`**: Prompt the agent to activate the project at session start and read Serena's instructions.
- **`remind`**: Remind the agent to use Serena's tools instead of built-in read and search tools.
- **`cleanup`**: Clean up hook session data when the session ends.
- **`auto-approve`**: Auto-approve Serena tool calls whenever ZCode is in a permissive permission mode, so
  blanket approvals cover Serena's destructive tools (e.g. `replace_symbol_body`, `rename_symbol`) instead
  of prompting on every call. Wire it to `serena-hooks auto-approve --client=zcode` the same way as
  `remind` above; the exact matcher for restricting it to Serena's own tool calls has not been confirmed
  against a live ZCode session, so start with an unscoped matcher and narrow it if ZCode's permission
  prompts turn out to fire on non-Serena tools too.

## Other Clients

For other clients, follow the [general instructions](#clients-general-instructions) above to set up Serena as an MCP server.

### Terminal-Based Clients

There are many terminal-based coding assistants that support MCP servers, such as

 * [Gemini-CLI](https://github.com/google-gemini/gemini-cli), 
 * [Qwen3-Coder](https://github.com/QwenLM/Qwen3-Coder),
 * [rovodev](https://community.atlassian.com/forums/Rovo-for-Software-Teams-Beta/Introducing-Rovo-Dev-CLI-AI-Powered-Development-in-your-terminal/ba-p/3043623),
 * [OpenHands CLI](https://docs.all-hands.dev/usage/how-to/cli-mode),
 * [opencode](https://github.com/sst/opencode) and
 * [CodeBuddy-Code](https://www.codebuddy.cn/cli/).

They generally benefit from the symbolic tools provided by Serena. You might want to customize some aspects of Serena
by writing your own context, modes or prompts to adjust it to the client's respective internal capabilities (and your general workflow).

In most cases, the `ide` context is likely to be appropriate for such clients, i.e. add the arguments `--context ide` 
in order to reduce tool duplication.

### MCP-Enabled IDEs and Coding Clients (Cline, Roo-Code, Cursor, Windsurf, etc.)

Most of the popular existing coding assistants (e.g. IDE extensions) and AI-enabled IDEs themselves support connections
to MCP Servers. Serena generally boosts performance by providing efficient tools for symbolic operations.

We generally recommend using the `ide` context for these integrations by adding the arguments `--context ide` 
in order to reduce tool duplication.

### Local GUIs and Agent Frameworks

Over the last months, several technologies have emerged that allow you to run a local GUI client
and connect it to an MCP server. The respective applications will typically work with Serena out of the box.
Some of the leading open source GUI applications are

  * [Jan](https://jan.ai/docs/mcp), 
  * [OpenHands](https://github.com/All-Hands-AI/OpenHands/),
  * [OpenWebUI](https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/mcp) and 
  * [Agno](https://docs.agno.com/introduction/playground).

These applications allow combining Serena with almost any LLM (including locally running ones) 
and offer various other integrations.
