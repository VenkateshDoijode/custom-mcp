"""
zephyr_client.py — HTTP Client for Jira & Zephyr Scale REST APIs
================================================================
Author: Venkateshwara Doijode

This module is the ONLY place that makes HTTP calls.
It wraps two REST APIs:

    1. Jira Core REST API v2  →  {JIRA_BASE_URL}/rest/api/2/
       Used for: creating issues, searching (JQL), resolving IDs, linking issues

    2. Zephyr Scale API (ATM)  →  {JIRA_BASE_URL}/rest/atm/1.0/
       Used for: test cases, test steps, test cycles, test executions

Authentication:
    All requests use a Jira Personal Access Token (PAT) sent as:
        Authorization: Bearer <token>

Retry:
    All requests auto-retry up to 3 times on 500/502/503/504 server errors
    with exponential backoff (0.5s → 1s → 2s).

Configuration:
    All settings are read from config.toml (config loader, falls back to env vars).
    JIRA_TOKEN is read exclusively from Azure Key Vault (secret name: JIRA-TOKEN).
"""
import os
import time
import tomllib
from typing import Any, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------Config Loader ---------------------------------

_CONFIG: dict = {}

def _config() -> dict:
    global _CONFIG
    if not _CONFIG:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
        if os.path.exists(config_path):
            with open(config_path, "rb") as f:
                _CONFIG = tomllib.load(f)
    return _CONFIG

# ----------------Credential Helpers ---------------------------------

def _base_url() -> str:
    return (_config().get("jira", {}).get("base_url") or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")

def _token() -> str:
    # Returns the Jira PAT — from Azure Key Vault if configured, else env var JIRA_TOKEN.
    # Result is cached for _TOKEN_TTL seconds to avoid a Key Vault round-trip on every call.
    global _TOKEN_CACHE
    if _TOKEN_CACHE and (time.time() - _TOKEN_CACHE[0]) < _TOKEN_TTL:
        return _TOKEN_CACHE[1]
    value = _get_secret("JIRA-TOKEN", "JIRA_TOKEN")
    _TOKEN_CACHE = (time.time(), value)
    return value

def _jira_api_base() -> str:
    # Full base URL for standard Jira REST API v2 calls
    return f"{_base_url()}/rest/api/2"

_SESSION: requests.Session | None = None          # singleton — reused across all tool calls
_TOKEN_CACHE: tuple[float, str] | None = None     # (fetched_at, token_value) — refreshed every hour
_TOKEN_TTL = 3600                                 # seconds before re-fetching the token from Azure Key Vault
_PLACEHOLDER_TOKEN = "your-personal-access-token-here"
_KV_CLIENT = None                                 # singleton Azure Key Vault SecretClient

def _get_secret(secret_name: str, env_fallback: str) -> str:
    """Read a secret from Azure Key Vault if configured, else fall back to env var.
    Azure Key Vault secret names use hyphens (e.g. JIRA-TOKEN), not underscores.
    Authentication uses DefaultAzureCredential — run `az login` once on your machine.
    """
    vault_url = (_config().get("azure", {}).get("key_vault_url") or os.environ.get("AZURE_KEY_VAULT_URL", "")).rstrip("/")
    if vault_url:
        global _KV_CLIENT
        if _KV_CLIENT is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
                _KV_CLIENT = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
            except ImportError:
                raise EnvironmentError(
                    "azure-keyvault-secrets and azure-identity packages are required. "
                    "Run: pip install azure-keyvault-secrets azure-identity"
                )
        try:
            return _KV_CLIENT.get_secret(secret_name).value
        except Exception as e:
            raise EnvironmentError(f"Failed to read secret '{secret_name}' from Azure Key Vault ({vault_url}): {e}")
    return os.environ.get(env_fallback, "")

def _validate_env() -> None:
    if not _base_url():
        raise EnvironmentError("jira.base_url is not set in config.toml (or JIRA_BASE_URL env var).")
    if _base_url().startswith("http://"):
        raise EnvironmentError(
            "jira.base_url must use HTTPS (not HTTP). "
            "Sending a Bearer token over plain HTTP exposes credentials."
        )
    token = _token()   # reads from Azure Key Vault if configured, else JIRA_TOKEN env var
    if not token:
        raise EnvironmentError(
            "JIRA token not found. Set JIRA_TOKEN in environment variables "
            "or configure AZURE_KEY_VAULT_URL to read from Azure Key Vault."
        )
    if token == _PLACEHOLDER_TOKEN:
        raise EnvironmentError(
            "JIRA_TOKEN is still set to the placeholder value. "
            "Replace it with your actual Jira Personal Access Token."
        )

def _session() -> requests.Session:
    # Returns the shared singleton Session, creating it on first call.
    # Reusing the session enables HTTP keep-alive and connection pooling,
    # which eliminates TLS handshake overhead on every request.
    # If the token has been refreshed (rotated PAT), the Authorization header is updated in-place.
    global _SESSION
    current_token = _token()   # honours TTL cache; fetches from Key Vault only when stale
    if _SESSION is None:
        _validate_env()   # ensures base_url and token are set before creating the session
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {current_token}",   # Jira Personal Access Token
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=10)
        session.mount("https://", adapter)   # apply retry + pool for HTTPS
        session.mount("http://", adapter)    # apply retry + pool for HTTP
        _SESSION = session
    else:
        expected_auth = f"Bearer {current_token}"
        if _SESSION.headers.get("Authorization") != expected_auth:   # token was rotated
            _SESSION.headers["Authorization"] = expected_auth
    return _SESSION

