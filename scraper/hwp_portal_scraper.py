#!/usr/bin/env python3
"""
HWP Planning Portal Scraper
============================
Scrapes Irish planning authority portals for application status updates.
Supports two portal types:
  1. Agile Applications Citizen Portal (Cork County, Cork City) -- AngularJS SPA
     Uses Playwright headless browser to render the SPA and extract data
     from Angular scope objects.
  2. ePlanning.ie (Limerick) -- Server-rendered HTML
     Uses requests + BeautifulSoup for fast, lightweight scraping.

Each authority can have a primary and fallback portal URL to handle
the ongoing transition between portal systems.

Installation:
  pip install playwright beautifulsoup4 requests
  playwright install chromium

Usage:
  python hwp_portal_scraper.py                    # Check all tracked applications
  python hwp_portal_scraper.py --ref 25/6796      # Check a specific application
  python hwp_portal_scraper.py --output json       # Output as JSON
  python hwp_portal_scraper.py --output csv        # Output as CSV
  python hwp_portal_scraper.py --active-only       # Skip granted/invalid apps
"""

import json
import re
import sys
import argparse
import time
from datetime import datetime

# Optional imports -- graceful fallback if not installed
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# PORTAL REGISTRY
# =============================================================================
# Maps each authority to its portal URLs and types.
# Each authority can have a primary and fallback portal.
# Portal types: "agile" (Citizen Portal SPA) or "eplanning" (legacy ePlanning.ie)

PORTAL_REGISTRY = {
    "Cork County Council": {
        "primary": {
            "type": "agile",
            "base_url": "https://planning.agileapplications.ie/corkcoco",
            "api_url": "https://planningapi.agileapplications.ie",
            "identity_url": "https://identity.agileapplications.ie",
        },
        "fallback": None,
        "notes": "Citizen Portal (AngularJS SPA). Cork County ePlanning has been decommissioned.",
    },
    "Cork City Council": {
        "primary": {
            "type": "agile",
            "base_url": "https://planning.agileapplications.ie/corkcity",
            "api_url": "https://planningapi.agileapplications.ie",
            "identity_url": "https://identity.agileapplications.ie",
        },
        "fallback": None,
        "notes": "Citizen Portal (AngularJS SPA).",
    },
    "Limerick City & County Council": {
        "primary": {
            "type": "eplanning",
            "base_url": "https://www.eplanning.ie/LimerickCCC",
            "detail_path": "/AppFileRefDetails",
        },
        "fallback": None,
        "notes": "Legacy ePlanning.ie (server-rendered HTML). May transition to Citizen Portal.",
    },
}


# Status mapping: normalise portal status strings to dashboard status values
STATUS_MAP = {
    # Agile Applications statuses (from scope.row.status)
    "further information": "Further Information Requested",
    "further information requested": "Further Information Requested",
    "new application": "New Application",
    "new": "New Application",
    "decision made": "Decision Made",
    "grant": "Final Grant Issued",
    "grant permission": "Final Grant Issued",
    "grant with conditions": "Decision Made",
    "refuse": "Decision Made",
    "refuse permission": "Decision Made",
    "invalid": "Invalid",
    "withdrawn": "Invalid",
    # ePlanning.ie statuses (from HTML table)
    "new app": "New Application",
    "new application": "New Application",
    "fi requested": "Further Information Requested",
    "fi received": "Further Information Requested",
    "decided": "Decision Made",
    "application finalised": "Decision Made",
    "granted": "Final Grant Issued",
    "permission c": "Final Grant Issued",
    "permission": "Final Grant Issued",
    "refused": "Decision Made",
    "refuse permission": "Decision Made",
    "retention": "New Application",
}


def normalise_status(raw_status):
    """Normalise a raw status string from any portal to a dashboard status."""
    if not raw_status:
        return None
    cleaned = raw_status.strip().lower()
    for key, value in STATUS_MAP.items():
        if key in cleaned:
            return value
    # If no match, return the original with title case
    return raw_status.strip()


