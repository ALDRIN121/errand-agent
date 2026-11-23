"""A local web UI: one stdlib HTTP server, one embedded page, no build step.

Three rules, inherited from the CLI and just as load-bearing here:

1. **This module never touches a tool.** It calls `Orchestrator.handle_turn` and
   `Watcher.tick` and nothing lower, so the approval monopoly holds. A UI that could
   call `server.book` would be the second door the architecture test exists to forbid.
2. **It never formats a terminal sentence.** The agent's final words come from
   `render_outcome` inside the orchestrator; if this file could write them, I3b's
   byte-stability would depend on the UI author's memory.
3. **State shown is state read.** Every panel is rendered from the session, the trace
   and the db. No invented progress, no optimistic "booking…" that the runtime has not
   confirmed.

The page is served from a string. There is no CDN, no npm, no bundler: the whole point
of this project is that it runs from a checkout with nothing installed.
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from errand.orchestrator import Orchestrator
from errand.trace import read_trace
from errand.watcher import Watcher

# ---------------------------------------------------------------- state snapshots


def snapshot(orch: Orchestrator, watcher: Watcher, trace_path: Path | None) -> dict:
    """Everything the page shows, read from the live objects.

    Deliberately boring: each field is a real attribute, not a derived guess.
    """
    # Copied, not referenced: a turn may be mutating these while the page polls, and a
    # "dictionary changed size during iteration" would take the status line down exactly
    # when it is needed.
    memory_rows = list(orch.memory.admissible(orch.session.user_id))
    pending = orch.pending
    slots = dict(orch.session.slots or {})
    state_value = orch.session.state.value
    terminal = state_value == "TERMINAL"
    # A terminal session has nothing left to approve. `orch.pending` deliberately
    # survives `_terminal` — the trace path reads it to attribute the outcome — so the
    # finished-ness must be applied here. Without this the UI kept offering a live
    # "Yes" button for a booking that had already completed.
    if terminal:
        pending = None
    return {
        "state": state_value,
        "terminal": terminal,
        "outcome": orch.session.outcome,
        "outcome_verb": orch.session.outcome_verb,
        "sentence": orch.session.sentence if hasattr(orch.session, "sentence") else None,
        "turns": orch.session.turns,
        "tool_calls": orch.session.tool_calls,
        "clarify_turns": orch.session.clarify_turns,
        "budgets": {"turns": 16, "tools": 20, "clarify": 4},
        "slots": _jsonable(slots),
        # A terminal session has nothing left to approve. `orch.pending` deliberately
        # survives _terminal (the trace path reads it to attribute the outcome), so the
        # UI is where the finished-ness must be applied.
        "pending": (
            {
                "kind": pending.kind,
                "provider_id": pending.provider_id,
                "service": pending.service,
                "start": pending.slot_start.isoformat(),
                "end": pending.slot_end.isoformat(),
                "price_paise": pending.price_paise,
                "booking_id": pending.booking_id,
            }
            if pending
            else None
        ),
        "memory": [
            {"key": m.key, "value": str(m.value), "source": m.source} for m in memory_rows
        ],
        "events": _events(trace_path),
        "last_tool_result": orch.last_tool_result,
        # The most recent human-readable progress line, so the page can say what is
        # happening instead of leaving a spinner with no explanation.
        "progress": _last_progress(trace_path),
        # The turn in flight, assembled from the same trace. `reply_stream` is prose the
        # model is writing right now and is NOT the answer: the answer is `reply`, which
        # only exists once the runtime has decided something.
        "reply_stream": _reply_stream(trace_path),
        "search_hits": [
            {"title": h.title, "url": h.url, "snippet": h.snippet}
            for h in getattr(orch, "search_hits", [])[:5]
        ],
    }


def _reply_stream(trace_path: Path | None) -> dict | None:
    """Deltas of the turn in flight, and the turn they belong to.

    Bounded by the newest `turn` event so text from an earlier turn can never be shown
    as if it were current — a stale sentence is worse than no sentence.

    Marked `provisional` deliberately: this is the model talking, and only
    `render_outcome` decides what a user is told actually happened.
    """
    if not trace_path:
        return None
    rows = read_trace(trace_path)[-120:]
    start = 0
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("type") == "turn":
            start = index
            break
    parts: list[str] = []
    for row in rows[start:]:
        if row.get("type") == "reply_delta":
            parts.append(row.get("text") or "")
    if not parts:
        return None
    return {"text": "".join(parts), "provisional": True}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "booking_id"):
        return {
            "booking_id": getattr(value, "booking_id", None),
            "service": getattr(value, "service", None),
            "provider_id": getattr(value, "provider_id", None),
            "status": getattr(value, "status", None),
            "slot": _jsonable(getattr(value, "slot", None)),
        }
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


# Event types worth showing a human, and the label to give them. Anything not listed is
# still counted in the raw stream but not dressed up as meaningful.
_LABELS = {
    "turn": "user turn",
    "prompt": "prompt assembled",
    "action": "model proposed",
    "tool_call": "tool called",
    "tool_refused": "tool refused",
    "tool_error": "tool failed",
    "tool_result_summary": "result summarised",
    "progress": "progress",
    "web_search": "web search",
    "research_done": "research returned",
    "loop_detected": "loop stopped",
    "approval_opened": "approval raised",
    "approval_validated": "approval checked",
    "side_effect_ok": "side effect",
    "side_effect_retry": "retry",
    "timeout": "timeout",
    "status_check": "status check",
    "terminal": "terminal",
    "proactive": "proactive",
}


def _events(trace_path: Path | None, limit: int = 400) -> list[dict]:
    if not trace_path:
        return []
    rows = read_trace(trace_path)
    out = []
    for row in rows[-limit:]:
        kind = row.get("type", "?")
        out.append({
            "seq": row.get("seq"),
            "type": kind,
            "label": _LABELS.get(kind, kind),
            "detail": _detail(kind, row),
        })
    return out


def _last_progress(trace_path: Path | None) -> dict | None:
    """The newest `progress` event, or None. Read from the trace, not a side channel."""
    if not trace_path:
        return None
    for row in reversed(read_trace(trace_path)[-80:]):
        if row.get("type") == "progress":
            return {"key": row.get("key"), "text": row.get("text"),
                    "state": row.get("state")}
    return None


def _detail(kind: str, row: dict) -> str:
    """One line per event, built from fields that exist. Never invents a value."""
    for key in ("tool", "name", "outcome", "reason", "user", "state", "chars"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    return ""


# ------------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>errand agent</title>
<style>
  :root{
    --bg:#0f1115; --panel:#161a21; --panel2:#1c222b; --line:#262d38;
    --ink:#e6e9ef; --dim:#8b94a3; --accent:#5b9dff; --ok:#3ecf8e;
    --warn:#f2b34b; --bad:#f0616d; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  @media (prefers-color-scheme:light){
    :root{--bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--line:#dfe3e9;
          --ink:#161a21;--dim:#5d6675;}
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{display:flex;align-items:center;gap:14px;padding:12px 18px;
         border-bottom:1px solid var(--line);background:var(--panel);
         position:sticky;top:0;z-index:5}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.01em}
  .pill{font:11px/1 var(--mono);padding:5px 9px;border-radius:99px;
        border:1px solid var(--line);color:var(--dim);background:var(--panel2)}
  .pill b{color:var(--ink);font-weight:600}
  .wrap{display:grid;grid-template-columns:1fr 340px;gap:0;height:calc(100vh - 50px)}
  @media (max-width:900px){.wrap{grid-template-columns:1fr;height:auto}}
  .chat{display:flex;flex-direction:column;min-height:0}
  #log{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
  .msg{max-width:min(760px,92%);padding:11px 14px;border-radius:12px;
       white-space:pre-wrap;word-wrap:break-word}
  .user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
  .agent{align-self:flex-start;background:var(--panel);border:1px solid var(--line);
         border-bottom-left-radius:4px}
  .sys{align-self:center;font:12px/1.4 var(--mono);color:var(--dim);
       background:var(--panel2);border:1px solid var(--line);border-radius:8px;
       padding:7px 12px;max-width:92%}
  .sys.bad{color:var(--bad);border-color:var(--bad)}
  .sys.ok{color:var(--ok);border-color:var(--ok)}
  form{display:flex;gap:9px;padding:13px 18px;border-top:1px solid var(--line);
       background:var(--panel)}
  input[type=text]{flex:1;padding:11px 13px;border-radius:9px;border:1px solid var(--line);
       background:var(--bg);color:var(--ink);font:inherit;outline:none}
  input[type=text]:focus{border-color:var(--accent)}
  button{padding:11px 17px;border-radius:9px;border:1px solid var(--line);
       background:var(--panel2);color:var(--ink);font:inherit;cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button:disabled{opacity:.45;cursor:not-allowed}
  aside{border-left:1px solid var(--line);background:var(--panel);overflow-y:auto;
        padding:15px;min-height:0}
  @media (max-width:900px){aside{border-left:none;border-top:1px solid var(--line)}}
  .card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
        padding:12px;margin-bottom:13px}
  .card h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;
           color:var(--dim);margin:0 0 9px;font-weight:600}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font:13px var(--mono)}
  .kv span:first-child{color:var(--dim)}
  .bar{height:4px;border-radius:99px;background:var(--line);margin-top:7px;overflow:hidden}
  .bar i{display:block;height:100%;background:var(--accent);transition:width .25s}
  .bar i.warn{background:var(--warn)} .bar i.bad{background:var(--bad)}
  #events{font:11px/1.6 var(--mono);max-height:300px;overflow-y:auto}
  .ev{display:flex;gap:8px;padding:1px 0;border-bottom:1px solid rgba(128,128,128,.09)}
  .ev .n{color:var(--dim);min-width:26px;text-align:right}
  .ev .t{color:var(--accent)} .ev .d{color:var(--dim);overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
  .mem{font:12px/1.5 var(--mono);padding:3px 0;border-bottom:1px solid rgba(128,128,128,.09);
       cursor:pointer}
  .mem:hover{color:var(--bad)}
  .mem b{font-weight:600;color:var(--dim)}
  .empty{color:var(--dim);font:12px var(--mono)}
  .approve{display:flex;gap:9px;margin-top:9px}
  .approve button{flex:1;padding:9px}
  #status{display:flex;align-items:center;gap:9px;padding:8px 18px;
          font:12px/1.4 var(--mono);color:var(--dim);min-height:34px;
          border-top:1px solid var(--line);background:var(--panel)}
  #status .dot{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none}
  #status.busy .dot{background:var(--accent);animation:pulse 1.1s ease-in-out infinite}
  #status.ok .dot{background:var(--ok)}
  #status.bad .dot{background:var(--bad)}
  @keyframes pulse{0%,100%{opacity:.35;transform:scale(.85)}50%{opacity:1;transform:scale(1.15)}}
  .hit{padding:7px 0;border-bottom:1px solid rgba(128,128,128,.1);font:12px/1.45 var(--mono)}
  .hit a{color:var(--accent);text-decoration:none;word-break:break-all}
  .hit a:hover{text-decoration:underline}
  .hit .sn{color:var(--dim);display:block;margin-top:3px}
</style>
</head>
<body>
<header>
  <h1>errand agent</h1>
  <span class="pill">state <b id="p-state">—</b></span>
  <span class="pill">model <b id="p-model">—</b></span>
  <span class="pill" id="p-outcome-wrap" style="display:none">outcome <b id="p-outcome"></b></span>
  <span style="flex:1"></span>
  <span class="pill" id="p-conn">connecting…</span>
</header>

<div class="wrap">
  <section class="chat">
    <div id="log"></div>
    <div id="live" hidden><span class="lbl">writing</span><span id="live-text"></span></div>
    <div id="status"><span class="dot"></span><span id="status-text">ready</span></div>
    <form id="say" autocomplete="off">
      <input id="text" type="text" placeholder="Find a plumber in Koramangala this week, mornings, under Rs 1500" autofocus>
      <button class="primary" id="send">Send</button>
    </form>
  </section>

  <aside>
    <div class="card">
      <h2>Approval</h2>
      <div id="approval"><span class="empty">nothing pending</span></div>
    </div>

    <div class="card">
      <h2>Budgets</h2>
      <div class="kv"><span>turns</span><span id="b-turns">0 / 16</span></div>
      <div class="bar"><i id="bar-turns" style="width:0"></i></div>
      <div class="kv" style="margin-top:9px"><span>tool calls</span><span id="b-tools">0 / 20</span></div>
      <div class="bar"><i id="bar-tools" style="width:0"></i></div>
      <div class="kv" style="margin-top:9px"><span>clarifying</span><span id="b-clar">0 / 4</span></div>
      <div class="bar"><i id="bar-clar" style="width:0"></i></div>
    </div>

    <div class="card" id="research-card" style="display:none">
      <h2>Web results <span style="font-weight:400;text-transform:none;
          letter-spacing:0;color:var(--warn)">untrusted</span></h2>
      <div id="research"></div>
    </div>

    <div class="card">
      <h2>Slots</h2>
      <div id="slots"><span class="empty">nothing established</span></div>
    </div>

    <div class="card">
      <h2>Memory <span title="click a row to forget it" style="float:right;color:var(--dim)">forget ›</span></h2>
      <div id="memory"><span class="empty">nothing stored</span></div>
    </div>

    <div class="card">
      <h2>Trace</h2>
      <div id="events"><span class="empty">no events yet</span></div>
    </div>
  </aside>
</div>

<script>
const $ = id => document.getElementById(id);
let seenEvents = 0, model = "—", busy = false;

function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}

function bubble(kind, text){
  const el = document.createElement('div');
  el.className = 'msg ' + kind;
  el.textContent = text;
  $('log').appendChild(el);
  $('log').scrollTop = 1e9;
  return el;
}
function sys(text, cls){
  const el = document.createElement('div');
  el.className = 'sys' + (cls ? ' ' + cls : '');
  el.textContent = text;
  $('log').appendChild(el);
  $('log').scrollTop = 1e9;
}

function money(paise){ return '₹' + (paise/100).toLocaleString('en-IN'); }
function when(iso){
  if(!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('en-IN',{weekday:'short',day:'numeric',month:'short',
    hour:'2-digit',minute:'2-digit',hour12:false});
}

function render(s){
  $('p-state').textContent = s.state;
  $('p-model').textContent = model;
  if(s.outcome){ $('p-outcome-wrap').style.display=''; $('p-outcome').textContent = s.outcome; }

  // budgets
  const set = (id, used, cap) => {
    $('b-'+id).textContent = used + ' / ' + cap;
    const pct = Math.min(100, cap ? used/cap*100 : 0);
    const bar = $('bar-'+id);
    bar.style.width = pct + '%';
    bar.className = pct >= 100 ? 'bad' : pct >= 75 ? 'warn' : '';
  };
  set('turns', s.turns, s.budgets.turns);
  set('tools', s.tool_calls, s.budgets.tools);
  set('clar', s.clarify_turns, s.budgets.clarify);

  // approval
  const ap = $('approval');
  if(s.pending){
    const p = s.pending;
    ap.innerHTML =
      `<div style="font:13px/1.6 var(--mono)">
         <div style="color:var(--warn);font-weight:600;margin-bottom:6px">
           ${esc(p.kind)} — needs your yes</div>
         <div class="kv"><span>provider</span><span>${esc(p.provider_id)}</span></div>
         <div class="kv"><span>when</span><span>${esc(when(p.start))}</span></div>
         <div class="kv"><span>quoted</span><span>${esc(money(p.price_paise))}</span></div>
         ${p.booking_id?`<div class="kv"><span>booking</span><span>${esc(p.booking_id)}</span></div>`:''}
       </div>
       <div class="approve">
         <button onclick="say('yes, go ahead')">Yes</button>
         <button onclick="say('no, cancel that')">No</button>
       </div>`;
  } else {
    ap.innerHTML = '<span class="empty">nothing pending</span>';
  }

  // The model's prose, arriving. Shown while it is being written and removed as soon
  // as the turn ends, because the finished message already carries it: leaving both
  // would show the same sentence twice, and the top copy would be the one that had not
  // been checked against what actually happened.
  const live = $('live'), liveText = $('live-text'), stream = s.reply_stream;
  if (busy && stream && stream.text) {
    live.hidden = false;
    liveText.textContent = stream.text;
  } else {
    live.hidden = true;
    liveText.textContent = '';
  }

  // live progress: say what is happening, in words, from the trace
  const st = $('status'), stText = $('status-text');
  if (busy) {
    st.className = 'busy';
    stText.textContent = (s.progress && s.progress.text) || 'working…';
  } else if (s.terminal) {
    st.className = 'ok';
    stText.textContent = s.outcome ? ('finished — ' + s.outcome) : 'finished';
  } else if (s.pending) {
    st.className = 'busy';
    stText.textContent = 'waiting for your yes or no';
  } else {
    st.className = '';
    stText.textContent = 'ready — type a request';
  }

  // web results, clearly marked as external and untrusted
  const rc = $('research-card'), rl = $('research');
  const hits = s.search_hits || [];
  if (hits.length) {
    rc.style.display = '';
    rl.innerHTML = hits.map(h => {
      let host = '';
      try { host = new URL(h.url).hostname.replace(/^www\./, ''); } catch(e) {}
      return `<div class="hit"><a href="${esc(h.url)}" target="_blank"
                rel="noopener noreferrer">${esc(h.title || host || h.url)}</a>
              <span class="sn">${esc(h.snippet || '')}</span></div>`;
    }).join('');
  } else {
    rc.style.display = 'none';
  }

  // slots
  const sl = $('slots'); const keys = Object.keys(s.slots || {});
  sl.innerHTML = keys.length
    ? keys.map(k => `<div class="kv"><span>${esc(k)}</span><span>${esc(
        typeof s.slots[k] === 'object' ? JSON.stringify(s.slots[k]) : s.slots[k])}</span></div>`).join('')
    : '<span class="empty">nothing established</span>';

  // memory
  const mem = $('memory');
  mem.innerHTML = (s.memory && s.memory.length)
    ? s.memory.map(m => `<div class="mem" onclick="forget('${esc(m.key)}')"
        title="click to forget"><b>${esc(m.key)}</b><br>${esc(m.value)}
        <span style="color:var(--dim)">(${esc(m.source)})</span></div>`).join('')
    : '<span class="empty">nothing stored</span>';

  // trace
  const ev = s.events || [];
  if(ev.length !== seenEvents){
    seenEvents = ev.length;
    $('events').innerHTML = ev.map(e =>
      `<div class="ev"><span class="n">${e.seq}</span>
        <span class="t">${esc(e.label)}</span>
        <span class="d">${esc(e.detail)}</span></div>`).join('');
    $('events').scrollTop = 1e9;
  }

  // input lock: a terminal session cannot take more turns
  $('text').disabled = !!s.terminal || busy;
  $('send').disabled = !!s.terminal || busy;
  $('text').placeholder = s.terminal
    ? 'session ended — /new to start another'
    : 'Find a plumber in Koramangala this week, mornings, under Rs 1500';
}

async function refresh(){
  try{
    const r = await fetch('/api/state');
    const s = await r.json();
    model = s.model || model;
    $('p-conn').textContent = 'live';
    $('p-conn').style.color = 'var(--ok)';
    render(s);
  }catch(e){
    $('p-conn').textContent = 'disconnected';
    $('p-conn').style.color = 'var(--bad)';
  }
}

async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(body||{})});
  return r.json();
}

async function say(text){
  text = (text || $('text').value).trim();
  if(!text) return;
  if(text.startsWith('/')){ await command(text); return; }
  bubble('user', text);
  $('text').value = '';
  busy = true;
  $('text').disabled = true; $('send').disabled = true;
  $('status').className = 'busy';
  $('status-text').textContent = 'sending…';
  try{
    const res = await post('/api/turn', {text});
    if(res.reply) bubble('agent', res.reply);
    for(const p of (res.proactive || [])) sys('[proactive] ' + p);
    if(res.error) sys(res.error, 'bad');
  }catch(e){
    sys('request failed: ' + e.message, 'bad');
  }finally{
    busy = false;
    $('text').disabled = false; $('send').disabled = false;
    $('text').focus();
    refresh();
  }
}

async function command(line){
  $('text').value = '';
  const res = await post('/api/command', {line});
  if(res.text) sys(res.text, res.cls || '');
  refresh();
}

async function forget(key){
  const res = await post('/api/command', {line: '/forget ' + key});
  if(res.text) sys(res.text);
  refresh();
}

$('say').addEventListener('submit', e => { e.preventDefault(); say(); });
refresh();
setInterval(refresh, 1500);
// during a turn, poll faster: the point of the status line is that it keeps up
setInterval(() => { if (busy) refresh(); }, 400);
</script>
</body>
</html>
"""