# ----------------Project Helpers ---------------------------------

_PROJECT_ID_CACHE: dict[str, str] = {}   # project_key → numeric id cache (permanent)
_FIELD_ID_CACHE:   dict[str, dict[str, str]] = {}   # "project:issuetype" → {field_name → field_id}
_ISSUE_ID_CACHE:   dict[str, str] = {}   # issue_key → numeric id cache (permanent)

# TTL-based result caches — stores (timestamp, result) tuples
# Keys are the function arguments; values expire after TTL seconds.
_TTL_SHORT  = 60    # 60s — live execution/cycle data that can change frequently
_TTL_MEDIUM = 300   # 5min — test case lists, steps (change less often)
_CACHE: dict[str, tuple[float, Any]] = {}

def _cache_get(key: str, ttl: int) -> Any:
    # Returns cached value if it exists and hasn't expired, else None.
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None

def _cache_set(key: str, value: Any) -> None:
    # Stores a value in the cache with the current timestamp.
    _CACHE[key] = (time.time(), value)

def _cache_invalidate(key: str) -> None:
    # Removes a single cache entry (used after write operations).
    _CACHE.pop(key, None)

def get_project_id(project_key: str) -> str:
    """Resolve a Jira project key (e.g. PG1) to its numeric project ID."""
    # Jira API accepts numeric project ID in issue creation payloads.
    # This helper converts e.g. 'PG1' → '10234'
    if project_key not in _PROJECT_ID_CACHE:   # skip HTTP call if already resolved
        r = _session().get(
            f"{_jira_api_base()}/project/{project_key}",
            timeout=30
        )
        r.raise_for_status()
        _PROJECT_ID_CACHE[project_key] = str(r.json()["id"])
    return _PROJECT_ID_CACHE[project_key]   # return as string for consistent use in payloads

_ISSUE_META_CACHE: dict[str, dict] = {}   # issue_key → {id, type, summary} (permanent)

