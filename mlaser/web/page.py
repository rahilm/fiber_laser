"""The LAN monitor page: one self-contained, phone-friendly HTML document.

The page shows the machine state (colour + text + icon), the job and sheet, progress and ETA,
the active alarms with their remedies, the queue and the sheet thumbnail, and refreshes them
from ``status.json`` every few seconds.  Labels come from the snapshot (``labels``: the
application's translated texts, i18n ids ``web.*``) with the English catalogue as fallback.
It has no buttons that act on the machine: there is nothing to act on (read-only server).
"""
from __future__ import annotations

import json
from typing import Any

from mlaser.i18n import catalogue

from .snapshot import esc

LABEL_IDS = ("web.title", "web.state", "web.job", "web.sheet", "web.progress", "web.elapsed",
             "web.eta", "web.alarms", "web.no_alarms", "web.remedy", "web.queue", "web.updated",
             "web.readonly", "web.offline", "web.stale", "web.speed", "web.power", "web.position")


def labels(overrides: Any = None) -> dict:
    cat = catalogue("en")
    out = {k: cat.strings.get(k, k.split(".", 1)[1]) for k in LABEL_IDS}
    if isinstance(overrides, dict):
        out.update({k: str(v) for k, v in overrides.items() if k in out and v})
    return out


CSS = """
:root{--bg:#fafafa;--fg:#212121;--card:#fff;--mute:#616161;--line:#e0e0e0}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--card:#1e1e1e;--mute:#aaa;--line:#333}}
*{box-sizing:border-box}body{margin:0;font:16px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 16px;font-weight:600;display:flex;justify-content:space-between;align-items:center}
main{padding:0 12px 24px;display:grid;gap:12px;max-width:900px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
#state{font-size:28px;font-weight:700;border-radius:10px;padding:14px 16px;color:#fff;background:#616161}
#state small{display:block;font-size:15px;font-weight:400;opacity:.9}
.st-running{background:#2e7d32!important}.st-hold{background:#ff8f00!important;color:#000!important}
.st-fault,.st-alarm{background:#b71c1c!important}.st-ready{background:#1565c0!important}
.bar{height:22px;background:var(--line);border-radius:11px;overflow:hidden}
.bar>div{height:100%;background:#2e7d32;width:0}
.row{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.mute{color:var(--mute)}h2{font-size:15px;margin:0 0 6px;color:var(--mute);text-transform:uppercase}
ul{margin:0;padding-left:18px}li{margin:4px 0}.alarm{color:#c62828;font-weight:600}
img{width:100%;height:auto;background:#fff;border-radius:6px}
footer{font-size:13px;color:var(--mute);text-align:center;padding:8px}
"""

JS = """
const T=%(token)s, L=%(labels)s, R=%(refresh)d;
function fmt(s){if(s==null||s<0)return'-';s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%%3600/60),x=s%%60;
return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0');}
function txt(id,v){const e=document.getElementById(id);if(e)e.textContent=v==null?'':v;}
function li(ul,items){ul.replaceChildren();for(const it of items){const l=document.createElement('li');
if(it.cls)l.className=it.cls;l.textContent=it.text;ul.appendChild(l);}}
async function tick(){
 try{const r=await fetch('status.json?t='+encodeURIComponent(T),{cache:'no-store'});
  if(!r.ok)throw new Error(r.status);const s=await r.json();
  const st=document.getElementById('state');st.className='st-'+(s.kind||'offline');
  st.firstChild.textContent=(s.icon?s.icon+' ':'')+(s.state||'');txt('reason',s.reason||'');
  txt('job',s.job||'-');txt('sheet',s.sheet||'-');
  const p=Math.max(0,Math.min(1,s.progress||0));document.getElementById('pbar').style.width=(p*100).toFixed(1)+'%%';
  txt('pct',(p*100).toFixed(0)+' %%');txt('elapsed',fmt(s.elapsed_s));txt('eta',fmt(s.eta_s));
  txt('speed',s.speed||'-');txt('power',s.power||'-');txt('pos',s.position||'-');
  const al=s.alarms||[];li(document.getElementById('alarms'),al.length?al.map(a=>({cls:'alarm',
   text:a.text+(a.remedy?' — '+L['web.remedy']+': '+a.remedy:'')})):[{text:L['web.no_alarms']}]);
  li(document.getElementById('queue'),(s.queue||[]).map(q=>({text:q})));
  const age=(Date.now()/1000)-(s.published||0);
  txt('upd',L['web.updated']+' '+new Date((s.published||0)*1000).toLocaleTimeString()+(age>10?' — '+L['web.stale']:''));
  if(s.sheet_rev!==window._rev){window._rev=s.sheet_rev;document.getElementById('svg').src='sheet.svg?t='+encodeURIComponent(T)+'&r='+s.sheet_rev;}
 }catch(e){const st=document.getElementById('state');st.className='st-offline';st.firstChild.textContent=L['web.offline'];}
 setTimeout(tick,R*1000);}
tick();
"""


def _script_json(value) -> str:
    """JSON for an inline ``<script>``: ``<`` escaped, so a text holding ``</script>`` or
    ``<!--`` cannot end or change the element (code review P2-4)."""
    return (json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_page(snapshot: dict, token: str, nonce: str, refresh_s: float = 2.0) -> str:
    L = labels(snapshot.get("labels"))
    js = JS % {"token": _script_json(token), "labels": _script_json(L),
               "refresh": max(1, int(round(refresh_s)))}
    e = {k: esc(v) for k, v in L.items()}
    return f"""<!doctype html>
<html lang="{esc(snapshot.get('lang', 'en'))}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{e['web.title']}</title><style nonce="{nonce}">{CSS}</style></head>
<body><header><span>{e['web.title']}</span><span class="mute">{e['web.readonly']}</span></header>
<main>
<div id="state" class="st-offline">{esc(snapshot.get('state') or '...')}<small id="reason"></small></div>
<div class="card"><div class="row"><span>{e['web.job']}: <b id="job">-</b></span>
<span>{e['web.sheet']}: <b id="sheet">-</b></span></div>
<div class="bar" aria-label="{e['web.progress']}"><div id="pbar"></div></div>
<div class="row"><span id="pct">0 %</span><span>{e['web.elapsed']} <b id="elapsed">-</b></span>
<span>{e['web.eta']} <b id="eta">-</b></span></div>
<div class="row mute"><span>{e['web.speed']} <span id="speed">-</span></span>
<span>{e['web.power']} <span id="power">-</span></span><span>{e['web.position']} <span id="pos">-</span></span></div></div>
<div class="card"><h2>{e['web.alarms']}</h2><ul id="alarms"></ul></div>
<div class="card"><img id="svg" alt="{e['web.sheet']}"></div>
<div class="card"><h2>{e['web.queue']}</h2><ul id="queue"></ul></div>
</main><footer id="upd"></footer>
<script nonce="{nonce}">{js}</script></body></html>
"""


__all__ = ["render_page", "labels", "LABEL_IDS"]