# -------------------------------------------------------------------- the server


class Handler(BaseHTTPRequestHandler):
    """A thin transport. All decisions live in Orchestrator; this only moves bytes."""

    server_version = "errand-ui/0.1"
    orch: Orchestrator
    watcher: Watcher
    trace_path: Path | None
    lock: threading.Lock
    model_label: str

    def log_message(self, *args) -> None:  # quiet: the page is the log
        pass

    # -- helpers ------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            # Deliberately NOT under the turn lock. A turn can run for twenty seconds
            # of model latency, and the whole purpose of this endpoint during that time
            # is to show what is happening. snapshot() copies what it reads.
            payload = snapshot(self.orch, self.watcher, self.trace_path)
            payload["model"] = self.model_label
            self._json(payload)
            return
        if path.startswith("/api/trace"):
            # the raw JSONL, for anyone who wants the machine-readable record
            if not self.trace_path or not self.trace_path.exists():
                self._json({"error": "no trace"}, 404)
                return
            self._send(200, self.trace_path.read_bytes(), "application/x-ndjson")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._send(404, b"not found", ctype)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/turn":
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "empty"}, 400)
                return
            with self.lock:
                if self.orch.session.state.value == "TERMINAL":
                    self._json({"reply": None,
                                "error": "session ended — restart with /new"}, 409)
                    return
                try:
                    result = self.orch.handle_turn(text)
                    proactive = list(self.watcher.tick())
                    self._json({"reply": result.reply,
                                "state": result.state.value,
                                "outcome": result.outcome,
                                "refused": result.refused,
                                "proactive": proactive})
                except Exception as exc:  # report, never leak a traceback to the page
                    self._json({"reply": None, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if path == "/api/command":
            line = str(body.get("line") or "").strip()
            with self.lock:
                payload, code = self._command(line)
            self._json(payload, code)
            return

        self._json({"error": "not found"}, 404)

    # -- the small command set, mirroring the CLI ---------------------------
    def _command(self, line: str) -> tuple[dict, int]:
        orch = self.orch
        if line in ("/quit", "/exit"):
            return {"text": "Use Ctrl-C in the server terminal to stop."}, 200

        if line == "/help":
            return {"text": "commands: /new  /memory  /forget <key>  /state  /trace  "
                            "simulate cancel <booking_id>"}, 200

        if line in ("/new", "/reset"):
            # Advertised in the placeholder text since the UI was written and never
            # implemented: a finished session could only be revived by restarting the
            # process.
            session = orch.reset()
            return {"text": f"new conversation started (session {session.id}). "
                            f"Memory and existing bookings are kept."}, 200

        if line == "/state":
            return {"text": (f"state:  {orch.session.state.value}\n"
                             f"slots:  {_jsonable(orch.session.slots)}\n"
                             f"budget: turns={orch.session.turns} "
                             f"tools={orch.session.tool_calls}")}, 200

        if line == "/memory":
            rows = orch.memory.admissible(orch.session.user_id)
            if not rows:
                return {"text": "  (nothing stored)"}, 200
            return {"text": "\n".join(
                f"  {m.key:28s} {m.value}  ({m.source})" for m in rows)}, 200

        if line.startswith("/forget "):
            key = line.split(maxsplit=1)[1].strip()
            n = orch.memory.forget(orch.session.user_id, key)
            return {"text": f"  forgot {n} row(s) for {key!r}"}, 200

        if line == "/trace":
            return {"text": str(self.trace_path) if self.trace_path
                            else "no trace (start the server with --trace <path>)"}, 200

        if line.startswith("simulate cancel "):
            booking_id = line.split()[-1]
            orch.server.emit_provider_cancel(booking_id)
            fired = list(self.watcher.tick())
            text = "\n".join(f"[proactive] {b}" for b in fired) if fired else "raised"
            return {"text": text}, 200

        return {"text": "unknown command — /help"}, 200


def serve(orch: Orchestrator, watcher: Watcher, *, trace_path: Path | None = None,
          model_label: str = "scripted", host: str = "127.0.0.1", port: int = 8765,
          ready: threading.Event | None = None) -> ThreadingHTTPServer:
    """Bind the server and return it, already serving on a background thread.

    Separate from `run` so a test can bind port 0 and drive the real routes without a
    subprocess or a sleep.
    """
    handler = type("BoundHandler", (Handler,), {
        "orch": orch, "watcher": watcher, "trace_path": trace_path,
        "lock": threading.Lock(), "model_label": model_label,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    if ready is not None:
        ready.set()
    return httpd


__all__ = ["serve", "snapshot", "PAGE"]
