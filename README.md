# Zephyr Squad MCP Server (FastMCP)

The Zephyr Squad MCP Server is a standalone [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that enables AI-driven management of Zephyr test assets directly from an MCP client (Claude, Devin, Windsurf, etc.). It lets you create and manage test cases, test steps, test cycles, and Jira associations using natural language — eliminating manual CSV imports and repetitive clicking through Jira and Zephyr UIs.

It integrates with Jira and Zephyr while maintaining enterprise-grade security through system environment variable / Azure Key Vault authentication and existing Jira authorization controls.

---

## Business Problem

Organizations spend significant time creating and managing test assets across the software development lifecycle. Developers, QA engineers, and product teams often need to create test cases, document test steps, link tests to Jira stories, and organize them into test cycles. These activities are typically performed manually through the Jira and Zephyr UIs, making the process repetitive, time-consuming, and prone to inconsistencies — especially during sprint planning, feature delivery, and release testing.

To address this, this standalone MCP server lets an AI assistant interact directly with Jira and Zephyr through natural language. Instead of navigating multiple screens and manually entering data, you describe what you want in plain English and the assistant performs the required actions.

Typical activities covered:

- Creating test cases
- Defining test steps
- Linking tests to Jira requirements
- Organizing tests into execution cycles
- Maintaining traceability between stories and tests

## Why Not the Official Atlassian MCP Server?

| Reason | Detail |
|---|---|
| **Cloud-only** | `mcp.atlassian.com` only connects to Atlassian Cloud instances (e.g. `yourcompany.atlassian.net`). |
| **Private/on-premise blocked** | Our Jira `https://jira.yourcompany.net` is a self-hosted, on-premise, corporate instance. The cloud MCP endpoint cannot reach it. |
| **No Zephyr support** | Even on Cloud, the Atlassian MCP covers only native Jira/Confluence APIs. Zephyr is a third-party plugin with its own separate REST API that isn't exposed by the Atlassian MCP. |
| **No Zephyr API access** | The Atlassian MCP has no knowledge of Zephyr's test case, test step, test cycle, or execution endpoints. |

**Conclusion:** The official Atlassian MCP server is structurally incompatible with this setup — both because of on-premise hosting and because Zephyr is outside its scope. Hence, this custom MCP server.

---

## Architecture

```
┌─────────────────────┐
│   MCP Client          │  (Claude Desktop / Cascade / Devin, etc.)
└──────────┬───────────┘
           │ MCP tool calls (stdio or streamable-http)
┌──────────▼───────────┐
│ zephyr_mcp_server.py  │  FastMCP server — exposes every operation
│                       │  as an @mcp.tool(), does logging
└──────────┬───────────┘
           │ plain function calls
┌──────────▼───────────┐
│ zephyr_client.py      │  The ONLY place that makes HTTP calls.
│                       │  Wraps Jira REST API v2 + Zephyr REST API.
│                       │  Handles auth, retries, caching.
└──────────┬───────────┘
           │ HTTPS + Bearer token
┌──────────▼───────────┐
│  Jira / Zephyr        │  Self-hosted Jira instance + Zephyr plugin
└───────────────────────┘
```

- **`zephyr_client.py`** — HTTP client. Wraps the Jira Core REST API v2 (issue CRUD, search, linking) and the Zephyr test-management REST API (test cases, steps, cycles, executions). Handles session pooling, retry/backoff on 5xx errors, and result caching (permanent ID caches + TTL caches for volatile data).
- **`zephyr_mcp_server.py`** — Thin [FastMCP](https://github.com/jlowin/fastmcp) wrapper. Every public function in `zephyr_client.py` is exposed as an MCP tool with a docstring the AI client uses to decide when/how to call it. Also handles structured logging to `zephyr_mcp.log`.

---

## Available Tools

| Category | Tool | Description |
|---|---|---|
| Resolution | `get_project_id` | Resolve a Jira project key to its numeric project ID |
| | `get_issue_id` | Resolve a Jira issue key to its numeric issue ID |
| | `get_issue_type` | Get the Jira issue type for an issue key |
| Test Cases | `create_test_case` | Create a Zephyr test case (Jira issue of type `Test`) |
| | `create_test_plan` | Create a Zephyr test plan |
| | `get_test_case_field_ids` | Discover custom field IDs for Test issues in a project |
| | `get_test_cases` | List Test-type issues in a project |
| | `delete_test_case` | Permanently delete a test case |
| | `bulk_create_test_cases` | Create multiple test cases in one call |
| | `get_test_coverage` | Report Story/Bug/Task coverage by linked test cases |
| | `get_tests_for_issue` | Fetch test cases linked to a specific Jira issue |
| Test Steps | `display_test_design_steps` | Fetch the test script (Step-by-Step / BDD / Plain) |
| | `create_test_design_steps` | Create or replace a test script |
| Linking & Comments | `link_test_to_issue` | Create a Jira "Tests" link between a Test and a Story/Bug/Epic |
| | `add_comment` | Add a comment to any Jira issue |
| Test Cycles | `get_test_cycles_for_issue` | Fetch test cycles linked to a Jira issue |
| | `get_test_cycle_details` | Full details of a test cycle, including execution items |
| | `audit_test_cycle` | Flag missing attachments / unexecuted / failed / blocked steps |
| Execution | `get_execution_results` | Per-test status, executor, and execution date for a cycle |
| | `get_cycle_execution_summary` | Pass/fail/blocked counts and pass % for a cycle |
| Jira Issues | `get_issue` | Fetch full details of any Jira issue |
| | `search_issues` | Run any JQL query |
| Reporting | `generate_test_summary` | Aggregate latest execution status across a project |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

Create a `config.toml` in the project root:

```toml
[jira]
base_url = "https://jira.yourcompany.net"
username = "your-jira-username"

[azure]
key_vault_url = "https://your-vault.vault.azure.net"

[defaults]
test_reviewer = "your-jira-username"
folder = "/Regression"
app_mnemonic = "MYAPP"
test_type = "Regression Test"
automation_status = "To Be Automated"
```

Or use environment variables instead (`JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_TOKEN`, `AZURE_KEY_VAULT_URL`).

**Authentication:** the Jira Personal Access Token is read from Azure Key Vault (secret name `JIRA-TOKEN`) if `key_vault_url` is configured, falling back to the `JIRA_TOKEN` environment variable. Requires `az login` once on your machine for `DefaultAzureCredential` to work. `base_url` must be HTTPS — plain HTTP is rejected to avoid sending the bearer token unencrypted.

### 3. Register the MCP server

Copy `claude_desktop_config.json` to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or the equivalent config path for your MCP client, replacing `<PATH_OF_YOUR_PROJECT>` with the absolute path to this project folder. Restart the client and check its MCP server settings to confirm it connected.

### 4. Run standalone (optional)

```bash
python zephyr_mcp_server.py            # stdio transport (default, for MCP clients)
python zephyr_mcp_server.py --http     # streamable-http transport on port 8000
```

---

## Security Notes

- No hardcoded credentials — token resolution goes Key Vault → env var → explicit failure on a placeholder token.
- HTTPS enforced for `base_url`.
- Shared `requests.Session` with automatic retry/backoff on `500/502/503/504`.
- Destructive operations (`delete_test_case`) require an explicit, non-empty issue key and are logged.

## License

Internal tool — no license specified.
