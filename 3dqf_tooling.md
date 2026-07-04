# 3DQF filament tooling

Scripts that (re)generate [`filaments/3dqf.json`](filaments/3dqf.json) from the
live 3DQF store. 3DQF runs on **Wix Stores**, so both scripts talk to the same
GraphQL catalog API the site itself uses (product grid + option drop-downs are
rendered client-side, so plain HTML scraping misses most of it).

## Files

| File | Purpose |
|------|---------|
| `scrape_3dqf.py` | Fetches the catalog via the Wix GraphQL API, downloads product images, computes the average colour of each, and caches everything to `catalog.json` (+ saved images under `images/`). |
| `build_3dqf_db.py` | Turns `catalog.json` into the SpoolmanDB `filaments/3dqf.json`. |
| `catalog.json` | Cache of the full catalog (names, options, image URLs, and the computed `avg_hex`/`avg_rgb`). Regenerable; lets you rebuild without re-hitting the API. |
| `images/` | Saved product images (reused instead of re-downloading). |

## Rebuild from scratch

```bash
# 1. Scrape the whole shop: refreshes catalog.json + images, writes avg colours
#    back into catalog.json. --details also pulls diameter/weight options.
./scrape_3dqf.py shop --details            # needs uv (inline deps: httpx, pillow)

# 2. Build the SpoolmanDB file from the cache.
python3 build_3dqf_db.py
```

`build_3dqf_db.py` reads the average colours straight from `catalog.json`
(single source of truth); it falls back to a `3dqf_all.csv` export only if the
catalog has no colours in it.

## Notes on the data

- **Grouping** — colours are grouped into filament objects by identical
  `(material, diameters, weights, fill)`. SpoolmanDB expands each object into the
  cartesian product of colour × diameter × weight, so this keeps a 1.75 mm-only
  colour from gaining a phantom 2.85 mm entry (and vice-versa).
- **Diameters** come from the product's authoritative Diameter drop-down, not the
  product name (3DQF's larger size is **2.85 mm**, not 3.0 mm).
- **Density / temperatures** are transcribed from 3DQF's own TDS sheets at
  <https://www.3dqf.co.uk/material-profiles> (see `MATERIAL`/`WOOD` in
  `build_3dqf_db.py`). ASA has no full TDS; it uses 3DQF's stated print settings.
- **Spools** — the standard 1 kg roll is a known cardboard spool
  (`spool_type: cardboard`), and the 1.75 mm 1 kg roll is given
  `spool_weight: 55`. Larger rolls use spools we haven't identified, so their
  `spool_type`/`spool_weight` are left unset.
- **Special cases** (`SPECIAL` in `build_3dqf_db.py`): Naked/Glass →
  `translucent`, Light Stone Effect → `pattern: marble`, Universe Black →
  `pattern: sparkle`, Pearl/Regal "(… shimmer)" → `finish: glossy`. Wood-filled
  products (Woodchucker, Wine Stopper) are split into a `fill: "wood"` group.
