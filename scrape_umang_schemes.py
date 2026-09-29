"""
Scrapes government schemes from UMANG (myscheme-prod-api.umangapp.in)
for a given category, cleans each one into flat fields, and tags
relevance for investor / startup_founder / manufacturer.
 
Outputs:
  schemes.json          -- all scraped schemes, cleaned
  schemes_relevant.json -- subset tagged investor/startup_founder/manufacturer
  slugs.json            -- list of scheme slugs found (bookkeeping)
  umang_schemes_raw/    -- raw per-scheme API responses (cache, ignore)
 
Usage:
    pip install requests tqdm
    python scrape_umang_schemes.py
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm

BASE = "https://myscheme-prod-api.umangapp.in"
SEARCH_URL = f"{BASE}/search/v7/schemes"
DETAIL_URL = f"{BASE}/schemes/v7/public/schemes"

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://web.umang.gov.in",
    "Referer": "https://web.umang.gov.in/",
    "x-api-key": "hOMSczmhIg32lHy0jJHHW6P2Eg7skS1V9cI3skBc",
    "User-Agent": "Mozilla/5.0 (compatible; scheme-collector/1.0)",
}

PAGE_SIZE = 20
WORKERS = 5              # Keep low to avoid overwhelming the gov server
OUT_DIR = "umang_schemes_raw"
OUT_FILE = "schemes.json"
RELEVANT_FILE = "schemes_relevant.json"

# Keywords used to tag scheme relevance (matched case-insensitively across 
# categories, tags, descriptions, etc.)
RELEVANCE_KEYWORDS = {
    "investor": [
        "investor", "investment", "venture", "equity", "fund of funds",
        "seed fund", "angel", "capital",
    ],
    "startup_founder": [
        "startup", "entrepreneur", "entrepreneurship", "incubat",
        "innovation", "self-employ", "new enterprise",
    ],
    "manufacturer": [
        "manufactur", "msme", "industry", "industrial", "production",
        "export", "make in india", "textile", "handicraft", "artisan",
        "fabrication", "processing unit",
    ],
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=5,
        backoff_factor=1.5,   # 0, 1.5s, 3s, 6s, 12s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    return s


SESSION = make_session()


CATEGORY_FILTER = "Business & Entrepreneurship"


def search_page(offset: int) -> dict:
    # a category filter is required -- an empty q=[] returns no schemes
    q = json.dumps([{"identifier": "schemeCategory", "value": CATEGORY_FILTER}])
    params = {"size": PAGE_SIZE, "lang": "en", "from": offset, "q": q,
              "sort": "multiple_sort"}
    r = SESSION.get(SEARCH_URL, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_detail(slug: str) -> dict:
    r = SESSION.get(DETAIL_URL, params={"slug": slug, "lang": "en"}, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_items_and_total(page_json: dict):
    """
    Confirmed shape (from a real capture):
      data.hits.items[]        -- each item: {"id", "fields": {...}, "highlight"}
      data.hits.page.total     -- overall count for the applied filter
    """
    data = page_json.get("data", page_json)
    hits = data.get("hits", {})
    items = hits.get("items", [])
    total = hits.get("page", {}).get("total")
    if not isinstance(total, int):
        total = data.get("summary", {}).get("total", len(items))
    if not items:
        print("[warning] no items on this page -- dumping response to "
              "debug_response.json")
        with open("debug_response.json", "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2, ensure_ascii=False)
    return items, total


def slug_from_item(item: dict, warn_if_missing=False):
    fields = item.get("fields", item)
    for key in ("slug", "schemeSlug"):
        if key in fields:
            v = fields[key]
            return v[0] if isinstance(v, list) else v
    slug = item.get("_id") or item.get("id")
    if not slug and warn_if_missing:
        print("[warning] could not find a slug in this item -- full item:")
        print(json.dumps(item, indent=2)[:2000])
    return slug


def _label(x, default=""):
    """Lots of fields come as {"value":..., "label":...} -- we only want label."""
    if isinstance(x, dict):
        return x.get("label", default)
    return x if x else default


def _labels(xs):
    if not xs:
        return []
    return [_label(x) for x in xs if _label(x)]


def _richtext_to_text(nodes) -> str:
    """Fallback plain-text extractor for rich-text trees, used only if
    a *_md field is missing/empty for some scheme."""
    if not nodes:
        return ""
    out = []

    def walk(n):
        if isinstance(n, dict):
            if "text" in n:
                out.append(n["text"])
            for child in n.get("children", []):
                walk(child)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(nodes)
    return " ".join(t for t in out if t).strip()


def tag_relevance(scheme: dict) -> list:
    haystack = " ".join([
        scheme.get("name", ""),
        scheme.get("brief_description", ""),
        scheme.get("scheme_for", ""),
        " ".join(scheme.get("categories", [])),
        " ".join(scheme.get("sub_categories", [])),
        " ".join(scheme.get("tags", [])),
        " ".join(scheme.get("target_beneficiaries", [])),
    ]).lower()
    return [tag for tag, keywords in RELEVANCE_KEYWORDS.items()
            if any(kw in haystack for kw in keywords)]


def build_full_text(scheme: dict) -> str:
    parts = [f"# {scheme['name']}"]
    if scheme.get("brief_description"):
        parts.append(scheme["brief_description"])
    if scheme.get("detailed_description"):
        parts.append("## Description\n" + scheme["detailed_description"])
    if scheme.get("eligibility"):
        parts.append("## Eligibility\n" + scheme["eligibility"])
    if scheme.get("benefits"):
        parts.append("## Benefits\n" + scheme["benefits"])
    for step in scheme.get("application_process", []):
        if step.get("steps"):
            parts.append(f"## How to Apply ({step.get('mode', 'N/A')})\n{step['steps']}")
    if scheme.get("exclusions"):
        parts.append("## Exclusions\n" + scheme["exclusions"])
    return "\n\n".join(parts).strip()


def parse_detail(detail_json: dict, slug: str) -> dict:
    data = detail_json.get("data", {})
    en = data.get("en", {})
    basic = en.get("basicDetails", {})
    content = en.get("schemeContent", {})
    elig = en.get("eligibilityCriteria", {})
    app_process = en.get("applicationProcess", []) or []

    eligibility_md = (elig.get("eligibilityDescription_md") or "").strip()
    if not eligibility_md:
        eligibility_md = _richtext_to_text(elig.get("eligibilityDescription"))

    detailed_desc_md = (content.get("detailedDescription_md") or "").strip()
    if not detailed_desc_md:
        detailed_desc_md = _richtext_to_text(content.get("detailedDescription"))

    benefits_md = (content.get("benefits_md") or "").strip()
    if not benefits_md:
        benefits_md = _richtext_to_text(content.get("benefits"))

    application_steps = []
    for step in app_process:
        text = (step.get("process_md") or "").strip()
        if not text:
            text = _richtext_to_text(step.get("process"))
        application_steps.append({"mode": step.get("mode", ""), "steps": text})

    scheme = {
        "slug": data.get("slug", slug),
        "name": (basic.get("schemeName") or "").strip(),
        "short_title": basic.get("schemeShortTitle", ""),
        "brief_description": content.get("briefDescription", ""),
        "detailed_description": detailed_desc_md,
        "eligibility": eligibility_md,
        "benefits": benefits_md,
        "benefit_type": _label(content.get("benefitTypes")),
        "exclusions": content.get("exclusions_md", ""),
        "application_process": application_steps,
        "level": _label(basic.get("level")),
        "scheme_type": _label(basic.get("schemeType")),
        "implementing_agency": basic.get("implementingAgency", ""),
        "nodal_ministry": _label(basic.get("nodalMinistryName")),
        "nodal_department": _label(basic.get("nodalDepartmentName")),
        "categories": _labels(basic.get("schemeCategory")),
        "sub_categories": _labels(basic.get("schemeSubCategory")),
        "target_beneficiaries": _labels(basic.get("targetBeneficiaries")),
        "scheme_for": basic.get("schemeFor", ""),
        "tags": basic.get("tags", []),
        "references": [
            {"title": r.get("title", ""), "url": r.get("url", "")}
            for r in content.get("references", [])
        ],
    }
    scheme["relevance"] = tag_relevance(scheme)
    scheme["full_text"] = build_full_text(scheme)
    return scheme


def collect_all_slugs() -> list:
    slugs = []
    offset = 0
    first = True
    total = None
    with tqdm(desc="Collecting scheme slugs") as pbar:
        while True:
            page = search_page(offset)
            items, total = extract_items_and_total(page)
            if first:
                print(f"Site reports total for '{CATEGORY_FILTER}': {total}")
                pbar.total = total
                first = False
            if not items:
                break
            for i, it in enumerate(items):
                s = slug_from_item(it, warn_if_missing=(offset == 0 and i == 0))
                if s:
                    slugs.append(s)
            pbar.update(len(items))
            offset += PAGE_SIZE
            if offset >= total:
                break
    return slugs


def fetch_and_parse(slug: str):
    raw_path = os.path.join(OUT_DIR, f"{slug}.json")
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            detail = json.load(f)
    else:
        detail = get_detail(slug)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
    return parse_detail(detail, slug)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    slugs = sorted(set(collect_all_slugs()))
    print(f"\nTotal unique schemes collected: {len(slugs)}")
    with open("slugs.json", "w", encoding="utf-8") as f:
        json.dump(slugs, f, indent=2)

    all_schemes = []
    failures = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_and_parse, slug): slug for slug in slugs}
        for fut in tqdm(as_completed(futures), total=len(futures),
                         desc="Fetching scheme details"):
            slug = futures[fut]
            try:
                all_schemes.append(fut.result())
            except Exception as e:
                failures.append((slug, str(e)))

    all_schemes.sort(key=lambda s: s["slug"])

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_schemes, f, ensure_ascii=False, indent=2)

    relevant = [s for s in all_schemes if s["relevance"]]
    with open(RELEVANT_FILE, "w", encoding="utf-8") as f:
        json.dump(relevant, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(all_schemes)} schemes -> {OUT_FILE}")
    print(f"{len(relevant)} of those tagged investor/startup_founder/manufacturer -> {RELEVANT_FILE}")
    if failures:
        print(f"{len(failures)} schemes failed to fetch/parse:")
        for slug, err in failures[:20]:
            print(f"  {slug}: {err}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")


if __name__ == "__main__":
    main()