def parse_date(date_str):
    """Parse various Irish date formats to ISO YYYY-MM-DD."""
    if not date_str or date_str.strip() in ("", "-", "N/A", "None"):
        return None
    date_str = date_str.strip()

    # Handle ISO datetime from Agile API (e.g. "2025-12-15T00:00:00")
    if "T" in date_str:
        try:
            return datetime.fromisoformat(date_str.replace("Z", "")).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass

    # Try various date formats
    formats = [
        "%d %b %Y",      # 11 Feb 2026
        "%d/%m/%Y",       # 11/02/2026
        "%Y-%m-%d",       # 2026-02-11
        "%d %B %Y",       # 11 February 2026
        "%d-%m-%Y",       # 11-02-2026
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# =============================================================================
# AGILE APPLICATIONS SCRAPER (Cork County, Cork City)
# =============================================================================
# The Agile Citizen Portal is an AngularJS single-page application.
# Data is NOT in the server HTML -- it's rendered client-side by Angular.
# We use Playwright to load the page, wait for Angular, then extract
# data directly from the Angular scope objects via JavaScript evaluation.
#
# Key discovery:
#   - Search results URL:
#     /corkcoco/search-applications/results?criteria={"openApplications":false,"reference":"25/6796"}&page=1
#   - Angular table rows have ng-click="$ctrl.actionClickRow(row)"
#   - scope.row contains the full JSON: {id, reference, status, applicantSurname,
#     registrationDate, decisionDate, finalGrantDate, location, proposal, ...}
#   - Detail page: /corkcoco/application-details/{id}
#   - Detail page has form textboxes with labels for all fields

class AgilePortalScraper:
    """Scrapes the Agile Applications Citizen Portal using Playwright."""

    def __init__(self, portal_config, playwright_browser=None):
        self.base_url = portal_config["base_url"]
        self.browser = playwright_browser
        self.page = None

    def _ensure_page(self):
        """Create a browser page if we don't have one."""
        if self.page is None:
            self.page = self.browser.new_page()

    def scrape_application(self, ref):
        """Scrape a single application by reference number."""
        self._ensure_page()

        # Build the search URL with criteria
        criteria = json.dumps({"openApplications": False, "reference": ref})
        search_url = f"{self.base_url}/search-applications/results?criteria={criteria}&page=1"

        print(f"  Loading Agile portal: {self.base_url}", file=sys.stderr)
        print(f"  Searching for {ref}...", file=sys.stderr)

        try:
            self.page.goto(search_url, wait_until="networkidle", timeout=30000)
            # Wait for Angular to render the table
            self.page.wait_for_selector("tr[ng-click]", timeout=15000)
        except Exception as e:
            print(f"  [WARNING] Page load/render failed: {e}", file=sys.stderr)
            return None

        # Extract data from Angular scope
        try:
            row_data = self.page.evaluate("""() => {
                try {
                    const row = document.querySelector('tr[ng-click]');
                    if (!row) return null;
                    const scope = angular.element(row).scope();
                    const rowVar = scope.row || scope.$parent?.row;
                    if (!rowVar) return null;
                    # Return a clean copy (no Angular $$hashKey etc.)
                    return JSON.parse(JSON.stringify(rowVar));
                } catch(e) {
                    return {error: e.message};
                }
            }""")
        except Exception as e:
            print(f"  [WARNING] Angular scope extraction failed: {e}", file=sys.stderr)
            return None

        if not row_data or "error" in row_data:
            print(f"  [INFO] No results found for {ref}", file=sys.stderr)
            return None

        # Verify the reference matches
        if row_data.get("reference") != ref:
            print(f"  [WARNING] Reference mismatch: expected {ref}, got {row_data.get('reference')}", file=sys.stderr)
            return None

        # Map Agile API fields to our standard format
        result = {
            "ref": row_data.get("reference", ref),
            "status": normalise_status(row_data.get("status")),
            "raw_status": row_data.get("status"),
            "client": row_data.get("applicantSurname", ""),
            "agent": row_data.get("agentName", ""),
            "proposal": row_data.get("proposal", ""),
            "location": row_data.get("location", ""),
            "regDate": parse_date(row_data.get("registrationDate")),
            "decDate": parse_date(row_data.get("decisionDate")),
            "grantDate": parse_date(row_data.get("finalGrantDate")),
            "decisionOutcome": row_data.get("decisionText", ""),
            "agile_id": row_data.get("id"),
            "detail_url": f"{self.base_url}/application-details/{row_data.get('id')}",
        }

        # Optionally fetch the detail page for extra fields (subDue, decDue, status description)
        if row_data.get("id"):
            detail = self._fetch_detail(row_data["id"])
            if detail:
                result.update(detail)

        return result

    def _fetch_detail(self, app_id):
        """Fetch the detail page for additional fields not in search results."""
        detail_url = f"{self.base_url}/application-details/{app_id}"
        print(f"  Fetching detail page (ID: {app_id})...", file=sys.stderr)

        try:
            self.page.goto(detail_url, wait_until="networkidle", timeout=30000)
            # Wait for the form to load
            self.page.wait_for_selector("label", timeout=10000)
            time.sleep(1)  # Extra wait for Angular to populate form fields
        except Exception as e:
            print(f"  [WARNING] Detail page load failed: {e}", file=sys.stderr)
            return {}

        # Extract form field values via JavaScript
        try:
            detail_data = self.page.evaluate("""() => {
                const data = {};
                const labels = document.querySelectorAll('label');
                labels.forEach(label => {
                    const labelText = label.textContent.trim();
                    const forAttr = label.getAttribute('for');
                    let input = forAttr ? document.getElementById(forAttr) : null;
                    if (!input) input = label.parentElement?.querySelector('input, textarea');
                    if (input && input.value && input.value !== '$ctrl.model') {
                        data[labelText] = input.value;
                    }
                });
                # Also check for non-input display elements (used for Decision, Status description)
                const genericLabels = document.querySelectorAll('label');
                genericLabels.forEach(label => {
                    const labelText = label.textContent.trim();
                    const next = label.nextElementSibling;
                    if (next && !next.matches('input, textarea, select') && next.textContent.trim()) {
                        data[labelText + ' (display)'] = next.textContent.trim();
                    }
                });
                return data;
            }""")
        except Exception as e:
            print(f"  [WARNING] Detail extraction failed: {e}", file=sys.stderr)
            return {}

        if not detail_data:
            return {}

        # Map detail fields
        result = {}
        for key, value in detail_data.items():
            key_lower = key.lower()
            if "submissions" in key_lower or "observations" in key_lower:
                result["subDue"] = parse_date(value)
            elif "decision due" in key_lower:
                result["decDue"] = parse_date(value)
            elif "final grant" in key_lower:
                if not result.get("grantDate"):
                    result["grantDate"] = parse_date(value)
            elif key_lower == "status":
                if value and value != "$ctrl.model":
                    result["status"] = normalise_status(value)
                    result["raw_status"] = value
            elif "applicant" in key_lower and value:
                result["client"] = value
            elif "eircode" in key_lower and value:
                result["eircode"] = value

        return result

    def close(self):
        """Close the browser page."""
        if self.page:
            self.page.close()
            self.page = None


# =============================================================================
# ePLANNING.IE SCRAPER (Limerick, and other legacy councils)
# =============================================================================
# ePlanning.ie is a traditional server-rendered ASP.NET site.
# Application detail pages return full HTML with all data visible.
# URL format: /LimerickCCC/AppFileRefDetails/{ref}/0
#
# Key fields found in the HTML (from live inspection of ref 2561339):
#   - File Number, Application Type, Planning Status
#   - Received Date, Decision Due Date, Validated Date
#   - Decision Type, Decision Date, Grant Date
#   - Further Info Requested/Received dates
#   - Applicant name, Development Description, Development Address
#   - Submissions By date

class EPlanningPortalScraper:
    """Scrapes the ePlanning.ie portal using requests + BeautifulSoup."""

    def __init__(self, portal_config):
        self.base_url = portal_config["base_url"]
        self.detail_path = portal_config.get("detail_path", "/AppFileRefDetails")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def scrape_application(self, ref):
        """Scrape an application by reference from ePlanning.ie."""
        # ePlanning URL format: /LimerickCCC/AppFileRefDetails/{ref}/0
        # ref may contain slashes (e.g. 25/1234) -- strip them for the URL
        clean_ref = ref.replace("/", "")
        url = f"{self.base_url}{self.detail_path}/{clean_ref}/0"

        print(f"  Fetching ePlanning page: {url}", file=sys.stderr)

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            return self._parse_detail_page(resp.text, ref)
        except Exception as e:
            print(f"  [WARNING] ePlanning request failed: {e}", file=sys.stderr)
            return None

    def _parse_detail_page(self, html, ref):
        """
        Parse an ePlanning.ie application detail page.

        The page uses HTML tables with label-value pairs in <td> elements.
        Key structure (from live Limerick portal):
          <td>File Number:</td><td>2561339</td>
          <td>Planning Status:</td><td>NEW APPLICATION</td>
          <td>Received Date:</td><td>16/12/2025</td>
          <td>Decision Due Date:</td><td>18/02/2026</td>
          etc.
        """
        soup = BeautifulSoup(html, "html.parser")
        result = {"ref": ref}

        # Check for error page
        if "no results found" in html.lower() or "error" in soup.title.string.lower() if soup.title else False:
            return None

        # Extract all table rows with label-value pairs
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().rstrip(":")
                value = cells[1].get_text(strip=True)

                if not value or value == "-":
                    continue

                # Map ePlanning fields to our standard format
                if "file number" in label:
                    result["ref"] = value
                elif "planning status" in label:
                    result["status"] = normalise_status(value)
                    result["raw_status"] = value
                elif "decision due" in label:
                    result["decDue"] = parse_date(value)
                elif label == "decision date" or label == "decision date":
                    result["decDate"] = parse_date(value)
                elif "decision type" in label:
                    result["decisionOutcome"] = value
                elif "received date" in label:
                    result["regDate"] = parse_date(value)
                elif "applicant name" in label:
                    result["client"] = value
                elif "development address" in label:
                    result["location"] = value
                elif "development description" in label:
                    result["proposal"] = value
                elif "grant date" in label:
                    result["grantDate"] = parse_date(value)
                elif "submissions by" in label:
                    result["subDue"] = parse_date(value)
                elif "further info requested" in label and value:
                    result["fi_requested"] = parse_date(value)
                    # If FI was requested, override status
                    if result.get("status") == "New Application" and parse_date(value):
                        result["status"] = "Further Information Requested"
                elif "further info received" in label and value:
                    result["fi_received"] = parse_date(value)

        return result if len(result) > 1 else None


# =============================================================================
# MAIN SCRAPER ORCHESTRATOR
# =============================================================================

class HWPPortalScraper:
    """
    Main scraper that orchestrates checking all portals for all tracked
    applications, with fallback logic for dual-portal support.
    """

    def __init__(self, portal_registry=None):
        self.registry = portal_registry or PORTAL_REGISTRY
        self._scrapers = {}
        self._playwright = None
        self._browser = None

    def _start_browser(self):
        """Start Playwright browser for Agile portal scraping."""
        if not HAS_PLAYWRIGHT:
            print("  [ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
            return False
        if self._browser is None:
            print("  Starting headless browser...", file=sys.stderr)
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        return True

    def _get_scraper(self, portal_config):
        """Get or create a scraper instance for a portal config."""
        key = portal_config["base_url"]
        if key not in self._scrapers:
            if portal_config["type"] == "agile":
                if not self._start_browser():
                    return None
                self._scrapers[key] = AgilePortalScraper(portal_config, self._browser)
            elif portal_config["type"] == "eplanning":
                if not HAS_REQUESTS or not HAS_BS4:
                    print("  [ERROR] requests/beautifulsoup4 not installed.", file=sys.stderr)
                    return None
                self._scrapers[key] = EPlanningPortalScraper(portal_config)
            else:
                raise ValueError(f"Unknown portal type: {portal_config['type']}")
        return self._scrapers[key]

    def check_application(self, auth, ref):
        """
        Check an application's current status on the council portal.
        Tries the primary portal first, falls back to secondary if available.
        """
        if auth not in self.registry:
            print(f"  [WARNING] Unknown authority: {auth}", file=sys.stderr)
            return None

        entry = self.registry[auth]

        # Try primary portal
        primary = entry.get("primary")
        if primary:
            scraper = self._get_scraper(primary)
            if scraper:
                result = scraper.scrape_application(ref)
                if result:
                    result["source"] = primary["base_url"]
                    result["portal_type"] = primary["type"]
                    return result

        # Try fallback portal
        fallback = entry.get("fallback")
        if fallback:
            print(f"  Primary portal failed, trying fallback...", file=sys.stderr)
            scraper = self._get_scraper(fallback)
            if scraper:
                result = scraper.scrape_application(ref)
                if result:
                    result["source"] = fallback["base_url"]
                    result["portal_type"] = fallback["type"]
                    return result

        return None

    def check_all(self, applications):
        """
        Check all tracked applications for status updates.

        Args:
            applications: list of dicts with at least {auth, ref, status}

        Returns:
            list of dicts with updated fields and change indicators
        """
        results = []

        for app in applications:
            auth = app.get("auth", "")
            ref = app.get("ref", "")
            current_status = app.get("status", "")

            print(f"\nChecking {ref} ({auth})...", file=sys.stderr)

            portal_data = self.check_application(auth, ref)

            if portal_data is None:
                results.append({
                    **app,
                    "_scrape_status": "failed",
                    "_error": "Could not fetch from any portal",
                })
                continue

            # Determine if there are changes
            changes = {}
            new_status = portal_data.get("status")
            if new_status and new_status != current_status:
                changes["status"] = {
                    "old": current_status,
                    "new": new_status,
                }

            # Check for new/changed dates and fields
            for field in ["decDue", "decDate", "grantDate", "client", "decisionOutcome", "subDue"]:
                old_val = app.get(field)
                new_val = portal_data.get(field)
                if new_val and new_val != old_val:
                    changes[field] = {"old": old_val, "new": new_val}

            results.append({
                **app,
                "_scrape_status": "success",
                "_portal_data": portal_data,
                "_changes": changes,
                "_has_changes": len(changes) > 0,
                "_source": portal_data.get("source", ""),
            })

        return results

    def print_report(self, results):
        """Print a human-readable report of scrape results."""
        print("\n" + "=" * 70, file=sys.stderr)
        print("HWP PLANNING PORTAL SCRAPE REPORT", file=sys.stderr)
        print(f"Date: {datetime.now().strftime('%d %B %Y %H:%M')}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)

        changed = [r for r in results if r.get("_has_changes")]
        failed = [r for r in results if r.get("_scrape_status") == "failed"]
        unchanged = [r for r in results if r.get("_scrape_status") == "success" and not r.get("_has_changes")]

        if changed:
            print(f"\n--- STATUS CHANGES DETECTED ({len(changed)}) ---", file=sys.stderr)
            for r in changed:
                print(f"\n  {r['ref']} - {r.get('project', r.get('proposal', 'Unknown'))}", file=sys.stderr)
                print(f"  Authority: {r['auth']}", file=sys.stderr)
                for field, change in r["_changes"].items():
                    print(f"  {field}: {change['old']} -> {change['new']}", file=sys.stderr)

        if unchanged:
            print(f"\n--- NO CHANGES ({len(unchanged)}) ---", file=sys.stderr)
            for r in unchanged:
                print(f"  {r['ref']} - {r.get('status', 'Unknown')} (unchanged)", file=sys.stderr)

        if failed:
            print(f"\n--- FAILED TO CHECK ({len(failed)}) ---", file=sys.stderr)
            for r in failed:
                print(f"  {r['ref']} - {r.get('_error', 'Unknown error')}", file=sys.stderr)

        print("\n" + "=" * 70, file=sys.stderr)
        return changed

    def close(self):
        """Clean up browser resources."""
        for scraper in self._scrapers.values():
            if hasattr(scraper, "close"):
                scraper.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()


# =============================================================================
# DEFAULT TRACKED APPLICATIONS (from dashboard)
# =============================================================================

DEFAULT_APPLICATIONS = [
    {"auth": "Cork County Council", "ref": "25/5800", "project": "Health & Wellbeing Clinic", "status": "Final Grant Issued"},
    {"auth": "Cork County Council", "ref": "25/6573", "project": "Solar Farm Development", "status": "Invalid"},
    {"auth": "Cork County Council", "ref": "25/6607", "project": "Castlelake LRD Amendments", "status": "Further Information Requested"},
    {"auth": "Cork County Council", "ref": "25/6672", "project": "Mallow Sports Facility", "status": "Further Information Requested"},
    {"auth": "Cork County Council", "ref": "25/6737", "project": "Stryker Carrigtwohill Upgrade", "status": "Decision Made"},
    {"auth": "Cork County Council", "ref": "25/6773", "project": "Bantry Industrial Unit", "status": "Further Information Requested"},
    {"auth": "Cork County Council", "ref": "25/6796", "project": "Cobh Apartment Conversion", "status": "Further Information Requested"},
    {"auth": "Cork County Council", "ref": "25/6898", "project": "Millstreet Office Extension", "status": "New Application"},
    {"auth": "Cork County Council", "ref": "25/6968", "project": "Midleton Mixed-Use", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "25/44409", "project": "Woods Street Residential", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "25/44429", "project": "Douglas LRD", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "25/44442", "project": "Bessborough LRD", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "26/44475", "project": "Ballincollig Residential", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "26/44480", "project": "South Mall Cafe Retention", "status": "New Application"},
    {"auth": "Cork City Council", "ref": "26/44506", "project": "Sullivan's Quay LRD", "status": "New Application"},
    {"auth": "Limerick City & County Council", "ref": "2561339", "project": "Limerick Retention", "status": "New Application"},
]


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HWP Planning Portal Scraper - Check planning application statuses"
    )
    parser.add_argument(
        "--ref", type=str, help="Check a specific application reference only"
    )
    parser.add_argument(
        "--auth", type=str, help="Filter by authority name"
    )
    parser.add_argument(
        "--output", choices=["text", "json", "csv"], default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--active-only", action="store_true",
        help="Only check active applications (skip Granted/Invalid)"
    )

    args = parser.parse_args()

    # Check dependencies
    if not HAS_PLAYWRIGHT:
        print("WARNING: Playwright not installed. Agile portal scraping (Cork) will be skipped.", file=sys.stderr)
        print("  Install: pip install playwright && playwright install chromium\n", file=sys.stderr)
    if not HAS_REQUESTS or not HAS_BS4:
        print("WARNING: requests/beautifulsoup4 not installed. ePlanning scraping (Limerick) will be skipped.", file=sys.stderr)
        print("  Install: pip install requests beautifulsoup4\n", file=sys.stderr)

    # Filter applications
    apps = DEFAULT_APPLICATIONS

    if args.ref:
        apps = [a for a in apps if a["ref"] == args.ref]
        if not apps:
            print(f"Application {args.ref} not found in tracked list.", file=sys.stderr)
            sys.exit(1)

    if args.auth:
        apps = [a for a in apps if args.auth.lower() in a["auth"].lower()]

    if args.active_only:
        apps = [a for a in apps if a["status"] not in ("Final Grant Issued", "Invalid")]

    # Run scraper
    scraper = HWPPortalScraper()
    try:
        results = scraper.check_all(apps)

        if args.output == "json":
            clean = []
            for r in results:
                entry = {k: v for k, v in r.items() if not k.startswith("_")}
                entry["_changes"] = r.get("_changes", {})
                entry["_has_changes"] = r.get("_has_changes", False)
                entry["_scrape_status"] = r.get("_scrape_status", "unknown")
                clean.append(entry)
            print(json.dumps(clean, indent=2, default=str))
        elif args.output == "csv":
            print("ref,project,auth,old_status,new_status,change_detected,source")
            for r in results:
                changes = r.get("_changes", {})
                new_status = changes.get("status", {}).get("new", "")
                print(f'{r["ref"]},{r.get("project","")},{r["auth"]},{r["status"]},{new_status},{r.get("_has_changes",False)},{r.get("_source","")}')
        else:
            changed = scraper.print_report(results)
            if changed:
                print(f"\n{len(changed)} application(s) have status changes.", file=sys.stderr)
            else:
                print("\nNo status changes detected.", file=sys.stderr)
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
