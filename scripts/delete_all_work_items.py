#!/usr/bin/env python3
"""Delete Azure DevOps work items in a project.

This script uses the Azure DevOps REST API with a Personal Access Token (PAT).
By default it performs a dry-run; pass --yes to actually delete.

Notes:
- "Delete" moves items to the recycle bin (soft delete).
- Permanent destroy is optional via --destroy, but is more dangerous.
- Test Plans are NOT deleted by default. Use --delete-test-plans to delete them.

Required env var:
- ADO_MCP_AUTH_TOKEN (preferred) OR AZURE_DEVOPS_EXT_PAT OR AZURE_DEVOPS_PAT

Example:
  export ADO_MCP_AUTH_TOKEN="..."
  .venv/bin/python scripts/delete_all_work_items.py --org appatr --project testingmcp --yes
"""

from __future__ import annotations

import argparse
import base64
import os
from dataclasses import dataclass

import httpx

try:
    from dotenv import load_dotenv  # type: ignore

    # Avoid dotenv's stack inspection (can assert in some Python versions) by
    # explicitly pointing at the repo-local .env.
    load_dotenv(dotenv_path=".env")
except Exception:
    # Optional dependency / best-effort; env vars can still be provided via shell.
    pass


def _get_pat() -> str:
    return (
        os.environ.get("ADO_MCP_AUTH_TOKEN")
        or os.environ.get("AZURE_DEVOPS_EXT_PAT")
        or os.environ.get("AZURE_DEVOPS_PAT")
        or ""
    ).strip()


def _auth_headers(pat: str) -> dict[str, str]:
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


@dataclass(frozen=True)
class AdoTarget:
    org: str
    project: str


def _wiql_url(target: AdoTarget) -> str:
    return f"https://dev.azure.com/{target.org}/{target.project}/_apis/wit/wiql?api-version=7.1"


def _delete_work_item_url(org: str, work_item_id: int) -> str:
    return f"https://dev.azure.com/{org}/_apis/wit/workitems/{work_item_id}?api-version=7.1-preview.3"


def _destroy_recycle_bin_url(org: str, work_item_id: int) -> str:
    # Permanently destroy from recycle bin.
    return (
        f"https://dev.azure.com/{org}/_apis/wit/recyclebin/{work_item_id}"
        f"?api-version=7.1-preview.1&destroy=true"
    )


def _delete_test_artifact_urls(target: AdoTarget, work_item_id: int) -> list[str]:
    """Candidate Test Management API endpoints for deleting test work items."""
    return [
        # Common Test Management endpoint for Test Case artifacts
        f"https://dev.azure.com/{target.org}/{target.project}/_apis/test/testcases/{work_item_id}?api-version=7.1-preview.1",
        f"https://dev.azure.com/{target.org}/{target.project}/_apis/test/testcases/{work_item_id}?api-version=6.0-preview.1",
        # Some orgs expose test cases under testplan
        f"https://dev.azure.com/{target.org}/{target.project}/_apis/testplan/testcases/{work_item_id}?api-version=7.1-preview.1",
    ]


def _list_test_plans_url(target: AdoTarget) -> str:
    return (
        f"https://dev.azure.com/{target.org}/{target.project}"
        f"/_apis/testplan/plans?api-version=7.1-preview.1"
    )


def _delete_test_plan_url(target: AdoTarget, plan_id: int) -> str:
    return (
        f"https://dev.azure.com/{target.org}/{target.project}"
        f"/_apis/testplan/plans/{plan_id}?api-version=7.1-preview.1"
    )


def _delete_all_test_plans(client: httpx.Client, target: AdoTarget) -> tuple[int, int]:
    """Delete all Test Plans in a project.

    Returns (deleted_count, total_count).
    """
    resp = client.get(_list_test_plans_url(target))
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Failed to list test plans ({resp.status_code}): {detail}")

    data = resp.json() or {}
    plans = data.get("value") or []
    plan_ids = [int(p["id"]) for p in plans if isinstance(p, dict) and "id" in p]

    deleted = 0
    for plan_id in plan_ids:
        d = client.delete(_delete_test_plan_url(target, plan_id))
        if d.status_code in (200, 204):
            deleted += 1
        else:
            try:
                detail = d.json()
            except Exception:
                detail = d.text
            print(f"Failed to delete Test Plan {plan_id}: {d.status_code} {detail}")

    return deleted, len(plan_ids)


