from pathlib import Path
from datetime import datetime, timezone
import json, re, time, requests
import xml.etree.ElementTree as ET

DATA = Path("data.json")
HISTORY = Path("institutional_history.json")
ARCH = "https://www.sec.gov/Archives/edgar/data"

# V1.7.3 bypasses data.sec.gov, which returned 403 from GitHub Actions.
# It reads the official SEC filing XML directly from EDGAR Archives.
HEADERS = {
    "User-Agent": "InstitutionalAlpha/1.7.3 madectoussaint-ai@users.noreply.github.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/xml,text/xml,text/plain,*/*",
    "Connection": "keep-alive",
}
TIMEOUT = 120
REQUEST_DELAY = 0.75
MAX_RETRIES = 4

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Official SEC 13F filing manifest, latest two report periods available
# as of 2026-09-01. This avoids the blocked submissions API.
MANIFEST = {
    "blackrock": {
        "current_period": "2026-06-30",
        "previous_period": "2026-03-31",
        "current": [
            {
                "cik": "2012383",
                "accession": "0002012383-26-003238",
                "file": "form13fInfoTable.xml",
                "filing_date": "2026-08-07",
            }
        ],
        "previous": [
            {
                "cik": "2012383",
                "accession": "0002012383-26-001841",
                "file": "form13fInfoTable.xml",
                "filing_date": "2026-05-13",
            }
        ],
    },
    "vanguard": {
        "current_period": "2026-06-30",
        "previous_period": "2026-03-31",
        "current": [
            {
                "cik": "2100119",
                "accession": "0002100119-26-001527",
                "file": "13F_0002100119_20260630.xml",
                "filing_date": "2026-08-13",
            },
            {
                "cik": "2100121",
                "accession": "0002100121-26-001018",
                "file": "13F_0002100121_20260630.xml",
                "filing_date": "2026-08-13",
            },
        ],
        "previous": [
            {
                "cik": "2100119",
                "accession": "0002100119-26-001306",
                "file": "13F_0002100119_20260331.xml",
                "filing_date": "2026-05-08",
            },
            {
                "cik": "2100121",
                "accession": "0002100121-26-000861",
                "file": "13F_0002100121_20260331.xml",
                "filing_date": "2026-05-08",
            },
        ],
    },
    "statestreet": {
        "current_period": "2026-06-30",
        "previous_period": "2026-03-31",
        "current": [
            {
                "cik": "93751",
                "accession": "0000093751-26-000507",
                "file": "XML_Infotable.xml",
                "filing_date": "2026-08-07",
            }
        ],
        "previous": [
            {
                "cik": "93751",
                "accession": "0000093751-26-000315",
                "file": "XML_Infotable.xml",
                "filing_date": "2026-05-15",
            }
        ],
    },
}

def sec_url(item):
    acc = item["accession"].replace("-", "")
    return f"{ARCH}/{int(item['cik'])}/{acc}/{item['file']}"

def sec_get_text(url):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.status_code in (403, 429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code} for {url}")
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r.text
        except requests.RequestException as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise
    raise last or RuntimeError(f"SEC request failed: {url}")

def norm(x):
    return re.sub(r"[^A-Z0-9]+", " ", (x or "").upper()).strip()

def child_text(el, local):
    for c in el:
        if c.tag.split("}")[-1] == local:
            return c.text or ""
    return ""

