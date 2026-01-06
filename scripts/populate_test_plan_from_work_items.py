"""Populate an Azure DevOps Test Plan suite with test cases derived from existing work items.

This script is intentionally non-LLM: it reads work items from Azure DevOps and creates
Test Case work items, then adds them to an existing Test Suite under an existing Test Plan.

It is designed to work even when Test Plan creation is broken (TF200001 projectName empty),
by targeting user-supplied existing plan/suite IDs.

Usage:
  python3 scripts/populate_test_plan_from_work_items.py \
    --org appatr \
    --project testingmcp \
        --plan-id 369 \
        --suite-id 370 \
    --wiql "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject]=@project AND [System.WorkItemType] IN ('Issue','Task') ORDER BY [System.ChangedDate] DESC"

Auth:
  - Uses ADO_MCP_AUTH_TOKEN (or AZURE_DEVOPS_EXT_PAT / AZURE_DEVOPS_TOKEN).

Notes:
  - Work item types in this project include: Epic, Issue, Task (Basic process).
  - Suite ID in Test Plans UI corresponds to a 'Test Suite' work item (WIT) ID.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from html import unescape
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv_if_present(env_path: Path) -> None:
    """Minimal .env loader (avoids adding python-dotenv as a dependency)."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_if_present(REPO_ROOT / ".env")


from src.mcp_client.ado_client import AzureDevOpsMCPClient


def _strip_html(text: str) -> str:
    # ADO often stores rich text as HTML.
    # This is a minimal, dependency-free cleaner good enough for steps text.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    # normalize whitespace
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join([ln for ln in lines if ln])
    return cleaned.strip()


def _to_steps_from_text(text: str) -> str:
    """Convert a blob of text (acceptance criteria / description) into ADO test steps.

    ADO step format required by MCP tool:
      1. Do X|Expected Y
      2. Do Z|Expected W

    We keep it simple: each bullet/line becomes one step with a generic expected result.
    """

    cleaned = _strip_html(text or "")
    if not cleaned:
        return "1. Execute the scenario|Scenario completes successfully"

    # Split on bullets/lines; keep only meaningful lines.
    raw_lines = []
    for ln in cleaned.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # trim common bullet prefixes
        ln = re.sub(r"^[-*\u2022\d+.\)]\s+", "", ln)
        if ln:
            raw_lines.append(ln)

    if not raw_lines:
        return "1. Execute the scenario|Scenario completes successfully"

    # Cap to a reasonable number of steps to avoid huge test cases.
    raw_lines = raw_lines[:10]

    steps = []
    for i, ln in enumerate(raw_lines, start=1):
        # Avoid '|' which is a reserved delimiter for ADO steps.
        ln = ln.replace("|", "-")
        steps.append(f"{i}. {ln}|Expected behavior matches requirements")

    return "\n".join(steps)


def _extract_work_item_ids_from_query_result(result: Any) -> list[int]:
    """Handle different shapes returned by the MCP server for WIQL queries."""
    if result is None:
        return []

    # Common shapes:
    # - {"workItems": [{"id": 123}, ...]}
    # - {"workItems": [ ... ], "columns": [...]}
    # - [{"id": 123}, ...]
    if isinstance(result, dict):
        items = result.get("workItems") or result.get("value") or result.get("items")
        if isinstance(items, list):
            ids: list[int] = []
            for it in items:
                if isinstance(it, dict) and "id" in it:
                    try:
                        ids.append(int(it["id"]))
                    except Exception:
                        continue
            return ids
        return []

    if isinstance(result, list):
        ids = []
        for it in result:
            if isinstance(it, dict) and "id" in it:
                try:
                    ids.append(int(it["id"]))
                except Exception:
                    continue
        return ids

    return []


def _ado_auth_headers(pat: str) -> dict[str, str]:
    # ADO uses Basic auth with PAT as the password.
    import base64

    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _wiql_query_ids_via_rest(org: str, project: str, pat: str, wiql: str) -> list[int]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=_ado_auth_headers(pat), json={"query": wiql})
        resp.raise_for_status()
        data = resp.json()
    return [int(w["id"]) for w in (data.get("workItems") or []) if isinstance(w, dict) and "id" in w]


async def _get_work_items_batch_via_rest(
    org: str, project: str, pat: str, ids: list[int], fields: list[str]
) -> list[dict[str, Any]]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/workitemsbatch?api-version=7.0"
    payload = {"ids": ids, "fields": fields}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=_ado_auth_headers(pat), json=payload)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("value")
    return items if isinstance(items, list) else []


