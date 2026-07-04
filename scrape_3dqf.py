#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.27",
#     "pillow>=10",
# ]
# ///
"""
Scrape 3DQF (https://www.3dqf.co.uk) filament listings.

3DQF's storefront is a Wix Stores site. The product grid and the per-product
option drop-downs (Diameter / Weight) are rendered client-side from Wix's
GraphQL catalog API, so plain HTML scraping of a page such as
https://www.3dqf.co.uk/pla-product only ever sees the first ~21 items and never
sees the drop-downs. This script therefore talks to the same GraphQL endpoint
the site itself uses. That also means every listing page can be fetched in one
go (they are all just categories of one catalog), and pagination / "load more"
/ "?page=N" are handled server-side for us.

For each product it extracts:
  * the Name
  * the average RGB of the central portion of the product image
  * (optionally) the per-product detail: available diameters (is it 1.75 mm
    only, or is the larger 2.85 mm size -- 3DQF's equivalent of "3.0 mm" -- also
    offered?) and the available weight options.

Because the whole catalog -- including the Diameter/Weight drop-down options --
comes back in one bulk GraphQL sweep, there is no per-product "sub-page" request:
--details simply decides whether to emit those fields (it costs nothing extra).
The only repeated network activity is downloading product images, so that is
what --image-delay rate-limits (and already-saved images are never re-fetched).

The full catalog is cached to disk (--cache) so re-runs don't re-query the API.

Examples
--------
  # PLA grid, names + average colour only
  ./scrape_3dqf.py pla

  # PLA + PLA+ + ABS + PETG, with diameter/weight details, 1 image/sec
  ./scrape_3dqf.py pla pla-plus abs petg --details --image-delay 1.0

  # the whole shop, as CSV
  ./scrape_3dqf.py shop --format csv -o filaments.csv

  # list the available categories and exit
  ./scrape_3dqf.py --list-categories
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx
from PIL import Image

BASE = "https://www.3dqf.co.uk"
GRAPHQL = f"{BASE}/_api/wix-ecommerce-storefront-web/api"
ACCESS_TOKENS = f"{BASE}/_api/v1/access-tokens"
# Wix Stores appDefId -- the same on every Wix site.
WIX_STORES_APP_ID = "1380b703-ce81-ff05-f115-39571d94dfcd"
USER_AGENT = "Mozilla/5.0 (3dqf-scraper)"

# Friendly name -> Wix collection slug. The keys correspond to the on-site
# listing pages the user cares about; the values are the catalog category slugs
# (the on-site page URL, e.g. "/pla-product", is not the same string as the
# category slug, so we map them explicitly here).
PAGE_CATEGORIES = {
    "pla": "pla-175-285-mm",       # https://www.3dqf.co.uk/pla-product
    "pla-plus": "pla-plus-175mm",  # https://www.3dqf.co.uk/pla-plus-1-75mm-1
    "abs": "abs-175-285-mm",       # https://www.3dqf.co.uk/abs-product
    "petg": "petg-175mm",          # https://www.3dqf.co.uk/petg
    "shop": "all-products",        # https://www.3dqf.co.uk/shop
}

# A selection value that looks like a spool weight, e.g. "1KG", "4.1KG", "500g".
WEIGHT_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:kg|g)\s*$", re.IGNORECASE)
# A selection value that looks like a filament diameter, e.g. "1.75 mm".
DIAMETER_RE = re.compile(r"\d+(?:\.\d+)?\s*mm", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Wix GraphQL client
# --------------------------------------------------------------------------- #
class WixCatalog:
    """Minimal client for the Wix Stores storefront GraphQL catalog."""

    def __init__(self, client: httpx.Client):
        self._client = client
        self._auth = self._fetch_instance_token()

    def _fetch_instance_token(self) -> str:
        r = self._client.get(ACCESS_TOKENS)
        r.raise_for_status()
        apps = r.json().get("apps", {})
        app = apps.get(WIX_STORES_APP_ID)
        if not app or "instance" not in app:
            raise RuntimeError("Could not find the Wix Stores app instance token")
        return app["instance"]

    def query(self, query: str, variables: dict | None = None) -> dict:
        r = self._client.post(
            GRAPHQL,
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}, "source": "WixStoresWebClient"},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]

    def categories(self) -> list[dict]:
        data = self.query(
            "{ catalog { categories(limit: 200) { list { id name slug numOfProducts } } } }"
        )
        return data["catalog"]["categories"]["list"]

    _PRODUCTS_QUERY = """
    query All($limit: Int!, $offset: Int!) {
      catalog {
        products(limit: $limit, offset: $offset, onlyVisible: true) {
          totalCount
          list {
            id name sku productType urlPart formattedPrice isInStock
            categoryIds
            options { title optionType selections { value description inStock } }
            media { url fullUrl mediaType width height index title }
          }
        }
      }
    }
    """

    def all_products(self, page_size: int = 50, delay: float = 0.3) -> list[dict]:
        """Fetch every visible product (all categories in one sweep)."""
        out: list[dict] = []
        offset = 0
        while True:
            data = self.query(self._PRODUCTS_QUERY, {"limit": page_size, "offset": offset})
            pq = data["catalog"]["products"]
            out.extend(pq["list"])
            total = pq["totalCount"]
            print(f"  fetched {len(out)}/{total} products", file=sys.stderr)
            offset += page_size
            if offset >= total:
                break
            time.sleep(delay)
        return out


# --------------------------------------------------------------------------- #
# Catalog cache
# --------------------------------------------------------------------------- #
def load_catalog(cache: Path | None, client: httpx.Client, refresh: bool) -> dict:
    if cache and cache.exists() and not refresh:
        print(f"Using cached catalog: {cache}", file=sys.stderr)
        return json.loads(cache.read_text())

    print("Fetching catalog from Wix GraphQL API...", file=sys.stderr)
    api = WixCatalog(client)
    catalog = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "categories": api.categories(),
        "products": api.all_products(),
    }
    if cache:
        cache.write_text(json.dumps(catalog, indent=1))
        print(f"Saved catalog cache: {cache}", file=sys.stderr)
    return catalog


# --------------------------------------------------------------------------- #
# Option / colour extraction
# --------------------------------------------------------------------------- #
@dataclass
class Filament:
    id: str
    name: str
    price: str | None
    in_stock: bool
    image_url: str | None
    avg_rgb: tuple[int, int, int] | None = None
    avg_hex: str | None = None
    image_path: str | None = None
    # populated only when --details is used
    diameters: list[str] | None = None
    only_175: bool | None = None
    has_larger_diameter: bool | None = None
    weights: list[str] | None = None


def image_url(product: dict, size: int) -> str | None:
    """Return a JPEG URL for the product's primary photo at ~size px."""
    photos = [m for m in product.get("media", []) if m.get("mediaType") == "PHOTO"]
    if not photos:
        return None
    photos.sort(key=lambda m: m.get("index", 0))
    uri = photos[0]["url"]  # e.g. "33dbdc_xx...~mv2.jpg"
    return f"https://static.wixstatic.com/media/{uri}/v1/fill/w_{size},h_{size},q_90/file.jpg"


