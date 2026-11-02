"""Activity and sleep dashboard: GET /fitness/summary (JSON) and GET /dashboard (one offline HTML page).

Wire it with `app.include_router(dashboard.router(store, status=...))`. Demo without the band:
`python -m bridge.dashboard --demo --port 8799`.
"""
import argparse
import json
import math
import random
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from band.fitness_store import STAGES, FitnessStore

CONFIG, DEFAULT_GOAL = "config/band.json", 8000


def goal() -> int:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return int(json.load(f).get("steps_goal", DEFAULT_GOAL))
    except (OSError, ValueError, TypeError):
        return DEFAULT_GOAL


def sleep_totals(segments) -> dict:
    t = dict.fromkeys(STAGES, 0)
    for s in segments:
        t[s["stage"]] += round((s["end"] - s["start"]) / 60)
    return t


def stress_level(score) -> str | None:
    return None if score is None else "Relaxed" if score < 30 else "Normal" if score < 60 else "Medium" if score < 80 else "High"


def stats(rows, name) -> dict:
    v = [r[1] for r in rows]
    return {f"{name}_latest": v[-1] if v else None, f"{name}_min": min(v) if v else None,
            f"{name}_avg": round(sum(v) / len(v)) if v else None}


def summary(store, days=7, status=lambda: {}) -> dict:
    today = date.today().isoformat()
    sleep = store.sleep(today)
    s = status() or {}
    last = store.meta("last_sync")
    spo2, stress = store.spo2(today), store.stress(today)
    return dict(today=store.day(today), goal=goal(), hourly=store.hourly(today), days=store.days(days),
                sleep=sleep, sleep_totals=sleep_totals(sleep), heart_rates=[list(r) for r in store.heart_rates(today)],
                spo2=[list(r) for r in spo2], stress=[list(r) for r in stress], **stats(spo2, "spo2"), **stats(stress, "stress"),
                stress_level=stress_level(stress[-1][1] if stress else None),
                last_sync=int(float(last)) if last else None, battery=s.get("battery"), state=s.get("state"))


def router(store, status=lambda: {}) -> APIRouter:
    r = APIRouter()

    @r.get("/fitness/summary")
    def fitness_summary(days: int = 7):
        return summary(store, max(1, min(days, 90)), status)

    @r.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return PAGE

    return r


