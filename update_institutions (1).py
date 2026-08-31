from pathlib import Path
from datetime import datetime, timezone
import json, re, time, requests
import xml.etree.ElementTree as ET

DATA=Path("data.json")
HISTORY=Path("institutional_history.json")
BASE="https://data.sec.gov"
ARCH="https://www.sec.gov/Archives/edgar/data"
HEADERS={
    # SEC asks automated clients to identify the application and provide contact information.
    "User-Agent":"InstitutionalAlpha/1.7.2 madectoussaint-ai@users.noreply.github.com",
    "Accept-Encoding":"gzip, deflate",
    "Accept":"application/json,text/xml,application/xml,text/plain,*/*",
    "Connection":"keep-alive",
}
TIMEOUT=90
REQUEST_DELAY=0.35
MAX_RETRIES=5

SESSION=requests.Session()
SESSION.headers.update(HEADERS)

def sec_get(url):
    last_error=None
    for attempt in range(MAX_RETRIES):
        try:
            r=SESSION.get(url,timeout=TIMEOUT)

            # SEC can temporarily throttle automated/cloud traffic.
            if r.status_code in (403,429,500,502,503,504):
                wait=min(2 ** attempt, 16)
                last_error=RuntimeError(
                    f"HTTP {r.status_code} for {url}; retry {attempt+1}/{MAX_RETRIES}"
                )
                time.sleep(wait)
                continue

            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r
        except requests.RequestException as e:
            last_error=e
            if attempt < MAX_RETRIES-1:
                time.sleep(min(2 ** attempt,16))
                continue
            raise

    raise last_error or RuntimeError(f"SEC request failed: {url}")

def get_json(url):
    return sec_get(url).json()

def get_text(url):
    return sec_get(url).text

def norm(x):
    return re.sub(r"[^A-Z0-9]+"," ",(x or "").upper()).strip()

def latest_13f_filings(cik, limit=2):
    j=get_json(f"{BASE}/submissions/CIK{cik.zfill(10)}.json")
    r=j["filings"]["recent"]
    rows=[]
    for i,form in enumerate(r["form"]):
        if form=="13F-HR":
            rows.append({
                "accession":r["accessionNumber"][i],
                "reportDate":r["reportDate"][i],
                "filingDate":r["filingDate"][i],
            })
    rows.sort(key=lambda x:x["reportDate"],reverse=True)
    return rows[:limit]

def find_info_xml(cik, accession):
    acc=accession.replace("-","")
    url=f"{ARCH}/{int(cik)}/{acc}/index.json"
    j=get_json(url)
    names=[x["name"] for x in j["directory"]["item"]]
    xmls=[n for n in names if n.lower().endswith(".xml") and "primary" not in n.lower()]
    preferred=[n for n in xmls if "info" in n.lower() or "13f_" in n.lower()]
    if preferred: return f"{ARCH}/{int(cik)}/{acc}/{preferred[0]}"
    if xmls: return f"{ARCH}/{int(cik)}/{acc}/{xmls[0]}"
    raise RuntimeError("information table XML introuvable")

def child_text(el, local):
    for c in el:
        if c.tag.split("}")[-1]==local:
            return c.text or ""
    return ""

def parse_holdings(xml_text):
    root=ET.fromstring(xml_text)
    out=[]
    for el in root.iter():
        if el.tag.split("}")[-1]!="infoTable": continue
        issuer=child_text(el,"nameOfIssuer")
        cusip=child_text(el,"cusip")
        title=child_text(el,"titleOfClass")
        putcall=child_text(el,"putCall")
        value=child_text(el,"value")
        shares=0.0
        for c in el.iter():
            if c.tag.split("}")[-1]=="sshPrnamt":
                try: shares=float((c.text or "0").replace(",",""))
                except: shares=0.0
                break
        try: value=float((value or "0").replace(",",""))
        except: value=0.0
        # Exclude options so movement represents reported equity/ADR shares.
        if putcall.strip(): continue
        out.append({"issuer":issuer,"issuer_n":norm(issuer),"cusip":cusip,"title":title,"shares":shares,"value":value})
    return out