def extract_diameters(product: dict) -> list[str]:
    diams: list[str] = []
    for opt in product.get("options", []):
        if "diam" in opt["title"].lower():
            for sel in opt["selections"]:
                val = (sel.get("description") or sel.get("value") or "").strip()
                if val and val not in diams:
                    diams.append(val)
    return diams


def extract_weights(product: dict) -> list[str]:
    """Weight options are sometimes under a 'Weight' title, sometimes 'Size',
    and aren't always spelled out on the grid -- so match by value too."""
    weights: list[str] = []
    for opt in product.get("options", []):
        title = opt["title"].lower()
        is_weight_col = "weight" in title
        for sel in opt["selections"]:
            val = (sel.get("description") or sel.get("value") or "").strip()
            if not val:
                continue
            if (is_weight_col or WEIGHT_RE.match(val)) and val not in weights:
                weights.append(val)
    return weights


def is_175(value: str) -> bool:
    return "1.75" in value


def safe_filename(name: str, product_id: str) -> str:
    """A filesystem-safe, unique-ish name for a product's saved image."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]
    return f"{slug or 'filament'}-{product_id[:8]}.jpg"


# --------------------------------------------------------------------------- #
# Average RGB of the central region
# --------------------------------------------------------------------------- #
def average_central_rgb(
    data: bytes, fraction: float = 0.5
) -> tuple[int, int, int]:
    """Average RGB over the central `fraction` (per axis) of the image.

    fraction=0.5 -> the middle box spanning half the width and half the height.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    bw, bh = int(w * fraction), int(h * fraction)
    left = (w - bw) // 2
    top = (h - bh) // 2
    crop = img.crop((left, top, left + bw, top + bh))
    # Down-sampling to 1x1 with a box filter yields the mean pixel value.
    r, g, b = crop.resize((1, 1), Image.BOX).getpixel((0, 0))
    return (r, g, b)


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def select_products(catalog: dict, pages: list[str]) -> list[dict]:
    cats = {c["slug"]: c for c in catalog["categories"]}
    by_name = {c["name"].lower(): c for c in catalog["categories"]}

    wanted_ids: set[str] = set()
    all_products = "all-products" in [PAGE_CATEGORIES.get(p, p) for p in pages]
    for page in pages:
        slug = PAGE_CATEGORIES.get(page, page)
        cat = cats.get(slug) or by_name.get(page.lower())
        if not cat:
            raise SystemExit(f"Unknown category: {page!r}. Use --list-categories to see options.")
        if cat["slug"] == "all-products":
            all_products = True
        wanted_ids.add(cat["id"])

    if all_products:
        return list(catalog["products"])
    return [p for p in catalog["products"] if wanted_ids & set(p.get("categoryIds", []))]


