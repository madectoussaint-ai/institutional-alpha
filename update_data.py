from pathlib import Path
from datetime import datetime, timezone
import json, math, yfinance as yf
p=Path("data.json"); d=json.loads(p.read_text(encoding="utf-8"))
now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
def num(x):
    try:
        x=float(x)
        return None if math.isnan(x) else x
    except: return None
for s in d["stocks"]:
    try:
        t=yf.Ticker(s["yf"])
        h=t.history(period="2mo",interval="1d",auto_adjust=False)
        c=h["Close"].dropna()
        if c.empty: raise RuntimeError("historique vide")
        price=num(c.iloc[-1]); prev=num(c.iloc[-2]) if len(c)>=2 else None; old=num(c.iloc[-22]) if len(c)>=22 else num(c.iloc[0])
        ch1=((price/prev)-1)*100 if price and prev else None
        ch30=((price/old)-1)*100 if price and old else None
        mom=round(max(0,min(100,50+(ch30 or 0)*3.5)))
        cur=None
        try: cur=getattr(t.fast_info,"currency",None)
        except: pass
        s.update({"price":round(price,4) if price else None,"currency":cur,"change_1d_pct":round(ch1,2) if ch1 is not None else None,"change_30d_pct":round(ch30,2) if ch30 is not None else None,"momentum":mom,"market_timestamp_utc":now,"market_error":None})
    except Exception as e:
        s["market_error"]=str(e)[:160]
d["last_market_update_utc"]=now
ok=sum(1 for s in d["stocks"] if s.get("price") is not None)
d["market_data_status"]=f"updated_{ok}_of_{len(d['stocks'])}"
p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")
print(d["market_data_status"],now)