def _get_issue_meta(issue_key: str) -> dict:
    # Fetches and caches full issue metadata (id, type, summary) in one HTTP call.
    # Shared by get_issue_id and get_issue_type to avoid duplicate requests.
    if issue_key not in _ISSUE_META_CACHE:
        r = _session().get(
            f"{_jira_api_base()}/issue/{issue_key}",
            params={"fields": "issuetype,summary"},
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        _ISSUE_META_CACHE[issue_key] = {
            "id":      str(data["id"]),
            "type":    data["fields"]["issuetype"]["name"],
            "summary": data["fields"]["summary"],
        }
        _ISSUE_ID_CACHE[issue_key] = _ISSUE_META_CACHE[issue_key]["id"]   # keep id cache in sync
    return _ISSUE_META_CACHE[issue_key]

def get_issue_id(issue_key: str) -> str:
    """Resolve a Jira issue key (e.g. PG1-26982) to its numeric issue ID."""
    # Jira issueLink API requires numeric IDs, not key strings.
    if issue_key in _ISSUE_ID_CACHE:
        return _ISSUE_ID_CACHE[issue_key]
    return _get_issue_meta(issue_key)["id"]

def get_issue_type(issue_key: str) -> str:
    """Return the Jira issue type for a given issue key, e.g. 'Story', 'Bug', 'Test'."""
    return _get_issue_meta(issue_key)["type"]

def _get_field_id_map(project_key: str, issue_type: str = "Test") -> dict[str, str]:
    # Returns a mapping of {field_name → field_id} for a given project + issue type.
    # Uses the Jira create metadata endpoint, which lists every field (including custom
    # fields) that can be set when creating an issue of that type.
    # Results are cached permanently per project+issuetype — field IDs don't change.
    cache_key = f"{project_key}:{issue_type}"
    if cache_key in _FIELD_ID_CACHE:
        return _FIELD_ID_CACHE[cache_key]
    r = _session().get(
        f"{_jira_api_base()}/issue/createmeta",
        params={
            "projectKeys":    project_key,
            "issuetypeNames": issue_type,
            "expand":         "projects.issuetypes.fields",
        },
        timeout=30
    )
    r.raise_for_status()
    field_map: dict[str, str] = {}
    for project in r.json().get("projects", []):
        for it in project.get("issuetypes", []):
            for fid, finfo in it.get("fields", {}).items():
                name = finfo.get("name", "")
                if name:
                    field_map[name] = fid
    _FIELD_ID_CACHE[cache_key] = field_map
    return field_map

def get_test_case_field_ids(project_key: str) -> dict[str, str]:
    """
    Return the Jira field ID mapping for Test issues in a project.
    Custom fields have IDs like 'customfield_12345'.
    Use this to discover the exact field IDs for your Jira instance before
    using the custom field parameters in create_test_case.
    """
    return _get_field_id_map(project_key, "Test")

# ----------------Test Case Management ---------------------------------

def create_test_case(
    project_key: str,
    summary: str,
    description: str = "",
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[list[str]] = None,
    components: Optional[list[str]] = None,
    owner: Optional[str] = None,
    folder: Optional[str] = None,
    test_reviewer: Optional[str] = None,   # defaults to JIRA_USERNAME env var if not set
    app_mnemonic: Optional[str] = None,
    test_type: str = "Regression Test",
    automation_status: str = "To Be Automated",
) -> dict:
    """
    Create a Zephyr test case (Jira issue of type 'Test') under the given project.
    Immediately sets the Zephyr test case status to 'Approved' after creation.
    Returns the created issue's key and id.
    """
    # --- Input validation ---
    if not summary or not summary.strip():
        raise ValueError("summary is required and cannot be empty.")
    if len(summary) > 255:
        raise ValueError(f"summary exceeds Jira's 255-character limit ({len(summary)} chars).")
    if not project_key or not project_key.strip():
        raise ValueError("project_key is required and cannot be empty.")
    _validate_env()

    project_id = get_project_id(project_key)   # Jira API needs numeric project ID
    fields: dict = {
        "project":     {"id": project_id},
        "summary":     summary.strip(),
        "description": description or "",   # guard against explicit None
        "issuetype":   {"name": "Test"},    # 'Test' is the Zephyr Scale issue type
    }

    # --- Defaults from config.toml, then env var fallback ---
    cfg_jira     = _config().get("jira", {})
    cfg_defaults = _config().get("defaults", {})
    jira_username = cfg_jira.get("username") or os.environ.get("JIRA_USERNAME")
    if not assignee:
        assignee = jira_username
    if not test_reviewer:
        test_reviewer = cfg_defaults.get("test_reviewer") or jira_username
    if not folder:
        folder = cfg_defaults.get("folder")
    if not app_mnemonic:
        app_mnemonic = cfg_defaults.get("app_mnemonic")
    if not test_type:
        test_type = cfg_defaults.get("test_type") or "Regression Test"
    if not automation_status:
        automation_status = cfg_defaults.get("automation_status") or "To Be Automated"


    if not assignee:
        raise EnvironmentError("jira.username is not set in config.toml (or JIRA_USERNAME env var).")
    if not test_reviewer:
        raise EnvironmentError("defaults.test_reviewer is not set in config.toml.")

    if labels is not None and not isinstance(labels, list):
        raise ValueError(f"labels must be a list of strings, got {type(labels).__name__}.")
    if labels:
        bad_labels = [l for l in labels if " " in str(l)]
        if bad_labels:
            raise ValueError(f"Jira labels cannot contain spaces: {bad_labels}")
    if components is not None and not isinstance(components, list):
        raise ValueError(f"components must be a list of strings, got {type(components).__name__}.")

    if priority:
        fields["priority"] = {"name": priority}
    fields["assignee"] = {"name": assignee}   # always set — guaranteed non-None above
    if labels:
        fields["labels"] = labels
    if components:
        fields["components"] = [{"name": c} for c in components]

    # --- Custom fields — IDs auto-discovered from Jira, cached for the session ---
    fmap = _get_field_id_map(project_key, "Test")   # uses _FIELD_ID_CACHE; API called only once
    if not fmap:
        raise RuntimeError(
            f"Could not discover custom field IDs for project '{project_key}'. "
            f"Check that the JIRA_BASE_URL is correct and the project key exists."
        )
    _custom = {
        "Owner":             (owner,             lambda v: {"name": v}),
        "Folder":            (folder,            lambda v: v),
        "Test Reviewer":     (test_reviewer,     lambda v: {"name": v}),
        "App Mnemonic":      (app_mnemonic,      lambda v: {"value": v}),
        "Test Type":         (test_type,         lambda v: {"value": v}),
        "Automation Status": (automation_status, lambda v: {"value": v}),
    }
    for field_name, (value, formatter) in _custom.items():
        field_id = fmap.get(field_name)
        if value and field_id:
            fields[field_id] = formatter(value)

    payload = {"fields": fields}
    r = _session().post(
        f"{_jira_api_base()}/issue",
        json=payload, timeout=30
    )
    if not r.ok:
        try:
            jira_errors = r.json().get("errors", {})
            jira_msgs   = r.json().get("errorMessages", [])
        except Exception:
            jira_errors, jira_msgs = {}, []
        raise RuntimeError(
            f"Jira rejected test case creation [{r.status_code}]: "
            f"errors={jira_errors} messages={jira_msgs}"
        )
    data = r.json()
    issue_id  = data.get("id")
    issue_key = data.get("key")
    if not issue_id or not issue_key:
        raise RuntimeError(f"Jira response missing 'id' or 'key': {data}")

    # Set Zephyr Scale test case status to 'Approved' via the ATM REST API.
    # ATM endpoint: PUT /rest/atm/1.0/testcase/{key}  with body {"status": "Approved"}
    try:
        _session().put(
            f"{_base_url()}/rest/atm/1.0/testcase/{issue_key}",
            json={"status": "Approved"},
            timeout=30
        )
    except Exception:
        pass   # status update is best-effort; creation itself already succeeded

    return {"id": issue_id, "key": issue_key}   # e.g. {"id": "98765", "key": "PG1-26990"}

def create_test_plan(project_key: str, name: str, description: str = "") -> dict:
    """
    Create a Zephyr test plan (Jira issue of type 'Test Plan') under the given project.
    Returns the created issue's key and id.
    """
    if not name or not name.strip():
        raise ValueError("name is required and cannot be empty.")
    if len(name) > 255:
        raise ValueError(f"name exceeds Jira's 255-character limit ({len(name)} chars).")
    project_id = get_project_id(project_key)
    payload = {
        "fields": {
            "project":     {"id": project_id},
            "summary":     name.strip(),
            "description": description,
            "issuetype":   {"name": "Test Plan"},
        }
    }
    r = _session().post(
        f"{_jira_api_base()}/issue",
        json=payload, timeout=30
    )
    r.raise_for_status()
    data = r.json()
    return {"id": data["id"], "key": data["key"]}

def get_tests_for_issue(issue_key: str) -> list[dict]:
    """Fetch all Zephyr Scale test cases linked to a specific Jira issue via the ATM API.
    Also resolves and returns the actual Jira issue type so callers can verify it matches expectations.
    """
    cache_key = f"tests_for_issue:{issue_key}"
    cached = _cache_get(cache_key, _TTL_MEDIUM)
    if cached is not None:
        return cached
    # Resolve actual issue type — warns caller if they passed the wrong type label
    issue_type = "Unknown"
    issue_type_error = None
    try:
        meta = _get_issue_meta(issue_key)
        issue_type = meta["type"]
    except Exception as e:
        issue_type_error = str(e)   # surface the real error (e.g. 403 Forbidden, 404 Not Found)
    r = _session().get(
        f"{_base_url()}/rest/atm/1.0/testcase/search",
        params={"query": f'issueKeys IN ("{issue_key}")', "maxResults": 100},
        timeout=30
    )
    r.raise_for_status()
    data = r.json()
    result = [
        {
            "key":         t.get("key"),
            "name":        t.get("name"),
            "status":      t.get("status"),
            "priority":    t.get("priority"),
            "last_result": t.get("lastTestResultStatus"),
            "issue_type":  issue_type,   # actual Jira issue type for the parent issue
        }
        for t in data
    ]
    # Build sentinel first so it is included in the cached value — consistent on every call
    sentinel = {"_issue_type": issue_type, "_meta_only": True}
    if issue_type_error:
        sentinel["_issue_type_error"] = issue_type_error
    full_result = [sentinel] + result
    _cache_set(cache_key, full_result)
    return full_result

def get_test_cycles_for_issue(issue_key: str) -> list[dict]:
    """Fetch all Zephyr Scale test cycles (test runs) linked to a specific Jira issue.
    Cycles are matched by name == issue_key, which is the convention used in Zephyr Scale.
    """
    cache_key = f"cycles_for_issue:{issue_key}"
    cached = _cache_get(cache_key, _TTL_SHORT)
    if cached is not None:
        return cached
    project_key = issue_key.split("-")[0]   # derive project key from issue key e.g. PG1 from PG1-26882
    # Try filtering by issue key directly first (more efficient for large projects)
    r = _session().get(
        f"{_base_url()}/rest/atm/1.0/testrun/search",
        params={"query": f'projectKey = "{project_key}" AND issue = "{issue_key}"', "maxResults": 100},
        timeout=60
    )
    r.raise_for_status()
    data = r.json()
    # Fallback: also match by name convention (name == issue_key)
    if not data:
        r2 = _session().get(
            f"{_base_url()}/rest/atm/1.0/testrun/search",
            params={"query": f'projectKey = "{project_key}" AND name = "{issue_key}"', "maxResults": 100},
            timeout=60
        )
        r2.raise_for_status()
        data = r2.json()
    result = [
        {
            "key":             c.get("key"),
            "name":            c.get("name"),
            "status":          c.get("status"),
            "test_case_count": c.get("testCaseCount"),
            "planned_start":   c.get("plannedStartDate", "")[:10] if c.get("plannedStartDate") else "",
            "planned_end":     c.get("plannedEndDate", "")[:10] if c.get("plannedEndDate") else "",
        }
        for c in data
    ]
    _cache_set(cache_key, result)
    return result

def get_test_cycle_details(cycle_key: str) -> dict:
    """Fetch full details of a Zephyr Scale test cycle including all execution items."""
    cache_key = f"cycle_details:{cycle_key}"
    cached = _cache_get(cache_key, _TTL_SHORT)
    if cached is not None:
        return cached
    r = _session().get(
        f"{_base_url()}/rest/atm/1.0/testrun/{cycle_key}",
        timeout=30
    )
    r.raise_for_status()
    data = r.json()
    items = [
        {
            "test_case_key": item.get("testCaseKey"),
            "status":        item.get("status"),
            "executed_by":   item.get("executedBy"),
            "execution_date": item.get("executionDate", "")[:10] if item.get("executionDate") else "",
        }
        for item in data.get("items", [])
    ]
    result = {
        "key":             data.get("key"),
        "name":            data.get("name"),
        "status":          data.get("status"),
        "project_key":     data.get("projectKey"),
        "version":         data.get("version"),
        "test_case_count": data.get("testCaseCount"),
        "planned_start":   data.get("plannedStartDate", "")[:10] if data.get("plannedStartDate") else "",
        "planned_end":     data.get("plannedEndDate", "")[:10] if data.get("plannedEndDate") else "",
        "issue_links":     data.get("issueLinks", []),
        "execution_summary": data.get("executionSummary", {}),
        "items":           items,
    }
    _cache_set(cache_key, result)
    return result

def audit_test_cycle(cycle_key: str) -> dict:
    """Audit a test cycle: check every executed test case has attachments and all steps are executed."""
    cache_key = f"audit:{cycle_key}"
    cached = _cache_get(cache_key, _TTL_SHORT)
    if cached is not None:
        return cached
    r = _session().get(
        f"{_base_url()}/rest/atm/1.0/testrun/{cycle_key}/testresults",
        timeout=30
    )
    r.raise_for_status()
    results = r.json()
    audit_items = []
    issues = []
    for res in results:
        tc_key = res.get("testCaseKey", "")
        status = res.get("status", "")
        attachments = res.get("attachments", [])
        steps = res.get("scriptResults", [])
        missing_attachment = len(attachments) == 0
        not_executed_steps = [s.get("index") for s in steps if not s.get("status") or s.get("status", "").lower() in ("not executed", "unexecuted", "")]
        in_progress_steps  = [s.get("index") for s in steps if s.get("status", "").lower() == "in progress"]
        failed_steps       = [s.get("index") for s in steps if s.get("status", "").lower() == "fail"]
        blocked_steps      = [s.get("index") for s in steps if s.get("status", "").lower() == "blocked"]
        deferred_steps     = [s.get("index") for s in steps if s.get("status", "").lower() == "deferred"]
        item = {
            "test_case_key":       tc_key,
            "status":              status,
            "attachment_count":    len(attachments),
            "attachments":         [a.get("filename") for a in attachments],
            "step_count":          len(steps),
            "not_executed_steps":  not_executed_steps,
            "in_progress_steps":   in_progress_steps,
            "failed_steps":        failed_steps,
            "blocked_steps":       blocked_steps,
            "deferred_steps":      deferred_steps,
            "has_attachment":      not missing_attachment,
            "all_steps_completed": len(not_executed_steps) == 0 and len(in_progress_steps) == 0,
            "flags":               [],
        }
        if missing_attachment:
            item["flags"].append("NO ATTACHMENT")
            issues.append(f"{tc_key}: missing attachment")
        if not_executed_steps:
            item["flags"].append(f"NOT EXECUTED STEPS: {not_executed_steps}")
            issues.append(f"{tc_key}: steps {not_executed_steps} are NOT EXECUTED")
        if in_progress_steps:
            item["flags"].append(f"IN PROGRESS STEPS: {in_progress_steps}")
            issues.append(f"{tc_key}: steps {in_progress_steps} are still IN PROGRESS")
        if failed_steps:
            item["flags"].append(f"FAILED STEPS: {failed_steps}")
            issues.append(f"{tc_key}: steps {failed_steps} have FAIL status")
        if blocked_steps:
            item["flags"].append(f"BLOCKED STEPS: {blocked_steps}")
            issues.append(f"{tc_key}: steps {blocked_steps} are BLOCKED")
        if deferred_steps:
            item["flags"].append(f"DEFERRED STEPS: {deferred_steps}")
            issues.append(f"{tc_key}: steps {deferred_steps} are DEFERRED")
        if status.lower() == "fail":
            item["flags"].append("TEST FAILED")
            issues.append(f"{tc_key}: test case status is FAIL")
        audit_items.append(item)
    result = {
        "cycle_key":    cycle_key,
        "total_tests":  len(results),
        "issues_found": len(issues),
        "issues":       issues,
        "audit_items":  audit_items,
    }
    _cache_set(cache_key, result)
    return result

def get_test_cases(project_key: str, max_results: int = 50) -> list[dict]:
    """List Test-type issues in a project."""
    cache_key = f"test_cases:{project_key}:{max_results}"
    cached = _cache_get(cache_key, _TTL_MEDIUM)
    if cached is not None:
        return cached
    # JQL (Jira Query Language) filters issues to only 'Test' type in the project
    r = _session().get(
        f"{_jira_api_base()}/search",
        params={
            "jql": f"project={project_key} AND issuetype=Test ORDER BY created DESC",
            "maxResults": max_results,
            "fields": "summary,status,assignee",   # only fetch fields we need
        },
        timeout=30
    )
    r.raise_for_status()
    issues = r.json().get("issues", [])   # 'issues' key holds the array of results
    result = [
        {
            "key":      i["key"],
            "id":       i["id"],
            "summary":  i["fields"]["summary"],
            "status":   i["fields"]["status"]["name"],
        }
        for i in issues
    ]
    _cache_set(cache_key, result)
    return result

def delete_test_case(issue_key: str) -> dict:
    """Permanently delete a test case. This action cannot be undone."""
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    r = _session().delete(
        f"{_jira_api_base()}/issue/{issue_key}",
        timeout=30,
    )
    r.raise_for_status()
    _ISSUE_META_CACHE.pop(issue_key, None)   # remove deleted issue from permanent caches
    _ISSUE_ID_CACHE.pop(issue_key, None)
    return {"deleted": True, "key": issue_key}

def bulk_create_test_cases(project_key: str, test_cases: list[dict]) -> list[dict]:
    """
    Create multiple test cases in one call.
    Each dict in test_cases: {"summary": str, "description": str (opt), "priority": str (opt)}
    Returns per-test creation status with key or error message.
    """
    if not test_cases:
        raise ValueError("test_cases list is empty — provide at least one test case dict.")
    results = []
    for idx, tc in enumerate(test_cases):
        summary = tc.get("summary", "").strip() if isinstance(tc, dict) else ""
        if not summary:
            results.append({"status": "failed", "index": idx, "summary": "", "error": "'summary' is required and cannot be empty."})
            continue
        try:
            created = create_test_case(
                project_key=project_key,
                summary=summary,
                description=tc.get("description", ""),
                priority=tc.get("priority"),
            )
            results.append({"status": "created", "index": idx, "key": created.get("key"), "summary": summary})
        except Exception as exc:
            results.append({"status": "failed", "index": idx, "summary": summary, "error": str(exc)})
    return results

def get_test_coverage(project_key: str, jql_filter: str = "") -> dict:
    """
    Report test coverage for Story/Bug/Task issues in a project or sprint.
    Fetches all matching issues in one paginated call (with issuelinks field),
    then checks whether each issue has at least one linked Test issue.

    jql_filter : optional extra JQL clause, e.g. 'sprint="Sprint 24"'
                 or 'fixVersion="2.0"' — appended with AND to the base query.

    Returns a dict with:
        total_issues      : total stories/bugs checked
        covered           : count with at least one linked test case
        uncovered         : count with no linked test case
        coverage_pct      : percentage covered (1 decimal place)
        covered_issues    : list of covered issue keys
        uncovered_issues  : list of uncovered issue keys
    """
    base_jql = f'project="{project_key}" AND issuetype in (Story, Bug, Task)'
    sf = jql_filter.strip() if jql_filter else ""
    if sf.upper().startswith("AND "): sf = sf[4:]
    elif sf.upper().startswith("OR "): sf = sf[3:]
    safe_filter = sf.strip()
    jql = f"{base_jql} AND {safe_filter}" if safe_filter else base_jql

    all_issues: list[dict] = []
    start_at = 0
    max_per_page = 100
    while True:
        r = _session().get(
            f"{_jira_api_base()}/search",
            params={
                "jql":        jql,
                "maxResults": max_per_page,
                "startAt":    start_at,
                "fields":     "summary,status,issuetype,issuelinks",
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("issues", [])
        all_issues.extend(batch)
        if not batch or len(all_issues) >= data.get("total", 0):
            break
        start_at += max_per_page

    if not all_issues:
        return {
            "project": project_key, "jql_filter": jql_filter or "none",
            "total_issues": 0, "covered": 0, "uncovered": 0,
            "coverage_pct": 0.0, "covered_issues": [], "uncovered_issues": [],
        }

    covered_keys: list[str] = []
    uncovered_keys: list[str] = []
    for issue in all_issues:
        links = issue["fields"].get("issuelinks", [])
        has_test = any(
            link.get("inwardIssue",  {}).get("fields", {}).get("issuetype", {}).get("name") == "Test"
            or link.get("outwardIssue", {}).get("fields", {}).get("issuetype", {}).get("name") == "Test"
            for link in links
        )
        (covered_keys if has_test else uncovered_keys).append(issue["key"])

    total       = len(all_issues)
    covered_cnt = len(covered_keys)
    coverage_pct = round(covered_cnt / total * 100, 1) if total else 0.0
    return {
        "project":         project_key,
        "jql_filter":      jql_filter or "none",
        "total_issues":    total,
        "covered":         covered_cnt,
        "uncovered":       len(uncovered_keys),
        "coverage_pct":    coverage_pct,
        "covered_issues":  covered_keys,
        "uncovered_issues": uncovered_keys,
    }

# ----------------Test Steps ---------------------------------

def display_test_design_steps(issue_key: str) -> dict:
    """
    Fetch the test script (design steps) for a Zephyr Scale test case via ATM API.
    Supports Step-by-Step, BDD-Gherkin, and Plain text script types.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required (e.g. 'PG1-123').")
    cache_key = f"test_design_steps:{issue_key}"
    cached = _cache_get(cache_key, _TTL_MEDIUM)
    if cached is not None:
        return cached
    r = _session().get(
        f"{_base_url()}/rest/atm/1.0/testcase/{issue_key}",
        timeout=30,
    )
    r.raise_for_status()
    script = r.json().get("testScript", {})
    script_type = script.get("type", "UNKNOWN")
    if script_type == "STEP_BY_STEP":
        result = {
            "type": "Step-by-Step",
            "steps": [
                {
                    "index":  idx + 1,
                    "step":   s.get("description", ""),
                    "data":   s.get("testData", ""),
                    "result": s.get("expectedResult", ""),
                }
                for idx, s in enumerate(script.get("steps", []))
            ],
        }
    elif script_type == "BDD":
        result = {"type": "BDD-Gherkin", "script": script.get("text", "")}
    elif script_type == "PLAIN":
        result = {"type": "Plain", "script": script.get("text", "")}
    else:
        result = {"type": script_type or "None", "raw": script}
    _cache_set(cache_key, result)
    return result

def create_test_design_steps(
    issue_key: str,
    script_type: str,
    steps: list[dict] = None,
    script_text: str = "",
) -> dict:
    """
    Create or replace the test script (design steps) for a Zephyr Scale test case via ATM API.
    script_type : 'step_by_step', 'bdd', or 'plain'
    steps       : for step_by_step — list of dicts with keys: 'step', 'data', 'result'
    script_text : for bdd or plain — the raw Gherkin or plain text string
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required (e.g. 'PG1-123').")
    script_type_norm = (script_type or "").strip().lower()
    if script_type_norm not in ("step_by_step", "bdd", "plain"):
        raise ValueError("script_type must be 'step_by_step', 'bdd', or 'plain'.")
    if script_type_norm == "step_by_step":
        if not steps:
            raise ValueError("steps list is required for step_by_step script type.")
        payload = {
            "testScript": {
                "type": "STEP_BY_STEP",
                "steps": [
                    {
                        "description":    s.get("step", ""),
                        "testData":       s.get("data", ""),
                        "expectedResult": s.get("result", ""),
                    }
                    for s in steps
                ],
            }
        }
    else:
        if not script_text or not script_text.strip():
            raise ValueError(f"script_text is required for {script_type_norm} script type.")
        if script_type_norm == "bdd":
            _strip_keywords = {"feature", "scenario outline", "background", "examples"}
            cleaned_lines = []
            for line in script_text.splitlines():
                stripped = line.strip().lower()
                if any(stripped.startswith(kw + ":") for kw in _strip_keywords):
                    continue
                if stripped.startswith("scenario:"):
                    cleaned_lines.append(f"'{line.strip()}'")
                else:
                    cleaned_lines.append(line)
            script_text = "\n".join(cleaned_lines).strip()
        atm_type = "BDD" if script_type_norm == "bdd" else "PLAIN"
        payload = {"testScript": {"type": atm_type, "text": script_text}}
    r = _session().put(
        f"{_base_url()}/rest/atm/1.0/testcase/{issue_key}",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    _cache_invalidate(f"test_design_steps:{issue_key}")
    return {"created": True, "key": issue_key, "type": script_type_norm}

# ----------------Comments ---------------------------------

def add_comment(issue_key: str, comment: str) -> dict:
    """
    Add a comment to any Jira issue (story, bug, epic, test case, etc.).
    Returns the created comment's id, author, and body.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not comment or not comment.strip():
        raise ValueError("comment cannot be empty.")
    r = _session().post(
        f"{_jira_api_base()}/issue/{issue_key}/comment",   # Jira comment endpoint
        json={"body": comment},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return {
        "id":     data.get("id"),
        "author": data.get("author", {}).get("displayName", ""),
        "body":   data.get("body", ""),
    }

def link_test_to_issue(test_issue_key: str, target_issue_key: str) -> dict:
    """
    Create a Jira 'tests' link between a Test issue and a story/bug/epic.
    """
    if not test_issue_key or not test_issue_key.strip():
        raise ValueError("test_issue_key is required.")
    if not target_issue_key or not target_issue_key.strip():
        raise ValueError("target_issue_key is required.")
    # Resolve both issue keys to their numeric IDs (required by issueLink API)
    test_id   = get_issue_id(test_issue_key)
    target_id = get_issue_id(target_issue_key)
    payload = {
        "type":         {"name": "Tests"},        # Jira issue link type name
        "inwardIssue":  {"id": test_id},          # the Test issue
        "outwardIssue": {"id": target_id},        # the story/bug/epic being tested
    }
    r = _session().post(
        f"{_jira_api_base()}/issueLink",   # Jira core issue link endpoint
        json=payload, timeout=30
    )
    r.raise_for_status()   # 201 Created on success; no response body
    return {
        "linked":      True,
        "test_issue":  test_issue_key,
        "target_issue":target_issue_key,
    }

# -------------------test execution------------

    def get_execution_results(cycle_key: str) -> list[dict]:
        """
        
        Returns per-test status, executor, and execution date.
        """
        r = _session().get(
            f"{_base_url()}/rest/atm/1.0/testrun/{cycle_key}/testresults",
            timeout=30,
        )
        r.raise_for_status()
        return [
            {
                "test_case_key": res.get("testCaseKey"),
                "status": res.get("status"),
                "executed_by": res.get("executedBy", ""),
                "executed_on": (res.get("executionDate") or "")[:10],
                "comment": res.get("comment", ""),
            }
            for res in r.json()
        ]

    def get_cycle_execution_summary(cycle_key: str) -> dict:
        """
        Aggregate pass/fail/blocked counts for all tests in a Zephyr Scale test run.
        Returns counts by status and overall pass percentage.
        """
        results = get_execution_results(cycle_key)
        counts: dict[str, int] = {}
        for res in results:
            s = res["status"] or "Unknown"
            counts[s] = counts.get(s, 0) + 1
        total = len(results)
        passed = counts.get("Pass", 0)
        return {
            "cycle_key": cycle_key,
            "total": total,
            "summary": counts,
            "pass_pct": round(passed / total * 100, 1) if total else 0.0,
        }
        
    # ----------------Jira Issues----------------------------------

    def get_issue(issue_key: str) -> dict:
        """Fetch full details of any Jira issue."""
        r = _session().get(
            f"{_jira_api_base()}/issue/{issue_key}",
            params={"fields": "summary,description,status,priority,assignee,issuetype,labels,components,created,updated,reporter"},
            timeout=30,
        )
        r.raise_for_status()
        i = r.json()
        fields = i["fields"]
        return {
            "key": i["key"],
            "id": i["id"],
            "summary": fields.get("summary"),
            "description": fields.get("description", ""),
            "status": fields.get("status").get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "issuetype": fields.get("issuetype", {}).get("name"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "reporter": (fields.get("reporter") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "components": [c["name"] for c in fields.get("components", [])],
            "created": fields.get("created"),
            "updated": fields.get("updated"),
        }

    def search_issues(jql: str, max_results: int = 50) -> list[dict]:
        """Run any JQL query and return matching issues."""
        r = _session().get(
            f"{_jira_api_base()}/search",
            params={"jql": jql, "maxResults": max_results, "fields": "summary,status,priority,assignee,issuetype"},
            timeout=30,
        )
        r.raise_for_status()
        return [
            {
                "key": i["key"],
                "id": i["id"],
                "summary": i["fields"]["summary"],
                "status": i["fields"]["status"]["name"],
                "issuetype": i["fields"]["issuetype"]["name"],
                "priority": (i["fields"].get("priority") or {}).get("name", ""),
                "assignee": (i["fields"].get("assignee") or {}).get("displayName", ""),
            }
            for i in r.json().get("issues", [])
        ]
    
    # ----------------Reporting----------------------------------

    def generate_test_summary(project_key: str, tql_filter: str = "") -> dict:
        """
        Aggregate latest execution status for all test cases in a project.
        Uses lastTestResultStatus from Zephyr Scale ATM test case search.
        tql_filter : optional TQL clause to narrow scope, e.g. 'folder = "/Regression"'
        Returns counts by status and overall pass percentage.
        """
        base_query = f'projectKey = "{project_key}"'
        sf = tql_filter.strip() if tql_filter else ""
        if sf.upper().startswith("AND "): sf = sf[4:]
        elif sf.upper().startswith("OR "): sf = sf[3:]
        safe_filter = sf.strip()
        query = f"{base_query} AND {safe_filter}" if safe_filter else base_query
        all_tests: list[dict] = []
        start_at = 0
        max_per_page = 100
        while True:
            r = _session().get(
                f"{_base_url()}/rest/atm/1.0/testcase/search",
                params={"query": query, "maxResults": max_per_page, "startAt": start_at},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            all_tests.extend(batch)
            if not batch or len(batch) < max_per_page:
                break
            start_at += max_per_page
        counts: dict[str, int] = {}
        for tc in all_tests:
            status = tc.get("lastTestResultStatus") or "Unexecuted"
            counts[status] = counts.get(status, 0) + 1
        total = len(all_tests)
        passed = counts.get("Pass", 0)
        return {
            "project":     project_key,
            "tql_filter": tql_filter or "none",
            "total": total,
            "summary": counts,
            "pass_pct": round(passed / total * 100, 1) if total else 0.0,
        }