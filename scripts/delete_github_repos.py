#!/usr/bin/env python3
"""Delete GitHub repositories matching a pattern.

This script uses the GitHub REST API with a Personal Access Token.
By default it performs a dry-run; pass --yes to actually delete.

Required env var:
- GITHUB_TOKEN

Example:
  export GITHUB_TOKEN="ghp_..."
  python scripts/delete_github_repos.py --owner rameshappat --pattern "fin-demo*" --yes
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=".env")
except Exception:
    pass


def _get_github_token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_repositories(
    client: httpx.Client, owner: str
) -> list[dict[str, any]]:
    """List all repositories for a user/org.
    
    Args:
        client: HTTP client with auth headers.
        owner: GitHub username or organization.
        
    Returns:
        List of repository objects.
    """
    repos = []
    page = 1
    per_page = 100
    
    while True:
        url = f"https://api.github.com/users/{owner}/repos"
        params = {"page": page, "per_page": per_page, "type": "owner"}
        
        resp = client.get(url, params=params)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Failed to list repositories ({resp.status_code}): {detail}")
        
        batch = resp.json()
        if not batch:
            break
            
        repos.extend(batch)
        page += 1
        
        # Break if we got fewer results than requested (last page)
        if len(batch) < per_page:
            break
    
    return repos


def delete_repository(
    client: httpx.Client, owner: str, repo_name: str
) -> bool:
    """Delete a GitHub repository.
    
    Args:
        client: HTTP client with auth headers.
        owner: GitHub username or organization.
        repo_name: Repository name to delete.
        
    Returns:
        True if successful, False otherwise.
    """
    url = f"https://api.github.com/repos/{owner}/{repo_name}"
    resp = client.delete(url)
    
    if resp.status_code == 204:
        return True
    else:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        print(f"Failed to delete {repo_name}: {resp.status_code} {detail}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete GitHub repositories matching a pattern"
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub username or organization (e.g., rameshappat)",
    )
    parser.add_argument(
        "--pattern",
        required=True,
        help="Pattern to match repository names (e.g., 'fin-demo*')",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete repositories (otherwise dry-run)",
    )
    args = parser.parse_args()
    
    token = _get_github_token()
    if not token:
        print("Error: Missing GITHUB_TOKEN environment variable.", file=sys.stderr)
        print("Please set GITHUB_TOKEN to your GitHub Personal Access Token.", file=sys.stderr)
        return 1
    
    owner = args.owner.strip()
    pattern = args.pattern.strip()
    
    if not owner or not pattern:
        print("Error: --owner and --pattern must be non-empty", file=sys.stderr)
        return 1
    
    with httpx.Client(headers=_auth_headers(token), timeout=30.0) as client:
        # List all repositories
        print(f"Fetching repositories for {owner}...")
        repos = list_repositories(client, owner)
        print(f"Found {len(repos)} total repositories.")
        
        # Filter by pattern
        matching_repos = [
            r for r in repos
            if fnmatch.fnmatch(r["name"], pattern)
        ]
        
        if not matching_repos:
            print(f"No repositories match pattern '{pattern}'")
            return 0
        
        print(f"\nFound {len(matching_repos)} repositories matching '{pattern}':")
        for repo in matching_repos:
            print(f"  - {repo['name']} ({repo['html_url']})")
        
        if not args.yes:
            print("\nDry-run only. Re-run with --yes to delete these repositories.")
            return 0
        
        # Confirm deletion
        print(f"\n⚠️  WARNING: About to delete {len(matching_repos)} repositories!")
        print("This action cannot be undone.")
        confirmation = input("Type 'DELETE' to confirm: ").strip()
        
        if confirmation != "DELETE":
            print("Deletion cancelled.")
            return 0
        
        # Delete repositories
        print("\nDeleting repositories...")
        deleted = 0
        for repo in matching_repos:
            repo_name = repo["name"]
            if delete_repository(client, owner, repo_name):
                print(f"✓ Deleted: {repo_name}")
                deleted += 1
            else:
                print(f"✗ Failed: {repo_name}")
        
        print(f"\nDeleted {deleted}/{len(matching_repos)} repositories.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
