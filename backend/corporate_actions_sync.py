"""Import official NSE equity corporate actions into the ML adjustment store."""
import argparse
import hashlib
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from .config import DATA_DIR
from .ml_pipeline import ResearchStore
from .security_master import load_security_master


BASE = "https://www.nseindia.com"
PAGE = BASE + "/companies-listing/corporate-filings-actions"
API = BASE + "/api/corporates-corporateActions"
RAW_DIR = DATA_DIR / "raw" / "corporate_actions" / "nse"


def parse_subject(subject: str):
    text=" ".join((subject or "").split()); lower=text.lower()
    if "bonus" in lower:
        match=re.search(r"bonus\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)",lower)
        if match:
            issued,held=map(float,match.groups())
            return {"action_type":"bonus","ratio_from":held,"ratio_to":held+issued,"cash_amount":0.0}
    if "split" in lower or "sub-division" in lower or "sub division" in lower:
        match=re.search(r"from\s+(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?).*?to\s+(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)",lower)
        if match:
            old_face,new_face=map(float,match.groups())
            return {"action_type":"split","ratio_from":new_face,"ratio_to":old_face,"cash_amount":0.0}
    if "dividend" in lower:
        amounts=[float(value) for value in re.findall(r"(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)",lower)]
        if amounts: return {"action_type":"dividend","ratio_from":1.0,"ratio_to":1.0,"cash_amount":sum(amounts)}
    return None


def _opener():
    jar=http.cookiejar.CookieJar(); opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders=[("User-Agent","Mozilla/5.0"),("Accept","application/json,text/plain,*/*"),("Referer",PAGE)]
    opener.open(PAGE,timeout=30).read(1024)
    return opener


def _windows(start: date, end: date, days: int = 90):
    cursor=start
    while cursor<=end:
        stop=min(end,cursor+timedelta(days=days-1)); yield cursor,stop; cursor=stop+timedelta(days=1)


def sync(start: str, end: str = "", chunk_days: int = 90):
    start_date=date.fromisoformat(start); end_date=date.fromisoformat(end) if end else date.today()
    if start_date>end_date: raise ValueError("start must not be after end")
    securities=load_security_master()["securities"]
    master={item["symbol"] for item in securities if item["exchange"]=="NSE"}
    bse_by_isin={item.get("isin"):item["symbol"] for item in securities if item["exchange"]=="BSE" and item.get("isin")}
    store=ResearchStore(); opener=_opener(); imported=mirrored=ignored=0; chunks=[]
    for first,last in _windows(start_date,end_date,chunk_days):
        query=urllib.parse.urlencode({"index":"equities","from_date":first.strftime("%d-%m-%Y"),"to_date":last.strftime("%d-%m-%Y")})
        url=f"{API}?{query}"; payload=opener.open(url,timeout=45).read(); digest=hashlib.sha256(payload).hexdigest()
        records=json.loads(payload.decode("utf-8")); accepted=0
        for row in records:
            symbol=str(row.get("symbol") or "").upper(); series=str(row.get("series") or "").upper()
            parsed=parse_subject(str(row.get("subject") or ""))
            if series!="EQ" or symbol not in master or not parsed: ignored+=1; continue
            effective=datetime.strptime(row["exDate"],"%d-%b-%Y").date().isoformat()
            store.add_action("NSE",symbol,effective,parsed["action_type"],parsed["ratio_from"],parsed["ratio_to"],parsed["cash_amount"],"official_nse_corporate_actions_api")
            bse_symbol=bse_by_isin.get(row.get("isin"))
            if bse_symbol:
                store.add_action("BSE",bse_symbol,effective,parsed["action_type"],parsed["ratio_from"],parsed["ratio_to"],parsed["cash_amount"],"official_nse_api_isin_mirrored_bse")
                mirrored+=1
            accepted+=1; imported+=1
        RAW_DIR.mkdir(parents=True,exist_ok=True); raw_path=RAW_DIR/f"{first}_{last}.json"; raw_path.write_bytes(payload)
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO corporate_action_ingestions VALUES(?,?,?,?,?,?,?,?)",("NSE",first.isoformat(),last.isoformat(),url,digest,accepted,"complete",datetime.now(timezone.utc).isoformat()))
        chunks.append({"start":first.isoformat(),"end":last.isoformat(),"received":len(records),"imported":accepted,"sha256":digest})
        print(f"[NSE actions] {first} → {last} · {accepted} adjustments",flush=True)
    return {"exchange":"NSE","start":start_date.isoformat(),"end":end_date.isoformat(),"imported":imported,"bse_isin_mirrors":mirrored,"ignored":ignored,"chunks":chunks,
            "note":"Only EQ dividends, bonuses and parsable face-value splits are adjusted; rights and buybacks remain event metadata only."}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--start",required=True); parser.add_argument("--end",default=""); parser.add_argument("--chunk-days",type=int,default=90); args=parser.parse_args()
    print(json.dumps(sync(args.start,args.end,args.chunk_days),indent=2))


if __name__=="__main__": main()