def build_filaments(
    products: list[dict],
    client: httpx.Client,
    *,
    with_details: bool,
    fetch_images: bool,
    image_size: int,
    fraction: float,
    image_delay: float,
    image_dir: Path | None,
) -> list[Filament]:
    if fetch_images and image_dir:
        image_dir.mkdir(parents=True, exist_ok=True)
    results: list[Filament] = []
    for i, p in enumerate(products):
        url = image_url(p, image_size)
        fil = Filament(
            id=p["id"],
            name=p["name"],
            price=p.get("formattedPrice"),
            in_stock=bool(p.get("isInStock")),
            image_url=url,
        )

        if fetch_images and url:
            try:
                dest = image_dir / safe_filename(p["name"], p["id"]) if image_dir else None
                if dest and dest.exists():
                    # Reuse the previously-saved image; don't re-download.
                    data = dest.read_bytes()
                else:
                    resp = client.get(url, headers={"User-Agent": USER_AGENT})
                    resp.raise_for_status()
                    data = resp.content
                    if dest:
                        dest.write_bytes(data)
                    # Rate-limit only real downloads (cache hits are free).
                    if image_delay and i + 1 < len(products):
                        time.sleep(image_delay)
                if dest:
                    fil.image_path = str(dest)
                r, g, b = average_central_rgb(data, fraction)
                fil.avg_rgb = (r, g, b)
                fil.avg_hex = f"#{r:02x}{g:02x}{b:02x}"
            except Exception as exc:  # noqa: BLE001
                print(f"  ! image failed for {p['name']!r}: {exc}", file=sys.stderr)

        if with_details:
            # The drop-down data is already present in the bulk catalog query,
            # so this is a free field-population step -- no extra request.
            diams = extract_diameters(p)
            fil.diameters = diams
            fil.only_175 = bool(diams) and all(is_175(d) for d in diams)
            fil.has_larger_diameter = any(not is_175(d) for d in diams)
            fil.weights = extract_weights(p)

        results.append(fil)
        print(f"  [{i + 1}/{len(products)}] {fil.name}", file=sys.stderr)
    return results


