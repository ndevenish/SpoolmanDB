#!/usr/bin/env python3
"""Generate filaments/3dqf.json (SpoolmanDB format) from the cached catalog.json.

Grouping rule: colours are grouped into filament objects sharing an identical
(material, diameters, weights) tuple. Because SpoolmanDB expands each object into
the cartesian product of colour x diameter x weight, this ensures a colour only
sold in 1.75mm never gets a phantom 2.85mm entry, and vice-versa.
"""
import json, re, collections

CATALOG = "catalog.json"
OUT = "filaments/3dqf.json"

# Wix collection slug -> material, in priority order (first match wins).
CATMAT = [
    ("pla-plus-175mm", "PLA+"),
    ("petg-175mm", "PETG"),
    ("abs-175-285-mm", "ABS"),
    ("asa-175mm", "ASA"),
    ("pla-175-285-mm", "PLA"),
    ("285mm-pla-filament", "PLA"),
]
# Density and print temps from 3DQF's own TDS sheets (3dqf.co.uk/material-profiles),
# except ASA which 3DQF does not publish a TDS for (generic values, flagged below).
MATERIAL = {
    "PLA":  dict(density=1.24, extruder_temp_range=[210, 240], bed_temp_range=[50, 60]),
    "PLA+": dict(density=1.24, extruder_temp_range=[220, 250], bed_temp_range=[50, 60]),
    "PETG": dict(density=1.27, extruder_temp_range=[235, 260], bed_temp_range=[70, 90]),
    "ABS":  dict(density=1.05, extruder_temp_range=[250, 280], bed_temp_range=[90, 100]),
    "ASA":  dict(density=1.05, extruder_temp_range=[230, 240], bed_temp=100),  # 3DQF ASA settings (no full TDS)
}
# 3DQF Woodchucker (PLA wood-filled) TDS. Applied to the wood-filled group.
WOOD = dict(density=1.05, extruder_temp_range=[165, 190], bed_temp_range=[50, 60])
WOOD_RE = re.compile(r"woodchucker|wine\s*stopper|(?:wood|cork)\s*filled", re.I)

WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.I)

# Per-colour schema extras, matched (case-insensitive) against the raw product name.
SPECIAL = [
    (r"\bNaked\b",       dict(translucent=True)),                   # Naked = transparent PLA
    (r"\bGlass\b",       dict(translucent=True)),                   # Glass = see-through
    (r"Stone Effect",    dict(pattern="marble")),
    (r"Universe Black",  dict(pattern="sparkle")),                  # 3DQF's galaxy/sparkle black
    (r"shimmer",         dict(finish="glossy")),                    # Pearl/Regal "(Gold/Silver shimmer)"
    # Note: "silk" has no representable finish (schema finish is only matte/glossy).
]