async def _get_work_item_via_rest(org: str, project: str, pat: str, work_item_id: int) -> dict[str, Any]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{work_item_id}?api-version=7.0"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_ado_auth_headers(pat))
        resp.raise_for_status()
        return resp.json()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="Azure DevOps organization name (e.g. appatr)")
    parser.add_argument("--project", required=True, help="Azure DevOps project name")
    parser.add_argument(
        "--plan-id",
        type=int,
        default=int(os.environ.get("AZURE_DEVOPS_TEST_PLAN_ID", "369")),
        help="Existing Test Plan ID (default: AZURE_DEVOPS_TEST_PLAN_ID or 369)",
    )
    parser.add_argument(
        "--suite-id",
        type=int,
        default=int(os.environ.get("AZURE_DEVOPS_TEST_SUITE_ID", "370")),
        help="Existing Test Suite ID (default: AZURE_DEVOPS_TEST_SUITE_ID or 370)",
    )
    parser.add_argument(
        "--wiql",
        required=False,
        default="SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject]=@project AND [System.WorkItemType] IN ('Issue','Task') ORDER BY [System.ChangedDate] DESC",
        help="WIQL returning System.Id of source work items",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max number of test cases to create")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created, but don't create test cases or add to suite",
    )

    args = parser.parse_args()

    # Ensure PAT is present (MCP client maps common env vars too).
    pat = (
        os.environ.get("ADO_MCP_AUTH_TOKEN")
        or os.environ.get("AZURE_DEVOPS_EXT_PAT")
        or os.environ.get("AZURE_DEVOPS_PAT")
        or os.environ.get("AZURE_DEVOPS_TOKEN")
    )
    if not pat:
        raise SystemExit("Missing PAT: set ADO_MCP_AUTH_TOKEN (or AZURE_DEVOPS_EXT_PAT)")

    client = AzureDevOpsMCPClient(organization=args.org, project=args.project)
    await client.connect()

    wiql = args.wiql

    # Query source work items via REST for reliability (MCP WIQL query tool isn't
    # always available/consistent across MCP server versions).
    source_ids = await _wiql_query_ids_via_rest(args.org, args.project, pat, wiql)
    source_ids = source_ids[: max(0, args.limit)]

    if not source_ids:
        print("No source work items found for WIQL. Try adjusting --wiql.")
        return 2

    print(f"Found {len(source_ids)} source work items: {source_ids}")

    # Derive iteration/area path from the suite (or plan) so test cases land in
    # the same sprint.
    suite_wi = await _get_work_item_via_rest(args.org, args.project, pat, args.suite_id)
    suite_fields = (suite_wi or {}).get("fields") or {}
    plan_wi = await _get_work_item_via_rest(args.org, args.project, pat, args.plan_id)
    plan_fields = (plan_wi or {}).get("fields") or {}

    iteration_path = suite_fields.get("System.IterationPath") or plan_fields.get("System.IterationPath")
    area_path = suite_fields.get("System.AreaPath") or plan_fields.get("System.AreaPath")

    created_test_case_ids: list[int] = []

    batch_fields = [
        "System.Id",
        "System.WorkItemType",
        "System.Title",
        "System.State",
        "System.Description",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
    ]

    work_items = await _get_work_items_batch_via_rest(args.org, args.project, pat, source_ids, batch_fields)

    for wi in work_items:
        work_item_id = wi.get("id")
        fields = (wi or {}).get("fields") or {}

        title = fields.get("System.Title") or f"Work item {work_item_id}"
        wi_type = fields.get("System.WorkItemType") or "Work Item"
        state = fields.get("System.State") or ""

        # Prefer Acceptance Criteria if present; else fall back to Description.
        ac = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""
        desc = fields.get("System.Description") or ""
        steps_source = ac if str(ac).strip() else desc

        test_title = f"{wi_type}: {title}" + (f" ({state})" if state else "")
        steps = _to_steps_from_text(str(steps_source))

        if args.dry_run:
            print(f"[DRY RUN] Would create Test Case: {test_title}")
            continue

        created = await client.create_test_case(
            title=test_title,
            steps=steps,
            priority=2,
            area_path=area_path,
            iteration_path=iteration_path,
            tests_work_item_id=int(work_item_id) if work_item_id else None,
        )

        tc_id = created.get("id") if isinstance(created, dict) else None
        if not tc_id:
            print(f"Failed to create test case for work item {work_item_id}. Response: {created}")
            continue

        created_test_case_ids.append(int(tc_id))
        print(f"Created Test Case {tc_id} for work item {work_item_id}")

    if args.dry_run:
        print("Dry run complete.")
        return 0

    if not created_test_case_ids:
        print("No test cases were created; nothing to add to suite.")
        return 3

    added = await client.add_test_cases_to_suite(
        plan_id=args.plan_id,
        suite_id=args.suite_id,
        test_case_ids=created_test_case_ids,
        project=args.project,
    )

    print(f"Added {len(created_test_case_ids)} test case(s) to suite {args.suite_id} in plan {args.plan_id}.")
    print(f"Add-to-suite response: {added}")

    # Verify by listing test cases in the suite via MCP (when available).
    try:
        list_result = await client._call_first_available_tool(
            [
                "testplan_list_test_cases",
                "mcp_ado_testplan_list_test_cases",
            ],
            {
                "project": args.project,
                "planid": args.plan_id,
                "suiteid": args.suite_id,
            },
        )
        listed = list_result.get("value") if isinstance(list_result, dict) else None
        if isinstance(listed, list):
            ids = []
            for row in listed:
                tc = (row or {}).get("testCase") if isinstance(row, dict) else None
                if isinstance(tc, dict) and "id" in tc:
                    ids.append(tc["id"])
            print(f"Suite now lists {len(ids)} test case(s).")
        else:
            print(f"Suite verification result: {list_result}")
    except Exception as e:
        print(f"Suite verification skipped (tool unavailable or error): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