def to_rows(filaments: list[Filament]) -> list[dict]:
    rows = []
    for f in filaments:
        d = asdict(f)
        d["avg_rgb"] = ",".join(map(str, f.avg_rgb)) if f.avg_rgb else ""
        d["diameters"] = "; ".join(f.diameters) if f.diameters else ""
        d["weights"] = "; ".join(f.weights) if f.weights else ""
        rows.append(d)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scrape 3DQF filament listings (name, average colour, diameter & weight options).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Categories: " + ", ".join(PAGE_CATEGORIES),
    )
    ap.add_argument(
        "pages",
        nargs="*",
        default=["pla"],
        help="Which listing(s) to scrape: %(default)s by default. "
        "Accepts friendly names (pla, pla-plus, abs, petg, shop) or a category slug.",
    )
    ap.add_argument("--list-categories", action="store_true", help="List catalog categories and exit.")
    ap.add_argument("--details", action="store_true",
                    help="Also emit per-product diameter/weight options (already in the bulk query -- costs nothing).")
    ap.add_argument("--no-images", action="store_true", help="Skip downloading images / computing average RGB.")
    ap.add_argument("--image-dir", type=Path, default=Path("images"),
                    help="Directory to save each fetched image into (default: images/). Use '' to disable saving.")
    ap.add_argument("--image-size", type=int, default=500, help="Requested image edge in px (default: 500).")
    ap.add_argument("--central-fraction", type=float, default=0.5,
                    help="Central fraction per axis to average (default: 0.5 = central half).")
    ap.add_argument("--image-delay", type=float, default=0.0,
                    help="Rate-limit seconds between image downloads (cache hits are exempt; default: 0).")
    ap.add_argument("--cache", type=Path, default=Path("catalog.json"),
                    help="Catalog cache file (default: catalog.json). Reused unless --refresh.")
    ap.add_argument("--refresh", action="store_true", help="Ignore the cache and re-query the API.")
    ap.add_argument("--format", choices=["json", "csv"], default="json")
    ap.add_argument("-o", "--output", type=Path, help="Write to file instead of stdout.")
    args = ap.parse_args(argv)

    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        catalog = load_catalog(args.cache, client, args.refresh)

        if args.list_categories:
            for c in sorted(catalog["categories"], key=lambda c: c["name"]):
                print(f"{c['slug']:<28} {c['numOfProducts']:>4}  {c['name']}")
            return 0

        products = select_products(catalog, args.pages)
        print(f"Selected {len(products)} products from {args.pages}", file=sys.stderr)

        filaments = build_filaments(
            products, client,
            with_details=args.details,
            fetch_images=not args.no_images,
            image_size=args.image_size,
            fraction=args.central_fraction,
            image_delay=args.image_delay,
            image_dir=args.image_dir if str(args.image_dir) else None,
        )

        # Persist the computed average colours back into the catalog cache so it
        # is a single self-contained source (build_3dqf_db.py reads them here).
        if args.cache and not args.no_images:
            by_id = {f.id: f for f in filaments}
            for p in catalog["products"]:
                f = by_id.get(p["id"])
                if f and f.avg_hex and f.avg_rgb:
                    p["avg_hex"] = f.avg_hex
                    p["avg_rgb"] = list(f.avg_rgb)
                    if f.image_path:
                        p["image_path"] = f.image_path
            args.cache.write_text(json.dumps(catalog, indent=1))

    if args.format == "json":
        text = json.dumps([asdict(f) for f in filaments], indent=2)
    else:
        rows = to_rows(filaments)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()

    if args.output:
        args.output.write_text(text)
        print(f"Wrote {len(filaments)} filaments to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
