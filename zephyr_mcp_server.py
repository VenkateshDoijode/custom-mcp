"""
zephyr_mcp_server.py — FastMCP server for Jira & Zephyr Squad
================================================================
Author: Venkateshwara Doijode

Exposes all Zephyr/Jira operations from zephyr_client.py as MCP tools.

Run:
    python zephyr_mcp_server.py            # stdio transport (default, for MCP clients)
    python zephyr_mcp_server.py --http     # streamable-http transport on port 8000

Credentials:
    Configure jira.base_url and azure.key_vault_url in config.toml.
    JIRA_TOKEN is read from Azure Key Vault (secret name: JIRA-TOKEN).
"""
from __future__ import annotations
import logging
import os
import zephyr_client as zc
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "zephyr_mcp.log")),
        logging.StreamHandler(),
    ],
)
_log = logging.getLogger(__name__)
mcp = FastMCP("Zephyr Squad")

# ----------------Project / Issue Resolution ----------------

@mcp.tool()
def get_project_id(project_key: str) -> str:
    """Resolve a Jira project key (e.g. PG1) to its numeric project ID."""
    return zc.get_project_id(project_key)

@mcp.tool()
def get_issue_id(issue_key: str) -> str:
    """Resolve a Jira issue key (e.g. PG1-26982) to its numeric issue ID."""
    return zc.get_issue_id(issue_key)

# ----------------Test Cases ----------------

@mcp.tool()
def create_test_case(
    project_key: str,
    summary: str,
    description: str = "",
    priority: str = "",
    assignee: str = "",
    labels: list[str] = None,
    components: list[str] = None,
    owner: str = "",
    folder: str = "",
    test_reviewer: str = "",
    app_mnemonic: str = "",
    test_type: str = "Regression Test",
    automation_status: str = "To Be Automated",
) -> dict:
    """
    Create a Zephyr test case (Jira issue of type 'Test') under the given project.
    Sets Zephyr test case status to 'Approved' immediately after creation.
    Returns the created issue's key and id.

    priority        : e.g. 'High', 'Medium', 'Low', 'Critical'
    assignee        : Jira username or accountId
    labels          : list of label strings, e.g. ['regression', 'smoke']
    components      : list of component name strings, e.g. ['Login', 'API']
    owner           : Jira username for the Owner field
    folder          : Zephyr folder path
    test_reviewer   : Jira username for Test Reviewer (required in some projects)
    app_mnemonic    : App Mnemonic value, e.g. 'MYAPP'
    test_type       : e.g. 'Regression Test', 'Functional', 'Smoke' (defaults to 'Regression Test')
    automation_status : e.g. 'Manual', 'Automated', 'To Be Automated', 'Cannot Be Automated' (defaults to 'To Be Automated')
    """
    _log.info("create_test_case called: project=%s summary=%r user=%s", project_key, summary, zc._config().get("jira", {}).get("username") or os.environ.get("JIRA_USERNAME", "unknown"))
    result = zc.create_test_case(
        project_key, summary, description,
        priority=priority or None,
        assignee=assignee or None,
        labels=labels or None,
        components=components or None,
        owner=owner or None,
        folder=folder or None,
        test_reviewer=test_reviewer or None,
        app_mnemonic=app_mnemonic or None,
        test_type=test_type or "Regression Test",
        automation_status=automation_status or "To Be Automated",
    )
    _log.info("create_test_case succeeded: key=%s", result.get("key"))
    return result

@mcp.tool()
def create_test_plan(project_key: str, name: str, description: str = "") -> dict:
    """
    Create a Zephyr test plan (Jira issue of type 'Test Plan') under the given project.
    Returns the created issue's key and id.
    """
    return zc.create_test_plan(project_key, name, description)

@mcp.tool()
def get_test_case_field_ids(project_key: str) -> dict:
    """
    Return the Jira custom field ID mapping for Test issues in a project.
    Use this to discover exact field IDs (e.g. 'customfield_12345') for your Jira instance
    before using the custom field parameters in create_test_case.
    """
    return zc.get_test_case_field_ids(project_key)

@mcp.tool()
def get_test_cases(project_key: str, max_results: int = 50) -> list[dict]:
    """List Test-type issues in a Jira project (most recently created first)."""
    return zc.get_test_cases(project_key, max_results)

@mcp.tool()
def delete_test_case(issue_key: str) -> dict:
    """Permanently delete a test case. This action cannot be undone."""
    return zc.delete_test_case(issue_key)

@mcp.tool()
def bulk_create_test_cases(project_key: str, test_cases: list[dict]) -> list[dict]:
    """
    Create multiple test cases in one call.
    test_cases : list of dicts, each with keys:
                 'summary' (required), 'description' (optional), 'priority' (optional)
    Returns per-test creation status with key or error message.
    """
    return zc.bulk_create_test_cases(project_key, test_cases)