def demo_store() -> FitnessStore:
    """In-memory store with a plausible day, a week and a night with stages."""
    rnd, st = random.Random(7), FitnessStore(":memory:")
    now = datetime.now()
    midnight = datetime.combine(now.date(), datetime.min.time())
    rows = []
    for q in range(4 * max(now.hour, 18) + 1):  # one sample per 15 min, at least up to 18:00
        t = midnight + timedelta(minutes=15 * q)
        h = t.hour + t.minute / 60
        walk = 0 if h < 7 else max(0, 1 + math.sin((h - 7) * 0.9)) * (1.6 if h in (8, 12.5, 17.5) or 17 < h < 19 else 1)
        steps = int(rnd.random() * 90 * walk) if walk else 0
        hr = int(58 + 14 * math.sin(h / 24 * 2 * math.pi - 2) + (35 if steps > 80 else 0) + rnd.randint(-4, 4))
        rows.append((int(t.timestamp()), steps, int(steps * 0.04), int(steps * 0.72), hr if h > 0.2 else None))
    st.add_samples(rows)
    for i in range(1, 7):
        d = (now.date() - timedelta(days=i)).isoformat()
        steps = rnd.randint(4200, 11800)
        st.set_totals(d, steps, int(steps * 0.045), int(steps * 0.72), int(time.time()))
    stages, t, seg = ["light", "deep", "light", "rem", "light", "deep", "light", "rem", "awake", "light", "rem", "light"], \
        datetime.combine(now.date(), datetime.min.time()) - timedelta(hours=1, minutes=10), []
    for i, s in enumerate(stages):
        n = rnd.randint(12, 24) if s == "awake" else rnd.randint(25, 70)
        seg.append((int(t.timestamp()), int((t + timedelta(minutes=n)).timestamp()), s))
        t += timedelta(minutes=n)
    st.add_sleep(seg)
    st.add_spo2([(int(t.timestamp()), min(100, 96 + rnd.randint(-2, 3))) for t in
                 (midnight + timedelta(minutes=30 * q) for q in range(2 * max(now.hour, 18)))])
    st.add_stress([(int(t.timestamp()), int(35 + 25 * math.sin(t.hour / 3) + rnd.randint(-8, 8))) for t in
                   (midnight + timedelta(minutes=45 * q) for q in range(int(max(now.hour, 18) * 4 / 3)))])
    st.set_meta("last_sync", int(time.time()) - 240)
    return st


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Band Bridge</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--fg:#e8ecf1;--mut:#8a94a3;--ring:#3ddc97;--bar:#4c8dff;--bar2:#2b3a55;
--deep:#4a3fd0;--light:#7aa2ff;--rem:#c77dff;--awake:#ff9f5a;--hr:#ff5c7a;--ok:#3ddc97;--bad:#ff5c5c;--warn:#ffb020}
@media(prefers-color-scheme:light){:root{--bg:#f2f4f8;--card:#fff;--line:#e2e6ee;--fg:#161a21;--mut:#69727f;--bar2:#d5def0;
--deep:#4a3fd0;--light:#5b8cff;--rem:#a24fe0;--awake:#f08a3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px;display:grid;gap:14px;grid-template-columns:repeat(6,1fr)}
header{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;min-width:0}
.ringc{grid-column:span 3;display:flex;align-items:center;justify-content:center}.tiles{grid-column:span 3;display:grid;gap:14px;grid-template-columns:1fr 1fr}
.w2{grid-column:span 3}.w3{grid-column:span 6}.sm{grid-column:span 2}
.big{font-size:30px;font-weight:700}.unit{font-size:13px;color:var(--mut);font-weight:500;margin-left:4px}.mut{color:var(--mut);font-size:13px}
.ring{width:100%;max-width:260px}.ring .arc{transition:stroke-dashoffset 1.1s cubic-bezier(.2,.8,.2,1)}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--mut)}.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.dot.ready{background:var(--ok)}.dot.connecting,.dot.backoff{background:var(--warn)}.dot.bad{background:var(--bad)}
svg text{fill:var(--mut);font-size:10px}.empty{color:var(--mut);padding:26px 0;text-align:center}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px}.legend span{font-size:13px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
.stages{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}.stages b{display:block;font-size:17px}
@media(max-width:760px){.ringc,.tiles,.w2,.sm{grid-column:span 6}.big{font-size:26px}}
@media(max-width:420px){main{padding:12px;gap:10px}.stages{grid-template-columns:repeat(2,1fr)}}
</style></head><body><main>
<header><h1>Band Bridge</h1><div><span class="pill"><span class="dot" id="dot"></span><span id="conn">connecting</span></span>
&nbsp; <span class="pill" id="batt"></span> &nbsp; <span class="pill" id="sync"></span></div></header>
<section class="card ringc"><svg class="ring" viewBox="0 0 200 200" role="img" aria-label="Steps vs goal">
<circle cx="100" cy="100" r="84" fill="none" stroke="var(--bar2)" stroke-width="16"/>
<circle class="arc" id="arc" cx="100" cy="100" r="84" fill="none" stroke="var(--ring)" stroke-width="16" stroke-linecap="round"
 transform="rotate(-90 100 100)" stroke-dasharray="527.8" stroke-dashoffset="527.8"/>
<text id="steps" x="100" y="102" text-anchor="middle" style="font-size:38px;font-weight:700;fill:var(--fg)">0</text>
<text id="goal" x="100" y="124" text-anchor="middle" style="font-size:12px">of 8,000 steps</text></svg></section>
<div class="tiles"><section class="card"><h2>Energy</h2><span class="big" id="kcal">-</span><span class="unit">kcal</span></section>
<section class="card"><h2>Distance</h2><span class="big" id="km">-</span><span class="unit">km</span></section>
<section class="card" style="grid-column:span 2"><h2>Heart rate today</h2><div id="hr"></div></section></div>
<section class="card w2"><h2>Blood oxygen</h2><span class="big" id="spo2">-</span><span class="unit" id="spo2s"></span><div id="spo2c"></div></section>
<section class="card w2"><h2>Stress</h2><span class="big" id="stress">-</span><span class="unit" id="stressl"></span><div id="stressc"></div></section>
<section class="card w2"><h2>Steps per hour</h2><div id="hourly"></div></section>
<section class="card w2"><h2>Last 7 days</h2><div id="week"></div></section>
<section class="card w3"><h2>Last night</h2><div id="sleep"></div></section>
</main><script>
const $=id=>document.getElementById(id),C=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt=n=>Math.round(n).toLocaleString(),dur=m=>m>=60?Math.floor(m/60)+"h "+String(m%60).padStart(2,"0")+"m":m+"m";
const hm=t=>new Date(t*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
const empty=t=>`<div class="empty">${t}</div>`;
function bars(vals,labels,hi,W=420,H=150){const m=Math.max(...vals,1),n=vals.length,bw=W/n,p=Math.min(4,bw*.2);
 if(!vals.some(v=>v))return null;
 return `<svg viewBox="0 0 ${W} ${H+16}" width="100%" role="img">`+vals.map((v,i)=>{const h=v/m*H;
 return `<rect x="${i*bw+p/2}" y="${H-h}" width="${bw-p}" height="${Math.max(h,v?2:0)}" rx="3" fill="${i==hi?C("--ring"):C("--bar")}"><title>${labels[i]}: ${fmt(v)}</title></rect>`
 +(labels[i]?`<text x="${i*bw+bw/2}" y="${H+12}" text-anchor="middle">${labels[i]}</text>`:"")}).join("")+`</svg>`}
function hr(r){if(r.length<2)return empty("No heart-rate readings yet");
 const W=300,H=90,t0=r[0][0],t1=r[r.length-1][0]||1,lo=Math.min(...r.map(x=>x[1]))-3,hi=Math.max(...r.map(x=>x[1]))+3,
 pts=r.map(x=>((x[0]-t0)/Math.max(t1-t0,1)*W).toFixed(1)+","+(H-(x[1]-lo)/(hi-lo)*H).toFixed(1)).join(" ");
 return `<svg viewBox="0 0 ${W} ${H+14}" width="100%"><polyline points="${pts}" fill="none" stroke="${C("--hr")}" stroke-width="2" stroke-linejoin="round"/>
 <text x="0" y="${H+12}">${hm(t0)}</text><text x="${W}" y="${H+12}" text-anchor="end">${hm(t1)}</text>
 <text x="${W}" y="9" text-anchor="end">${lo+3}-${hi-3} bpm</text></svg>`}
function line(r,color,unit,lo,hi,dots){if(r.length<1)return empty("No readings yet today");
 const W=300,H=70,t0=r[0][0],t1=Math.max(r[r.length-1][0],t0+1),y=v=>(H-(v-lo)/(hi-lo)*H).toFixed(1),x=t=>((t-t0)/(t1-t0)*(W-4)+2).toFixed(1),
 pts=r.map(p=>x(p[0])+","+y(p[1])).join(" ");
 return `<svg viewBox="0 0 ${W} ${H+14}" width="100%" style="margin-top:8px">`+(dots?"":`<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>`)
 +r.map(p=>`<circle cx="${x(p[0])}" cy="${y(p[1])}" r="${dots?2.5:r.length<3?3:0}" fill="${color}"><title>${hm(p[0])}: ${p[1]}${unit}</title></circle>`).join("")
 +`<text x="0" y="${H+12}">${hm(t0)}</text><text x="${W}" y="${H+12}" text-anchor="end">${hm(t1)}</text></svg>`}
const LV={Relaxed:"--ok",Normal:"--bar",Medium:"--warn",High:"--bad"};
const ST=["awake","rem","light","deep"],NAME={deep:"Deep",light:"Light",rem:"REM",awake:"Awake"};
function sleep(s,t){if(!s.length)return empty("No sleep recorded last night");
 const W=700,rh=26,t0=s[0].start,t1=s[s.length-1].end,k=W/(t1-t0),tot=t.deep+t.light+t.rem;
 const rects=s.map(x=>`<rect x="${(x.start-t0)*k}" y="${ST.indexOf(x.stage)*rh}" width="${Math.max((x.end-x.start)*k,1.5)}" height="${rh-4}" rx="4" fill="${C("--"+x.stage)}"><title>${NAME[x.stage]} ${hm(x.start)}-${hm(x.end)}</title></rect>`).join("");
 return `<div><span class="big">${dur(tot)}</span><span class="unit">asleep, ${hm(t0)} to ${hm(t1)}</span></div>
 <svg viewBox="0 0 ${W} ${rh*4+14}" width="100%" style="margin-top:10px">${rects}
 <text x="0" y="${rh*4+11}">${hm(t0)}</text><text x="${W}" y="${rh*4+11}" text-anchor="end">${hm(t1)}</text></svg>
 <div class="stages">${["deep","light","rem","awake"].map(n=>`<div><span class="legend" style="margin:0"><span><i style="background:${C("--"+n)}"></i>${NAME[n]}</span></span><b>${dur(t[n])}</b></div>`).join("")}</div>`}
function render(d){
 const g=d.goal||8000,p=Math.min(d.today.steps/g,1);$("arc").style.strokeDashoffset=527.8*(1-p);
 $("steps").textContent=fmt(d.today.steps);$("goal").textContent=`of ${fmt(g)} steps (${Math.round(d.today.steps/g*100)}%)`;
 $("kcal").textContent=fmt(d.today.calories);$("km").textContent=(d.today.distance/1000).toFixed(1);
 $("hr").innerHTML=hr(d.heart_rates);
 const na=d.spo2_latest==null;$("spo2").textContent=na?"-":d.spo2_latest+"%";$("spo2s").textContent=na?"":`min ${d.spo2_min}%, avg ${d.spo2_avg}%`;
 $("spo2c").innerHTML=line(d.spo2,C("--bar"),"%",Math.min(90,d.spo2_min??90),100,true);
 const ns=d.stress_latest==null,lv=d.stress_level;$("stress").textContent=ns?"-":d.stress_latest;$("stressl").textContent=ns?"":`${lv}, avg ${d.stress_avg}`;
 $("stress").style.color=ns?"":C(LV[lv]);$("stressc").innerHTML=line(d.stress,ns?"":C(LV[lv]),"",0,100,false);
 $("hourly").innerHTML=bars(d.hourly,d.hourly.map((_,i)=>i%6==0?i:""),new Date().getHours())||empty("No steps recorded yet today");
 const dn=d.days.map(x=>new Date(x.day+"T12:00").toLocaleDateString([],{weekday:"short"}));
 $("week").innerHTML=bars(d.days.map(x=>x.steps),dn,d.days.length-1)||empty("No history yet");
 $("sleep").innerHTML=sleep(d.sleep,d.sleep_totals);
 const s=d.state||"bridge offline";$("conn").textContent=s;$("dot").className="dot "+(s=="ready"||s=="connecting"||s=="backoff"?s:"bad");
 $("batt").textContent=d.battery!=null?`Battery ${d.battery}%`:"";
 $("sync").textContent=d.last_sync?"Synced "+(m=>m<1?"just now":m<60?m+" min ago":dur(m)+" ago")(Math.round((Date.now()/1000-d.last_sync)/60)):"Not synced yet"}
async function load(){try{render(await (await fetch("/fitness/summary?days=7")).json())}catch(e){$("conn").textContent="bridge not reachable";$("dot").className="dot bad"}}
load();setInterval(load,60000);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI
    ap = argparse.ArgumentParser(description="Serve the dashboard with demo data.")
    ap.add_argument("--demo", action="store_true", required=True)
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    app = FastAPI()
    app.include_router(router(demo_store(), status=lambda: {"state": "ready", "battery": 72}))
    uvicorn.run(app, host="127.0.0.1", port=args.port)
