#!/usr/bin/env python3
"""Find the Workday endpoint for a company, so you can add it to config.yaml.

Most large-but-not-big-tech employers — regional banks, insurers, hospital
systems, retailers, manufacturers — run Workday. They're invisible to the
GitHub tracker repos, which only cover big tech and startups. The problem is
that a Workday feed needs three values (host, tenant, site) that aren't
published anywhere, so this finds them by probing.

How it works — the Workday CXS API leaks whether a tenant exists:

    POST /wday/cxs/<tenant>/<bogus-site>/jobs
      404 -> tenant and cluster are correct, site name is wrong
      422 -> tenant or cluster is wrong

So we sweep clusters for a 404 to pin down the tenant, then try common site
names against it until one returns 200.

Usage:
    python tools/discover_workday.py rocket comerica huntington
    python tools/discover_workday.py --yaml rocket       # emit config.yaml block

Anything it prints is ready to paste into the `companies:` list.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import urllib.error
import urllib.request

# Workday shards customers across numbered clusters. These are the common ones.
CLUSTERS = ["wd1", "wd3", "wd5", "wd10", "wd12", "wd101", "wd103", "wd105"]

BOGUS_SITE = "ZZZ_NOT_A_REAL_SITE"

# Ordered roughly by how often they turn up in the wild.
SITE_CANDIDATES = [
    "External",
    "Careers",
    "External_Career_Site",
    "ExternalCareerSite",
    "CareerSite",
    "External_Careers",
    "Careers_External",
    "careers",
    "external",
    "Search",
    "Jobs",
    "External_Site",
    "US_External",
    "Corporate",
    "Professional",
    "GlobalCareers",
    "Global_Careers",
    "Campus",
    "Student",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _post(host: str, tenant: str, site: str, timeout: int = 15):
    """Returns (status_code, parsed_body_or_None)."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    body = json.dumps(
        {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "intern"}
    ).encode()
    req = urllib.request.Request(url, data=body, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return -1, None


def find_cluster(tenant: str) -> str | None:
    """Sweep clusters for the 404 that means 'tenant exists, site name wrong'."""
    def check(cluster):
        host = f"{tenant}.{cluster}.myworkdayjobs.com"
        status, _ = _post(host, tenant, BOGUS_SITE)
        return cluster if status == 404 else None

    with cf.ThreadPoolExecutor(max_workers=len(CLUSTERS)) as ex:
        for result in ex.map(check, CLUSTERS):
            if result:
                return result
    return None


def site_candidates(tenant: str, name: str | None = None) -> list[str]:
    """Generic slugs plus ones derived from the company name.

    Plenty of tenants use their own name as the site (Capital One's is
    `Capital_One`), which no generic list will ever guess — so build those
    variants from whatever name we were given.
    """
    cands = list(SITE_CANDIDATES)
    words = [w for w in (name or tenant).replace("_", " ").split() if w]
    if words:
        joined = "".join(w.capitalize() for w in words)     # CapitalOne
        under = "_".join(w.capitalize() for w in words)     # Capital_One
        for base in dict.fromkeys([joined, under, tenant.capitalize(), tenant]):
            cands += [
                base,
                f"{base}_Careers",
                f"{base}Careers",
                f"{base}_External",
                f"{base}_External_Career_Site",
                f"{base}_Career_Site",
                f"{base}_Jobs",
            ]
    # De-dupe, preserve order.
    return list(dict.fromkeys(cands))


def find_site(tenant: str, cluster: str, name: str | None = None):
    """Try candidate site slugs until one returns 200. Returns (site, intern_hits)."""
    host = f"{tenant}.{cluster}.myworkdayjobs.com"

    def check(site):
        status, data = _post(host, tenant, site)
        if status == 200 and data is not None:
            return site, data.get("total", 0)
        return None

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for result in ex.map(check, site_candidates(tenant, name)):
            if result:
                return result
    return None


def discover(tenant: str, name: str | None = None) -> dict | None:
    cluster = find_cluster(tenant)
    if not cluster:
        return None
    found = find_site(tenant, cluster, name)
    if not found:
        # Tenant is real but none of our guesses matched; still worth reporting,
        # since the site name can be read off the company's careers URL by hand.
        return {"tenant": tenant, "cluster": cluster, "site": None, "intern_hits": 0}
    site, hits = found
    return {
        "tenant": tenant,
        "cluster": cluster,
        "site": site,
        "intern_hits": hits,
        "host": f"{tenant}.{cluster}.myworkdayjobs.com",
    }


def as_yaml(name: str, result: dict) -> str:
    return (
        f'  - name: "{name}"\n'
        f'    type: "workday"\n'
        f'    host: "{result["host"]}"\n'
        f'    tenant: "{result["tenant"]}"\n'
        f'    site: "{result["site"]}"\n'
        f"    enabled: true"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tenants", nargs="+",
                    help="Tenant slugs to try. Use 'slug=Display Name' to also "
                         "derive site guesses from the name, e.g. "
                         "'capitalone=Capital One'.")
    ap.add_argument("--yaml", action="store_true",
                    help="Print a ready-to-paste config.yaml block for each hit.")
    args = ap.parse_args()

    blocks = []
    for raw in args.tenants:
        tenant, _, name = raw.partition("=")
        result = discover(tenant, name or None)
        if not result:
            print(f"{tenant:22} no Workday tenant found (try another spelling)")
            continue
        if not result["site"]:
            print(f"{tenant:22} tenant exists on {result['cluster']}, "
                  f"but site name not in the candidate list — check their "
                  f"careers URL for the slug after the domain")
            continue
        print(f"{tenant:22} {result['host']:44} site={result['site']:24} "
              f"intern_hits={result['intern_hits']}")
        blocks.append(as_yaml(name or tenant.capitalize(), result))

    if args.yaml and blocks:
        print("\n# --- paste into config.yaml under `companies:` ---")
        print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