def _query_work_item_ids(client: httpx.Client, target: AdoTarget) -> list[int]:
    """Return all work item IDs in a project.

    Uses a descending ID cursor to avoid server-side result limits.
    """

    ids: list[int] = []
    cursor_lt: int | None = None

    # Use an explicit project name rather than the @project macro, which can be
    # rejected depending on org/project settings and API versions.
    project_name = target.project.replace("'", "''")

    while True:
        where = f"[System.TeamProject] = '{project_name}'"
        if cursor_lt is not None:
            where += f" AND [System.Id] < {cursor_lt}"

        wiql = {
            "query": (
                "SELECT [System.Id] "
                "FROM WorkItems "
                f"WHERE {where} "
                "ORDER BY [System.Id] DESC"
            )
        }

        resp = client.post(_wiql_url(target), json=wiql)
        if resp.status_code >= 400:
            # Surface the server's message; WIQL errors often come back as JSON.
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"WIQL query failed ({resp.status_code}): {detail}")
        data = resp.json() or {}

        work_items = data.get("workItems") or []
        batch_ids = [int(item["id"]) for item in work_items if "id" in item]

        if not batch_ids:
            break

        ids.extend(batch_ids)
        cursor_lt = min(batch_ids)

    # Deduplicate while preserving order (descending).
    seen: set[int] = set()
    ordered: list[int] = []
    for work_item_id in ids:
        if work_item_id not in seen:
            seen.add(work_item_id)
            ordered.append(work_item_id)

    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all Azure DevOps work items in a project")
    parser.add_argument("--org", required=True, help="Azure DevOps organization (e.g., appatr)")
    parser.add_argument("--project", required=True, help="Azure DevOps project (e.g., testingmcp)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete items (otherwise dry-run)",
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Permanently destroy items from recycle bin after deletion",
    )
    parser.add_argument(
        "--delete-test-plans",
        action="store_true",
        help="Also delete Azure DevOps Test Plans (disabled by default)",
    )
    args = parser.parse_args()

    pat = _get_pat()
    if not pat:
        raise SystemExit(
            "Missing PAT. Set ADO_MCP_AUTH_TOKEN (or AZURE_DEVOPS_EXT_PAT / AZURE_DEVOPS_PAT)."
        )

    target = AdoTarget(org=args.org.strip(), project=args.project.strip())
    if not target.org or not target.project:
        raise SystemExit("--org and --project must be non-empty")

    with httpx.Client(headers=_auth_headers(pat), timeout=30.0) as client:
        ids = _query_work_item_ids(client, target)

        print(f"Found {len(ids)} work items in {target.org}/{target.project}.")
        if not ids:
            return 0

        if not args.yes:
            print("Dry-run only. Re-run with --yes to delete.")
            print(f"First 25 IDs: {ids[:25]}")
            return 0

        # Delete in descending ID order.
        deleted = 0
        test_artifact_ids: list[int] = []
        for work_item_id in ids:
            resp = client.delete(_delete_work_item_url(target.org, work_item_id))
            if resp.status_code in (200, 204):
                deleted += 1
            else:
                # Continue, but show the error.
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                if (
                    resp.status_code == 400
                    and isinstance(detail, dict)
                    and isinstance(detail.get("message"), str)
                    and "test work items" in detail["message"].lower()
                ):
                    test_artifact_ids.append(work_item_id)
                print(f"Failed to delete {work_item_id}: {resp.status_code} {detail}")

        print(f"Deleted {deleted}/{len(ids)} work items (moved to recycle bin).")

        if test_artifact_ids:
            print(
                f"Retrying {len(test_artifact_ids)} test work items via Test Management REST API..."
            )
            test_deleted = 0
            skipped_non_testcase = 0
            for work_item_id in test_artifact_ids:
                ok = False
                last_detail: object | None = None
                for url in _delete_test_artifact_urls(target, work_item_id):
                    resp = client.delete(url)
                    if resp.status_code in (200, 204):
                        ok = True
                        test_deleted += 1
                        break
                    try:
                        last_detail = resp.json()
                    except Exception:
                        last_detail = resp.text
                if not ok:
                    # Many tenants represent Test Plans/Suites as "test work items" but they are
                    # not TestCaseCategory and cannot be deleted via the testcases API.
                    # Keep output quiet unless the user explicitly asked to delete Test Plans.
                    if (
                        not args.delete_test_plans
                        and isinstance(last_detail, dict)
                        and isinstance(last_detail.get("message"), str)
                        and "does not belong to microsoft.testcasecategory" in last_detail["message"].lower()
                    ):
                        skipped_non_testcase += 1
                        continue
                    print(f"Failed to delete test artifact {work_item_id}: {last_detail}")
            print(f"Deleted {test_deleted}/{len(test_artifact_ids)} test work items.")
            if skipped_non_testcase and not args.delete_test_plans:
                print(
                    f"Skipped {skipped_non_testcase} Test Plan/Suite work items (use --delete-test-plans to remove them)."
                )

        if args.delete_test_plans:
            # Test Plans and Suites appear as work items but cannot be deleted via the WIT API.
            # Clean them up via the Test Plans REST API.
            deleted_plans, total_plans = _delete_all_test_plans(client, target)
            if total_plans:
                print(f"Deleted {deleted_plans}/{total_plans} Test Plans.")

            # One more pass: deleting plans can unblock deletion of their associated work items.
            remaining = _query_work_item_ids(client, target)
            if remaining:
                print(f"Remaining after Test Plan cleanup: {len(remaining)}. Retrying WIT delete...")
                deleted2 = 0
                for work_item_id in remaining:
                    resp = client.delete(_delete_work_item_url(target.org, work_item_id))
                    if resp.status_code in (200, 204):
                        deleted2 += 1
                    else:
                        try:
                            detail = resp.json()
                        except Exception:
                            detail = resp.text
                        print(f"Still failed to delete {work_item_id}: {resp.status_code} {detail}")
                if deleted2:
                    print(f"Deleted {deleted2}/{len(remaining)} remaining work items.")

        if args.destroy and deleted:
            destroyed = 0
            for work_item_id in ids:
                resp = client.delete(_destroy_recycle_bin_url(target.org, work_item_id))
                if resp.status_code in (200, 204):
                    destroyed += 1
                else:
                    try:
                        detail = resp.json()
                    except Exception:
                        detail = resp.text
                    print(f"Failed to destroy {work_item_id}: {resp.status_code} {detail}")
            print(f"Destroyed {destroyed}/{len(ids)} work items from recycle bin.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