def match_amount(rows, aliases):
    aliases=[norm(a) for a in aliases]
    matched=[r for r in rows if any(a in r["issuer_n"] or r["issuer_n"] in a for a in aliases)]
    return {
        "shares":sum(r["shares"] for r in matched),
        "value":sum(r["value"] for r in matched),
        "matches":[r["issuer"] for r in matched][:5]
    }

def manager_snapshots(manager):
    periods={}
    sources=[]
    for cik in manager["ciks"]:
        for f in latest_13f_filings(cik,2):
            key=f["reportDate"]
            xml_url=find_info_xml(cik,f["accession"])
            rows=parse_holdings(get_text(xml_url))
            periods.setdefault(key,[]).extend(rows)
            sources.append({"cik":cik,**f,"info_url":xml_url})
    keys=sorted(periods.keys(),reverse=True)[:2]
    if not keys: raise RuntimeError("aucun 13F-HR")
    current=keys[0]; previous=keys[1] if len(keys)>1 else None
    return current, previous, periods[current], periods.get(previous,[]), sources

def movement(cur,prev):
    if cur>0 and prev<=0: return "NEW",None
    if cur<=0 and prev>0: return "EXIT",-100.0
    if cur<=0 and prev<=0: return "ABSENT",None
    pct=(cur-prev)/prev*100 if prev else None
    if pct is None: return "HELD",None
    if pct>=1.0: return "INCREASE",pct
    if pct<=-1.0: return "REDUCE",pct
    return "STABLE",pct

def signal_score(move):
    return {"NEW":100,"INCREASE":90,"STABLE":70,"HELD":70,"REDUCE":35,"EXIT":0,"ABSENT":0}.get(move,50)

d=json.loads(DATA.read_text(encoding="utf-8"))
history=json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
manager_results={}
errors=[]

for m in d["managers"]:
    try:
        curp,prevp,currows,prevrows,sources=manager_snapshots(m)
        manager_results[m["id"]]={"current_period":curp,"previous_period":prevp,"sources":sources}
        for s in d["stocks"]:
            if not s.get("sec13f_eligible"): continue
            cur=match_amount(currows,s["issuer_aliases"]); prev=match_amount(prevrows,s["issuer_aliases"])
            move,pct=movement(cur["shares"],prev["shares"])
            s["institutional_signals"][m["id"]]={
                "manager":m["name"],"period":curp,"previous_period":prevp,
                "shares":round(cur["shares"],4),"previous_shares":round(prev["shares"],4),
                "change_pct":round(pct,2) if pct is not None else None,
                "movement":move,"matched_issuers":cur["matches"],"source":"SEC Form 13F"
            }
        # Persist one compact manager snapshot per new period.
        if not any(x.get("manager")==m["id"] and x.get("period")==curp for x in history):
            history.append({"manager":m["id"],"period":curp,"previous_period":prevp,"captured_utc":now})
    except Exception as e:
        errors.append(f'{m["id"]}: {e}')

for s in d["stocks"]:
    if not s.get("sec13f_eligible"):
        s.update({"institutional_real_score":None,"institutional_coverage":0,"institutional_total":0,"institutional_period":None})
        continue
    sigs=s.get("institutional_signals",{})
    usable=[v for v in sigs.values() if v.get("movement")]
    if usable:
        s["institutional_real_score"]=round(sum(signal_score(v["movement"]) for v in usable)/len(usable))
        s["institutional_coverage"]=sum(1 for v in usable if v.get("shares",0)>0)
        s["institutional_total"]=len(usable)
        periods=[v.get("period") for v in usable if v.get("period")]
        s["institutional_period"]=max(periods) if periods else None
    else:
        s.update({"institutional_real_score":None,"institutional_coverage":0,"institutional_total":0,"institutional_period":None})

d["last_institutional_update_utc"]=now
d["institutional_manager_results"]=manager_results
d["institutional_data_status"]="ok" if not errors else "partial"
d["institutional_errors"]=errors
DATA.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")
HISTORY.write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding="utf-8")
print("institutional",d["institutional_data_status"],"errors",errors)
if errors:
    print("SEC note: 403/429 can be temporary when SEC throttles cloud-hosted requests.")