def clean_color(name: str) -> str:
    s = name
    s = re.sub(r"\(\s*[^)]*shimmer[^)]*\)", " ", s, flags=re.I)  # drop "(Gold/Silver shimmer)"
    s = re.sub(r"\b(?:wood|cork)\s*filled\b", " ", s, flags=re.I)  # fill captured via 'fill' field
    s = re.sub(r"\b(?:UK|Uk)\s+Made\b", " ", s)
    s = re.sub(r"\b3D\s*Print(?:er|ing)\s*Filament\b", " ", s, flags=re.I)
    s = re.sub(r"\d+(?:\.\d+)?\s*mm\b", " ", s, flags=re.I)
    s = re.sub(r"\d+(?:\.\d+)?\s*KG\b", " ", s, flags=re.I)
    s = re.sub(r"\bPLA[\s+-]*Plus\b", " ", s, flags=re.I)   # "PLA + Plus", "PLA-Plus"
    s = re.sub(r"\b(?:PLA|PETG|ABS|ASA)\b", " ", s, flags=re.I)
    s = re.sub(r"\bPlus\b", " ", s, flags=re.I)
    s = re.sub(r"(?<!\w)\+(?!\w)", " ", s)                  # stray standalone '+'
    s = re.sub(r"\d+(?:\.\d+)?", " ", s)          # leftover bare 1.75 / 2.85 / version
    s = re.sub(r"[&/]", " ", s)
    s = re.sub(r"\bmm?\b", " ", s, flags=re.I)    # stray 'm'/'mm' from typo'd diameters (e.g. "2.85m")
    s = re.sub(r"\b3D\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return TYPOS.get(s, s)


# Manufacturer typos to correct in colour names.
TYPOS = {
    "Smoth Black": "Smooth Black",
}


def main():
    cat = json.load(open(CATALOG))
    slug2id = {c["slug"]: c["id"] for c in cat["categories"]}
    filament_ids = {slug2id[s] for s, _ in CATMAT if s in slug2id}

    def material_of(p):
        pcats = set(p.get("categoryIds", []))
        for slug, mat in CATMAT:
            if slug in slug2id and slug2id[slug] in pcats:
                return mat
        return None

    def diameters(p):
        for o in p["options"]:
            if "diam" in o["title"].lower():
                ds = []
                for s in o["selections"]:
                    m = re.search(r"(\d+\.\d+)", s.get("description") or s.get("value") or "")
                    if m:
                        ds.append(float(m.group(1)))
                if ds:
                    return sorted(set(ds))
        return [1.75]  # every 3DQF filament is at least 1.75mm

    def weights(p):
        ws = []
        for o in p["options"]:
            if "weight" in o["title"].lower():
                for s in o["selections"]:
                    m = WEIGHT_RE.search(s.get("description") or s.get("value") or "")
                    if m:
                        ws.append(int(round(float(m.group(1)) * 1000)))
        return sorted(set(ws)) or [1000]  # default single 1kg spool

    # Average colours are stored in the catalog cache by the scraper (single
    # source of truth). Fall back to the CSV export only if they are absent.
    hexes = {p["id"]: p["avg_hex"].lstrip("#")
             for p in cat["products"] if p.get("avg_hex")}
    if not hexes:
        try:
            import csv
            for r in csv.DictReader(open("3dqf_all.csv")):
                if r.get("avg_hex"):
                    hexes[r["id"]] = r["avg_hex"].lstrip("#")
        except FileNotFoundError:
            pass

    groups = collections.OrderedDict()  # (mat, diam_tuple, weight_tuple) -> [colors]
    for p in cat["products"]:
        if not (set(p.get("categoryIds", [])) & filament_ids):
            continue
        mat = material_of(p)
        if mat not in MATERIAL:
            continue
        d = tuple(diameters(p))
        w = tuple(weights(p))
        color = clean_color(p["name"])
        hx = hexes.get(p["id"])
        if not color or not hx:
            continue
        fill = "wood" if WOOD_RE.search(p["name"]) else None
        extra = {}
        for pat, ex in SPECIAL:
            if re.search(pat, p["name"], re.I):
                extra.update(ex)
        groups.setdefault((mat, d, w, fill), []).append((color, hx, extra))

    filaments = []
    for (mat, d, w, fill), colors in groups.items():
        seen, uniq = set(), []
        for name, hx, extra in colors:
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            uniq.append({"name": name, "hex": hx, **extra})
        spec = WOOD if fill == "wood" else MATERIAL[mat]
        # We only know the spool weight for the standard 1.75mm 1kg cardboard roll
        # (~55g). Attach it solely to a 1000g weight in a 1.75mm-only group, so the
        # cartesian expansion never applies it to 2.85mm or larger rolls (which use
        # bigger spools) we haven't weighed.
        only_175 = d == (1.75,)
        weight_objs = []
        for x in w:
            obj = {"weight": x}
            # Only the standard 1kg roll is a known cardboard spool (~55g on the
            # 1.75mm-only spool). Larger rolls use spools we haven't identified,
            # so leave their spool_type/spool_weight unset.
            if x == 1000:
                obj["spool_type"] = "cardboard"
                if only_175:
                    obj["spool_weight"] = 55
            weight_objs.append(obj)
        obj = {
            # Material is already its own field, so the name is just the colour
            # (matching the common SpoolmanDB convention of bare "{color_name}").
            "name": "{color_name}",
            "material": mat,
            "density": spec["density"],
            "weights": weight_objs,
            "diameters": list(d),
        }
        for k in ("extruder_temp", "extruder_temp_range", "bed_temp", "bed_temp_range"):
            if k in spec:
                obj[k] = spec[k]
        if fill:
            obj["fill"] = fill
        obj["colors"] = sorted(uniq, key=lambda c: c["name"])
        filaments.append(obj)

    # Stable, readable order: material, then diameter count, then weight count.
    filaments.sort(key=lambda f: (f["material"], f["diameters"], len(f["weights"])))
    db = {"manufacturer": "3DQF", "filaments": filaments}
    json.dump(db, open(OUT, "w"), indent=4, ensure_ascii=False)
    open(OUT, "a").write("\n")

    print(f"Wrote {OUT}: {len(filaments)} filament groups, "
          f"{sum(len(f['colors']) for f in filaments)} colours")
    for f in filaments:
        print(f"  {f['material']:5} d={f['diameters']} "
              f"w={[x['weight'] for x in f['weights']]:} -> {len(f['colors'])} colours")


if __name__ == "__main__":
    main()
