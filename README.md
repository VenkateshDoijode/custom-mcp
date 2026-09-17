# Zephyr-Jira MCP Server

A standalone, self-hosted [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes Jira and Zephyr Scale test-management operations as tools an AI assistant (Claude, Cursor, Windsurf or any MCP-compatible clien) can call directly — create test cases, link them to stories, audit test cycles, and generate coverage reports, all from natural language.

---

## Background

Organizations spend significant time creating and managing test assets across the software development lifecycle. Developers, QA engineers, and product teams routinely need to create test cases, document test steps, link tests to Jira stories, and organize them into test cycles. These activities are typically performed manually through the Jira and Zephyr UIs, which makes the process repetitive, time-consuming, and prone to inconsistency — especially during sprint planning, feature delivery, and release testing.

To address this, this standalone MCP server lets an AI assistant interact directly with Jira and Zephyr through natural language. Instead of navigating multiple screens and manually entering data, you describe what you want in plain English and the assistant performs the required actions.

## Problem Statement

There are now hundreds of community and vendor MCP servers available for Jira, Zephyr, and other QA/test-management tools. In practice, almost none of them are usable inside a bank, insurer, or other regulated enterprise environment, because they typically:

- **Route data through a third-party proxy or SaaS backend** that the security team has no visibility into or control over.
- **Risk data leakage** — test data, defect descriptions, and Jira issue content routinely contain confidential project, customer, or infrastructure details that cannot leave the corporate network.
- **Require broad, unscoped credentials** (personal API tokens pasted into config files, no secret rotation, no audit trail).
- **Fail enterprise security review** outright, because the vendor cannot show where the server runs, where the token is stored, or what egress the tool makes.

## Why Not the Official Atlassian MCP Server?

| Reason | Detail |
|---|---|
| **Cloud-only** | `mcp.atlassian.com` only connects to Atlassian Cloud instances (e.g. `yourcompany.atlassian.net`). |
| **Private/on-premise blocked** | Our Jira instance, `https://jira.yourcompany.net`, is a self-hosted, on-premise, corporate deployment. The cloud MCP endpoint cannot reach it. |
| **No Zephyr support** | Even on Cloud, the Atlassian MCP covers only native Jira/Confluence APIs. Zephyr is a third-party plugin with its own separate REST API that isn't exposed by the Atlassian MCP. |
| **No Zephyr API access** | The Atlassian MCP has no knowledge of Zephyr's test case, test step, test cycle, or execution endpoints. |

**Conclusion:** the official Atlassian MCP server is structurally incompatible with this setup — both because of on-premise hosting and because Zephyr is outside its scope. Hence, this custom MCP server.

## Design Principles

- **Standalone, self-hosted.** The server runs entirely inside your own network (or your own machine) as a local process. There is no external SaaS component, no vendor cloud, and no data ever leaves your environment unless your own Jira instance is reached.
- **Your Jira, your rules.** The Jira base URL is a configuration value you set yourself, e.g. `https://jira.yourcompany.net` — the server talks directly to your Jira/Zephyr instance over your existing network path (VPN, internal DNS, corporate proxy — whatever your organization already enforces). No requests go anywhere else.
- **No secrets in code or config files.** The Jira API token is never hardcoded and never committed to source control. It is resolved at runtime from **Azure Key Vault/HashiCorp Vault** (secret name: `JIRA-TOKEN`), so credential storage, rotation, and access policy stay fully owned by your organization's existing secrets-management infrastructure.
- **Least-surface footprint.** The server only implements the Jira/Zephyr operations it explicitly exposes as tools (see below) — there is no generic proxy, no arbitrary API pass-through, and no catch-all "run any REST call" tool.
- **Transparent and auditable.** Every tool call is logged locally (`zephyr_mcp.log`) with the acting user, action, and target issue/project — giving you a local audit trail independent of Jira's own logs.

## What It Does

The server wraps `zephyr_client.py` (a thin client over the Jira and Zephyr Scale REST APIs) and exposes each operation as an MCP tool. Once connected to an MCP client, an assistant can:

| Category | Tools |
|---|---|
| **Resolution** | Resolve project keys, issue keys, and issue types to their underlying Jira IDs |
| **Test case management** | Create, bulk-create, list, and delete Zephyr test cases; discover custom field IDs per project |
| **Test design** | Read and write test steps in Step-by-Step, BDD/Gherkin, or plain-text format |
| **Linking & collaboration** | Link tests to stories/bugs/epics; add comments to any Jira issue |
| **Test cycles & execution** | Fetch test cycles, execution results, and per-cycle pass/fail summaries; audit a cycle for missing attachments or unresolved statuses |
| **Coverage & reporting** | Report test coverage against stories/bugs in a project or sprint; generate aggregate pass/fail summaries |
| **Jira queries** | Fetch any issue by key; run arbitrary JQL searches |

## Architecture

```
MCP Client (Claude Desktop, etc.)
        │  stdio / streamable-http
        ▼
zephyr_mcp_server.py   (FastMCP — tool definitions, logging)
        │
        ▼
zephyr_client.py       (Jira + Zephyr Scale REST client)
        │
        ▼
Your Jira instance  —  https://jira.yourcompany.net
        │
        ▼
Azure Key Vault  —  resolves JIRA_TOKEN at runtime
```

## Prerequisites

- Python 3.10+
- Access to a Jira instance with Zephyr Scale (ATM) enabled
- An Azure Key Vault instance holding your Jira API token as a secret named `JIRA-TOKEN`, with the identity running this server granted `get`/`list` access to that secret
- An MCP-compatible client (e.g. Claude Desktop)

## Setup

1. **Clone and install dependencies**
   ```bash
   git clone https://github.com/VenkateshDoijode/custom-mcp.git
   cd custom-mcp
   pip install -r requirements.txt
   ```

2. **Configure `config.toml`** (create this file; it is intentionally not committed to the repo)
   ```toml
   [jira]
   base_url = "https://jira.yourcompany.net"
   username = "your-jira-username"

   [azure]
   key_vault_url = "https://your-keyvault-name.vault.azure.net/"
   ```

3. **Grant Key Vault access** to the identity that will run the server (managed identity, service principal, or your own Azure AD login via `az login`), scoped to read the `JIRA-TOKEN` secret only.

4. **Register the server with your MCP client** — see `claude_desktop_config.json` for a working example, and point it at the absolute path of `zephyr_mcp_server.py` on your machine.

5. **Run it**
   ```bash
   python zephyr_mcp_server.py          # stdio transport — default, for MCP clients
   python zephyr_mcp_server.py --http   # streamable-http transport on port 8000
   ```

## Security Posture

| Concern | How it's handled here |
|---|---|
| Credential storage | Jira token pulled from Azure Key Vault/HashiCorp Vault at runtime; never stored in code, config, or logs |
| Network exposure | Server runs locally/on-prem; only outbound calls are to your own Jira base URL |
| Data residency | No third-party proxy or SaaS relay — test and issue data never leaves your network boundary |
| Least privilege | Tools are scoped to specific Jira/Zephyr operations, not a general-purpose API proxy |
| Auditability | All tool invocations are logged with the acting user, action, and target key |
| Destructive actions | Operations like `delete_test_case` are explicit, named tools — never a hidden side effect of another call |

> This is a personal/team project, not an officially reviewed enterprise product. Before deploying it in a regulated environment, run it through your organization's standard security and architecture review — the design choices above are meant to make that review straightforward, not to replace it.

## Future Roadmap: AI Agent Layer

The current server exposes atomic, single-purpose tools — creating one test case, fetching one cycle's results, and so on. The next phase of this project is an **AI agent layer** on top of these tools, so an engineer can hand over a whole workflow instead of individual calls. For example: *"Generate test cases for this new story, link them, and audit last sprint's execution cycle for gaps."*

Advantages of moving from tool-calling to an agent layer:

- **Multi-step orchestration** — the agent chains resolution → creation → linking → reporting into one instruction, instead of the user (or a script) sequencing each MCP call by hand.
- **Context-aware test generation** — given a story or acceptance criteria, the agent can draft relevant test cases and steps directly, rather than requiring a human to write the summary/description for each one.
- **Proactive quality checks** — the agent can run coverage and cycle audits on a schedule or on every sprint close, flagging gaps before they reach a human reviewer.
- **Consistent output** — since the agent uses the same fixed set of tools, its actions stay within the same security boundary and audit trail as manual use, with no new attack surface introduced.
- **Lower ramp-up cost for the team** — SDETs interact in plain language ("cover this story with regression tests") instead of having to know Zephyr's field names, folder structure, or JQL syntax.

This layer is planned, not yet implemented — this repository currently provides the underlying, secure tool surface it will be built on.

## Repository Contents

| File | Purpose |
|---|---|
| `zephyr_mcp_server.py` | FastMCP server — registers all tools, handles logging and transport |
| `zephyr_client.py` | Jira + Zephyr Scale REST client used by the server |
| `claude_desktop_config.json` | Example MCP client configuration |
| `requirements.txt` | Python dependencies |

## Author

Venkateshwara Doijode — Test Automation Lead 
