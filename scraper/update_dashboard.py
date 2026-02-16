#!/usr/bin/env python3
"""
update_dashboard.py
===================
Runs the HWP portal scraper and applies any detected changes
directly to the DEFAULT_DATA array in index.html.

This script is designed to run in a GitHub Actions workflow:
  1. Runs hwp_portal_scraper.py --active-only --output json
  2. Parses the JSON output for status/field changes
  3. Updates the corresponding entries in index.html
  4. Writes a change summary for the commit message

Usage:
  python scraper/update_dashboard.py

Exit codes:
  0 - Success (changes applied or no changes needed)
  1 - Error
"""

import json
import re
import subprocess
import sys
import os
from datetime import datetime


def run_scraper():
    """Run the portal scraper and return parsed JSON results."""
    scraper_path = os.path.join(os.path.dirname(__file__), "hwp_portal_scraper.py")

    print("Running portal scraper...")
    result = subprocess.run(
        [sys.executable, scraper_path, "--active-only", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute timeout
    )

    if result.returncode != 0:
        print(f"Scraper stderr:\n{result.stderr}", file=sys.stderr)
        // Don't fail — partial results may still be useful

    // Extract JSON from stdout (scraper prints progress to stderr)
    stdout = result.stdout.strip()
    if not stdout:
        print("No output from scraper.")
        return []

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse scraper output: {e}", file=sys.stderr)
        print(f"Raw output: {stdout[:500]}", file=sys.stderr)
        return []


def find_and_update_entry(html, ref, changes):
    """
    Find the DEFAULT_DATA entry for a given ref and update changed fields.

    The entries look like:
      {auth:"Cork County Council",...,ref:"25/6796",...,status:"New Application",...},

    We use regex to find the entry by ref, then replace specific field values.
    """
    // Escape the ref for regex (handle slashes)
    escaped_ref = re.escape(ref)

    // Find the line containing this ref
    // Pattern: matches from { to the next }, capturing the full entry
    pattern = rf'(\{{[^}}]*ref:"{escaped_ref}"[^}}]*\}})'
    match = re.search(pattern, html)

    if not match:
        print(f"  Could not find entry for ref {ref} in index.html")
        return html, False

    old_entry = match.group(1)
    new_entry = old_entry

    applied = []

    for field, change in changes.items():
        new_val = change.get("new")
        if new_val is None:
            continue

        // Handle different field types
        if isinstance(new_val, bool):
            val_str = "true" if new_val else "false"
        elif new_val is None or new_val == "":
            val_str = "null"
        else:
            // String value — escape quotes
            val_str = f'"{new_val}"'

        // Replace the field value in the entry
        // Pattern: field:"old_value" or field:null
        field_pattern = rf'{field}:(?:"[^"]*"|null|true|false)'
        field_match = re.search(field_pattern, new_entry)

        if field_match:
            new_entry = new_entry[:field_match.start()] + f'{field}:{val_str}' + new_entry[field_match.end():]
            applied.append(f"{field}: {change.get('old')} -> {new_val}")
        else:
            print(f"  Could not find field {field} in entry for {ref}")

    if new_entry != old_entry:
        html = html.replace(old_entry, new_entry)
        return html, True

    return html, False


def update_summary_for_fi(html, ref):
    """
    If status changed to Further Information Requested,
    append a note to the summary field.
    """
    escaped_ref = re.escape(ref)
    pattern = rf'(\{{[^}}]*ref:"{escaped_ref}"[^}}]*\}})'
    match = re.search(pattern, html)
    if not match:
        return html

    entry = match.group(1)

    // Check if summary already mentions FI
    if "further information" in entry.lower() and "requested" in entry.lower():
        return html

    // Find summary field
    summary_match = re.search(r'summary:"([^"]*)"', entry)
    if summary_match:
        old_summary = summary_match.group(1)
        new_summary = old_summary.rstrip(".") + ". Further information requested by the planning authority."
        new_entry = entry.replace(f'summary:"{old_summary}"', f'summary:"{new_summary}"')
        html = html.replace(entry, new_entry)

    return html


def clear_decision_due_for_fi(html, ref):
    """
    When status changes to FI, clear the decDue field
    (FI pauses the statutory decision clock).
    """
    escaped_ref = re.escape(ref)
    pattern = rf'(\{{[^}}]*ref:"{escaped_ref}"[^}}]*\}})'
    match = re.search(pattern, html)
    if not match:
        return html

    entry = match.group(1)
    // Replace decDue with null
    new_entry = re.sub(r'decDue:"[^"]*"', 'decDue:null', entry)
    if new_entry != entry:
        html = html.replace(entry, new_entry)

    return html


def main():
    # Paths
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index_path = os.path.join(repo_root, "index.html")

    if not os.path.exists(index_path):
        print(f"Error: index.html not found at {index_path}", file=sys.stderr)
        sys.exit(1)

    # Run scraper
    results = run_scraper()

    if not results:
        print("No scraper results. Exiting.")
        sys.exit(0)

    # Filter to only results with changes
    changed = [r for r in results if r.get("_has_changes")]
    failed = [r for r in results if r.get("_scrape_status") == "failed"]

    print(f"\nScraper results: {len(results)} checked, {len(changed)} changed, {len(failed)} failed")

    if not changed:
        print("No changes detected. Dashboard is up to date.")
        // Write empty changes file for the workflow to check
        changes_path = os.path.join(repo_root, ".scraper_changes.json")
        with open(changes_path, "w") as f:
            json.dump({"changes": [], "timestamp": datetime.now().isoformat()}, f)
        sys.exit(0)

    // Read index.html
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

    // Apply changes
    commit_lines = []
    for r in changed:
        ref = r.get("ref", "")
        changes = r.get("_changes", {})
        project = r.get("project", "Unknown")

        print(f"\nUpdating {ref} ({project}):")
        for field, change in changes.items():
            print(f"  {field}: {change.get('old')} -> {change.get('new')}")

        html, updated = find_and_update_entry(html, ref, changes)

        if updated:
            commit_lines.append(f"  - {ref} ({project}): {', '.join(f'{k}: {v.get(\"new\")}' for k, v in changes.items())}")

            // Special handling for FI status
            status_change = changes.get("status", {})
            if status_change.get("new") == "Further Information Requested":
                html = update_summary_for_fi(html, ref)
                html = clear_decision_due_for_fi(html, ref)

    if commit_lines:
        // Write updated index.html
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nUpdated index.html with {len(commit_lines)} change(s).")

        // Write changes summary for commit message
        changes_path = os.path.join(repo_root, ".scraper_changes.json")
        summary = {
            "changes": commit_lines,
            "timestamp": datetime.now().isoformat(),
            "commit_message": f"Auto-update: {len(commit_lines)} application status change(s)\n\n" + "\n".join(commit_lines),
        }
        with open(changes_path, "w") as f:
            json.dump(summary, f, indent=2)

        print("\nCommit message:")
        print(summary["commit_message"])
    else:
        print("\nNo changes could be applied to index.html.")


if __name__ == "__main__":
    main()
