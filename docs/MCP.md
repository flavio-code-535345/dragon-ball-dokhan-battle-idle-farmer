# Letting Claude use the farmer (MCP)

With `MCP_TOKEN` set, the farmer serves an [MCP](https://modelcontextprotocol.io) endpoint at
`/mcp/<MCP_TOKEN>` (Streamable HTTP, JSON answers, no sessions). An AI assistant connected to it can
look at the device and work with you: take screenshots, tap, swipe, press keys, crop and test
templates, read and edit routines, start and stop the farm and read its log. Coordinates are 0..1
fractions, like everywhere in the farmer.

| Tool | Does |
|---|---|
| `screenshot` | A fresh screenshot (with its `frame_id`) |
| `tap`, `swipe`, `key`, `game` | Input and launch/stop/restart the game; they return the screen afterwards |
| `find_templates` | Which templates the current screen shows, with scores and boxes |
| `list_templates`, `template_image`, `create_template`, `update_template`, `delete_template` | Templates (`create_template` crops from a screenshot's `frame_id`) |
| `list_routines`, `get_routine`, `save_routine` | Routines and their YAML |
| `farm_status`, `start_farm`, `stop_farm`, `logs` | The farm |

## Setting it up

1. Make a long random token, e.g. `openssl rand -hex 24`, and add `MCP_TOKEN=<token>` to the stack's
   environment variables in Portainer. Update the stack.
2. Make the endpoint reachable for the assistant:
   - **claude.ai / the Claude apps** (a custom connector) connect from Anthropic's servers, so the
     farmer needs a public HTTPS address, e.g. a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
     or the reverse proxy you already use for Home Assistant. Expose only the path `/mcp/` if your
     proxy can (the web UI then stays private).
   - **Claude Code or Claude Desktop on your network** can use the LAN address directly, e.g.
     `claude mcp add --transport http dokkan http://192.168.1.20:8080/mcp/<token>`.
3. In claude.ai: Settings → Connectors → Add custom connector, URL
   `https://your-domain/mcp/<token>`. No OAuth: the token in the URL is the password, so keep the
   URL secret and make a new token if it leaks.

The `/mcp/` path skips the web UI's Basic auth (MCP clients cannot do it) and answers 404 to a
wrong token. Whoever has the URL can control the device, like with the web UI's password.