def parse_holdings(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "infoTable":
            continue
        issuer = child_text(el, "nameOfIssuer")
        cusip = child_text(el, "cusip")
        title = child_text(el, "titleOfClass")
        putcall = child_text(el, "putCall")
        value = child_text(el, "value")
        shares = 0.0
        for c in el.iter():
            if c.tag.split("}")[-1] == "sshPrnamt":
                try:
                    shares = float((c.text or "0").replace(",", ""))
                except Exception:
                    shares = 0.0
                break
        try:
            value = float((value or "0").replace(",", ""))
        except Exception:
            value = 0.0
        if putcall.strip():
            continue
        out.append({
            "issuer": issuer,
            "issuer_n": norm(issuer),
            "cusip": cusip,
            "title": title,
            "shares": shares,
            "value": value,
        })
    return out

def match_amount(rows, aliases):
    aliases = [norm(a) for a in aliases]
    matched = [
        r for r in rows
        if any(a in r["issuer_n"] or r["issuer_n"] in a for a in aliases)
    ]
    return {
        "shares": sum(r["shares"] for r in matched),
        "value": sum(r["value"] for r in matched),
        "matches": [r["issuer"] for r in matched][:5],
    }

def load_group(items):
    rows = []
    sources = []
    for item in items:
        url = sec_url(item)
        rows.extend(parse_holdings(sec_get_text(url)))
        sources.append({**item, "info_url": url})
    return rows, sources

def movement(cur, prev):
    if cur > 0 and prev <= 0:
        return "NEW", None
    if cur <= 0 and prev > 0:
        return "EXIT", -100.0
    if cur <= 0 and prev <= 0:
        return "ABSENT", None
    pct = (cur - prev) / prev * 100 if prev else None
    if pct is None:
        return "HELD", None
    if pct >= 1.0:
        return "INCREASE", pct
    if pct <= -1.0:
        return "REDUCE", pct
    return "STABLE", pct

def signal_score(move):
    return {
        "NEW": 100, "INCREASE": 90, "STABLE": 70, "HELD": 70,
        "REDUCE": 35, "EXIT": 0, "ABSENT": 0
    }.get(move, 50)

d = json.loads(DATA.read_text(encoding="utf-8"))
history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

manager_results = {}
errors = []

for m in d["managers"]:
    mid = m["id"]
    cfg = MANIFEST.get(mid)
    if not cfg:
        errors.append(f"{mid}: no SEC manifest")
        continue
    try:
        currows, cursources = load_group(cfg["current"])
        prevrows, prevsources = load_group(cfg["previous"])

        manager_results[mid] = {
            "current_period": cfg["current_period"],
            "previous_period": cfg["previous_period"],
            "sources": cursources + prevsources,
            "mode": "official SEC EDGAR archives",
        }

        for s in d["stocks"]:
            if not s.get("sec13f_eligible"):
                continue
            cur = match_amount(currows, s["issuer_aliases"])
            prev = match_amount(prevrows, s["issuer_aliases"])
            move, pct = movement(cur["shares"], prev["shares"])
            s.setdefault("institutional_signals", {})[mid] = {
                "manager": m["name"],
                "period": cfg["current_period"],
                "previous_period": cfg["previous_period"],
                "shares": round(cur["shares"], 4),
                "previous_shares": round(prev["shares"], 4),
                "change_pct": round(pct, 2) if pct is not None else None,
                "movement": move,
                "matched_issuers": cur["matches"],
                "source": "SEC Form 13F / EDGAR Archives",
            }

        if not any(x.get("manager") == mid and x.get("period") == cfg["current_period"] for x in history):
            history.append({
                "manager": mid,
                "period": cfg["current_period"],
                "previous_period": cfg["previous_period"],
                "captured_utc": now,
                "source": "SEC EDGAR Archives",
            })

    except Exception as e:
        errors.append(f"{mid}: {e}")

for s in d["stocks"]:
    if not s.get("sec13f_eligible"):
        s.update({
            "institutional_real_score": None,
            "institutional_coverage": 0,
            "institutional_total": 0,
            "institutional_period": None,
        })
        continue

    sigs = s.get("institutional_signals", {})
    usable = [
        v for k, v in sigs.items()
        if k in MANIFEST and v.get("movement")
    ]

    if usable:
        s["institutional_real_score"] = round(
            sum(signal_score(v["movement"]) for v in usable) / len(usable)
        )
        s["institutional_coverage"] = sum(
            1 for v in usable if v.get("shares", 0) > 0
        )
        s["institutional_total"] = len(usable)
        periods = [v.get("period") for v in usable if v.get("period")]
        s["institutional_period"] = max(periods) if periods else None
    else:
        s.update({
            "institutional_real_score": None,
            "institutional_coverage": 0,
            "institutional_total": 0,
            "institutional_period": None,
        })

d["last_institutional_update_utc"] = now
d["institutional_manager_results"] = manager_results
d["institutional_errors"] = errors

# "ok" only when every configured manager was read successfully.
d["institutional_data_status"] = "ok" if not errors else "partial"

DATA.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

print("institutional", d["institutional_data_status"])
if manager_results:
    for k, v in manager_results.items():
        print(k, v["current_period"], "<-", v["previous_period"], "SEC Archives")
if errors:
    print("errors", errors)
