#!/usr/bin/env python3
"""Compare Nintendo eShop prices between any two regions for popular Switch titles.

Pipeline (stdlib only, no API keys needed):
  1. Pull games with popularityRank 1..N from Nintendo's US store search index
     (Algolia, the same backend nintendo.com/store uses), sliced by rank window.
  2. Resolve each title's NSUID for the two chosen countries:
       - Americas countries (US CA MX BR AR CL CO PE) share the US NSUID.
       - Europe/AU/NZ/ZA countries share the EU NSUID, matched by title against
         the EU catalogue (searching.nintendo-europe.com), cached in eu_catalog.json.
       - Japan has its own IDs. Matched via shared artwork hash, then via the product
         code (EU catalogue -> JP `icode`), cached in jp_catalog.json. HK/KR/TW unsupported.
  3. Look up the current eShop price in both countries (sale price if any).
  4. Convert both to --currency and keep titles that pass your filter.

Filters (all given ones must pass; none given = keep everything):
  --min-discount-pct 50     FROM is at least 50% cheaper than TO
  --max-ratio 0.3           FROM price <= 0.3 x TO price (a plain multiplier)
  --min-saving 500          TO price minus FROM price >= 500 (in --currency)
  --max-from-price 200      FROM price in --currency is at most 200
  --where "EXPR"            any Python expression over: from_price, to_price, saving,
                            ratio, discount_pct, from_amt, to_amt, title, rank
                            e.g. --where "saving > 800 and ratio < 0.25"

Usage:
  python3 eshop_compare.py --min-discount-pct 50                   # AR vs US in INR
  python3 eshop_compare.py --from BR --to GB --currency GBP --min-saving 5
  python3 eshop_compare.py --from ZA --to US --currency USD --max-ratio 0.5
  python3 eshop_compare.py --titles "Hades,Hollow Knight" --from PL --to DE --currency EUR
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ALGOLIA_APP = "U3B6GR4UA3"
ALGOLIA_KEY = "a29c6927638bfd8cee23993e51e721c9"  # public search-only key embedded in nintendo.com
ALGOLIA_INDEX = "store_game_en_us"
EU_SEARCH = "https://searching.nintendo-europe.com/en/select"
EU_CACHE = os.path.join(HERE, "eu_catalog.json")
JP_SEARCH = "https://search.nintendo.jp/nintendo_soft/search.json"
JP_CACHE = os.path.join(HERE, "jp_catalog.json")
CACHE_TTL = 7 * 24 * 3600
PRICE_API = "https://api.ec.nintendo.com/v1/price"
FX_API = "https://open.er-api.com/v6/latest/USD"
PRICE_BATCH = 50  # price API accepts up to 50 ids per call
RANK_WINDOW = 1000  # Algolia caps any single query at 1000 hits, so slice by rank
UA = "eshop-price-comparer/2.0 (personal use)"

AMERICAS = {"US", "CA", "MX", "BR", "AR", "CL", "CO", "PE"}
EUROPE = {"GB", "IE", "DE", "FR", "ES", "IT", "NL", "BE", "AT", "CH", "PT", "PL", "CZ", "SK", "HU",
          "SE", "NO", "DK", "FI", "GR", "HR", "SI", "BG", "RO", "LT", "LV", "EE", "CY", "MT", "LU",
          "AU", "NZ", "ZA", "RU"}


def region_of(country):
    if country in AMERICAS:
        return "americas"
    if country in EUROPE:
        return "europe"
    if country == "JP":
        return "japan"
    raise SystemExit(f"{country} is not supported. Americas: {sorted(AMERICAS)}. Europe/AU/NZ/ZA: {sorted(EUROPE)}. "
                     "Japan: JP. HK, KR and TW use separate NSUIDs with no usable catalogue.")


def fresh(path):
    return os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print(f"  retry {attempt + 1} after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


# ---------- US catalogue (popularity source) ----------

def algolia_query(query, page=0, hits_per_page=100, numeric_filters=None):
    body = json.dumps({
        "query": query,
        "page": page,
        "hitsPerPage": hits_per_page,
        "numericFilters": numeric_filters or [],
        "attributesToRetrieve": ["title", "nsuid", "platform", "popularityRank", "price", "url", "productImage"],
    }).encode()
    url = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
    return http_json(url, body, {
        "X-Algolia-API-Key": ALGOLIA_KEY,
        "X-Algolia-Application-Id": ALGOLIA_APP,
        "Content-Type": "application/json",
    })


def is_eshop_nsuid(n):
    """Mobile titles carry the literal id 'MOBILE'; real eShop NSUIDs are 14 digits."""
    return bool(n) and n.isdigit() and len(n) == 14


def popular_games(top_n):
    """Fetch every title with popularityRank 1..top_n, exactly, by slicing the index
    into rank windows (the store's default ordering is NOT rank order)."""
    games = []
    for lo in range(1, top_n + 1, RANK_WINDOW):
        hi = min(lo + RANK_WINDOW - 1, top_n)
        filters = [f"popularityRank>={lo}", f"popularityRank<={hi}"]
        page = 0
        while True:
            res = algolia_query("", page=page, numeric_filters=filters)
            hits = res.get("hits", [])
            games.extend(h for h in hits if is_eshop_nsuid(h.get("nsuid")))
            page += 1
            if not hits or page >= res.get("nbPages", 0):
                break
        print(f"    ranks {lo}-{hi}: {len(games)} so far", file=sys.stderr)
    games.sort(key=lambda h: h.get("popularityRank", 10**9))
    return games


def search_games(titles):
    """Resolve a hand-picked list of names to catalogue entries (first hit each)."""
    out = []
    for t in titles:
        res = algolia_query(t, hits_per_page=3)
        hit = next((h for h in res.get("hits", []) if is_eshop_nsuid(h.get("nsuid"))), None)
        if hit:
            out.append(hit)
        else:
            print(f"  not found in US store: {t}", file=sys.stderr)
    return out


# ---------- EU catalogue (for EU-family NSUIDs) ----------

def norm_title(t):
    t = t.lower().replace("™", "").replace("®", "").replace("–", "-")
    t = re.sub(r"\bnintendo switch 2 edition\b", "ns2", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def eu_catalog():
    """{normalised title: {"nsuid": EU nsuid, "codes": [product codes]}}, cached for a week."""
    if fresh(EU_CACHE):
        with open(EU_CACHE, encoding="utf-8") as f:
            cat = json.load(f)
        if cat and isinstance(next(iter(cat.values())), dict):
            return cat
    print("    downloading EU catalogue (one-off, cached for 7 days)...", file=sys.stderr)
    cat, start = {}, 0
    while True:
        q = urllib.parse.urlencode({"q": "*", "fq": "type:GAME", "rows": 1000, "start": start,
                                    "wt": "json", "fl": "title,nsuid_txt,product_code_txt"})
        res = http_json(f"{EU_SEARCH}?{q}")["response"]
        for h in res["docs"]:
            ids = [n for n in h.get("nsuid_txt", []) if is_eshop_nsuid(n)]
            if ids:
                cat.setdefault(norm_title(h["title"]), {"nsuid": ids[0], "codes": h.get("product_code_txt", [])})
        start += 1000
        if not res["docs"] or start >= res["numFound"]:
            break
    with open(EU_CACHE, "w", encoding="utf-8") as f:
        json.dump(cat, f)
    return cat


# ---------- JP catalogue ----------

def jp_catalog():
    """Base Switch games in the JP store keyed three ways: {"img": {hash: nsuid},
    "code": {5-char icode: nsuid}, "title": {normalised title: nsuid}}. Cached for a week."""
    if fresh(JP_CACHE):
        with open(JP_CACHE, encoding="utf-8") as f:
            return json.load(f)
    print("    downloading JP catalogue (one-off, ~3 min, cached for 7 days)...", file=sys.stderr)
    cat, page = {"img": {}, "code": {}, "title": {}}, 1
    while True:
        res = http_json(f"{JP_SEARCH}?{urllib.parse.urlencode({'q': '', 'limit': 300, 'page': page})}")["result"]
        for i in res["items"]:
            n = str(i.get("nsuid") or "")
            if not is_eshop_nsuid(n) or not n.startswith("7001") or "DLC" in (i.get("sform") or "") \
                    or i.get("hard") not in ("1_HAC", "05_BEE"):
                continue  # base Switch/Switch 2 games only (7001 = title, 7005 = DLC, 7007 = bundle)
            if i.get("iurl"):
                cat["img"].setdefault(i["iurl"], n)
            if i.get("icode"):
                cat["code"].setdefault(i["icode"], n)
            if i.get("title"):
                cat["title"].setdefault(norm_title(i["title"]), n)
        page += 1
        if not res["items"] or (page - 1) * 300 >= res["total"]:
            break
        time.sleep(0.1)
    with open(JP_CACHE, "w", encoding="utf-8") as f:
        json.dump(cat, f)
    return cat


def jp_nsuid(g, jp, eu):
    """US catalogue entry -> JP nsuid, trying artwork hash, product code, then title."""
    img = (g.get("productImage") or "").rsplit("/", 1)[-1]
    if img in jp["img"]:
        return jp["img"][img]
    ent = eu.get(norm_title(g["title"]))
    for code in (ent or {}).get("codes", []):
        if len(code) == 9 and code[4:9] in jp["code"]:  # HACPAABPA -> AABPA
            return jp["code"][code[4:9]]
    return jp["title"].get(norm_title(g["title"]))


def resolve_nsuids(games, country):
    """{us_nsuid: nsuid valid in `country`}."""
    region = region_of(country)
    if region == "americas":
        return {g["nsuid"]: g["nsuid"] for g in games}
    eu = eu_catalog()
    if region == "europe":
        out = {g["nsuid"]: eu[norm_title(g["title"])]["nsuid"] for g in games if norm_title(g["title"]) in eu}
    else:
        jp = jp_catalog()
        out = {g["nsuid"]: n for g in games for n in [jp_nsuid(g, jp, eu)] if n}
    print(f"    {country}: matched {len(out)}/{len(games)} titles", file=sys.stderr)
    return out


# ---------- prices and FX ----------

def fetch_prices(country, nsuids):
    """Return {nsuid: (amount_float, currency)} using sale price when there is one."""
    prices = {}
    nsuids = list(dict.fromkeys(nsuids))
    for i in range(0, len(nsuids), PRICE_BATCH):
        chunk = nsuids[i:i + PRICE_BATCH]
        q = urllib.parse.urlencode({"country": country, "lang": "en", "ids": ",".join(chunk)})
        res = http_json(f"{PRICE_API}?{q}")
        for p in res.get("prices", []):
            if p.get("sales_status") != "onsale":
                continue
            block = p.get("discount_price") or p.get("regular_price")
            if block:
                prices[str(p["title_id"])] = (float(block["raw_value"]), block["currency"])
        time.sleep(0.3)
    return prices


def fx_rates():
    res = http_json(FX_API)
    if res.get("result") != "success":
        raise SystemExit(f"FX lookup failed: {res}")
    return {"USD": 1.0, **res["rates"]}, res["time_last_update_utc"]


def convert(amount, from_cur, to_cur, rates):
    if from_cur not in rates or to_cur not in rates:
        raise SystemExit(f"no FX rate for {from_cur} or {to_cur}")
    return amount / rates[from_cur] * rates[to_cur]


# ---------- filtering ----------

def keep(args, v):
    """Apply every filter the user gave. Missing filters are ignored."""
    if args.min_discount_pct is not None and v["discount_pct"] < args.min_discount_pct:
        return False
    if args.max_ratio is not None and v["ratio"] > args.max_ratio:
        return False
    if args.min_saving is not None and v["saving"] < args.min_saving:
        return False
    if args.max_from_price is not None and v["from_price"] > args.max_from_price:
        return False
    if args.where and not eval(args.where, {"__builtins__": {}}, v):  # noqa: S307 - user's own expression
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", default="AR", metavar="CC", help="cheap country code (default AR)")
    ap.add_argument("--to", dest="dst", default="US", metavar="CC", help="reference country code (default US)")
    ap.add_argument("--currency", default="INR", help="currency to compare in (default INR)")
    ap.add_argument("--top", type=int, default=2000, help="check titles with popularity rank 1..N (default 2000)")
    ap.add_argument("--titles", help="comma-separated title names to check instead of the popularity list")
    ap.add_argument("--min-to-usd", type=float, default=0.5,
                    help="skip junk titles cheaper than this in the TO country, in USD (default 0.5)")
    ap.add_argument("--from-tax-pct", type=float, default=0.0,
                    help="extra %% to add on FROM price for card/import taxes (default 0)")
    ap.add_argument("--sort", choices=["saving", "popularity", "discount"], default="saving",
                    help="row order: saving (default), popularity rank, or discount %%")
    ap.add_argument("--out", default=None, help="CSV path (default: deals_<FROM>_vs_<TO>.csv next to this script)")
    flt = ap.add_argument_group("filters (all given must pass; none given keeps every title)")
    flt.add_argument("--min-discount-pct", type=float, help="FROM at least this %% cheaper than TO, e.g. 50")
    flt.add_argument("--max-ratio", type=float, help="FROM price / TO price at most this, e.g. 0.3")
    flt.add_argument("--min-saving", type=float, help="TO minus FROM at least this, in --currency, e.g. 500")
    flt.add_argument("--max-from-price", type=float, help="FROM price in --currency at most this, e.g. 200")
    flt.add_argument("--where", help="Python expression over from_price, to_price, saving, ratio, "
                                     "discount_pct, from_amt, to_amt, title, rank")
    args = ap.parse_args()

    src, dst, cur = args.src.upper(), args.dst.upper(), args.currency.upper()
    region_of(src), region_of(dst)
    if src == dst:
        raise SystemExit("--from and --to must differ")
    out = args.out or os.path.join(HERE, f"deals_{src}_vs_{dst}.csv")

    print("1/4 fetching catalogue...", file=sys.stderr)
    games = search_games([t.strip() for t in args.titles.split(",")]) if args.titles else popular_games(args.top)
    print(f"    {len(games)} titles", file=sys.stderr)

    print(f"2/4 resolving NSUIDs and fetching {dst} prices...", file=sys.stderr)
    dst_ids = resolve_nsuids(games, dst)
    dst_prices = fetch_prices(dst, list(dst_ids.values()))
    print(f"3/4 resolving NSUIDs and fetching {src} prices...", file=sys.stderr)
    src_ids = resolve_nsuids(games, src)
    src_prices = fetch_prices(src, list(src_ids.values()))

    print(f"4/4 converting to {cur}...", file=sys.stderr)
    rates, fx_date = fx_rates()
    if cur not in rates:
        raise SystemExit(f"unknown currency {cur}")
    print(f"    FX as of {fx_date}: 1 USD = {rates[cur]:.4f} {cur}", file=sys.stderr)

    rows = []
    for g in games:
        n = g["nsuid"]
        if dst_ids.get(n) not in dst_prices or src_ids.get(n) not in src_prices:
            continue
        to_amt, to_cur = dst_prices[dst_ids[n]]
        from_amt, from_cur = src_prices[src_ids[n]]
        if convert(to_amt, to_cur, "USD", rates) < args.min_to_usd:
            continue
        from_amt *= 1 + args.from_tax_pct / 100
        to_price = convert(to_amt, to_cur, cur, rates)
        from_price = convert(from_amt, from_cur, cur, rates)
        if to_price <= 0:
            continue
        ratio = from_price / to_price
        disc = (1 - ratio) * 100
        saving = to_price - from_price
        if keep(args, dict(from_price=from_price, to_price=to_price, saving=saving, ratio=ratio,
                           discount_pct=disc, from_amt=from_amt, to_amt=to_amt,
                           title=g["title"], rank=g.get("popularityRank") or 10**9)):
            rows.append({
                "title": g["title"],
                f"{src}_{cur}": round(from_price, 2),
                f"{dst}_{cur}": round(to_price, 2),
                f"saving_{cur}": round(saving, 2),
                "discount_pct": round(disc, 1),
                "ratio": round(ratio, 3),
                f"{src}_price": f"{from_amt:.2f} {from_cur}",
                f"{dst}_price": f"{to_amt:.2f} {to_cur}",
                "platform": g.get("platform", ""),
                "popularity_rank": g.get("popularityRank", ""),
                f"{src}_nsuid": src_ids[n],
                f"{dst}_nsuid": dst_ids[n],
            })

    rows.sort(key={"saving": lambda r: -r[f"saving_{cur}"],
                   "popularity": lambda r: r["popularity_rank"] or 10**9,
                   "discount": lambda r: -r["discount_pct"]}[args.sort])
    # utf-8-sig so Excel on Windows opens accented/™ titles correctly and nothing crashes on encode
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["title"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {os.path.abspath(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
