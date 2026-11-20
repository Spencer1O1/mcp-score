# Installing and updating this fork

This repository is [Spencer1O1/mcp-score](https://github.com/Spencer1O1/mcp-score).
Its distribution `mcp-score-server`, import `mcp_score`, executable `mcp-score`,
and package version remain inherited from upstream. Select the fork by
repository and commit, not by the version number or a plain
`pip install mcp-score-server`. This guide does not publish a package or
automatically change an existing MCP client configuration.

## Provenance

The fork started from upstream
[`6e16e08ea16176f0a2311b15f3df8d7984c96257`](https://github.com/tskovlund/mcp-score/commit/6e16e08ea16176f0a2311b15f3df8d7984c96257)
(`chore(deps): update dependency ruff to v0.17.2 (#130)`). That is the base of
the first fork PR,
[#1: required live score capabilities](https://github.com/Spencer1O1/mcp-score/pull/1),
and the first parent of its merge
[`5f7ffa742493ae6ff86f1b21253d142a1f78dd7f`](https://github.com/Spencer1O1/mcp-score/commit/5f7ffa742493ae6ff86f1b21253d142a1f78dd7f).
This identifies the historical starting point, not continuous synchronization.

Thomas Skovlund Hansen's authorship and [MIT license](../LICENSE) are retained.
The [upstream repository](https://github.com/tskovlund/mcp-score),
[upstream releases](https://github.com/tskovlund/mcp-score/releases), and
[upstream PyPI project](https://pypi.org/project/mcp-score-server/) remain useful
references; they do not identify this fork's delivered source.

## Install a matching source revision

Install Python 3.14+ and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/Spencer1O1/mcp-score.git
cd mcp-score
uv sync --locked --no-dev
git rev-parse HEAD
```

Record the printed full commit. To reproduce a chosen revision, run
`git checkout <full-fork-commit>` before `uv sync --locked --no-dev`; substitute
the recorded commit. Use the same revision for the Python server and the plugin
copied to MuseScore. The lock file selects runtime dependencies; the source
commit selects this fork's implementation.

From the checkout, `uv run --no-sync mcp-score --help` checks that environment
without connecting to MuseScore. Run `uv run --no-sync mcp-score install-plugin`
on the MuseScore host to install its bundled plugin into the default folder.
For a customized plugin folder, copy the complete plugin directory from that
checkout into the configured folder as described below. The current CLI does
not accept a destination argument.

## Point the client at the selected environment

Use the executable's absolute path rather than another `mcp-score` on `PATH`.
POSIX checkout example:

```json
{
  "mcpServers": {
    "mcp-score": {
      "command": "/absolute/path/to/mcp-score/.venv/bin/mcp-score",
      "args": ["serve"]
    }
  }
}
```

On Windows, use `.venv\Scripts\mcp-score.exe`. Escape backslashes in JSON:

```json
{
  "mcpServers": {
    "mcp-score": {
      "command": "C:\\Users\\you\\mcp-score\\.venv\\Scripts\\mcp-score.exe",
      "args": ["serve"]
    }
  }
}
```

Replace the example path with the actual checkout. Restart the MCP server/client
after changing its environment. Listing tools requires no MuseScore connection.

## Windows MuseScore with a separate client host

Install the plugin on the **Windows MuseScore machine**, even when the Python
server is launched by a client elsewhere. Install the fork at the recorded
commit on both machines, or copy the complete `src/mcp_score/musescore/plugin/`
folder from that commit into MuseScore's plugin directory as `mcp-score-bridge/`.
Include every QML and JS file. The destination is usually
`%USERPROFILE%\Documents\MuseScore4\Plugins\`; use MuseScore's configured folder
if Documents is redirected or that setting was customized.

Keep the bridge connection local to its host and forward its port through an
existing SSH connection when needed. On the Python-server host, with SSH access
already configured on the Windows machine:

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 windows-user@windows-score-host
```

Keep the tunnel running and call `connect_to_musescore` with `host: "127.0.0.1"`
and `port: 8765` on the Python-server host. The MCP client still communicates
with its configured Python server over stdio. The handshake checks protocol
compatibility, not authentication. Installing the fork does not configure SSH,
create a tunnel, or start MuseScore.

## Update the server and plugin together

1. Close the running bridge plugin window in MuseScore.
2. Update the fork checkout to the chosen commit (or `git pull --ff-only` on a
   clean tracking branch), then run `uv sync --locked --no-dev`.
3. Run `uv run --no-sync mcp-score install-plugin` on the MuseScore host, or
   replace its complete plugin folder from the same commit.
4. Restart the configured MCP server and reopen the MuseScore plugin. Connect
   and inspect `get_musescore_bridge_info` for negotiated commands.

Package/protocol versions alone do not prove two checkouts are identical;
retain the commit recorded on both hosts. Updating Python does not replace a
running plugin. See [plugin setup](musescore-plugin.md) for activation/errors.

## Check the built artifact

Contributors can run `uv run python scripts/smoke_wheel.py`. This separate
delivery check builds a wheel, installs it and runtime dependencies into a
temporary environment, and launches isolated Python from a temporary directory.
It verifies source-file hashes in the installed package, resolves bundled
QML/JS imports, copies the plugin into an explicit temporary destination, loads
the guide, and checks server tool registration/output schemas and CLI help.
CI runs the same script in its wheel-smoke job.

This check may download build/runtime dependencies. It is separate from offline
pytest, does not launch a notation application, and does not change user
plugins/client profiles. Native QML execution is covered by the integration
workflow. Wheel smoke currently runs on Linux; the Python wheel is platform
independent.