@mcp.tool()
def get_test_coverage(project_key: str, jql_filter: str = "") -> dict:
    """
    Report test coverage for Story/Bug/Task issues in a project or sprint.
    Checks which issues have at least one linked Test case.

    project_key : Jira project key, e.g. 'PG1'
    jql_filter  : optional extra JQL, e.g. 'sprint="Sprint 24"' or 'fixVersion="2.0"'

    Returns coverage_pct, covered/uncovered counts and issue key lists.
    """
    return zc.get_test_coverage(project_key, jql_filter)

@mcp.tool()
def get_tests_for_issue(issue_key: str) -> list[dict]:
    """
    Fetch all Zephyr Scale test cases linked to a specific Jira issue.
    Also returns the actual Jira issue type so callers can verify it matches expectations.
    """
    return zc.get_tests_for_issue(issue_key)

# ----------------Test Steps ----------------

@mcp.tool()
def display_test_design_steps(issue_key: str) -> dict:
    """
    Fetch the test script (design steps) for a Zephyr Scale test case.
    Returns type (Step-by-Step / BDD/Gherkin / Plain) and the steps or script text.
    """
    return zc.display_test_design_steps(issue_key)

@mcp.tool()
def create_test_design_steps(
    issue_key: str,
    script_type: str,
    steps: list[dict] = None,
    script_text: str = "",
) -> dict:
    """
    Create or replace the test script for a Zephyr Scale test case (ATM API — one call).
    script_type : 'step_by_step', 'bdd', or 'plain'
    steps       : for step_by_step — list of dicts with keys: 'step', 'data', 'result'
    script_text : for bdd or plain — raw Gherkin or plain text string
    """
    return zc.create_test_design_steps(
        issue_key,
        script_type,
        steps=steps or None,
        script_text=script_text,
    )

# ----------------Linking & Comments ----------------

@mcp.tool()
def link_test_to_issue(test_issue_key: str, target_issue_key: str) -> dict:
    """
    Create a Jira 'Tests' link between a Test issue and a story / bug / epic.
    """
    return zc.link_test_to_issue(test_issue_key, target_issue_key)

@mcp.tool()
def add_comment(issue_key: str, comment: str) -> dict:
    """
    Add a comment to any Jira issue (story, bug, epic, test case, etc.).
    Returns the created comment's id, author, and body.
    """
    return zc.add_comment(issue_key, comment)

# ----------------Test Cycles (Zephyr Scale ATM) ----------------

@mcp.tool()
def get_test_cycles_for_issue(issue_key: str) -> list[dict]:
    """
    Fetch all Zephyr Scale test cycles (test runs) linked to a specific Jira issue.
    Cycles are matched by issue link or by name convention (name == issue_key).
    """
    return zc.get_test_cycles_for_issue(issue_key)

@mcp.tool()
def get_test_cycle_details(cycle_key: str) -> dict:
    """
    Fetch full details of a Zephyr Scale test cycle including all execution items
    (test case key, status, executed_by, execution_date).
    """
    return zc.get_test_cycle_details(cycle_key)

@mcp.tool()
def audit_test_cycle(cycle_key: str) -> dict:
    """
    Audit a Zephyr Scale test cycle.
    Checks every executed test case for:
      - Missing attachments
      - Steps that are Not Executed / In Progress / Failed / Blocked / Deferred
    Returns a structured audit report with per-test flags and a flat issues list.
    """
    return zc.audit_test_cycle(cycle_key)


# ----------------Test Execution ----------------

@mcp.tool()
def get_execution_results(cycle_id: str, project_key: str, version_id: int = -1) -> list[dict]:
    """
    Get all test execution records for a test cycle.
    Returns per-test execution_id, status, executed_by, and executed_on.
    version_id = -1 means Unscheduled.
    """
    return zc.get_execution_results(cycle_id, project_key, version_id)

@mcp.tool()
def get_cycle_execution_summary(cycle_id: str, project_key: str, version_id: int = -1) -> dict:
    """
    Aggregate pass/fail/blocked counts for all tests in a cycle.
    Returns total, per-status counts, and overall pass percentage.
    """
    return zc.get_cycle_execution_summary(cycle_id, project_key, version_id)


# ----------------Jira Issues ----------------

@mcp.tool()
def get_issue(issue_key: str) -> dict:
    """Fetch full details of any Jira issue (summary, description, status, priority, assignee, etc.)."""
    return zc.get_issue(issue_key)

@mcp.tool()
def search_issues(jql: str, max_results: int = 50) -> list[dict]:
    """
    Run any JQL query and return matching issues.
    jql         : full JQL string, e.g. 'project=PG1 AND sprint="Sprint 24" AND issuetype=Story'
    max_results : max results to return (default 50)
    """
    return zc.search_issues(jql, max_results)

# ----------------Reporting ----------------

@mcp.tool()
def generate_test_summary(project_key: str, jql_filter: str = "") -> dict:
    """
    Aggregate latest execution status for all test cases in a project or sprint.
    jql_filter : optional JQL to narrow scope, e.g. 'sprint="Sprint 24"'
    Returns pass/fail/blocked counts and overall pass percentage.
    """
    return zc.generate_test_summary(project_key, jql_filter)

# ----------------Entry Point ----------------

if __name__ == "__main__":
    import sys
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
