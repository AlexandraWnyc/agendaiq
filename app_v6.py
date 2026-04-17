"""
app_v6.py — AgendaIQ v6 Web Application (full rebuild)
Matter Detail Drawer · Workflow Audit Trail · Due-Date Alerts · Cross-linking
"""
import os, sys, uuid, json, queue, threading, logging
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).parent))

# Configure logging EARLY so all modules (scraper, lifecycle, etc.)
# can write to stdout via the "oca-agent" logger.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,       # override any prior basicConfig
)

import db as database
from db import init_db, get_db
from utils import load_api_key, parse_date_arg, now_iso
from schema import WORKFLOW_STATUSES
import notifications

app = Flask(__name__)
JOBS: dict = {}


def _current_user() -> str:
    """Extract the username from Basic Auth header, or 'anonymous'."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("basic "):
        try:
            import base64
            raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", "ignore")
            user, _, _ = raw.partition(":")
            if user:
                return user
        except Exception:
            pass
    return "anonymous"

# ─────────────────────────────────────────────────────────────
# Optional shared-password gate (for cloud demo before SSO lands).
# Activated only when APP_PASSWORD env var is set. Local dev → no gate.
# ─────────────────────────────────────────────────────────────
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
if _APP_PASSWORD:
    import base64
    from functools import wraps
    _AUTH_REALM = 'AgendaIQ (Demo)'

    def _check_auth(header_val: str) -> bool:
        if not header_val or not header_val.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header_val.split(" ", 1)[1]).decode("utf-8", "ignore")
            _, _, pw = raw.partition(":")
            return pw == _APP_PASSWORD
        except Exception:
            return False

    @app.before_request
    def _require_basic_auth():
        # Allow health check through so Render/Railway can probe us
        if request.path in ("/healthz", "/favicon.ico"):
            return None
        if _check_auth(request.headers.get("Authorization", "")):
            return None
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'}
        )

@app.route("/healthz")
def _healthz():
    return "ok", 200

# ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgendaIQ — Miami-Dade OCA</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --blue:#003087;--blue2:#0058a7;--blue-lt:#e8f0fb;--blue-mid:#c5d8f5;
  --green:#00843d;--green-lt:#e6f4ed;
  --red:#dc2626;--red-lt:#fee2e2;
  --orange:#d97706;--orange-lt:#fef3c7;
  --gray-50:#f8fafc;--gray-100:#f1f5f9;--gray-200:#e2e8f0;
  --gray-400:#94a3b8;--gray-600:#475569;--gray-800:#1e293b;
  --white:#fff;
  --shadow:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -1px rgba(0,0,0,.06);
  --shadow-lg:0 10px 25px -5px rgba(0,0,0,.12);
  --r:10px;
}
body{font-family:'Inter',sans-serif;background:var(--gray-100);color:var(--gray-800);min-height:100vh}

/* ── Header ── */
header{background:linear-gradient(135deg,var(--blue) 0%,var(--blue2) 100%);
  color:#fff;height:62px;display:flex;align-items:center;padding:0 1.5rem;gap:.75rem;
  box-shadow:0 2px 12px rgba(0,48,135,.35);position:sticky;top:0;z-index:300}
.logo{display:flex;align-items:center;gap:.6rem;cursor:pointer}
.logo-icon{width:34px;height:34px;background:#fff;border-radius:7px;display:flex;align-items:center;justify-content:center}
.logo-icon svg{width:20px;height:20px}
.logo h1{font-size:1.2rem;font-weight:700}
.logo small{font-size:.65rem;opacity:.7;text-transform:uppercase;letter-spacing:.4px;display:block}
nav{display:flex;gap:.2rem;margin-left:1.25rem}
.nb{padding:.42rem .9rem;border-radius:7px;border:none;background:transparent;
  color:rgba(255,255,255,.75);font-family:inherit;font-size:.82rem;font-weight:500;
  cursor:pointer;transition:all .15s}
.nb:hover,.nb.on{background:rgba(255,255,255,.18);color:#fff}
.alert-badge{display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;background:#dc2626;color:#fff;border-radius:50%;
  font-size:.65rem;font-weight:700;margin-left:.3rem;vertical-align:middle}
.hbadge{margin-left:auto;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);
  padding:.22rem .65rem;border-radius:20px;font-size:.7rem;font-weight:500}
#current-user-badge{background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.3);
  padding:.22rem .65rem;border-radius:20px;font-size:.72rem;cursor:pointer}

/* ── Pages ── */
.pg{display:none;max-width:1380px;margin:1.5rem auto;padding:0 1.25rem}
.pg.on{display:block}

/* ── Alert banners ── */
.alert-bar{display:flex;align-items:center;gap:.75rem;padding:.75rem 1.1rem;border-radius:8px;
  margin-bottom:.75rem;font-size:.82rem;font-weight:500;cursor:pointer}
.alert-bar .icon{font-size:1rem;flex-shrink:0}
.alert-bar .txt{flex:1}
.alert-bar .count{font-weight:700;font-size:.9rem}
.ab-red{background:var(--red-lt);color:var(--red);border:1px solid #fca5a5}
.ab-orange{background:var(--orange-lt);color:var(--orange);border:1px solid #fcd34d}
.ab-green{background:#f0fdf4;color:#166534;border:1px solid #86efac}
.ab-gray{background:var(--gray-100);color:var(--gray-600);border:1px solid var(--gray-200)}

/* ── Cards ── */
.card{background:#fff;border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden;margin-bottom:1.1rem}
.ch{padding:.8rem 1.1rem;border-bottom:1px solid var(--gray-200);display:flex;align-items:center;
  gap:.5rem;font-weight:600;font-size:.875rem;justify-content:space-between}
.ch-left{display:flex;align-items:center;gap:.5rem}
.cicon{width:26px;height:26px;background:var(--blue-lt);border-radius:6px;display:flex;
  align-items:center;justify-content:center;font-size:.8rem}
.cb{padding:1.1rem}

/* ── Buttons ── */
.btn{padding:.48rem .95rem;border:none;border-radius:7px;font-family:inherit;font-size:.8rem;
  font-weight:600;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:.35rem}
.btn-p{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;box-shadow:0 3px 8px rgba(0,48,135,.25)}
.btn-p:hover:not(:disabled){opacity:.88;transform:translateY(-1px)}
.btn-p:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-s{background:var(--green);color:#fff}
.btn-s:hover{background:#006b30}
.btn-o{background:transparent;border:1.5px solid var(--gray-200);color:var(--gray-600)}
.btn-o:hover{border-color:var(--blue2);color:var(--blue2)}
.btn-d{background:var(--red-lt);color:var(--red);border:1px solid #fca5a5}
.btn-sm{padding:.3rem .65rem;font-size:.74rem}
.btn-xs{padding:.2rem .5rem;font-size:.7rem}
.full{width:100%;justify-content:center;margin-top:.85rem}

/* ── Grids ── */
.g2{display:grid;grid-template-columns:350px 1fr;gap:1.1rem;align-items:start}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.7rem}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:.7rem}
@media(max-width:1024px){.g2{grid-template-columns:1fr}.g4{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.g3{grid-template-columns:1fr 1fr}.g4{grid-template-columns:1fr 1fr}}

/* ════════════════════════════════════════════════════════════
   MOBILE / TABLET RESPONSIVE
════════════════════════════════════════════════════════════ */
@media(max-width:820px){
  /* Header collapses, nav wraps */
  header{height:auto;min-height:56px;padding:.5rem .75rem;flex-wrap:wrap;gap:.4rem}
  .logo h1{font-size:1rem}
  .logo small{display:none}
  nav{order:3;width:100%;margin-left:0;overflow-x:auto;padding-bottom:.25rem;
    -webkit-overflow-scrolling:touch;scrollbar-width:none}
  nav::-webkit-scrollbar{display:none}
  .nb{flex-shrink:0;padding:.45rem .7rem;font-size:.78rem}
  .hbadge{display:none}
  #current-user-sel{max-width:140px;font-size:.7rem !important}

  /* Pages get less side padding */
  .pg{margin:1rem auto;padding:0 .65rem}
  .g2,.g3,.g4{grid-template-columns:1fr !important;gap:.85rem}

  /* Cards full-width, less padding */
  .card .cb,.card .ch{padding:.85rem .9rem}

  /* Tables: horizontal scroll wrap */
  .pg table{display:block;overflow-x:auto;white-space:nowrap;
    -webkit-overflow-scrolling:touch;font-size:.78rem}
  .pg table th,.pg table td{padding:.45rem .55rem !important}

  /* Drawer becomes a true full-screen sheet */
  #drawer{width:100vw !important;right:-100vw}
  #drawer.open{right:0}
  .dr-header{padding:.75rem .85rem .65rem}
  .dr-title{font-size:.95rem;padding-right:42px}
  .dr-tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .dr-tabs::-webkit-scrollbar{display:none}
  .dtab{padding:.55rem .8rem;font-size:.74rem;flex-shrink:0}
  .dr-body{padding:.75rem}

  /* Help modal full-screen on phones */
  #help-modal > div{max-width:100% !important;width:100% !important;
    height:100vh;border-radius:0 !important;overflow-y:auto}
  .htab{padding:.55rem .7rem;font-size:.76rem}

  /* Workflow filters stack */
  .wf-filters{flex-direction:column;align-items:stretch !important}
  .wf-filters > div{width:100%}
  .wf-filters select{width:100%}

  /* Welcome strip tile grid → 1 col */
  .qa-tile{padding:.6rem .7rem}

  /* Buttons: bigger tap targets */
  .btn{min-height:38px;padding:.5rem .85rem}
  .btn-sm{min-height:34px;padding:.4rem .7rem}

  /* Inputs: prevent iOS auto-zoom (font-size must be ≥16px) */
  input,select,textarea{font-size:16px !important}

  /* Bulk-action button row wraps */
  .pg [onclick^="bulk"]{margin-bottom:.3rem}
}

@media(max-width:480px){
  /* Tighter on phones */
  .pg{padding:0 .5rem}
  .logo h1{font-size:.92rem}
  .nb{font-size:.74rem;padding:.4rem .55rem}
  .dr-meta{font-size:.7rem;gap:.3rem .8rem}
  .ds-title{font-size:.66rem}
  .editable-field{font-size:.78rem;padding:.6rem}
  /* Hide the v6 chip and Acting-as label text on tiny screens */
  #current-user-sel{max-width:110px}
}

/* ── Stats ── */
.stat{background:var(--gray-50);border:1px solid var(--gray-200);border-radius:8px;
  padding:.75rem;text-align:center}
.stat .n{font-size:1.75rem;font-weight:700;color:var(--blue)}
.stat .l{font-size:.7rem;color:var(--gray-400);font-weight:500;margin-top:.1rem}

/* ── Forms ── */
label{display:block;font-size:.76rem;font-weight:600;color:var(--gray-600);margin-bottom:.28rem}
input[type=text],input[type=email],input[type=password],select,textarea{
  width:100%;padding:.52rem .75rem;border:1.5px solid var(--gray-200);border-radius:7px;
  font-family:inherit;font-size:.85rem;background:#fff;transition:border-color .2s;margin-bottom:.75rem}
input:focus,select:focus,textarea:focus{
  outline:none;border-color:var(--blue2);box-shadow:0 0 0 3px rgba(0,88,167,.1)}
textarea{resize:vertical;min-height:70px;line-height:1.55}

.toggle{display:flex;background:var(--gray-100);border-radius:8px;padding:3px;margin-bottom:.75rem}
.toggle button{flex:1;padding:.42rem;border:none;background:transparent;border-radius:6px;
  font-size:.78rem;font-weight:500;cursor:pointer;color:var(--gray-600);transition:all .2s}
.toggle button.on{background:#fff;color:var(--blue);box-shadow:0 1px 3px rgba(0,0,0,.1);font-weight:600}

/* ── Tables ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--gray-50);color:var(--gray-600);font-weight:600;font-size:.73rem;
  text-align:left;padding:.58rem .85rem;border-bottom:2px solid var(--gray-200);white-space:nowrap}
td{padding:.6rem .85rem;border-bottom:1px solid var(--gray-100);vertical-align:top}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:var(--blue-lt)}
.file-link{color:var(--blue);font-weight:600;cursor:pointer;text-decoration:none;font-size:.78rem;
  font-family:monospace;white-space:nowrap}
.file-link:hover{text-decoration:underline}

/* ── Status badges ── */
.badge{display:inline-block;padding:.18rem .55rem;border-radius:99px;font-size:.69rem;font-weight:600;white-space:nowrap}
.b-New{background:#e0f2fe;color:#0369a1}
.b-Assigned{background:#fef3c7;color:#92400e}
.b-InProgress{background:#ede9fe;color:#5b21b6}
.b-DraftComplete{background:#fffbeb;color:#b45309}
.b-InReview{background:#dbeafe;color:#1d4ed8}
.b-NeedsRevision{background:#fef2f2;color:#dc2626}
.b-Finalized{background:var(--green-lt);color:var(--green)}
.b-Archived{background:var(--gray-100);color:var(--gray-400)}
.b-cf{background:var(--orange-lt);color:var(--orange)}
.b-overdue{background:var(--red-lt);color:var(--red)}
.b-soon{background:var(--orange-lt);color:var(--orange)}
.b-ok{background:var(--green-lt);color:var(--green)}

/* ── Progress / log ── */
.pw{background:var(--gray-100);border-radius:99px;height:7px;overflow:hidden;margin-bottom:.75rem}
.pb{height:100%;background:linear-gradient(90deg,var(--blue),var(--blue2));border-radius:99px;transition:width .4s;width:0%}
.pb.spin{width:35%;animation:spin 1.4s ease-in-out infinite}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(400%)}}
.logbox{background:#0f172a;color:#94a3b8;font-family:Menlo,Consolas,monospace;font-size:.74rem;
  padding:.85rem;border-radius:8px;height:210px;overflow-y:auto;line-height:1.65}
.logbox::-webkit-scrollbar{width:3px}
.logbox::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
.ll .ts{color:#475569;margin-right:.45rem}
.ll.ok .msg{color:#4ade80}
.ll.err .msg{color:#f87171}
.ll.sk .msg{color:#fbbf24}
.srow{display:flex;align-items:center;gap:.55rem;margin-bottom:.65rem}
.sdot{width:9px;height:9px;border-radius:50%;background:var(--gray-400);flex-shrink:0}
.sdot.run{background:var(--blue2);animation:pulse 1.2s ease-in-out infinite}
.sdot.ok{background:var(--green)}
.sdot.err{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* ── Committee checkboxes ── */
.cmteg{display:flex;flex-direction:column;gap:.28rem;max-height:240px;overflow-y:auto;padding:.05rem}
.cmteg::-webkit-scrollbar{width:3px}
.cmteg::-webkit-scrollbar-thumb{background:var(--gray-200)}
.ci{display:flex;align-items:center;gap:.5rem;padding:.38rem .55rem;border-radius:6px;cursor:pointer;transition:background .1s}
.ci:hover{background:var(--blue-lt)}
.ci input{accent-color:var(--blue);margin:0;width:14px;height:14px}
.ci span{font-size:.78rem}

/* ── File download items ── */
.fi{display:flex;align-items:center;justify-content:space-between;padding:.6rem .85rem;
  background:var(--gray-50);border:1px solid var(--gray-200);border-radius:8px;margin-bottom:.38rem}
.fi:hover{border-color:var(--blue2);background:var(--blue-lt)}
.fi-info{display:flex;align-items:center;gap:.55rem}
.ficon{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;
  font-size:.67rem;font-weight:700;color:#fff}
.ficon.xlsx{background:#1d6f42}
.ficon.docx{background:#2b5eb6}
a.dlbtn{padding:.28rem .7rem;background:var(--blue2);color:#fff;border-radius:6px;
  font-size:.73rem;font-weight:600;text-decoration:none}
a.dlbtn:hover{background:var(--blue)}

/* ════════════════════════════════════════════════════════════
   MATTER DETAIL DRAWER
════════════════════════════════════════════════════════════ */
#drawer-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:500;display:none;opacity:0;
  transition:opacity .25s}
#drawer-bg.open{display:block;opacity:1}
#drawer{position:fixed;top:0;right:-760px;width:min(760px,98vw);height:100vh;
  background:#fff;z-index:501;box-shadow:-8px 0 32px rgba(0,0,0,.18);
  display:flex;flex-direction:column;transition:right .3s cubic-bezier(.4,0,.2,1);overflow:hidden}
#drawer.open{right:0}
.dr-header{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;
  padding:1rem 1.25rem .85rem;flex-shrink:0}
.dr-title{font-size:1rem;font-weight:700;margin-bottom:.25rem}
.dr-meta{font-size:.75rem;opacity:.8;display:flex;flex-wrap:wrap;gap:.5rem 1.2rem}
.dr-tabs{display:flex;border-bottom:2px solid var(--gray-200);flex-shrink:0;background:#fff}
.dtab{padding:.65rem 1.1rem;border:none;background:transparent;font-family:inherit;
  font-size:.8rem;font-weight:500;color:var(--gray-600);cursor:pointer;border-bottom:2px solid transparent;
  margin-bottom:-2px;transition:all .15s}
.dtab.on{color:var(--blue);border-bottom-color:var(--blue);font-weight:600}
.dr-body{flex:1;overflow-y:auto;padding:1.1rem}
.dr-body::-webkit-scrollbar{width:4px}
.dr-body::-webkit-scrollbar-thumb{background:var(--gray-200);border-radius:4px}
.dr-close{position:absolute;top:.85rem;right:1rem;width:32px;height:32px;border:none;
  background:rgba(255,255,255,.2);border-radius:6px;color:#fff;font-size:1rem;
  cursor:pointer;display:flex;align-items:center;justify-content:center}
.dr-close:hover{background:rgba(255,255,255,.35)}

/* Drawer sections */
.ds{margin-bottom:1.1rem}
.ds-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.6px;
  color:var(--gray-400);margin-bottom:.45rem;display:flex;align-items:center;
  justify-content:space-between}
.editable-field{background:var(--gray-50);border:1px solid var(--gray-200);border-radius:8px;
  padding:.75rem;font-size:.82rem;line-height:1.6;white-space:pre-wrap;min-height:60px;
  transition:border-color .2s}
.editable-field[contenteditable=true]{cursor:text;border-color:var(--blue2)}
.editable-field[contenteditable=true]:focus{outline:none;box-shadow:0 0 0 3px rgba(0,88,167,.1)}
.cf-banner{background:var(--orange-lt);border:1px solid #fcd34d;border-radius:8px;
  padding:.6rem .85rem;font-size:.78rem;color:var(--orange);display:flex;align-items:center;
  gap:.5rem;margin-bottom:.85rem}

/* Timeline */
.timeline{position:relative;padding-left:1.5rem}
.timeline::before{content:'';position:absolute;left:.45rem;top:0;bottom:0;
  width:2px;background:var(--gray-200)}
.tl-item{position:relative;margin-bottom:.85rem}
.tl-dot{position:absolute;left:-1.2rem;top:.3rem;width:12px;height:12px;
  border-radius:50%;background:var(--blue2);border:2px solid #fff;
  box-shadow:0 0 0 2px var(--blue-mid)}
.tl-dot.status{background:var(--blue)}
.tl-dot.assign{background:var(--green)}
.tl-dot.note{background:var(--orange)}
.tl-dot.export{background:#7c3aed}
.tl-time{font-size:.7rem;color:var(--gray-400);margin-bottom:.15rem}
.tl-action{font-size:.8rem;font-weight:500;color:var(--gray-800)}
.tl-detail{font-size:.75rem;color:var(--gray-600);margin-top:.1rem}

/* Appearances list in drawer */
.app-row{display:flex;align-items:center;gap:.75rem;padding:.6rem .75rem;
  border:1px solid var(--gray-200);border-radius:8px;margin-bottom:.4rem;
  background:var(--gray-50);transition:all .15s;cursor:pointer}
.app-row:hover{border-color:var(--blue2);background:var(--blue-lt)}
.app-row .date{font-size:.78rem;font-weight:600;color:var(--gray-800);min-width:80px}
.app-row .body{font-size:.75rem;color:var(--gray-600);flex:1}
.app-row .right{display:flex;align-items:center;gap:.4rem}

/* ── AI Chat in drawer ── */
.chat-wrap{display:flex;flex-direction:column;height:calc(100vh - 220px);overflow:hidden}
.chat-msgs{flex:1;overflow-y:auto;padding:.5rem 0;display:flex;flex-direction:column;gap:.5rem}
.chat-msg{padding:.5rem .75rem;border-radius:10px;font-size:.82rem;line-height:1.45;max-width:88%;white-space:pre-wrap;word-wrap:break-word}
.chat-msg.user{background:var(--blue1);color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
.chat-msg.assistant{background:var(--gray-100);color:var(--gray-800);align-self:flex-start;border-bottom-left-radius:3px}
.chat-msg .chat-actions{margin-top:.35rem;display:flex;gap:.4rem}
.chat-msg .chat-actions button{font-size:.68rem;padding:1px 6px;border-radius:4px;border:1px solid var(--gray-300);
  background:#fff;color:var(--gray-600);cursor:pointer;transition:all .15s}
.chat-msg .chat-actions button:hover{border-color:var(--blue2);color:var(--blue2)}
.chat-msg .chat-actions .appended{color:var(--green);border-color:var(--green);cursor:default}
.chat-input-row{display:flex;gap:.4rem;padding:.5rem 0 0;border-top:1px solid var(--gray-200);align-items:flex-end}
.chat-input-row textarea{flex:1;font-size:.82rem;border:1px solid var(--gray-300);border-radius:8px;padding:.45rem .6rem;
  resize:none;min-height:38px;max-height:100px;font-family:inherit;line-height:1.4}
.chat-input-row textarea:focus{border-color:var(--blue2);outline:none;box-shadow:0 0 0 2px rgba(37,99,235,.12)}
.chat-send-btn{padding:.4rem .75rem;border-radius:8px;background:var(--blue2);color:#fff;
  font-size:.8rem;font-weight:600;border:none;cursor:pointer;white-space:nowrap;height:38px}
.chat-send-btn:disabled{opacity:.5;cursor:not-allowed}
.chat-ws-toggle{display:flex;align-items:center;gap:.35rem;font-size:.72rem;color:var(--gray-500);padding:.25rem 0}
.chat-ws-toggle input{margin:0}

/* ── Workflow page ── */
.wf-filters{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem;align-items:flex-end}
.wf-filters select,.wf-filters input{margin:0;width:auto;font-size:.8rem}
.due-cell{font-size:.76rem;font-weight:600;white-space:nowrap}
.due-over{color:var(--red)}
.due-soon{color:var(--orange)}
.due-ok{color:var(--green)}
.due-none{color:var(--gray-400)}
.inline-status{border:1px solid var(--gray-200);border-radius:6px;padding:.25rem .4rem;
  font-size:.75rem;cursor:pointer;background:#fff;font-family:inherit}

/* ── Settings page ── */
.settings-section{margin-bottom:1.5rem}
.settings-section h3{font-size:.85rem;font-weight:600;margin-bottom:.75rem;
  color:var(--blue);border-bottom:1px solid var(--gray-200);padding-bottom:.4rem}
.team-row{display:flex;gap:.5rem;align-items:center;margin-bottom:.4rem;
  background:var(--gray-50);padding:.5rem .75rem;border-radius:7px;border:1px solid var(--gray-200)}
.team-row span{flex:1;font-size:.82rem}

/* ════════════════════════════════════════════════════════════
   v6 POLISH — typography, badges, tables, empty states, motion
════════════════════════════════════════════════════════════ */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+4:wght@600;700&family=JetBrains+Mono:wght@500&display=swap');

:root{
  --ink:#0b1220;
  --ink-soft:#1e293b;
  --muted:#64748b;
  --border:#e5e7eb;
  --border-strong:#cbd5e1;
  --bg:#f6f8fb;
  --blue-deep:#0a2a6b;
  --accent:#0058a7;
  --ring:0 0 0 3px rgba(0,88,167,.14);
  --lift:0 10px 30px -12px rgba(15,23,42,.18), 0 2px 6px -2px rgba(15,23,42,.08);
  --r-sm:8px; --r-md:12px; --r-lg:16px;
}
body{background:var(--bg);color:var(--ink)}
.pg{animation:pgFade .28s cubic-bezier(.2,.7,.2,1)}
@keyframes pgFade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* Serif display for titles and drawer headlines */
.dr-title,.md-title,.pg-title,h1.logo-title,
.pg h2,.pg h3{font-family:'Source Serif 4',Georgia,serif;letter-spacing:-.01em}

/* Cards: sharper borders, softer shadow, lift on hover for interactive lists */
.card{border:1px solid var(--border);box-shadow:0 1px 2px rgba(15,23,42,.04),0 1px 0 rgba(255,255,255,.6) inset;border-radius:var(--r-md)}
.card .ch{background:linear-gradient(180deg,#fff,#fafbfd);border-bottom:1px solid var(--border)}
.card .ch .cicon{background:#eef2ff;color:var(--blue-deep)}

/* Tables: sticky headers, zebra-light rows, tighter density option */
.tbl-wrap{border-radius:var(--r-md);overflow:auto;max-height:calc(100vh - 240px)}
thead th{position:sticky;top:0;z-index:2;background:#f8fafc;
  font-variant-numeric:tabular-nums;letter-spacing:.02em;
  box-shadow:inset 0 -1px 0 var(--border-strong)}
tbody tr{transition:background-color .12s}
tbody tr:nth-child(even) td{background:rgba(248,250,252,.5)}
tbody tr.clickable:hover td{background:#eef4ff !important}
tr.wf-group-hdr td{background:linear-gradient(90deg,#eef2ff 0%,#f8fafc 60%)!important;
  box-shadow:inset 3px 0 0 var(--accent)}

/* Badges — slightly stronger borders, pill spacing */
.badge{border:1px solid transparent;letter-spacing:.02em;font-variant-numeric:tabular-nums;
  padding:.22rem .6rem;box-shadow:0 1px 0 rgba(15,23,42,.03)}
.b-New{background:#e0f2fe;color:#0c4a6e;border-color:#bae6fd}
.b-Assigned{background:#fef3c7;color:#854d0e;border-color:#fde68a}
.b-InProgress{background:#ede9fe;color:#4c1d95;border-color:#ddd6fe}
.b-DraftComplete{background:#fffbeb;color:#b45309;border-color:#fde68a}
.b-InReview{background:#dbeafe;color:#1d4ed8;border-color:#93c5fd}
.b-NeedsRevision{background:#fef2f2;color:#dc2626;border-color:#fca5a5}
.b-Finalized{background:#dcfce7;color:#064e3b;border-color:#86efac}
.b-Archived{background:#f1f5f9;color:#475569;border-color:#e2e8f0}
.b-cf{background:#fff7ed;color:#9a3412;border-color:#fed7aa}
.b-overdue{background:#fee2e2;color:#7f1d1d;border-color:#fecaca}
.b-soon{background:#fffbeb;color:#92400e;border-color:#fde68a}
.b-ok{background:#ecfdf5;color:#065f46;border-color:#a7f3d0}

/* Buttons — refined primary, outline hover, small icon buttons */
.btn{letter-spacing:.01em;border-radius:8px}
.btn-p{background:linear-gradient(180deg,#0068c7,#003e8f);
  box-shadow:0 1px 0 rgba(255,255,255,.25) inset,0 4px 14px -6px rgba(0,88,167,.55)}
.btn-p:hover:not(:disabled){filter:brightness(1.05);transform:translateY(-1px)}
.btn-o{background:#fff}
.btn-o:hover{background:#f8fafc;border-color:var(--accent);color:var(--accent);box-shadow:0 1px 0 rgba(15,23,42,.04)}
.btn:focus-visible{outline:none;box-shadow:var(--ring)}

/* Inputs — tighter focus ring, subtle inset */
input[type=text],input[type=email],input[type=password],select,textarea{
  background:#fff;border-color:var(--border);box-shadow:inset 0 1px 0 rgba(15,23,42,.03)}
input:focus,select:focus,textarea:focus{box-shadow:var(--ring);border-color:var(--accent)}

/* Empty states */
.empty{padding:2.4rem 1.25rem;text-align:center;color:var(--muted)}
.empty .icon{font-size:2rem;margin-bottom:.5rem;opacity:.6}
.empty .title{font-family:'Source Serif 4',Georgia,serif;font-size:1.05rem;color:var(--ink-soft);margin-bottom:.25rem}
.empty .hint{font-size:.82rem;line-height:1.55;max-width:44ch;margin:0 auto}
.empty .cta{margin-top:.9rem}

/* Skeleton shimmer for loading rows */
.sk{display:inline-block;height:.9em;border-radius:4px;
  background:linear-gradient(90deg,#e5e7eb 0%,#f1f5f9 50%,#e5e7eb 100%);
  background-size:200% 100%;animation:sh 1.25s linear infinite}
@keyframes sh{to{background-position:-200% 0}}

/* Drawer refinements */
#drawer{border-top-left-radius:18px;border-bottom-left-radius:18px}
.dr-header{background:
  radial-gradient(1200px 300px at 100% -50%,rgba(255,255,255,.18),transparent 60%),
  linear-gradient(135deg,#0a2a6b 0%,#0058a7 100%);
  padding:1.15rem 1.35rem 1rem}
.dr-title{font-size:1.1rem;letter-spacing:-.015em}
.dr-meta a{transition:opacity .15s}
.dr-meta a:hover{opacity:.8}
.dtab{position:relative;transition:color .15s}
.dtab:hover{color:var(--ink-soft)}
.dtab.on::after{content:'';position:absolute;left:.6rem;right:.6rem;bottom:-2px;
  height:2px;background:var(--accent);border-radius:2px}

/* Meeting Detail: hero banner */
.md-hero{background:linear-gradient(135deg,#f8fafc,#eef2ff);border:1px solid var(--border);
  border-radius:var(--r-md);padding:1.1rem 1.3rem;margin-bottom:1rem;
  display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.md-hero .md-title{font-size:1.3rem;font-weight:700;color:var(--blue-deep);margin:0}
.md-hero .sub{font-size:.8rem;color:var(--muted)}
.md-hero .spacer{flex:1}

/* File-number chip */
.file-link{background:#eef2ff;color:var(--blue-deep);padding:.12rem .5rem;border-radius:5px;
  border:1px solid #dbeafe;font-family:'JetBrains Mono',Menlo,monospace;font-size:.74rem}
.file-link:hover{background:#dbeafe;text-decoration:none}

/* Chip row inside drawer header */
.dr-chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.5rem}
.dr-chip{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.22);
  padding:.18rem .55rem;border-radius:99px;font-size:.7rem;color:#fff;letter-spacing:.02em}

/* Scrollbars */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:8px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#94a3b8}

/* Group header in workflow */
.wf-group-hdr:hover td{filter:brightness(.98)}
.wf-group-hdr .grp-caret{display:inline-block;transition:transform .2s;margin-right:.35rem;color:#64748b}

/* Subtle section dividers */
.ds-title{color:#475569;letter-spacing:.8px}
.ds-title::after{content:'';flex:1;height:1px;background:var(--border);margin-left:.6rem}
.ds-title{display:flex;align-items:center}

/* Tooltip-ish hint */
[data-hint]{position:relative}

/* Success / error toasts */
.toast{position:fixed;bottom:1.25rem;right:1.25rem;background:#0b1220;color:#fff;
  padding:.75rem 1rem;border-radius:10px;box-shadow:var(--lift);font-size:.82rem;
  z-index:9999;opacity:0;transform:translateY(10px);transition:all .2s;pointer-events:none}
.toast.show{opacity:1;transform:none}
.toast.ok{background:#065f46}
.toast.err{background:#7f1d1d}

/* ═══ V6 visual refresh — softer shadows, rounder corners, app-like feel ═══ */
:root{
  --r: 14px;
  --ink: #0f172a;
  --ink-soft: #1e293b;
  --muted: #64748b;
  --border: #e2e8f0;
  --ring: rgba(0,48,135,.18);
  --lift: 0 10px 30px -12px rgba(2,6,23,.18), 0 2px 6px -1px rgba(2,6,23,.06);
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 12px -4px rgba(0,0,0,.07);
  --shadow-lg: 0 20px 40px -16px rgba(2,6,23,.22), 0 4px 12px -4px rgba(2,6,23,.08);
}
body{background:linear-gradient(180deg,#f8fafc 0%,#eef2f7 100%) fixed}
.card{border-radius:var(--r);box-shadow:var(--shadow);border:1px solid #eef1f5}
.card:hover{box-shadow:var(--shadow-lg);transition:box-shadow .2s}
.ch{padding:1rem 1.2rem;font-size:.9rem;background:#fcfcfd;border-bottom-color:#eef1f5}
.cicon{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#e8f0fb,#dbe6f7);
       box-shadow:inset 0 0 0 1px rgba(0,48,135,.08);font-size:.95rem}
.btn{border-radius:10px;font-weight:600;letter-spacing:.01em;transition:all .15s}
.btn-p{background:linear-gradient(135deg,#003087 0%,#0058a7 100%);
       box-shadow:0 1px 2px rgba(0,48,135,.3),0 4px 10px -3px rgba(0,48,135,.35)}
.btn-p:hover:not(:disabled){transform:translateY(-1px);
       box-shadow:0 2px 4px rgba(0,48,135,.35),0 8px 18px -4px rgba(0,48,135,.45)}
.btn-o{background:#fff;border:1px solid var(--border);color:var(--ink-soft)}
.btn-o:hover:not(:disabled){border-color:#cbd5e1;background:#f8fafc;transform:translateY(-1px);box-shadow:0 2px 6px rgba(2,6,23,.06)}
nav .nb{border-radius:9px;font-size:.85rem;font-weight:500;padding:.5rem 1rem}
nav .nb.on{background:rgba(255,255,255,.22);box-shadow:inset 0 0 0 1px rgba(255,255,255,.35)}
header{box-shadow:0 4px 20px -4px rgba(0,48,135,.4),0 1px 0 rgba(255,255,255,.08) inset}
table{font-size:.8rem}
table th{background:#f7f8fb;color:#475569;font-weight:600;font-size:.7rem;
         text-transform:uppercase;letter-spacing:.04em;padding:.7rem .8rem;
         border-bottom:1px solid var(--border);position:sticky;top:0;z-index:2}
table td{padding:.65rem .8rem;border-bottom:1px solid #eef1f5;vertical-align:middle}
table tbody tr:hover{background:#f8fafc}
table tbody tr.clickable{cursor:pointer}
table tbody tr.clickable:hover{background:linear-gradient(90deg,#eef4ff 0%,#f8fafc 100%)}
.file-link{font-family:'JetBrains Mono','SF Mono',Consolas,monospace;font-size:.76rem;
           font-weight:600;color:var(--blue);background:#eef4ff;padding:.18rem .48rem;
           border-radius:6px;border:1px solid #dbe6f7}
.badge{display:inline-flex;align-items:center;gap:.2rem;padding:.22rem .55rem;
       border-radius:999px;font-size:.68rem;font-weight:600;letter-spacing:.02em;
       border:1px solid transparent}
input,select,textarea{border-radius:9px;border:1px solid var(--border);
       padding:.55rem .7rem;font-size:.85rem;transition:all .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:#a7c0e6;
       box-shadow:0 0 0 3px var(--ring)}
label{font-size:.72rem;font-weight:600;color:var(--muted);text-transform:uppercase;
      letter-spacing:.04em}
.pg{animation:pgFade .18s ease-out}
@keyframes pgFade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
/* Drawer refinement */
#drawer{border-top-left-radius:16px;border-bottom-left-radius:16px;
        box-shadow:-20px 0 60px -20px rgba(2,6,23,.35)}
.dtab{padding:.55rem 1rem;border:none;background:transparent;cursor:pointer;
      font-size:.82rem;font-weight:500;color:var(--muted);border-bottom:2px solid transparent}
.dtab.on{color:var(--blue);border-bottom-color:var(--blue)}
/* Subtler scrollbars */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#d3dae4;border-radius:10px;border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:#b9c2cf;background-clip:content-box;border:2px solid transparent}

/* Quick-action tiles on dashboard welcome strip */
.qa-tile{display:flex;align-items:center;gap:.65rem;padding:.7rem .85rem;
  background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);
  border-radius:10px;color:#fff;cursor:pointer;text-align:left;
  transition:all .15s ease;font-family:inherit}
.qa-tile:hover{background:rgba(255,255,255,.22);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(0,0,0,.15)}
.qa-icon{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-size:1.1rem;flex-shrink:0}
.qa-title{font-weight:600;font-size:.86rem;line-height:1.2}
.qa-sub{font-size:.72rem;opacity:.8;line-height:1.3;margin-top:.1rem}

/* Help modal tabs */
.htab{background:transparent;border:none;padding:.6rem 1rem;font-size:.82rem;font-weight:500;
  color:var(--gray-600);cursor:pointer;border-bottom:2px solid transparent;
  border-radius:0;margin:0;transition:all .15s ease}
.htab:hover{color:var(--gray-800);background:var(--gray-50)}
.htab.on{color:var(--blue);border-bottom-color:var(--blue);font-weight:600}
.hpane{padding:.25rem 0;font-size:.85rem;line-height:1.55;color:var(--gray-700)}
.hpane h3{font-size:.92rem;color:#1e293b;margin:1rem 0 .5rem;font-weight:600}
.hpane h3:first-child{margin-top:.2rem}
.hpane p{margin:.45rem 0}
.howto-item{background:var(--gray-50);border:1px solid var(--gray-200);border-radius:8px;
  padding:.7rem .9rem;margin:.55rem 0}
.howto-q{font-weight:600;color:#1e293b;font-size:.85rem;margin-bottom:.3rem}
.howto-a{font-size:.8rem;color:var(--gray-700);line-height:1.55}
.coldef,.statdef,.tabdef{display:grid;grid-template-columns:160px 1fr;gap:.45rem 1rem;
  padding:.55rem .25rem;border-bottom:1px solid var(--gray-100)}
.coldef:last-child,.statdef:last-child,.tabdef:last-child{border-bottom:none}
.coldef b,.statdef b,.tabdef b{color:#1e293b;font-size:.82rem}
.coldef span,.statdef span,.tabdef span{font-size:.8rem;color:var(--gray-700);line-height:1.5}

/* Friendlier empty states */
.empty-state{padding:2rem 1rem;text-align:center;color:var(--gray-500)}
.empty-state .ei{font-size:2rem;margin-bottom:.5rem;opacity:.6}
.empty-state .et{font-weight:600;color:var(--gray-700);margin-bottom:.25rem}
.empty-state .ed{font-size:.82rem;max-width:420px;margin:0 auto;line-height:1.5}

/* Subtle hint tooltip for column headers */
th[title]{cursor:help;border-bottom:1px dotted transparent}
th[title]:hover{border-bottom-color:var(--gray-400)}

/* Appearance timeline and cards */
.app-timeline{display:flex;flex-direction:column;gap:0;position:relative;margin-top:.75rem}
.app-timeline::before{content:'';position:absolute;left:14px;top:0;bottom:0;width:2px;background:var(--gray-200)}
.app-card{position:relative;margin-left:32px;border:1px solid var(--gray-200);border-radius:10px;padding:.75rem 1rem;background:#fff;margin-bottom:.75rem;transition:box-shadow .15s}
.app-card:hover{box-shadow:0 2px 8px rgba(0,0,0,.06);border-color:var(--blue2);transition:border-color .15s}
.app-card.current{border-color:var(--blue2);background:#f8fbff}
.app-card::before{content:'';position:absolute;left:-24px;top:1rem;width:12px;height:12px;border-radius:50%;background:var(--gray-300);border:2px solid #fff}
.app-card.current::before{background:var(--blue2)}
.app-card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem}
.app-card-title{font-weight:600;font-size:.82rem;color:var(--ink)}
.app-card-date{font-size:.72rem;color:var(--gray-500)}
.app-card-badges{display:flex;gap:.35rem;margin-bottom:.4rem}
.app-card-badges span{font-size:.62rem;padding:.1rem .4rem;border-radius:10px;font-weight:600}
.badge-current{background:var(--blue2);color:#fff}
.badge-transcript{background:#ede9fe;color:#6d28d9}
.badge-carried{background:#fef3c7;color:#92400e}
.change-box{border-radius:8px;padding:.55rem .75rem;margin-bottom:.5rem;font-size:.78rem;line-height:1.5}
.change-box.changed{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b}
.change-box.nochange{background:#f0fdf4;border:1px solid #86efac;color:#166534}
.change-box.carried{background:#fffbeb;border:1px solid #fde68a;color:#92400e}
.transcript-box{background:#f5f3ff;border:1px solid #ddd6fe;border-radius:8px;padding:.55rem .75rem;margin-bottom:.5rem}
.transcript-box-title{font-weight:600;font-size:.72rem;color:#6d28d9;text-transform:uppercase;letter-spacing:.3px;margin-bottom:.35rem}
.app-notes-section{margin-top:.5rem}
.app-notes-section textarea{width:100%;min-height:60px;border:1px solid var(--gray-200);border-radius:6px;padding:.4rem .55rem;font-size:.78rem;font-family:inherit;resize:vertical;line-height:1.5}
.app-notes-section label{font-size:.68rem;font-weight:600;color:var(--gray-500);text-transform:uppercase;letter-spacing:.3px;display:block;margin-bottom:.2rem;margin-top:.4rem}
</style>
</head>
<body>

<header>
  <div class="logo" onclick="showPg('dashboard')">
    <div class="logo-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="#003087" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2"/>
        <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>
        <line x1="3" y1="10" x2="21" y2="10"/>
        <line x1="8" y1="14" x2="10" y2="14"/><line x1="12" y1="14" x2="16" y2="14"/>
      </svg>
    </div>
    <div><h1>AgendaIQ</h1><small>Office of the Commission Auditor · Miami-Dade</small></div>
  </div>
  <nav>
    <button class="nb on" id="nb-dashboard" onclick="showPg('dashboard')" title="Overview of all work">Home</button>
    <button class="nb" id="nb-process" onclick="showPg('process')" title="Analyze a new agenda">Analyze</button>
    <button class="nb" id="nb-meetings" onclick="showPg('meetings')" title="Review past meeting packages">Meetings</button>
    <button class="nb" id="nb-search" onclick="showPg('search')" title="Find any past item">Search</button>
    <button class="nb" id="nb-workflow" onclick="showPg('workflow')" title="Track assigned work">
      Workflow<span class="alert-badge" id="overdue-badge" style="display:none">0</span>
    </button>
    <button class="nb" id="nb-myitems" onclick="showPg('myitems')" title="Items assigned to you">My Items</button>
    <button class="nb" id="nb-settings" onclick="showPg('settings')" title="Team & configuration">Settings</button>
    <button class="nb" id="nb-help" onclick="showPg('help')" title="Help & Reference guide">Help</button>
  </nav>
  <div style="margin-left:auto;display:flex;align-items:center;gap:.55rem;">
    <button class="nb" onclick="showHelp()" title="Quick tour of AgendaIQ"
      style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);border-radius:999px;
      width:30px;height:30px;padding:0;display:flex;align-items:center;justify-content:center;
      font-weight:700;font-size:.85rem">?</button>
    <select id="current-user-sel" onchange="setCurrentUser(this.value)"
      style="margin:0;width:auto;font-size:.75rem;padding:.22rem .5rem;background:rgba(255,255,255,.15);
      border:1px solid rgba(255,255,255,.3);color:#fff;border-radius:6px">
      <option value="">Acting as…</option>
    </select>
    <span class="hbadge">v6</span>
  </div>
</header>

<!-- ═══════════════ HELP / REFERENCE MODAL ═══════════════ -->
<div id="help-modal" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);
  z-index:9999;align-items:flex-start;justify-content:center;padding:3vh 2vw;overflow:auto" onclick="hideHelp(event)">
  <div style="background:#fff;max-width:860px;width:100%;border-radius:14px;
    box-shadow:0 25px 60px rgba(0,0,0,.3);overflow:hidden" onclick="event.stopPropagation()">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:1.15rem 1.5rem;
      background:linear-gradient(135deg,#1e3a8a,#3b82f6);color:#fff">
      <div>
        <div style="font-size:1.25rem;font-weight:700">AgendaIQ Help & Reference</div>
        <div style="font-size:.78rem;opacity:.85">Everything you need — no training required</div>
      </div>
      <button class="btn btn-o btn-sm" onclick="hideHelp()" style="background:rgba(255,255,255,.2);border-color:rgba(255,255,255,.4);color:#fff">✕ Close</button>
    </div>
    <div style="display:flex;border-bottom:1px solid var(--gray-200);background:var(--gray-50)">
      <button class="htab on" onclick="switchHelpTab('tour',this)">Quick Tour</button>
      <button class="htab" onclick="switchHelpTab('howto',this)">How-To</button>
      <button class="htab" onclick="switchHelpTab('columns',this)">Column Guide</button>
      <button class="htab" onclick="switchHelpTab('statuses',this)">Status Guide</button>
      <button class="htab" onclick="switchHelpTab('tabs',this)">Item Tabs</button>
    </div>
    <div style="padding:1.25rem 1.5rem;max-height:65vh;overflow:auto">

      <!-- TOUR -->
      <div class="hpane on" id="hpane-tour">
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem;margin-bottom:1rem">
          <div style="background:linear-gradient(135deg,#eef6ff,#dbeafe);padding:.95rem;border-radius:10px">
            <div style="font-size:1.4rem">①</div>
            <div style="font-weight:700;color:var(--blue);font-size:.95rem;margin-bottom:.25rem">Analyze</div>
            <div style="font-size:.78rem;color:var(--gray-600);line-height:1.45">
              Go to <strong>Analyze</strong>, pick a meeting date, select committees, click <em>Analyze agenda</em>. The system scrapes every item, summarizes it, and flags watchpoints.
            </div>
          </div>
          <div style="background:linear-gradient(135deg,#fef6e4,#fde68a);padding:.95rem;border-radius:10px">
            <div style="font-size:1.4rem">②</div>
            <div style="font-weight:700;color:#92400e;font-size:.95rem;margin-bottom:.25rem">Research & Note</div>
            <div style="font-size:.78rem;color:var(--gray-600);line-height:1.45">
              Open <strong>Meetings</strong> → click a meeting → click any item row. Add notes in the Notes tab. Prior committee notes carry forward automatically.
            </div>
          </div>
          <div style="background:linear-gradient(135deg,#ecfdf5,#bbf7d0);padding:.95rem;border-radius:10px">
            <div style="font-size:1.4rem">③</div>
            <div style="font-weight:700;color:#065f46;font-size:.95rem;margin-bottom:.25rem">Deliver</div>
            <div style="font-size:.78rem;color:var(--gray-600);line-height:1.45">
              When done, click <strong>Export Files</strong> in the item drawer or on the meeting page. Part 1 — Agenda Debrief — downloads as Word.
            </div>
          </div>
        </div>
        <div style="background:#eef6ff;border:1px solid #bfdbfe;border-radius:10px;padding:.9rem 1.1rem;font-size:.82rem;color:#1e3a8a;line-height:1.55">
          <strong>Key concept — one item can appear at many meetings.</strong> A bill reviewed at committee on March 10 and adopted at BCC on March 17 is the <em>same</em> item. AgendaIQ follows it across stages: notes made at committee show up when the same item hits the BCC agenda, and whoever was assigned at committee stays assigned at BCC.
        </div>
      </div>

      <!-- HOW-TO -->
      <div class="hpane" id="hpane-howto" style="display:none">
        <div class="howto-item">
          <div class="howto-q">How do I add research notes to an item?</div>
          <div class="howto-a">Open <strong>Meetings</strong> → click a meeting → click the item row. In the drawer, go to the <strong>Notes</strong> tab. Type in either "Analyst Working Notes" or "Reviewer Notes" and click <em>Append Note</em>. Each note is timestamped automatically and appears in the Part 1 deliverable.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How do I see notes from a prior meeting on the same item?</div>
          <div class="howto-a">Open the item drawer. The <strong>Part 1 · Debrief</strong> tab has a <em>Meeting Notes</em> section at the bottom listing every note from every appearance — Committee, BCC, and everything in between — each stamped with stage, date, and author. For more detail, the <strong>Appearances</strong> tab shows the full case history. Items with prior notes have a <strong>✓</strong> in the <em>Prior Notes</em> column of the grid.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How do I assign items to a researcher?</div>
          <div class="howto-a">In the meeting grid, use the <strong>Assigned</strong> cell on any row. To assign many at once: select the checkboxes, then click <em>Bulk Assign</em> at the top. On the <strong>Workflow</strong> page, the same bulk controls are available across all meetings.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">Will a researcher's assignment stick when the item moves to the next meeting?</div>
          <div class="howto-a">Yes. When a matter that was assigned at committee reappears at BCC (or any later stage), the assigned researcher, reviewer, and priority all carry forward automatically. You can override by changing the Assigned/Reviewer selectors on the new appearance.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How do I export the final deliverable?</div>
          <div class="howto-a">In the item drawer, click <strong>↓ Export Files</strong>. This produces the Part 1 Agenda Debrief as a Word document, including Agenda Debrief, Watchpoints, Legislative History, and timestamped Meeting Notes. Deep Research Notes are <em>not</em> included — those stay internal.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">What's the difference between Notes and Deep Research?</div>
          <div class="howto-a"><strong>Notes</strong> = what goes into the delivered Part 1 brief. <strong>Deep Research</strong> = internal reference material (background, citations, earlier memos). Deep Research is never exported.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">Why is my item flagged "MANUAL REVIEW NEEDED"?</div>
          <div class="howto-a">The source PDF was a scanned image, not a text PDF. The AI couldn't read it, so the system flagged it for you to review manually. Open the PDF link in the item to draft the debrief by hand.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">The committee date/number columns are empty — what do I do?</div>
          <div class="howto-a">Click the yellow <em>⚠ Missing data</em> banner on the Meetings page and run the Backfill. It pulls Legistar legislative history for every item and back-fills prior committee appearances.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">What do the progress phases (Scanning, Analyzing, Exporting) mean?</div>
          <div class="howto-a">When you run an analysis, the progress bar shows three phases: <strong>Scanning</strong> (checking Legistar for matching agendas, 0-15%), <strong>Analyzing</strong> (AI reads each item's PDF, generates summary and watch points, 15-90%), and <strong>Exporting</strong> (creating Excel and Word deliverables, 90-100%). The colored badge above the log updates in real time. If you reload the browser during analysis, it automatically reconnects and shows progress from where it left off.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How do I use the AI Chat?</div>
          <div class="howto-a">Open any item drawer and click the <strong>AI Chat</strong> tab. Type a question and hit Send. The AI knows the item's context (title, file number, existing analysis, watch points). Toggle <em>Enable web search</em> to let the AI search the web for current information. Your chat is private — other users cannot see it. To save an AI response, click <strong>+ Add to Notes</strong> (appends to your Analyst Working Notes) or <strong>+ Add to Part 1</strong> (appends to the item's Agenda Debrief summary).</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How does notes carry-forward work?</div>
          <div class="howto-a">When a matter moves from committee to BCC, the analyst working notes from the committee appearance are automatically carried forward into the BCC appearance's notes, prefixed with "[Carried from prior committee appearance]". Researcher/reviewer assignments and priority also carry forward. This ensures nothing is lost between stages.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">What is change detection (committee → BCC)?</div>
          <div class="howto-a">When a matter reappears at BCC after committee, the AI automatically compares the committee PDF with the BCC PDF and identifies substantive changes — amended language, dollar amounts, new conditions, scope changes, or added/removed sections. These are added to the notes as "[Changes from committee to BCC version]" with specific change bullets. If the item is unchanged, it notes "No substantive changes detected."</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">How does the Lifecycle tab work?</div>
          <div class="howto-a">The <strong>Lifecycle</strong> tab in the item drawer shows the full Legistar legislative history timeline — every action taken on the item from introduction through final disposition. This is parsed directly from the county's Legistar system and includes: acting body (committee or BCC), date, agenda item number, and action taken (e.g. "Forwarded to BCC with a favorable recommendation"). The timeline is refreshed when you run the Backfill.</div>
        </div>
        <div class="howto-item">
          <div class="howto-q">What does the Backfill do exactly?</div>
          <div class="howto-a">The Backfill (triggered from the yellow banner on the Meetings page) visits the Legistar matter page for every item in the database. For each item it: (1) navigates to the legislative item page via the county's two-step link process, (2) extracts all fields and legislative history, (3) parses history into structured timeline events, (4) creates stub committee appearances for items that went through committee before BCC, (5) updates the Legistar link and PDF URL on every appearance, and (6) downloads any missing PDFs. This populates the committee date/number columns and the Lifecycle tab.</div>
        </div>
      </div>

      <!-- COLUMN GUIDE -->
      <div class="hpane" id="hpane-columns" style="display:none">
        <div class="coldef"><b>File #</b> — Miami-Dade Legistar file number. Unique per bill/matter across its whole life.</div>
        <div class="coldef"><b>Cmte Date / Cmte #</b> — When and as what agenda item this matter was heard at committee. Populated from either a stored appearance or parsed Legistar history.</div>
        <div class="coldef"><b>BCC Date / BCC #</b> — Same, but for the Board of County Commissioners hearing.</div>
        <div class="coldef"><b>Title</b> — Short title. Hover for full.</div>
        <div class="coldef"><b>Type</b> — Resolution, Ordinance, Discussion, Report, etc.</div>
        <div class="coldef"><b>Leg Status</b> — Current legislative status snapshot (e.g., "In Committee," "Adopted"). Updates when backfill runs after a new meeting.</div>
        <div class="coldef"><b>Sponsor / Requester</b> — Commissioner or department that originated the item.</div>
        <div class="coldef"><b>Control</b> — Control body, usually a committee assignment.</div>
        <div class="coldef"><b>Journey</b> — Chronological path showing every stage (committee, BCC) this item has passed through. Green chips = committee, blue chips = BCC. The current stage is highlighted with a bold chip. Dashed borders indicate events from Legistar history (before AgendaIQ tracking).</div>
        <div class="coldef"><b>What's Next</b> — Forward-looking indicator showing what needs to happen next, derived from the Legistar legislative status. Green = done/adopted, blue = heading to BCC, yellow = pending.</div>
        <div class="coldef"><b>Prior Notes</b> — <strong>✓</strong> means this matter has analyst or reviewer notes on at least one prior appearance. Click the row to see them.</div>
        <div class="coldef"><b>History</b> — Quick badges showing if notes carried forward (↩ CF), AI ran, etc.</div>
        <div class="coldef"><b>Workflow</b> — Current status of the research task (see Status Guide).</div>
        <div class="coldef"><b>Assigned / Reviewer</b> — Researcher owning the item and the reviewer. Sticky across stages.</div>
        <div class="coldef"><b>Due</b> — Target date for this appearance's brief.</div>
      </div>

      <!-- STATUS GUIDE -->
      <div class="hpane" id="hpane-statuses" style="display:none">
        <div style="font-weight:700;color:var(--gray-800);margin-bottom:.55rem">Workflow Statuses</div>
        <div class="statdef"><span class="badge b-New">New</span> Just scraped. No researcher has touched it yet.</div>
        <div class="statdef"><span class="badge b-Assigned">Assigned</span> A researcher has been assigned but work hasn't started.</div>
        <div class="statdef"><span class="badge b-InProgress">In Progress</span> Actively being worked on.</div>
        <div class="statdef"><span class="badge b-DraftComplete">Draft Complete</span> Researcher finished a first draft.</div>
        <div class="statdef"><span class="badge b-InReview">In Review</span> Reviewer is checking the draft.</div>
        <div class="statdef"><span class="badge b-NeedsRevision">Needs Revision</span> Reviewer requested changes — sent back to analyst.</div>
        <div class="statdef"><span class="badge b-Finalized">Finalized</span> Ready for delivery / export.</div>
        <div class="statdef"><span class="badge b-Archived">Archived</span> Historical stub or closed-out item. Stays in database for lookup.</div>

        <div style="font-weight:700;color:var(--gray-800);margin:1rem 0 .55rem">Legislative Statuses (from Legistar)</div>
        <div class="statdef"><b>Introduced / Received</b> — Just filed. Not yet heard anywhere.</div>
        <div class="statdef"><b>Assigned to Committee</b> — Routed to a committee for review.</div>
        <div class="statdef"><b>Forwarded to BCC with favorable recommendation</b> — Committee voted to advance with endorsement.</div>
        <div class="statdef"><b>Forwarded without recommendation</b> — Committee advanced but didn't endorse.</div>
        <div class="statdef"><b>Deferred / Carried over</b> — Postponed to a later meeting. Will reappear.</div>
        <div class="statdef"><b>Adopted / Approved</b> — Final passage at BCC.</div>
        <div class="statdef"><b>Withdrawn</b> — Sponsor pulled the item.</div>
        <div class="statdef"><b>No action taken</b> — Died at that meeting without vote.</div>

        <div style="font-weight:700;color:var(--gray-800);margin:1rem 0 .55rem">Prior-Notes Indicators</div>
        <div class="statdef">✓ in <b>Prior Notes</b> column — this item has saved researcher notes from an earlier appearance.</div>
        <div class="statdef">↩ <b>CF</b> badge — notes were automatically carried forward from a prior appearance into this one.</div>
      </div>

      <!-- ITEM TABS -->
      <div class="hpane" id="hpane-tabs" style="display:none">
        <div class="tabdef"><b>Part 1 · Debrief</b> — The deliverable. Contains Agenda Debrief, Watch Points, Legislative Status (live), Legislative History, and all Meeting Notes stamped with author and date. <strong>This is what gets exported.</strong></div>
        <div class="tabdef"><b>Notes</b> — Where you add new research: Analyst Working Notes and Reviewer Notes. Each Append Note action timestamps automatically. Feeds Part 1.</div>
        <div class="tabdef"><b>Deep Research</b> — Reference-only background material. Never exported. Use for citations, prior memos, context a researcher may want to come back to.</div>
        <div class="tabdef"><b>History</b> — Audit trail of who changed what on this appearance (status changes, assignment changes, notes added, AI runs, exports).</div>
        <div class="tabdef"><b>Appearances</b> — Every time this matter has appeared on any agenda (Committee, BCC, supplements). Click a row to jump to that appearance. You can copy prior notes into the current one from here.</div>
        <div class="tabdef"><b>Lifecycle</b> — Chronological timeline pulled from Legistar showing every action taken on the matter — introduction through adoption.</div>
      </div>

    </div>
  </div>
</div>

<!-- ═══════════════════════════ DASHBOARD ═══════════════════════════ -->
<div class="pg on" id="pg-dashboard">
  <div id="alert-bars"></div>

  <!-- Welcome / quick-action strip — dismissable -->
  <div id="welcome-strip" style="background:linear-gradient(135deg,#1e3a8a 0%,#3b82f6 60%,#60a5fa 100%);
    color:#fff;border-radius:14px;padding:1.15rem 1.35rem;margin-bottom:1.1rem;
    box-shadow:0 8px 24px rgba(30,58,138,.18);position:relative">
    <button onclick="dismissWelcome()" title="Hide this"
      style="position:absolute;top:.55rem;right:.7rem;background:rgba(255,255,255,.15);border:none;
      color:#fff;width:22px;height:22px;border-radius:50%;cursor:pointer;font-size:.7rem">✕</button>
    <div style="display:flex;align-items:center;gap:.65rem;margin-bottom:.45rem">
      <div style="font-size:1.5rem">👋</div>
      <div>
        <div style="font-size:1.05rem;font-weight:700;letter-spacing:-.01em">Welcome to AgendaIQ</div>
        <div style="font-size:.8rem;opacity:.85">Your Miami-Dade legislative agenda intelligence tool.
          <a href="#" onclick="showHelp();return false" style="color:#fff;text-decoration:underline">Take the 60-second tour →</a>
        </div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.6rem;margin-top:.75rem">
      <button class="qa-tile" onclick="showPg('process')">
        <div class="qa-icon" style="background:rgba(255,255,255,.2)">▶</div>
        <div><div class="qa-title">Analyze an agenda</div>
          <div class="qa-sub">Pick a date, get item-by-item briefs</div></div>
      </button>
      <button class="qa-tile" onclick="showPg('myitems')">
        <div class="qa-icon" style="background:rgba(255,255,255,.2)">📝</div>
        <div><div class="qa-title">Work on my items</div>
          <div class="qa-sub">Open assignments, add notes</div></div>
      </button>
      <button class="qa-tile" onclick="showPg('meetings')">
        <div class="qa-icon" style="background:rgba(255,255,255,.2)">📅</div>
        <div><div class="qa-title">Browse meetings</div>
          <div class="qa-sub">Review past agenda packages</div></div>
      </button>
      <button class="qa-tile" onclick="showPg('search')">
        <div class="qa-icon" style="background:rgba(255,255,255,.2)">🔍</div>
        <div><div class="qa-title">Search any item</div>
          <div class="qa-sub">By file #, keyword, or sponsor</div></div>
      </button>
    </div>
  </div>

  <div class="g4" style="margin-bottom:1.1rem" id="dash-stats">
    <div class="stat"><div class="n" id="s-matters">—</div><div class="l">Total Matters</div></div>
    <div class="stat"><div class="n" id="s-apps">—</div><div class="l">Appearances</div></div>
    <div class="stat"><div class="n" id="s-mtgs">—</div><div class="l">Meetings</div></div>
    <div class="stat"><div class="n" id="s-open">—</div><div class="l">Open Items</div></div>
  </div>
  <div class="g2">
    <div>
      <div class="card">
        <div class="ch"><div class="ch-left"><div class="cicon">📊</div>By Status</div></div>
        <div class="cb" id="dash-status"></div>
      </div>
    </div>
    <div>
      <div class="card">
        <div class="ch">
          <div class="ch-left"><div class="cicon">🕒</div>Recent Activity</div>
          <button class="btn btn-o btn-sm" onclick="showPg('workflow')">View All →</button>
        </div>
        <div class="tbl-wrap">
          <table><thead><tr><th>File #</th><th>Title</th><th>Date</th><th>Body</th><th>Status</th><th>Assigned</th></tr></thead>
          <tbody id="dash-recent"></tbody></table>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ PROCESS ═══════════════════════════ -->
<div class="pg" id="pg-process">
  <div class="g2">
    <div>
      <div class="card">
        <div class="ch"><div class="ch-left"><div class="cicon">⚙️</div>Analyze a new agenda</div></div>
        <div class="cb">
          <div style="background:#eef6ff;border:1px solid #bfdbfe;border-radius:8px;
            padding:.55rem .8rem;margin-bottom:.85rem;font-size:.76rem;color:#1e3a8a;line-height:1.5">
            Pick a meeting date and which committees to include. The tool will pull each agenda item, summarize it, flag watchpoints, and save everything to the Meetings list.
          </div>
          <label>Date Mode</label>
          <div class="toggle">
            <button id="tm-single" class="on" onclick="setMode('single')">Single Date</button>
            <button id="tm-range" onclick="setMode('range')">From Date Onward</button>
          </div>
          <div id="inp-single"><label>Meeting Date</label>
            <input type="date" id="d-single">
            <div style="font-size:.7rem;color:var(--gray-400);margin-top:.25rem">📅 Click to open the calendar — pick the exact meeting date.</div></div>
          <div id="inp-range" style="display:none"><label>From Date Onward</label>
            <input type="date" id="d-from">
            <div style="font-size:.7rem;color:var(--gray-400);margin-top:.25rem">📅 All meetings on or after this date will be analyzed.</div></div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.3rem;">
            <label style="margin:0">Committees</label>
            <button class="btn btn-o btn-xs" onclick="toggleAll()">All / None</button>
          </div>
          <div class="cmteg" id="cmteg"></div>
          <button class="btn btn-p full" id="run-btn" onclick="runAgent()"
            title="Downloads the agenda, summarizes each item, flags watchpoints, and saves to Meetings">
            ▶  Analyze agenda
          </button>
        </div>
      </div>
    </div>
    <div>
      <div class="card">
        <div class="ch"><div class="ch-left"><div class="cicon">📊</div>Progress</div></div>
        <div class="cb">
          <div class="srow"><div class="sdot" id="sdot"></div>
            <span id="phase-badge" style="display:none;font-size:.68rem;font-weight:600;padding:1px 8px;border-radius:10px;background:var(--blue1);color:#fff;margin-right:6px;text-transform:uppercase;letter-spacing:.04em"></span>
            <span id="stxt" style="font-size:.85rem;font-weight:500;color:var(--gray-600)">Waiting…</span></div>
          <div class="pw"><div class="pb" id="pb"></div></div>
          <div class="logbox" id="logbox"></div>
        </div>
      </div>
      <div class="card" id="result-card" style="display:none">
        <div class="ch"><div class="ch-left"><div class="cicon">📁</div>Output Files</div>
          <button class="btn btn-o btn-sm" onclick="showPg('workflow')">View in Workflow →</button>
        </div>
        <div class="cb" id="result-body"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ SEARCH ═══════════════════════════ -->
<div class="pg" id="pg-search">
  <div class="card" style="margin-bottom:1.1rem">
    <div class="cb" style="display:flex;gap:.65rem;align-items:flex-end;flex-wrap:wrap;padding:.85rem 1.1rem">
      <div style="flex:0 0 140px"><label>File Number</label>
        <input type="text" id="sf" placeholder="251862" style="margin:0" onkeydown="if(event.key==='Enter')doSearch()"></div>
      <div style="flex:2;min-width:180px"><label>Keyword</label>
        <input type="text" id="sk" placeholder="Fisher Island, contract, zoning…" style="margin:0" onkeydown="if(event.key==='Enter')doSearch()"></div>
      <div style="flex:1;min-width:130px"><label>Sponsor</label>
        <input type="text" id="ss" placeholder="Diaz" style="margin:0" onkeydown="if(event.key==='Enter')doSearch()"></div>
      <button class="btn btn-p" onclick="doSearch()">Search</button>
    </div>
  </div>
  <div class="card">
    <div class="ch"><div class="ch-left"><div class="cicon">🔍</div>Results</div>
      <span id="srch-count" style="font-size:.75rem;color:var(--gray-400)"></span></div>
    <div class="tbl-wrap">
      <table><thead><tr><th>File #</th><th>Date</th><th>Body</th><th>Title</th><th>Type</th><th>Status</th><th>Workflow</th><th>Assigned</th><th></th></tr></thead>
      <tbody id="srch-tbody">
        <tr><td colspan="9">
          <div class="empty-state">
            <div class="ei">🔎</div>
            <div class="et">Search legislative items</div>
            <div class="ed">Find any matter by file number (e.g. <code>251862</code>), keyword (e.g. <code>Fisher Island</code>), or sponsor name. Results show everywhere the item has appeared.</div>
          </div>
        </td></tr>
      </tbody></table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ WORKFLOW ═══════════════════════════ -->
<div class="pg" id="pg-workflow">
  <div class="wf-filters">
    <div><label style="margin-bottom:.2rem">Status</label>
      <select id="wf-f-status" onchange="loadWorkflow()" style="margin:0"><option value="">All</option></select></div>
    <div><label style="margin-bottom:.2rem">Assigned To</label>
      <select id="wf-f-assigned" onchange="loadWorkflow()" style="margin:0"><option value="">Anyone</option><option value="me">My Items</option></select></div>
    <div><label style="margin-bottom:.2rem">Due Date</label>
      <select id="wf-f-due" onchange="loadWorkflow()" style="margin:0">
        <option value="">Any</option>
        <option value="overdue">Overdue</option>
        <option value="7">Due in 7 days</option>
        <option value="30">Due in 30 days</option>
      </select></div>
    <div style="margin-left:auto;display:flex;gap:.4rem;align-items:flex-end;flex-wrap:wrap">
      <button class="btn btn-sm" style="background:#059669;color:#fff;border:none" onclick="showReviewQueue()" title="Items awaiting your review">📋 My Review Queue</button>
      <button class="btn btn-o btn-sm" onclick="bulkAssign()">Bulk Assign</button>
      <button class="btn btn-o btn-sm" onclick="bulkStatus()">Bulk Status</button>
    </div>
  </div>
  <div class="card">
    <div class="ch">
      <div class="ch-left"><div class="cicon">📋</div>Appearances</div>
      <span id="wf-count" style="font-size:.75rem;color:var(--gray-400)"></span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th style="width:30px"><input type="checkbox" id="sel-all" onchange="selAll(this)"></th>
          <th>App#</th><th>File #</th><th>Title</th><th>Item #</th><th>Stage</th>
          <th>Status</th><th>Assigned To</th><th>Due Date</th><th>Priority</th><th></th>
        </tr></thead>
        <tbody id="wf-tbody">
          <tr><td colspan="11" style="padding:1.25rem;color:var(--gray-400)">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ SETTINGS ═══════════════════════════ -->
<div class="pg" id="pg-settings">
  <div class="g2">
    <div>
      <div class="card" style="margin-bottom:1rem">
        <div class="ch"><div class="ch-left"><div class="cicon">🙋</div>Who am I?</div></div>
        <div class="cb">
          <p style="font-size:.78rem;color:var(--gray-600);margin-bottom:.75rem;">
            Pick your name below so the <b>My Items</b> filter and the <b>Me (you)</b> option in assignment dropdowns know who you are. Stored locally on this browser.</p>
          <select id="settings-user-sel" onchange="setCurrentUser(this.value)" style="margin:0;max-width:320px">
            <option value="">— not set —</option>
          </select>
          <div id="settings-user-current" style="margin-top:.55rem;font-size:.78rem;color:var(--gray-600)"></div>
        </div>
      </div>
      <div class="card">
        <div class="ch"><div class="ch-left"><div class="cicon">👥</div>Team Members</div></div>
        <div class="cb">
          <p style="font-size:.78rem;color:var(--gray-600);margin-bottom:.85rem;">
            Add team members so they appear in assignment dropdowns.</p>
          <div id="team-list"></div>
          <div style="display:flex;gap:.5rem;margin-top:.5rem">
            <input type="text" id="new-member-name" placeholder="Name" style="margin:0;flex:1">
            <input type="email" id="new-member-email" placeholder="Email" style="margin:0;flex:2">
            <button class="btn btn-s btn-sm" onclick="addTeamMember()">Add</button>
          </div>
        </div>
      </div>
    </div>
    <div>
      <div class="card">
        <div class="ch"><div class="ch-left"><div class="cicon">📧</div>Email Notifications</div></div>
        <div class="cb">
          <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.85rem;">
            <label style="margin:0;font-size:.82rem;font-weight:500">Enable email alerts</label>
            <input type="checkbox" id="email-enabled" style="width:auto;margin:0;accent-color:var(--blue)">
          </div>
          <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:7px;padding:.65rem .9rem;margin-bottom:.75rem;font-size:.78rem;color:#1e40af;line-height:1.5;">
            <strong>Gmail setup:</strong> Host <code>smtp.gmail.com</code> · Port <code>587</code><br>
            You must use an <strong>App Password</strong> — not your regular Gmail password.
            Go to <a href="https://myaccount.google.com/apppasswords" target="_blank" style="color:#1d4ed8;">myaccount.google.com/apppasswords</a>,
            create one named "AgendaIQ", and paste the 16-character code below.
            (Requires 2-Step Verification to be on in your Google account.)
          </div>
          <label>SMTP Host</label><input type="text" id="smtp-host" placeholder="smtp.gmail.com">
          <label>SMTP Port</label><input type="text" id="smtp-port" placeholder="587">
          <label>Sender Gmail Address</label><input type="email" id="smtp-user" placeholder="you@gmail.com">
          <label>Gmail App Password (16 characters)</label><input type="password" id="smtp-pass" placeholder="••••••••••••••••">
          <label>Team Lead Recipients (comma-separated) — receives overdue summaries</label>
          <input type="text" id="smtp-recip" placeholder="colleague@gmail.com, manager@gmail.com">
          <label>Reminder days before due date</label>
          <input type="text" id="reminder-days" placeholder="7">
          <div style="display:flex;gap:.5rem">
            <button class="btn btn-p btn-sm" onclick="saveSettings()">Save Settings</button>
            <button class="btn btn-o btn-sm" onclick="testEmail()">Send Test Email</button>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:1rem">
        <div class="ch"><div class="ch-left"><div class="cicon">🔔</div>Teams / Slack Webhook</div></div>
        <div class="cb">
          <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.85rem;">
            <label style="margin:0;font-size:.82rem;font-weight:500">Enable webhook notifications</label>
            <input type="checkbox" id="webhook-enabled" style="width:auto;margin:0;accent-color:var(--blue)">
          </div>
          <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:7px;padding:.65rem .9rem;margin-bottom:.75rem;font-size:.78rem;color:#5b21b6;line-height:1.5;">
            <strong>How to set up:</strong><br>
            <b>Microsoft Teams:</b> Channel → Manage channel → Connectors → Incoming Webhook → Create → copy the URL.<br>
            <b>Slack:</b> Apps → Incoming WebHooks → Add to Slack → pick a channel → copy the URL.<br>
            AgendaIQ auto-detects which platform you're using from the URL.
          </div>
          <label>Webhook URL</label>
          <input type="text" id="webhook-url" placeholder="https://hooks.slack.com/services/... or https://xxx.webhook.office.com/...">
          <div style="display:flex;gap:.5rem;margin-top:.5rem">
            <button class="btn btn-p btn-sm" onclick="saveSettings()">Save Settings</button>
            <button class="btn btn-o btn-sm" onclick="testWebhook()">Send Test Message</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ SAVED MEETINGS ═══════════════════════════ -->
<div class="pg" id="pg-meetings">
  <!-- Backfill nudge banner: shown only when the probe says we're missing data. -->
  <div id="bf-nudge" style="display:none;background:linear-gradient(135deg,#fef3c7 0%,#fde68a 100%);
       border:1px solid #fbbf24;border-radius:12px;padding:.9rem 1.1rem;margin-bottom:1rem;
       display:none;align-items:center;gap:.85rem;box-shadow:0 1px 3px rgba(0,0,0,.06)">
    <div style="font-size:1.4rem">⚠️</div>
    <div style="flex:1;font-size:.82rem;color:#78350f;line-height:1.5">
      <strong id="bf-nudge-title">Some items are missing Legistar links.</strong>
      <div id="bf-nudge-body" style="margin-top:.15rem;opacity:.9"></div>
    </div>
    <button class="btn btn-p btn-sm" id="bf-nudge-btn" onclick="runBackfill(true)">
      ⟳ Fix now
    </button>
  </div>

  <!-- Live backfill progress panel: hidden until a backfill starts. -->
  <div id="bf-progress" style="display:none;background:#fff;border:1px solid var(--border);
       border-radius:12px;padding:.9rem 1.1rem;margin-bottom:1rem;box-shadow:var(--shadow)">
    <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.5rem">
      <div style="font-size:1rem">⏳</div>
      <div style="flex:1;font-weight:600;font-size:.9rem;color:var(--ink)">Backfill in progress</div>
      <div id="bf-progress-text" style="font-size:.78rem;color:var(--gray-600)"></div>
    </div>
    <div style="height:8px;background:#eef1f5;border-radius:999px;overflow:hidden;margin-bottom:.55rem">
      <div id="bf-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#003087,#0058a7);
           transition:width .4s ease"></div>
    </div>
    <div id="bf-log" style="max-height:120px;overflow-y:auto;background:#fafbfc;border-radius:8px;padding:.4rem .6rem"></div>
  </div>

  <div class="card" style="margin-bottom:1.1rem">
    <div class="ch">
      <div class="ch-left"><div class="cicon">🗂️</div>Saved Meeting Packages</div>
      <span id="mtg-count" style="font-size:.75rem;color:var(--gray-400)"></span>
      <div style="margin-left:auto;display:flex;gap:.5rem;align-items:center">
        <span id="bf-msg" style="font-size:.72rem;color:#64748b"></span>
        <button class="btn btn-o btn-sm" id="bf-btn" onclick="runBackfill(true)"
          title="Re-hit Legistar for every matter with a missing Item PDF / Legistar link and rebuild lifecycle timelines from legislative history">
          ⟳ Fill missing links + lifecycle
        </button>
        <button class="btn btn-o btn-sm" onclick="runBackfill(false)"
          title="Re-process EVERY matter in the DB (slower)">
          ⟳ Full refresh
        </button>
      </div>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Meeting</th><th>Date</th><th>Body</th>
          <th>Items</th><th>Progress</th><th>Package Status</th>
          <th>Exports</th><th></th>
        </tr></thead>
        <tbody id="mtg-tbody">
          <tr><td colspan="8" style="padding:1.25rem;color:var(--gray-400)">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ MEETING DETAIL ═══════════════════════════ -->
<div class="pg" id="pg-meeting-detail">
  <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.85rem">
    <button class="btn btn-o btn-sm" onclick="showPg('meetings')">← Back to Saved Meetings</button>
    <div id="md-title" style="font-size:1.1rem;font-weight:700;color:var(--gray-800);flex:1"></div>
    <span id="md-status-badge"></span>
  </div>

  <div class="card" style="margin-bottom:1.1rem">
    <div class="ch">
      <div class="ch-left"><div class="cicon">📦</div>Meeting Package</div>
      <div id="md-package-meta" style="font-size:.75rem;color:var(--gray-400)"></div>
    </div>
    <div class="cb" id="md-meta-body"></div>
  </div>

  <div class="card" style="margin-bottom:1.1rem">
    <div class="ch">
      <div class="ch-left"><div class="cicon">⬇</div>Exports</div>
      <div style="display:flex;gap:.4rem">
        <button class="btn btn-o btn-sm" id="md-regen-btn" onclick="regenDraft()">↻ Regenerate Draft</button>
        <button class="btn btn-s btn-sm" id="md-final-btn" onclick="genFinal()" disabled>✓ Generate Final Export</button>
        <button class="btn btn-o btn-sm" id="md-transcript-btn" onclick="backfillTranscript()"
          title="Search for this meeting's recording and add per-item discussion summaries to notes">
          🎙 Backfill Transcript
        </button>
        <button class="btn btn-o btn-sm" id="md-urls-btn" onclick="backfillMeetingUrls()"
          title="Fetch Legistar URLs and lifecycle history for all items in this meeting">
          🔗 Backfill URLs + Lifecycle
        </button>
        <button class="btn btn-o btn-sm" id="md-reanalyze-btn" onclick="reanalyzeMeetingItems()"
          title="Re-run AI analysis on all items in this meeting">
          🤖 Re-analyze All Items
        </button>
      </div>
    </div>
    <div id="md-operation-progress" style="display:none;margin:0 1rem .5rem;padding:.5rem .75rem;border-radius:.5rem;background:#f0f4ff;border:1px solid #c7d2fe;font-size:.82rem"></div>
    <div class="cb" id="md-artifacts"></div>
  </div>

  <div class="card">
    <div class="ch">
      <div class="ch-left"><div class="cicon">📋</div>Items in this Meeting</div>
      <span id="md-items-count" style="font-size:.75rem;color:var(--gray-400)"></span>
    </div>
    <div class="cb" style="padding:.7rem 1.1rem;background:var(--gray-50);border-bottom:1px solid var(--gray-200)">
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:flex-end">
        <div style="flex:1;min-width:160px">
          <label style="margin-bottom:.2rem">Search title / file #</label>
          <input type="text" id="md-f-q" placeholder="zoning, 251862…" oninput="renderItemsGrid()" style="margin:0">
        </div>
        <div><label style="margin-bottom:.2rem">Leg Status</label>
          <select id="md-f-legstatus" onchange="renderItemsGrid()" style="margin:0"><option value="">Any</option></select></div>
        <div><label style="margin-bottom:.2rem">File Type</label>
          <select id="md-f-filetype" onchange="renderItemsGrid()" style="margin:0"><option value="">Any</option></select></div>
        <div><label style="margin-bottom:.2rem">Sponsor / Requester</label>
          <select id="md-f-sponsor" onchange="renderItemsGrid()" style="margin:0"><option value="">Any</option></select></div>
        <div><label style="margin-bottom:.2rem">Workflow</label>
          <select id="md-f-wf" onchange="renderItemsGrid()" style="margin:0">
            <option value="">Any</option>
            <option>New</option><option>Assigned</option><option>In Progress</option>
            <option>Draft Complete</option><option>In Review</option>
            <option>Finalized</option><option>Archived</option>
          </select></div>
        <div><label style="margin-bottom:.2rem">Special</label>
          <select id="md-f-special" onchange="renderItemsGrid()" style="margin:0">
            <option value="">All items</option>
            <option value="cf">Carried forward</option>
            <option value="supp">Supplements only</option>
            <option value="notes">Has prior notes</option>
            <option value="unnotes">No researcher notes yet</option>
          </select></div>
        <button class="btn btn-o btn-xs" onclick="clearItemFilters()" style="margin-bottom:.75rem">Reset</button>
      </div>
    </div>
    <div class="tbl-wrap">
      <table style="min-width:1600px">
        <thead><tr>
          <th>File #</th>
          <th title="Links: ↗ opens Legistar matter page, 📄 downloads the item PDF">Links</th>
          <th>Cmte Date</th><th>Cmte #</th>
          <th>BCC Date</th><th>BCC #</th>
          <th>Title</th><th>Type</th><th>Leg Status</th>
          <th>Sponsor / Requester</th><th>Control</th>
          <th title="Chronological path showing every stage this item has passed through. Current stage is highlighted.">Journey</th>
          <th title="What needs to happen next based on Legistar legislative status and control body.">What's Next</th>
          <th title="✓ if this matter has analyst/reviewer notes from a previous appearance">Prior Notes</th>
          <th>History</th>
          <th>Workflow</th><th>Assigned</th><th>Reviewer</th><th>Due</th>
          <th>Notes</th><th></th>
        </tr></thead>
        <tbody id="md-items"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ MY ITEMS ═══════════════════════════ -->
<div class="pg" id="pg-myitems">
  <div class="card" style="margin-bottom:1rem">
    <div class="cb" style="padding:.85rem 1.1rem;display:flex;gap:.75rem;align-items:flex-end;flex-wrap:wrap">
      <div>
        <label style="margin-bottom:.2rem">Researcher</label>
        <select id="mi-researcher" onchange="loadMyItems()" style="margin:0"></select>
      </div>
      <div>
        <label style="margin-bottom:.2rem">Status</label>
        <select id="mi-status" onchange="loadMyItems()" style="margin:0">
          <option value="">Any</option>
        </select>
      </div>
      <div>
        <label style="margin-bottom:.2rem">Due</label>
        <select id="mi-due" onchange="loadMyItems()" style="margin:0">
          <option value="">Any</option>
          <option value="overdue">Overdue</option>
          <option value="7">Due in 7 days</option>
          <option value="30">Due in 30 days</option>
        </select>
      </div>
      <div style="margin-left:auto;font-size:.8rem;color:var(--gray-600)" id="mi-count"></div>
    </div>
  </div>
  <div class="card">
    <div class="ch"><div class="ch-left"><div class="cicon">👤</div>Assigned Items</div></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>File #</th><th>Title</th><th>Meeting</th><th>Body</th>
          <th>Status</th><th>Notes</th><th>Due Date</th><th></th>
        </tr></thead>
        <tbody id="mi-tbody">
          <tr><td colspan="8" style="padding:1.25rem;color:var(--gray-400)">Select a researcher to view their items.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ HELP PAGE ═══════════════════════════ -->
<div class="pg" id="pg-help">
  <div id="help-page-content"></div>
</div>

<!-- ═══════════════════════════ MATTER DETAIL DRAWER ═══════════════════════════ -->
<div id="drawer-bg" onclick="closeDrawer()"></div>
<div id="drawer">
  <button class="dr-close" onclick="closeDrawer()">✕</button>
  <div class="dr-header">
    <div class="dr-title" id="dr-title">Loading…</div>
    <div class="dr-meta" id="dr-meta"></div>
  </div>
  <div class="dr-tabs">
    <button class="dtab on" onclick="drTab('overview',this)" title="Debrief + Watchpoints + Legislative Status + Item Evolution">Agenda Debrief</button>
    <button class="dtab" onclick="drTab('notes',this)">Notes</button>
    <button class="dtab" onclick="drTab('deep',this)" title="Part 2 notes + meeting transcript notes">Deep Research</button>
    <button class="dtab" onclick="drTab('chat',this)" title="Private AI chat about this item">AI Chat</button>
    <button class="dtab" onclick="drTab('lifecycle',this)">Lifecycle</button>
    <button class="dtab" onclick="drTab('history',this)">History</button>
  </div>
  <div class="dr-body" id="dr-body">
    <div style="color:var(--gray-400);font-size:.85rem">Loading…</div>
  </div>
  <div style="padding:.85rem 1.1rem;border-top:1px solid var(--gray-200);
    display:flex;gap:.5rem;align-items:center;flex-shrink:0;background:#fff">
    <button class="btn btn-p btn-sm" onclick="exportAndDownload()" id="dr-export-btn">
      ↓ Export Files
    </button>
    <button class="btn btn-s btn-sm" onclick="saveSummaryEdits()" id="dr-save-btn" style="display:none">
      Save Edits
    </button>
    <span id="dr-save-msg" style="font-size:.75rem;color:var(--green);display:none">✓ Saved</span>
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════
let mode='single', allCmtes=[], jobId=null, evtSrc=null;
let currentUser = localStorage.getItem('oca_user') || '';
let currentAppId = null;  // appearance currently open in drawer
let currentFileNum = null;

// ════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════
(async () => {
  await loadConfig();
  await Promise.all([
    loadCmtes(),
    populateStatusFilters(),
    loadDashboard(),
  ]);
  loadWorkflow();
  // Auto-reattach to any running job (e.g. after browser reload)
  checkActiveJobs();
})();

// ════════════════════════════════════════════════════════════
// Navigation
// ════════════════════════════════════════════════════════════
function showHelp() {
  const m = document.getElementById('help-modal');
  if (m) m.style.display = 'flex';
}
function switchHelpTab(tab, el) {
  document.querySelectorAll('#help-modal .htab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('#help-modal .hpane').forEach(p=>{
    p.classList.remove('on');
    p.style.display='none';
  });
  if (el) el.classList.add('on');
  const pane = document.getElementById('hpane-'+tab);
  if (pane) { pane.classList.add('on'); pane.style.display='block'; }
}
function hideHelp(e) {
  if (e && e.target && e.target.id && e.target.id !== 'help-modal' && !e.target.matches('.btn')) {
    // only close on backdrop click or explicit button
    if (e.target.id !== 'help-modal') return;
  }
  const m = document.getElementById('help-modal');
  if (m) m.style.display = 'none';
}
function loadHelpPage() {
  const container = document.getElementById('help-page-content');
  if (!container) return;
  if (container.children.length) return; // already loaded
  // Clone the modal inner content (skip the modal backdrop wrapper)
  const modal = document.getElementById('help-modal');
  if (!modal) return;
  const inner = modal.querySelector(':scope > div'); // the white card
  if (!inner) return;
  const clone = inner.cloneNode(true);
  // Remove the close button and adjust header for page context
  const closeBtn = clone.querySelector('button[onclick*="hideHelp"]');
  if (closeBtn) closeBtn.remove();
  // Remove max-height constraint on the content area
  const contentArea = clone.querySelector('[style*="max-height"]');
  if (contentArea) contentArea.style.maxHeight = 'none';
  // Re-wire the tab switches to target the cloned panes
  clone.querySelectorAll('.htab').forEach(btn => {
    const origClick = btn.getAttribute('onclick') || '';
    const m = origClick.match(/switchHelpTab\('(\w+)'/);
    if (m) {
      const tab = m[1];
      btn.onclick = function() {
        clone.querySelectorAll('.htab').forEach(b=>b.classList.remove('on'));
        clone.querySelectorAll('.hpane').forEach(p=>{p.classList.remove('on');p.style.display='none';});
        this.classList.add('on');
        const pane = clone.querySelector('#hpane-'+tab) || clone.querySelectorAll('.hpane')[['tour','howto','columns','statuses','tabs'].indexOf(tab)];
        if (pane) { pane.classList.add('on'); pane.style.display='block'; }
      };
    }
  });
  // Give cloned panes unique IDs so they don't collide with modal
  clone.querySelectorAll('[id^="hpane-"]').forEach(p => {
    p.id = 'pg-' + p.id;
  });
  // Re-wire cloned tab clicks to use new IDs
  clone.querySelectorAll('.htab').forEach(btn => {
    const origClick = btn.getAttribute('onclick') || '';
    btn.removeAttribute('onclick');
  });
  clone.querySelectorAll('.htab').forEach((btn, i) => {
    const tabs = ['tour','howto','columns','statuses','tabs'];
    btn.onclick = function() {
      clone.querySelectorAll('.htab').forEach(b=>b.classList.remove('on'));
      clone.querySelectorAll('.hpane').forEach(p=>{p.classList.remove('on');p.style.display='none';});
      this.classList.add('on');
      const pane = clone.querySelector('#pg-hpane-'+tabs[i]);
      if (pane) { pane.classList.add('on'); pane.style.display='block'; }
    };
  });
  clone.style.boxShadow = '0 4px 20px rgba(0,0,0,.08)';
  clone.style.borderRadius = '14px';
  clone.style.overflow = 'hidden';
  clone.style.border = '1px solid var(--gray-200)';
  container.appendChild(clone);
}

function dismissWelcome() {
  const el = document.getElementById('welcome-strip');
  if (el) el.style.display = 'none';
  try { localStorage.setItem('oca_welcome_hidden','1'); } catch(_){}
}
// On first load, respect stored dismissal
try {
  if (localStorage.getItem('oca_welcome_hidden')==='1') {
    document.addEventListener('DOMContentLoaded',()=>{
      const el=document.getElementById('welcome-strip'); if(el) el.style.display='none';
    });
  }
} catch(_){}

function showPg(name) {
  document.querySelectorAll('.pg').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.nb').forEach(b => b.classList.remove('on'));
  document.getElementById('pg-'+name).classList.add('on');
  const nb = document.getElementById('nb-'+name);
  if (nb) nb.classList.add('on');
  if (name==='dashboard') loadDashboard();
  if (name==='workflow')  loadWorkflow();
  if (name==='settings')  loadSettings();
  if (name==='meetings')  loadSavedMeetings();
  if (name==='myitems')   { initMyItemsFilters(); loadMyItems(); }
  if (name==='help')      loadHelpPage();
}

// ════════════════════════════════════════════════════════════
// Dashboard
// ════════════════════════════════════════════════════════════
async function loadDashboard() {
  const r = await fetch('/api/stats');
  const d = await r.json();

  document.getElementById('s-matters').textContent = d.total_matters;
  document.getElementById('s-apps').textContent    = d.total_appearances;
  document.getElementById('s-mtgs').textContent    = d.total_meetings;
  const open = Object.entries(d.by_status)
    .filter(([s])=>!['Finalized','Archived'].includes(s))
    .reduce((a,[,v])=>a+v,0);
  document.getElementById('s-open').textContent = open;

  // Alert banners
  const bars = document.getElementById('alert-bars');
  bars.innerHTML = '';
  if (d.overdue_count > 0) {
    bars.innerHTML += `<div class="alert-bar ab-red" onclick="filterWorkflow('overdue')">
      <span class="icon">🔴</span>
      <span class="txt"><strong>${d.overdue_count} item${d.overdue_count>1?'s':''} OVERDUE</strong> — past due date, not yet finalized</span>
      <span class="count">View →</span>
    </div>`;
    const ob = document.getElementById('overdue-badge');
    ob.textContent = d.overdue_count; ob.style.display='';
  }
  if (d.due_soon_count > 0) {
    bars.innerHTML += `<div class="alert-bar ab-orange" onclick="filterWorkflow('7')">
      <span class="icon">🟡</span>
      <span class="txt"><strong>${d.due_soon_count} item${d.due_soon_count>1?'s':''} due within 7 days</strong></span>
      <span class="count">View →</span>
    </div>`;
  }
  if (d.pending_review_count > 0) {
    bars.innerHTML += `<div class="alert-bar ab-green" onclick="showPg('workflow');setTimeout(showReviewQueue,100)">
      <span class="icon">📋</span>
      <span class="txt"><strong>${d.pending_review_count} item${d.pending_review_count>1?'s':''} awaiting review</strong> — drafts ready for reviewer approval</span>
      <span class="count">Review Queue →</span>
    </div>`;
  }
  if (d.needs_revision_count > 0) {
    bars.innerHTML += `<div class="alert-bar ab-orange" onclick="document.getElementById('wf-f-status').value='Needs Revision';showPg('workflow');loadWorkflow()">
      <span class="icon">⚠</span>
      <span class="txt"><strong>${d.needs_revision_count} item${d.needs_revision_count>1?'s':''} need revision</strong> — reviewer requested changes</span>
      <span class="count">View →</span>
    </div>`;
  }
  if (d.unassigned_count > 0) {
    bars.innerHTML += `<div class="alert-bar ab-gray" onclick="filterWorkflow('unassigned')">
      <span class="icon">⚪</span>
      <span class="txt"><strong>${d.unassigned_count} new item${d.unassigned_count>1?'s':''} unassigned</strong></span>
      <span class="count">Assign →</span>
    </div>`;
  }

  // Status breakdown
  document.getElementById('dash-status').innerHTML = Object.entries(d.by_status)
    .map(([s,c]) => `<div style="display:flex;justify-content:space-between;align-items:center;
      padding:.32rem 0;border-bottom:1px solid var(--gray-100)">
      <span>${badge(s)} ${s}</span><strong>${c}</strong></div>`).join('') ||
    '<p style="color:var(--gray-400);font-size:.82rem">No data yet.</p>';

  // Recent
  document.getElementById('dash-recent').innerHTML = d.recent.map(r =>
    `<tr class="clickable" onclick="openDrawer('${r.file_number}',${r.appearance_id})">
      <td><span class="file-link">${r.file_number}</span></td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.short_title||'')}</td>
      <td style="white-space:nowrap">${fmtDate(r.meeting_date)}</td>
      <td style="font-size:.73rem">${esc(r.body_name||'')}</td>
      <td>${badge(r.workflow_status)}</td>
      <td style="font-size:.75rem">${esc(r.assigned_to||'—')}</td>
    </tr>`).join('') ||
    '<tr><td colspan="6" style="padding:1.25rem;color:var(--gray-400)">No data yet. Run a process job to populate.</td></tr>';
}

function filterWorkflow(filter) {
  document.getElementById('wf-f-due').value = filter==='unassigned' ? '' : filter;
  document.getElementById('wf-f-assigned').value = filter==='unassigned' ? 'unassigned' : '';
  showPg('workflow');
}

// ════════════════════════════════════════════════════════════
// Process
// ════════════════════════════════════════════════════════════
async function loadCmtes() {
  try {
    const r = await fetch('/api/committees');
    allCmtes = await r.json();
    const g = document.getElementById('cmteg');
    g.innerHTML = allCmtes.map(c =>
      `<label class="ci"><input type="checkbox" value="${esc(c)}" checked>
       <span>${esc(c)}</span></label>`).join('');
  } catch(e) {
    document.getElementById('cmteg').innerHTML = '<span style="color:var(--red);font-size:.78rem">Could not load.</span>';
  }
  // Default the date pickers to today (only if not already set)
  const today = new Date().toISOString().slice(0,10);
  const ds = document.getElementById('d-single');
  const df = document.getElementById('d-from');
  if(ds && !ds.value) ds.value = today;
  if(df && !df.value) df.value = today;
}

function setMode(m) {
  mode=m;
  document.getElementById('tm-single').classList.toggle('on',m==='single');
  document.getElementById('tm-range').classList.toggle('on',m==='range');
  document.getElementById('inp-single').style.display=m==='single'?'':'none';
  document.getElementById('inp-range').style.display=m==='range'?'':'none';
}

function toggleAll() {
  const cbs=[...document.querySelectorAll('#cmteg input')];
  const any=cbs.some(c=>c.checked);
  cbs.forEach(c=>c.checked=!any);
}

function selCmtes() {
  return [...document.querySelectorAll('#cmteg input:checked')].map(c=>c.value);
}

async function runAgent() {
  const dv=document.getElementById('d-single').value.trim();
  const fv=document.getElementById('d-from').value.trim();
  const val=mode==='single'?dv:fv;
  if(!val){alert('Enter a date.');return;}
  const btn=document.getElementById('run-btn');
  btn.disabled=true; btn.textContent='Running…';
  clearLog(); setSt('run','Starting…'); setPb(true);
  document.getElementById('result-card').style.display='none';
  const body={committees:selCmtes(),...(mode==='single'?{date:dv}:{from_date:fv})};
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const {job_id}=await r.json();
  jobId=job_id; listenJob(job_id);
}

const PHASE_LABELS = {
  scanning: 'Scanning',
  analyzing: 'Analyzing',
  exporting: 'Exporting',
  done: 'Complete',
};

function _handleProgressMsg(msg, id) {
  const isReplay = msg.replay;
  if(msg.type==='ping') return;
  if(msg.type==='progress'){
    if(!isReplay) addLog(msg.message);
    const pct = msg.pct;
    if(pct != null) {
      document.getElementById('pb').style.width = Math.min(99, Math.round(pct))+'%';
    }
    const phaseLabel = PHASE_LABELS[msg.phase] || msg.phase || '';
    const badge = document.getElementById('phase-badge');
    if(phaseLabel) {
      badge.textContent = phaseLabel;
      badge.style.display = '';
      const colors = {Scanning:'#3b82f6',Analyzing:'#f59e0b',Exporting:'#8b5cf6',Complete:'#22c55e'};
      badge.style.background = colors[phaseLabel] || 'var(--blue1)';
    }
    const shortMsg = msg.message.length > 80 ? msg.message.slice(0,77)+'...' : msg.message;
    setSt('run', shortMsg);
  }
  if(msg.type==='complete'){
    if(evtSrc){evtSrc.close(); evtSrc=null;}
    setPb(false);
    document.getElementById('pb').style.width='100%';
    const badge = document.getElementById('phase-badge');
    badge.textContent='Complete'; badge.style.display=''; badge.style.background='#22c55e';
    const ni = msg.results ? msg.results.total_new_items : 0;
    const nf = msg.results ? msg.results.total_files : 0;
    setSt('ok',`Done — ${ni} new item(s), ${nf} files`);
    addLog(`Complete: ${ni} new items`,'ok');
    if(id && msg.files) renderResultFiles(id, msg.files);
    resetBtn(); loadDashboard(); loadWorkflow();
  }
  if(msg.type==='error'){
    if(evtSrc){evtSrc.close(); evtSrc=null;}
    setPb(false);
    document.getElementById('phase-badge').style.display='none';
    setSt('err','Error: '+msg.message); addLog('ERROR: '+msg.message,'err'); resetBtn();
  }
}

function listenJob(id, replay) {
  if(evtSrc)evtSrc.close();
  const url = replay ? `/api/stream/${id}?replay=1` : `/api/stream/${id}`;
  evtSrc=new EventSource(url);
  evtSrc.onmessage=e=>{
    const msg=JSON.parse(e.data);
    _handleProgressMsg(msg, id);
  };
}

async function checkActiveJobs() {
  // Auto-reattach to a running job after browser reload
  try {
    const r = await fetch('/api/jobs/active');
    const d = await r.json();
    const running = (d.jobs||[]).find(j=>j.status==='running');
    if(running) {
      jobId = running.job_id;
      setSt('run','Reconnecting to running analysis…'); setPb(true);
      const btn=document.getElementById('run-btn');
      btn.disabled=true; btn.textContent='Running…';
      listenJob(running.job_id, true);
    }
  } catch(e) {}
}

function renderResultFiles(jid,files) {
  const card=document.getElementById('result-card');
  const body=document.getElementById('result-body');
  body.innerHTML=(files&&files.length)?files.map(f=>{
    const ext=f.split('.').pop().toLowerCase();
    return `<div class="fi">
      <div class="fi-info">
        <div class="ficon ${ext}">${ext.toUpperCase()}</div>
        <div><div style="font-size:.8rem;font-weight:500">${esc(f)}</div>
          <div style="font-size:.7rem;color:var(--gray-400)">${ext==='xlsx'?'Excel Tracking':'Part 1 — Agenda Debrief (deliverable)'}</div>
        </div>
      </div>
      <a class="dlbtn" href="/api/download/${jid}/${encodeURIComponent(f)}" download="${esc(f)}">↓ Download</a>
    </div>`;}).join('')
    :'<p style="color:var(--gray-400);font-size:.82rem">No new files — all items may already be processed.</p>';
  card.style.display='';
}

function clearLog(){document.getElementById('logbox').innerHTML='';}
function addLog(msg,cls=''){
  const b=document.getElementById('logbox');
  const ts=new Date().toTimeString().slice(0,8);
  const d=document.createElement('div'); d.className='ll '+(cls||'');
  d.innerHTML=`<span class="ts">${ts}</span><span class="msg">${esc(msg)}</span>`;
  b.appendChild(d); b.scrollTop=b.scrollHeight;
}
function setSt(state,text){
  document.getElementById('sdot').className='sdot '+state;
  document.getElementById('stxt').textContent=text;
}
function setPb(spin){
  const b=document.getElementById('pb');
  b.classList.toggle('spin',spin);
  if(spin)b.style.width='';
}
function resetBtn(){
  const btn=document.getElementById('run-btn');
  btn.disabled=false; btn.textContent='▶  Run Analysis';
}

// ════════════════════════════════════════════════════════════
// Search
// ════════════════════════════════════════════════════════════
async function doSearch() {
  const f=document.getElementById('sf').value.trim();
  const k=document.getElementById('sk').value.trim();
  const s=document.getElementById('ss').value.trim();
  let url='/api/search?';
  if(f) url+=`file=${encodeURIComponent(f)}`;
  else if(k) url+=`keyword=${encodeURIComponent(k)}`;
  else if(s) url+=`sponsor=${encodeURIComponent(s)}`;
  else return;
  const r=await fetch(url); const d=await r.json();
  const tb=document.getElementById('srch-tbody');
  const cnt=document.getElementById('srch-count');

  const rows = d.type==='matter' ? (d.data ? d.data.appearances||[] : []) : (d.data||[]);
  cnt.textContent=`${rows.length} result(s)`;

  if(!rows.length){
    tb.innerHTML='<tr><td colspan="9" style="padding:1.25rem;color:var(--gray-400)">No results.</td></tr>';
    return;
  }
  tb.innerHTML=rows.map(r=>{
    const fn=r.file_number||r.matter_file||r.file_number||'';
    const title=r.short_title||r.appearance_title||'';
    return `<tr class="clickable" onclick="openDrawer('${esc(fn)}',${r.id||r.appearance_id||0})">
      <td><span class="file-link">${fn}</span></td>
      <td style="white-space:nowrap">${fmtDate(r.meeting_date)}</td>
      <td style="font-size:.73rem">${esc(r.body_name||'')}</td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(title)}</td>
      <td style="font-size:.73rem">${esc(r.file_type||r.current_status||'')}</td>
      <td style="font-size:.73rem">${esc(r.current_status||'')}</td>
      <td>${badge(r.workflow_status||'')}</td>
      <td style="font-size:.73rem">${esc(r.assigned_to||'—')}</td>
      <td><button class="btn btn-o btn-xs" onclick="event.stopPropagation();openDrawer('${esc(fn)}',${r.id||r.appearance_id||0})">Open</button></td>
    </tr>`;}).join('');
}

// ════════════════════════════════════════════════════════════
// Workflow
// ════════════════════════════════════════════════════════════
async function populateStatusFilters() {
  const stats=['New','Assigned','In Progress','Draft Complete','In Review','Needs Revision','Finalized','Archived'];
  const wfs=document.getElementById('wf-f-status');
  stats.forEach(s=>wfs.add(new Option(s,s)));
}

async function loadWorkflow() {
  const status=document.getElementById('wf-f-status').value;
  const assigned=document.getElementById('wf-f-assigned').value;
  const due=document.getElementById('wf-f-due').value;
  // Guard: user picked "My Items" but never set their name
  if(assigned==='me' && !currentUser){
    alert('To use "My Items", first pick your name from the "Acting as…" dropdown in the top-right header (or on the Settings page). Then "My Items" will filter to just your items.');
    document.getElementById('wf-f-assigned').value='';
    // Highlight the header picker so they can find it
    const hdrSel=document.getElementById('current-user-sel');
    if(hdrSel){ hdrSel.focus(); hdrSel.style.outline='3px solid #fbbf24'; setTimeout(()=>hdrSel.style.outline='',2500); }
    return;
  }
  let url='/api/workflow?';
  if(status) url+=`status=${encodeURIComponent(status)}&`;
  if(assigned==='me'&&currentUser) url+=`assigned=${encodeURIComponent(currentUser)}&`;
  else if(assigned==='reviewer:me'&&currentUser) url+=`reviewer=${encodeURIComponent(currentUser)}&`;
  else if(assigned==='unassigned') url+=`assigned=__unassigned__&`;
  else if(assigned && assigned!=='me' && assigned!=='reviewer:me') url+=`assigned=${encodeURIComponent(assigned)}&`;
  if(due) url+=`due=${encodeURIComponent(due)}&`;
  const r=await fetch(url); const d=await r.json();
  document.getElementById('wf-count').textContent=`${d.length} item(s)`;
  const tb=document.getElementById('wf-tbody');
  if(!d.length){
    tb.innerHTML='<tr><td colspan="11">'+emptyState(
      '🔍','No items match these filters',
      'Try loosening Status / Assignee / Due filters, or switch to <b>Saved Meetings</b> to browse by agenda.'
    )+'</td></tr>';
    return;
  }

  // Group by meeting (body_name + meeting_date), newest meeting first
  const groups={};
  d.forEach(a=>{
    const key=`${a.meeting_date||'0000-00-00'}||${a.body_name||''}`;
    (groups[key]=groups[key]||[]).push(a);
  });
  const keys=Object.keys(groups).sort((x,y)=>y.localeCompare(x));

  const mkRow = a => {
    const due_cls=a._due_class||'due-none';
    const due_lbl=a._due_label||a.due_date||'—';
    return `<tr class="clickable" onclick="openDrawer('${esc(a.file_number)}',${a.id})">
      <td onclick="event.stopPropagation()"><input type="checkbox" class="row-sel" value="${a.id}"></td>
      <td style="font-size:.75rem;color:var(--gray-400)">${a.id}</td>
      <td><span class="file-link">${a.file_number}</span></td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.short_title||'')}</td>
      <td style="font-size:.72rem">${esc(a.committee_item_number||a.bcc_item_number||a.raw_agenda_item_number||'')}</td>
      <td style="font-size:.72rem">${esc(a.agenda_stage||'')}</td>
      <td onclick="event.stopPropagation()">
        <select class="inline-status" onchange="quickStatus(${a.id},this.value)">
          ${['New','Assigned','In Progress','Draft Complete','In Review','Needs Revision','Finalized','Archived']
            .map(s=>`<option${s===a.workflow_status?' selected':''}>${s}</option>`).join('')}
        </select>
      </td>
      <td style="font-size:.75rem">${esc(a.assigned_to||'—')}</td>
      <td class="due-cell ${due_cls}">${due_lbl}</td>
      <td style="font-size:.73rem">${esc(a.priority||'')}</td>
      <td onclick="event.stopPropagation()">
        <button class="btn btn-o btn-xs" onclick="openDrawer('${esc(a.file_number)}',${a.id})">Open</button>
      </td>
    </tr>`;
  };

  tb.innerHTML=keys.map(k=>{
    const [dt,body]=k.split('||');
    const rows=groups[k];
    const mid=rows[0].meeting_id;
    const finalized=rows.filter(r=>r.workflow_status==='Finalized'||r.workflow_status==='Archived').length;
    const total=rows.length;
    const pct=total?Math.round(100*finalized/total):0;
    const gid=`wf-grp-${mid||(dt+body).replace(/\\W/g,'')}`;
    const header=`<tr class="wf-group-hdr" style="background:linear-gradient(90deg,#eef2ff,#f8fafc);cursor:pointer" onclick="document.querySelectorAll('.${gid}').forEach(el=>el.style.display=el.style.display==='none'?'':'none')">
      <td colspan="11" style="padding:.55rem .75rem;font-weight:600;color:#1e3a8a;border-top:2px solid #6366f1">
        <span style="display:inline-block;min-width:7ch;color:#475569">${esc(dt||'—')}</span>
        <span style="margin:0 .5rem">·</span>
        <span>${esc(body||'')}</span>
        <span style="margin-left:.75rem;font-weight:400;color:#64748b;font-size:.8rem">${total} item(s) · ${finalized}/${total} finalized (${pct}%)</span>
        ${mid?`<button class="btn btn-o btn-xs" style="float:right" onclick="event.stopPropagation();openMeeting(${mid})">Open meeting →</button>`:''}
      </td></tr>`;
    const body_rows=rows.map(a=>{
      const h=mkRow(a);
      // inject group class for toggling
      return h.replace('<tr class="clickable"', `<tr class="clickable ${gid}"`);
    }).join('');
    return header+body_rows;
  }).join('');
}

async function quickStatus(appId, status) {
  await fetch(`/api/appearance/${appId}/workflow`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({status, changed_by:currentUser})
  });
  loadDashboard();
}

function selAll(cb) {
  document.querySelectorAll('.row-sel').forEach(c=>c.checked=cb.checked);
}

function getSelected() {
  return [...document.querySelectorAll('.row-sel:checked')].map(c=>parseInt(c.value));
}

function showBulkModal(title, content, onConfirm) {
  let overlay=document.getElementById('bulk-modal-overlay');
  if(!overlay){
    overlay=document.createElement('div');
    overlay.id='bulk-modal-overlay';
    overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9000;display:flex;align-items:center;justify-content:center;';
    document.body.appendChild(overlay);
  }
  overlay.innerHTML=`
    <div style="background:#fff;border-radius:12px;padding:1.5rem;min-width:320px;max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,.25);">
      <h3 style="margin:0 0 1rem;font-size:1rem;color:#1e293b;">${title}</h3>
      ${content}
      <div style="display:flex;gap:.6rem;margin-top:1.2rem;justify-content:flex-end;">
        <button class="btn btn-o btn-sm" onclick="document.getElementById('bulk-modal-overlay').style.display='none'">Cancel</button>
        <button class="btn btn-p btn-sm" id="bulk-confirm-btn">Apply</button>
      </div>
    </div>`;
  overlay.style.display='flex';
  document.getElementById('bulk-confirm-btn').onclick=()=>{
    overlay.style.display='none';
    onConfirm();
  };
}

async function bulkAssign() {
  const ids=getSelected();
  if(!ids.length){alert('Select items first.');return;}
  const members=(_cfg.team_members||[]).map(m=>m.name);
  if(!members.length){alert('No team members configured. Add team members in Settings first.');return;}
  const opts=members.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  const d=new Date(); d.setDate(d.getDate()+7);
  const defaultDue=d.toISOString().slice(0,10);
  showBulkModal(
    `Assign ${ids.length} item(s)`,
    `<label style="font-size:.78rem;color:var(--gray-600);display:block;margin-bottom:.25rem">Assign to</label>
     <select id="bulk-assign-sel" style="width:100%;margin:0;">${opts}</select>
     <label style="font-size:.78rem;color:var(--gray-600);display:block;margin-top:.65rem;margin-bottom:.25rem">Due date <span style="color:var(--gray-400)">(optional)</span></label>
     <input type="date" id="bulk-assign-due" value="${defaultDue}" style="width:100%;margin:0;">
     <div style="margin-top:.3rem;font-size:.7rem;color:var(--gray-400)">Leave blank to skip setting a due date.</div>`,
    async ()=>{
      const person=document.getElementById('bulk-assign-sel')?.value;
      if(!person)return;
      const due=document.getElementById('bulk-assign-due')?.value||'';
      const payload={assigned_to:person,changed_by:currentUser};
      if(due) payload.due_date=due;
      await Promise.all(ids.map(id=>
        fetch(`/api/appearance/${id}/workflow`,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)})
      ));
      loadWorkflow(); loadDashboard();
    }
  );
}

async function bulkStatus() {
  const ids=getSelected();
  if(!ids.length){alert('Select items first.');return;}
  const statuses=['New','Assigned','In Progress','Draft Complete','In Review','Needs Revision','Finalized','Archived'];
  const opts=statuses.map(s=>`<option value="${s}">${s}</option>`).join('');
  showBulkModal(
    `Change status for ${ids.length} item(s)`,
    `<select id="bulk-status-sel" style="width:100%;margin:0;">${opts}</select>`,
    async ()=>{
      const status=document.getElementById('bulk-status-sel')?.value;
      if(!status)return;
      await Promise.all(ids.map(id=>
        fetch(`/api/appearance/${id}/workflow`,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({status,changed_by:currentUser})})
      ));
      loadWorkflow(); loadDashboard();
    }
  );
}

async function bulkDueDate() {
  const ids=getSelected();
  if(!ids.length){alert('Select items first.');return;}
  // Default = 7 days out (same convention as the workflow reminders)
  const d=new Date(); d.setDate(d.getDate()+7);
  const iso=d.toISOString().slice(0,10);
  showBulkModal(
    `Set due date for ${ids.length} item(s)`,
    `<label style="font-size:.78rem;color:var(--gray-600);display:block;margin-bottom:.35rem">Due date</label>
     <input type="date" id="bulk-due-inp" value="${iso}" style="width:100%;margin:0;">
     <div style="margin-top:.6rem;font-size:.72rem;color:var(--gray-500)">Leave blank and click Apply to <b>clear</b> the due date on selected items.</div>`,
    async ()=>{
      const due=document.getElementById('bulk-due-inp')?.value||'';
      await Promise.all(ids.map(id=>
        fetch(`/api/appearance/${id}/workflow`,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({due_date:due,changed_by:currentUser})})
      ));
      loadWorkflow(); loadDashboard();
    }
  );
}

// ════════════════════════════════════════════════════════════
// Matter Detail Drawer
// ════════════════════════════════════════════════════════════
let _drData = null;

async function openDrawer(fileNum, appId) {
  currentFileNum=fileNum; currentAppId=appId||null;
  document.getElementById('drawer-bg').classList.add('open');
  document.getElementById('drawer').classList.add('open');
  document.getElementById('dr-body').innerHTML='<div style="color:var(--gray-400);padding:1rem;font-size:.85rem">Loading…</div>';
  document.getElementById('dr-title').textContent='Loading…';
  document.getElementById('dr-meta').innerHTML='';

  // Load matter + appearance
  const [mr, ar] = await Promise.all([
    fetch(`/api/search?file=${encodeURIComponent(fileNum)}`).then(r=>r.json()),
    appId ? fetch(`/api/appearance/${appId}`).then(r=>r.json()) : Promise.resolve(null),
  ]);

  const matter = mr.data || {};
  const appData = ar ? ar.appearance : (matter.appearances && matter.appearances[0]);
  const meetingInfo = ar ? {meeting_date: ar.appearance?.meeting_date, body_name: ar.appearance?.body_name} : {};

  _drData = {matter, appData, meetingInfo};

  document.getElementById('dr-title').textContent =
    `File# ${matter.file_number||fileNum} — ${matter.short_title||''}`;
  const priorCount = (matter.appearances||[]).filter(a => a.id !== currentAppId).length;
  document.getElementById('dr-meta').innerHTML = [
    matter.file_type && `<span>Type: ${esc(matter.file_type)}</span>`,
    matter.current_status && `<span><strong>Leg Status:</strong> ${esc(matter.current_status)}</span>`,
    matter.sponsor   && `<span>Requester: ${esc(matter.sponsor)}</span>`,
    matter.control_body && `<span>Control: ${esc(matter.control_body)}</span>`,
    meetingInfo.meeting_date && `<span>Meeting: ${meetingInfo.meeting_date}</span>`,
    meetingInfo.body_name && `<span>${esc(meetingInfo.body_name)}</span>`,
    appData?.agenda_stage && `<span>Stage: ${esc(appData.agenda_stage)}</span>`,
    priorCount > 0 ? `<span style="color:#fef3c7;background:rgba(146,64,14,.55);padding:.1rem .5rem;border-radius:10px">↔ ${priorCount} prior appearance${priorCount>1?'s':''}</span>` : '',
    appData?.carried_forward_from_prior ? '<span style="color:#fde68a;font-weight:600">↩ Carried Forward</span>' : '',
    appData?.matter_url ? `<a target="_blank" href="${esc(appData.matter_url)}" style="color:#fff;text-decoration:underline">↗ Legistar Item</a>` : '',
    (appData?.item_pdf_url || appData?.item_pdf_local_path)
      ? `<a target="_blank" href="/api/appearance/${appData.id}/pdf" style="color:#fff;text-decoration:underline">📄 Item PDF</a>` : '',
  ].filter(Boolean).join('');

  // Add 🎙 badge to Notes tab if transcript notes exist
  const notesTab = document.querySelectorAll('.dtab')[1]; // Notes is 2nd tab
  if (notesTab) {
    const hasTranscript = (appData?.analyst_working_notes || '').includes('[Meeting Discussion')
      || (appData?.transcript_analysis || '');
    notesTab.innerHTML = hasTranscript ? 'Notes <span style="font-size:.65rem" title="Has meeting transcript analysis">🎙</span>' : 'Notes';
  }

  // Default to overview tab
  drTab('overview', document.querySelector('.dtab'));
}

function closeDrawer() {
  document.getElementById('drawer-bg').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('dr-save-btn').style.display='none';
}

function drTab(tab, el) {
  document.querySelectorAll('.dtab').forEach(t=>t.classList.remove('on'));
  if(el)el.classList.add('on');
  const body=document.getElementById('dr-body');
  const save=document.getElementById('dr-save-btn');
  save.style.display='none';
  if(!_drData){body.innerHTML='<p style="color:var(--gray-400)">No data.</p>';return;}
  const {matter,appData} = _drData;

  if(tab==='overview') renderDrawerOverview(body,matter,appData,save);
  if(tab==='notes')   renderDrawerNotes(body,appData);
  if(tab==='deep')    renderDrawerDeepResearch(body,appData);
  if(tab==='chat')    renderDrawerChat(body,appData);
  if(tab==='history') renderDrawerHistory(body,appData);
  if(tab==='lifecycle')   renderDrawerLifecycle(body,appData,matter);
}

function renderDrawerOverview(body, matter, app, saveBtn) {
  const ai = app?.ai_summary_for_appearance || matter?.latest_ai_summary_part1 || '';
  const wp = app?.watch_points_for_appearance || matter?.latest_watch_points || '';
  // Sort all appearances chronologically (newest first)
  const allApps = (matter?.appearances||[]).slice().sort((a,b) =>
    (b.meeting_date||'').localeCompare(a.meeting_date||'')
  );

  body.innerHTML = `
    <div class="ds">
      <div class="ds-title">
        <span>AGENDA DEBRIEF</span>
        <button class="btn btn-o btn-xs" onclick="toggleEdit('edit-ai')">Edit</button>
      </div>
      <div class="editable-field" id="edit-ai" contenteditable="false">${esc(ai)||'<span style="color:var(--gray-400)">No debrief yet. Run AI analysis or write one manually.</span>'}</div>
    </div>
    <div class="ds">
      <div class="ds-title">
        <span>WATCH POINTS</span>
        <button class="btn btn-o btn-xs" onclick="toggleEdit('edit-wp')">Edit</button>
      </div>
      <div class="editable-field" id="edit-wp" contenteditable="false">${esc(wp)||'<span style="color:var(--gray-400)">None.</span>'}</div>
    </div>
    ${app?.leg_history_summary ? `<div class="ds"><div class="ds-title">LEGISLATIVE HISTORY (AI Summary)</div>
      <div class="editable-field" style="cursor:default;font-size:.82rem;line-height:1.55">${esc(app.leg_history_summary)}</div></div>` : ''}
    ${renderItemEvolution(allApps, app)}
    <div class="ds" id="debrief-backfill-section">
      <div class="ds-title">BACKFILL</div>
      <div style="font-size:.74rem;color:var(--gray-400);margin-bottom:.5rem">
        Fetch missing data for this item — URLs, lifecycle history, transcripts, and AI analysis.
      </div>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap">
        <button class="btn btn-s btn-sm" onclick="runBackfillForItem('urls')" id="bf-urls-btn">
          🔗 Backfill URLs + Lifecycle
        </button>
        <button class="btn btn-s btn-sm" onclick="runBackfillForItem('transcript')" id="bf-tx-btn">
          🎙 Backfill Transcript
        </button>
        <button class="btn btn-s btn-sm" onclick="reanalyzeAppearance()" id="bf-ai-btn">
          🤖 Re-run AI Analysis
        </button>
        <button class="btn btn-s btn-sm" onclick="reanalyzeAllAppearances()" id="bf-ai-all-btn">
          🤖 Re-analyze ALL Appearances
        </button>
        <button class="btn btn-s btn-sm" onclick="runBackfillForItem('all')" id="bf-all-btn">
          ⚡ Backfill Everything
        </button>
      </div>
      <div id="bf-item-progress" style="margin-top:.4rem;font-size:.72rem;color:var(--gray-400);display:none"></div>
    </div>
  `;
  saveBtn.style.display='';
}

function renderItemEvolution(allApps, currentApp) {
  // "Item Changes / Evolution" — shows what was DISCUSSED at each meeting
  // and whether the item PDF changed between appearances.
  if (!allApps || allApps.length < 2) return '';

  const fmtD = d => { if(!d) return ''; const x=new Date(d); return isNaN(x)?d:x.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); };
  let evolutionRows = '';
  // allApps is newest-first; iterate oldest-first for chronological flow
  const chronoApps = allApps.slice().reverse();

  for (let i = 0; i < chronoApps.length; i++) {
    const a = chronoApps[i];
    const prev = i > 0 ? chronoApps[i-1] : null;
    const bodyLabel = a.body_name || a.agenda_stage || 'Appearance';
    const dateLabel = fmtD(a.meeting_date);
    const isCurr = a.id === currentApp?.id;
    const notes = (a.analyst_working_notes||'');

    // === 1. What was DISCUSSED (transcript analysis) ===
    let discussionHtml = '';
    let txText = a.transcript_analysis || '';
    if (!txText) {
      const txMatch = notes.match(/(\[Meeting Discussion[\s\S]*?)(?=\n\n\[(?!Meeting)|$)/);
      if (txMatch) txText = txMatch[1];
    }
    if (txText) {
      // Extract key discussion points, truncate for overview
      const cleaned = txText.replace(/^\[Meeting Discussion[^\]]*\]\s*/i, '').trim();
      const preview = cleaned.slice(0, 400) + (cleaned.length > 400 ? '…' : '');
      discussionHtml = `
        <div style="margin-top:.4rem;padding:.4rem .6rem;background:#f5f3ff;border:1px solid #ddd6fe;border-radius:6px">
          <div style="font-size:.7rem;font-weight:600;color:#6d28d9;margin-bottom:.2rem">🎙 WHAT WAS DISCUSSED</div>
          <div style="font-size:.76rem;color:#374151;line-height:1.45;white-space:pre-wrap">${esc(preview)}</div>
        </div>`;
    } else {
      discussionHtml = `
        <div style="margin-top:.3rem;font-size:.72rem;color:#94a3b8;font-style:italic">
          No transcript analysis available for this appearance.
        </div>`;
    }

    // === 2. Did the PDF change? ===
    let pdfChangeHtml = '';
    const cdChanges = notes.match(/\[(Changes from ([^\]]+) to [^\]]+)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
    const cdNoChanges = notes.match(/\[(No changes from ([^\]]+) to [^\]]+)\]/);
    const hasPdf = !!(a.item_pdf_url || a.item_pdf_local_path);

    if (cdChanges) {
      const changeSummary = (cdChanges[3]||'').trim().split('\n').slice(0, 4).join('\n').slice(0, 400);
      pdfChangeHtml = `
        <div style="margin-top:.3rem;padding:.4rem .6rem;background:#fef2f2;border:1px solid #fecaca;border-radius:6px">
          <div style="font-size:.7rem;font-weight:600;color:#dc2626;margin-bottom:.2rem">⚠ PDF CHANGED from prior version</div>
          <div style="font-size:.76rem;color:#374151;line-height:1.45;white-space:pre-wrap">${esc(changeSummary)}</div>
        </div>`;
    } else if (cdNoChanges) {
      pdfChangeHtml = `
        <div style="margin-top:.3rem;padding:.3rem .6rem;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px">
          <div style="font-size:.72rem;color:#059669;font-weight:600">✓ PDF unchanged from prior version</div>
        </div>`;
    } else if (i > 0 && hasPdf) {
      pdfChangeHtml = `
        <div style="margin-top:.3rem;font-size:.72rem;color:#94a3b8;font-style:italic">
          PDF change detection not yet run. Use backfill to compare versions.
        </div>`;
    }

    evolutionRows += `
      <div style="display:flex;gap:.6rem;align-items:flex-start">
        <div style="display:flex;flex-direction:column;align-items:center;min-width:28px">
          <div style="width:10px;height:10px;border-radius:50%;background:${isCurr?'#2563eb':'#94a3b8'};margin-top:5px"></div>
          ${i < chronoApps.length-1 ? '<div style="width:2px;flex:1;background:#e2e8f0;min-height:30px"></div>':''}
        </div>
        <div style="flex:1;background:#f8fafc;border:1px solid ${isCurr?'#93c5fd':'#e2e8f0'};
          border-radius:8px;padding:.55rem .7rem;margin-bottom:.5rem;${isCurr?'box-shadow:0 0 0 2px #93c5fd':''}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:.8rem;font-weight:600;color:#1e293b">
              ${esc(bodyLabel)} — ${esc(dateLabel)}
              ${isCurr?' <span style="color:#2563eb;font-size:.68rem">● CURRENT</span>':''}
            </div>
            <button class="btn btn-o btn-xs" onclick="event.stopPropagation();openDrawer('${esc(a.file_number||'')}',${a.id})" style="font-size:.65rem">
              Open
            </button>
          </div>
          ${discussionHtml}
          ${pdfChangeHtml}
        </div>
      </div>`;
  }

  return `
    <div class="ds">
      <div class="ds-title">ITEM CHANGES / EVOLUTION</div>
      <div style="font-size:.74rem;color:var(--gray-400);margin-bottom:.5rem">
        What was discussed and whether the item PDF changed across ${chronoApps.length} appearances.
      </div>
      ${evolutionRows}
    </div>`;
}

function renderAppearanceCard(a, matter, isCurrent, priorApp) {
  const stage = (a.agenda_stage||'').toLowerCase();
  const stageLabel = stage.includes('committee') ? 'Committee'
                   : stage.includes('bcc') ? 'BCC'
                   : (a.agenda_stage||'Appearance');
  const bodyName = a.body_name || stageLabel;

  // Detect transcript analysis
  const transcriptNotes = a.transcript_analysis || '';
  const hasTranscript = !!transcriptNotes || (a.analyst_working_notes||'').includes('[Meeting Discussion');

  // Extract transcript block from analyst_working_notes if no dedicated field
  let transcriptHtml = '';
  if (transcriptNotes) {
    transcriptHtml = _renderTranscriptCard(transcriptNotes);
  } else if (hasTranscript) {
    const txMatch = (a.analyst_working_notes||'').match(/(\[Meeting Discussion[\s\S]*?)(?=\n\n\[(?!Meeting)|$)/);
    if (txMatch) transcriptHtml = _renderTranscriptCard(txMatch[1]);
  }

  // Change detection
  let changeHtml = '';
  const notes = (a.analyst_working_notes||'');
  const cdChanges = notes.match(/\[(Changes from ([^\]]+) to BCC version)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
  const cdNoChanges = notes.match(/\[(No changes from ([^\]]+) to BCC version)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
  if (cdChanges) {
    changeHtml = `<div class="change-box changed">
      <strong>⚠ CHANGES DETECTED</strong> from ${esc(cdChanges[2])}
      <div style="margin-top:.3rem;white-space:pre-wrap;font-size:.75rem">${esc((cdChanges[3]||'').trim()).slice(0,800)}</div>
    </div>`;
  } else if (cdNoChanges) {
    changeHtml = `<div class="change-box nochange">
      <strong>✓ No changes</strong> from ${esc(cdNoChanges[2])}
    </div>`;
  } else if (a.carried_forward_from_prior && priorApp) {
    changeHtml = `<div class="change-box carried">
      ↩ Carried forward from ${esc(priorApp.body_name||'')} (${esc(fmtDate(priorApp.meeting_date)||'')})
    </div>`;
  }

  // Strip transcript blocks and change detection blocks from notes for clean display
  let cleanNotes = (a.analyst_working_notes||'').trim();
  cleanNotes = cleanNotes.replace(/\[Meeting Discussion[\s\S]*?(?=\n\n\[(?!Meeting)|$)/g, '').trim();
  cleanNotes = cleanNotes.replace(/\[(Changes from[^\]]*|No changes from[^\]]*)\]\s*[\s\S]*?(?=\n\n\[|$)/g, '').trim();

  const reviewer = (a.reviewer_notes||'').trim();
  const fmt = d => { if(!d) return ''; const x=new Date(d); return isNaN(x)?d:x.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); };
  const whoA = a.analyst_notes_updated_by || '';
  const whoR = a.reviewer_notes_updated_by || '';

  // Vote result from AI analysis or transcript
  const voteResult = a.vote_result || '';

  const fileNum = matter?.file_number || a.file_number || '';
  const hasPdf = !!(a.item_pdf_url || a.item_pdf_local_path);

  return `
    <div class="app-card ${isCurrent?'current':''}" style="cursor:${isCurrent?'default':'pointer'}"
      ${!isCurrent ? `onclick="openDrawer('${esc(fileNum)}',${a.id})"` : ''}
      title="${isCurrent ? 'Currently viewing this appearance' : 'Click to open this appearance'}">
      <div class="app-card-header">
        <div>
          <div class="app-card-title">${esc(bodyName)}</div>
          <div class="app-card-date">${esc(fmt(a.meeting_date))} · ${esc(stageLabel)}</div>
        </div>
        <div class="app-card-badges">
          ${isCurrent ? '<span class="badge-current">● CURRENT</span>' : '<span style="font-size:.68rem;color:#2563eb">Open →</span>'}
          ${hasTranscript ? '<span class="badge-transcript">🎙 Transcript</span>' : ''}
          ${a.carried_forward_from_prior ? '<span class="badge-carried">↩ Carried</span>' : ''}
        </div>
      </div>
      ${changeHtml}
      ${isCurrent ? transcriptHtml : (hasTranscript ? '<div style="font-size:.72rem;color:#6d28d9;margin:.2rem 0">🎙 Meeting transcript available</div>' : '')}
      ${isCurrent ? `
      <div class="app-notes-section">
        <label>Analyst Notes ${whoA ? '('+esc(whoA)+')' : ''}</label>
        <div style="font-size:.78rem;color:var(--gray-600);line-height:1.45;max-height:120px;overflow-y:auto;white-space:pre-wrap;background:#f8fafc;border-radius:6px;padding:.4rem .55rem;border:1px solid #e2e8f0">${esc(cleanNotes).slice(0,600) || '<span style="color:var(--gray-400);font-style:italic">No notes yet</span>'}${cleanNotes.length > 600 ? '…' : ''}</div>
      </div>` : (cleanNotes ? `
      <div style="font-size:.74rem;color:var(--gray-500);margin-top:.3rem;line-height:1.4">
        ${esc(cleanNotes.slice(0,150))}${cleanNotes.length>150?'…':''}
      </div>` : '')}
      ${voteResult ? `<div style="margin-top:.4rem;font-size:.76rem;color:var(--gray-600)"><strong>Vote:</strong> ${esc(voteResult)}</div>` : ''}
      <div style="margin-top:.3rem;display:flex;gap:.5rem;font-size:.7rem;color:var(--gray-400);align-items:center">
        ${a.workflow_status ? `<span>Status: ${esc(a.workflow_status)}</span>` : ''}
        ${a.assigned_to ? `<span>· ${esc(a.assigned_to)}</span>` : ''}
        ${hasPdf ? `<a href="/api/appearance/${a.id}/pdf" target="_blank" onclick="event.stopPropagation()" style="color:var(--blue2);text-decoration:none">📄 Item PDF</a>` : ''}
        ${a.matter_url ? `<a href="${esc(a.matter_url)}" target="_blank" onclick="event.stopPropagation()" style="color:var(--blue2);text-decoration:none">↗ Legistar</a>` : ''}
      </div>
    </div>`;
}

function _renderTranscriptCard(text) {
  // Parse structured transcript analysis text into a nice card
  const lines = text.split('\n');
  let sentiment = '', tone = '', speakers = '', summary = '';
  let inSpeakers = false;
  for (const line of lines) {
    if (line.startsWith('Sentiment:')) sentiment = line.replace('Sentiment:','').trim();
    else if (line.startsWith('Tone:')) tone = line.replace('Tone:','').trim();
    else if (line.startsWith('Speaker Positions:')) { inSpeakers = true; speakers = ''; }
    else if (inSpeakers && line.trim().startsWith('•')) speakers += line.trim() + '\n';
    else if (inSpeakers && !line.trim().startsWith('•')) inSpeakers = false;
    else if (line.startsWith('[Meeting Discussion')) continue;
    else if (!sentiment && !tone && line.trim()) summary += line.trim() + ' ';
  }
  if (!summary) summary = lines.slice(1,4).join(' ').slice(0,300);

  const sentColor = sentiment.toLowerCase().includes('supportive') ? '#16a34a'
    : sentiment.toLowerCase().includes('opposed') ? '#dc2626'
    : sentiment.toLowerCase().includes('divided') ? '#ca8a04'
    : '#6d28d9';

  return `
    <div class="transcript-box">
      <div class="transcript-box-title">🎙 Meeting Discussion</div>
      <div style="font-size:.78rem;color:var(--gray-700);line-height:1.5;margin-bottom:.35rem">${esc(summary.trim()).slice(0,500)}</div>
      ${sentiment ? `<div style="font-size:.75rem"><strong style="color:${sentColor}">Sentiment:</strong> ${esc(sentiment)}</div>` : ''}
      ${tone ? `<div style="font-size:.75rem"><strong>Tone:</strong> ${esc(tone)}</div>` : ''}
      ${speakers ? `<div style="font-size:.75rem;margin-top:.2rem;white-space:pre-wrap">${esc(speakers.trim())}</div>` : ''}
    </div>`;
}

function markAppNoteDirty(appId, type) {
  const btn = document.getElementById('app-save-' + appId);
  if (btn) btn.style.display = '';
}

async function saveAppearanceNotes(appId) {
  const analyst = document.getElementById('app-notes-analyst-' + appId)?.value || '';
  const reviewer = document.getElementById('app-notes-reviewer-' + appId)?.value || '';
  try {
    await fetch('/api/appearance/' + appId + '/notes', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({analyst_working_notes: analyst, reviewer_notes: reviewer, replace: true, changed_by: currentUser || 'analyst'})
    });
    const btn = document.getElementById('app-save-' + appId);
    if (btn) { btn.textContent = '✓ Saved'; setTimeout(() => { btn.textContent = 'Save Notes'; btn.style.display = 'none'; }, 2000); }
    toast('Notes saved', 'ok');
  } catch(e) { toast('Failed to save: ' + e.message, 'err'); }
}

async function runBackfillForItem(type) {
  if (!currentAppId) { toast('No appearance selected','err'); return; }
  const prog = document.getElementById('bf-item-progress');
  if(prog){ prog.style.display=''; prog.textContent='Starting backfill…'; }

  try {
    if (type === 'urls' || type === 'all') {
      if(prog) prog.textContent='Backfilling URLs + lifecycle…';
      await fetch('/api/backfill/urls-and-lifecycle', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({only_missing: true})});
    }
    if (type === 'transcript' || type === 'all') {
      if(prog) prog.textContent='Starting transcript backfill…';
      // Need meeting_id from the appearance data
      const meetingId = _drData?.appData?.meeting_id;
      if (!meetingId) { if(prog) prog.textContent='❌ No meeting_id found for this appearance'; return; }
      const r = await fetch('/api/backfill/transcript', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({meeting_id: meetingId})});
      if (r.ok) {
        // Poll for transcript progress
        const pollTx = setInterval(async () => {
          try {
            const pr = await fetch('/api/backfill/transcript/progress');
            const pd = await pr.json();
            if(prog) prog.textContent = pd.step || 'Processing…';
            if (pd.done || pd.error) {
              clearInterval(pollTx);
              if(prog) prog.textContent = pd.error ? '❌ ' + pd.error : '✓ Transcript backfill complete';
              setTimeout(()=>{ if(prog) prog.style.display='none'; }, 4000);
              // Refresh drawer
              if(currentFileNum) openDrawer(currentFileNum, currentAppId);
            }
          } catch(e){ clearInterval(pollTx); }
        }, 3000);
        return; // Don't hide progress — poll will handle it
      }
    }
    if (type === 'urls') {
      if(prog){ prog.textContent='✓ URLs + lifecycle backfill complete'; setTimeout(()=>prog.style.display='none', 3000); }
      // Refresh drawer
      if(currentFileNum) openDrawer(currentFileNum, currentAppId);
    }
  } catch(e) {
    if(prog){ prog.textContent='❌ Backfill failed: ' + (e.message||e); setTimeout(()=>prog.style.display='none', 5000); }
  }
}

async function reanalyzeAppearance() {
  if (!currentAppId) { toast('No appearance selected','err'); return; }
  const prog = document.getElementById('bf-item-progress');
  if(prog){ prog.style.display=''; prog.textContent='🤖 Running AI analysis…'; }
  const btn = document.getElementById('bf-ai-btn');
  if(btn){ btn.disabled=true; btn.textContent='Analyzing…'; }
  try {
    const r = await fetch('/api/appearance/' + currentAppId + '/reanalyze', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      if(prog) prog.textContent='AI analysis started — this takes 15-30 seconds…';
      // Poll for completion by checking if the appearance has been updated
      const startTime = Date.now();
      const pollAI = setInterval(async () => {
        try {
          const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
          const analysisAt = ar.appearance?.analysis_at || '';
          if (analysisAt && new Date(analysisAt) > new Date(startTime)) {
            clearInterval(pollAI);
            if(prog){ prog.textContent='✓ AI analysis complete'; setTimeout(()=>prog.style.display='none', 3000); }
            if(btn){ btn.disabled=false; btn.textContent='🤖 Re-run AI Analysis'; }
            // Refresh drawer
            _drData.appData = ar.appearance;
            if(currentFileNum) openDrawer(currentFileNum, currentAppId);
          } else if (Date.now() - startTime > 120000) {
            clearInterval(pollAI);
            if(prog) prog.textContent='⏳ Analysis is still running — check back shortly';
            if(btn){ btn.disabled=false; btn.textContent='🤖 Re-run AI Analysis'; }
          }
        } catch(e){ /* keep polling */ }
      }, 5000);
    } else {
      if(prog) prog.textContent='❌ ' + (d.error||'Failed');
      if(btn){ btn.disabled=false; btn.textContent='🤖 Re-run AI Analysis'; }
    }
  } catch(e) {
    if(prog) prog.textContent='❌ Failed: ' + (e.message||e);
    if(btn){ btn.disabled=false; btn.textContent='🤖 Re-run AI Analysis'; }
  }
}

function _mdProgress(html, show=true) {
  const el = document.getElementById('md-operation-progress');
  if (!el) return;
  el.style.display = show ? 'block' : 'none';
  if (html) el.innerHTML = html;
}

async function backfillMeetingUrls() {
  const mid = currentMeetingId;
  if (!mid) { toast('No meeting loaded', 'err'); return; }
  const btn = document.getElementById('md-urls-btn');
  if (btn) { btn.disabled = true; btn.textContent = '🔗 Running…'; }
  _mdProgress('<b>🔗 URLs + Lifecycle Backfill</b><br>Starting…');

  try {
    const r = await fetch('/api/backfill/urls-and-lifecycle', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({meeting_id: mid})
    });
    const d = await r.json();
    if (!d.ok) {
      _mdProgress('<b>🔗 URLs + Lifecycle</b><br><span style="color:#dc2626">❌ ' + esc(d.error||'Failed') + '</span>');
      if (btn) { btn.disabled = false; btn.textContent = '🔗 Backfill URLs + Lifecycle'; }
      return;
    }
    // Poll progress
    const poll = setInterval(async () => {
      try {
        const pr = await fetch('/api/backfill/progress');
        const pd = await pr.json();
        const pct = pd.percent || 0;
        const done = pd.done || 0;
        const total = pd.total || 0;
        _mdProgress(`<b>🔗 URLs + Lifecycle Backfill</b>
          <div style="margin:.3rem 0;background:#e0e7ff;border-radius:4px;height:6px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:#6366f1;transition:width .3s"></div></div>
          <span style="color:#4338ca">${done}/${total} items · ${pct}% complete</span>`);
        if (pd.summary || pd.error || (!pd.running && pct >= 100)) {
          clearInterval(poll);
          const s = pd.summary || {};
          _mdProgress(`<b>🔗 URLs + Lifecycle</b><br>
            <span style="color:#15803d">✓ Done</span> — ${s.matters||0} matters · ${s.urls_filled||0} links · ${s.pdfs_downloaded||0} PDFs · ${s.timeline_events||0} lifecycle events`);
          if (btn) { btn.disabled = false; btn.textContent = '🔗 Backfill URLs + Lifecycle'; }
          if (currentMeetingId) openMeeting(currentMeetingId);
          setTimeout(() => _mdProgress('', false), 10000);
        }
      } catch(_){}
    }, 2000);
  } catch(e) {
    _mdProgress('<b>🔗 URLs + Lifecycle</b><br><span style="color:#dc2626">❌ ' + esc(e.message) + '</span>');
    if (btn) { btn.disabled = false; btn.textContent = '🔗 Backfill URLs + Lifecycle'; }
  }
}

async function reanalyzeMeetingItems() {
  const mid = currentMeetingId;
  if (!mid) { toast('No meeting loaded', 'err'); return; }
  const btn = document.getElementById('md-reanalyze-btn');
  if (btn) { btn.disabled = true; btn.textContent = '🤖 Running…'; }
  _mdProgress('<b>🤖 AI Re-analysis</b><br>Queuing all items…');

  try {
    const r = await fetch('/api/meeting/' + mid + '/reanalyze-all', {method:'POST'});
    const d = await r.json();
    if (!d.ok) {
      _mdProgress('<b>🤖 AI Re-analysis</b><br><span style="color:#dc2626">❌ ' + esc(d.error||'Failed') + '</span>');
      if (btn) { btn.disabled = false; btn.textContent = '🤖 Re-analyze All Items'; }
      return;
    }
    const total = d.count || 0;
    let elapsed = 0;
    _mdProgress(`<b>🤖 AI Re-analysis</b><br><span style="color:#4338ca">Analyzing ${total} items — this may take a few minutes…</span>`);
    // Poll — this endpoint runs sequentially so we estimate progress
    const poll = setInterval(() => {
      elapsed += 3;
      const estPct = Math.min(95, Math.round((elapsed / (total * 20)) * 100));
      _mdProgress(`<b>🤖 AI Re-analysis</b>
        <div style="margin:.3rem 0;background:#e0e7ff;border-radius:4px;height:6px;overflow:hidden">
          <div style="width:${estPct}%;height:100%;background:#6366f1;transition:width .3s"></div></div>
        <span style="color:#4338ca">~${estPct}% · ${elapsed}s elapsed · analyzing ${total} items…</span>`);
      if (elapsed > total * 25 + 60) {
        clearInterval(poll);
        _mdProgress(`<b>🤖 AI Re-analysis</b><br><span style="color:#15803d">✓ Should be complete — refresh to see results</span>`);
        if (btn) { btn.disabled = false; btn.textContent = '🤖 Re-analyze All Items'; }
        if (currentMeetingId) openMeeting(currentMeetingId);
      }
    }, 3000);
  } catch(e) {
    _mdProgress('<b>🤖 AI Re-analysis</b><br><span style="color:#dc2626">❌ ' + esc(e.message) + '</span>');
    if (btn) { btn.disabled = false; btn.textContent = '🤖 Re-analyze All Items'; }
  }
}

async function renderDrawerLifecycle(body, appData, matter) {
  if(!appData?.id){ body.innerHTML='<p style="color:var(--gray-400)">No appearance.</p>'; return; }
  body.innerHTML='<p style="color:var(--gray-400);font-size:.85rem">Loading lifecycle…</p>';
  try{
    const r = await fetch(`/api/appearance/${appData.id}/timeline`);
    const d = await r.json();
    const events = (d.events||[]);

    // Also get our own appearances for richer display
    const allApps = (_drData?.matter?.appearances || []).slice().sort((a,b) =>
      (a.meeting_date||'').localeCompare(b.meeting_date||'')
    );

    if(!events.length && !allApps.length){
      body.innerHTML = `
        <div style="padding:1rem;background:#fef3c7;border-radius:8px;color:#92400e;font-size:.85rem">
          No lifecycle events yet. Run <b>Backfill URLs + Lifecycle</b> on the
          Saved Meetings page to parse Legistar's legislative history for every
          matter we've ever scraped.
        </div>`;
      return;
    }

    // ── Next Steps computation ──
    const legStatus = (matter?.current_status || '').toLowerCase();
    const control = (matter?.control_body || '').toLowerCase();
    let nextStep = '', nextStepType = 'pending';

    if (/adopted|approved|passed/.test(legStatus) && !/first reading|tentatively/.test(legStatus)) {
      nextStep = 'Adopted'; nextStepType = 'done';
    } else if (/failed|withdrawn/.test(legStatus)) {
      nextStep = (matter?.current_status||'').replace(/^\w/, c=>c.toUpperCase()); nextStepType = 'done';
    } else if (/public hearing|tentatively scheduled/.test(legStatus)) {
      nextStep = 'BCC Public Hearing'; nextStepType = 'bcc';
    } else if (/first reading/.test(legStatus)) {
      nextStep = 'BCC 2nd Reading'; nextStepType = 'bcc';
    } else if (/deferred|continued/.test(legStatus)) {
      const lastBody = allApps.length ? (allApps[allApps.length-1].body_name||'Committee') : 'Committee';
      nextStep = 'Back to ' + lastBody; nextStepType = /bcc|board/i.test(lastBody) ? 'bcc' : 'cmte';
    } else if (/favorably|recommended|forwarded/.test(legStatus)) {
      nextStep = 'BCC'; nextStepType = 'bcc';
    } else if (/amended/.test(legStatus)) {
      nextStep = /board|bcc/.test(control) ? 'BCC (Amended)' : 'Committee (Amended)';
      nextStepType = /board|bcc/.test(control) ? 'bcc' : 'cmte';
    } else if (allApps.length) {
      const lastBn = (allApps[allApps.length-1].body_name||'').toLowerCase();
      if (/committee/.test(lastBn) && !/board of county/.test(lastBn)) {
        nextStep = 'BCC'; nextStepType = 'bcc';
      } else {
        nextStep = 'Pending Final Action'; nextStepType = 'pending';
      }
    }

    const nsColors = {done:'#059669', bcc:'#2563eb', cmte:'#7c3aed', pending:'#d97706'};
    const nsBg     = {done:'#f0fdf4', bcc:'#eff6ff', cmte:'#f5f3ff', pending:'#fffbeb'};
    const nsIcons  = {done:'✓', bcc:'→ BCC', cmte:'→ Committee', pending:'⏳'};
    const nextStepHtml = nextStep ? `
      <div style="background:${nsBg[nextStepType]||'#f1f5f9'};border:2px solid ${nsColors[nextStepType]||'#94a3b8'};
        border-radius:10px;padding:.65rem .85rem;margin-bottom:.75rem;display:flex;align-items:center;gap:.6rem">
        <div style="font-size:1.1rem">${nsIcons[nextStepType]||'→'}</div>
        <div>
          <div style="font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px;font-weight:600">NEXT STEPS</div>
          <div style="font-size:.9rem;font-weight:700;color:${nsColors[nextStepType]||'#475569'}">${esc(nextStep)}</div>
          ${matter?.current_status ? `<div style="font-size:.72rem;color:#64748b;margin-top:.15rem">Leg Status: ${esc(matter.current_status)}</div>` : ''}
        </div>
      </div>` : '';

    // Build change detection map between consecutive appearances
    const changeMap = {};
    for (let i = 1; i < allApps.length; i++) {
      const prev = allApps[i-1], curr = allApps[i];
      const notes = (curr.analyst_working_notes||'');
      const cdChanges = notes.match(/\[(Changes from ([^\]]+) to [^\]]+)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
      const cdNoChanges = notes.match(/\[(No changes from ([^\]]+) to [^\]]+)\]/);
      if (cdChanges) {
        changeMap[curr.id] = { type:'changed', from: cdChanges[2], detail: (cdChanges[3]||'').trim().slice(0,400) };
      } else if (cdNoChanges) {
        changeMap[curr.id] = { type:'nochange', from: cdNoChanges[2] };
      } else if (curr.carried_forward_from_prior) {
        changeMap[curr.id] = { type:'carried', from: prev.body_name + ' ' + (prev.meeting_date||'') };
      }
    }

    // Compute which appearance is "current" by date logic
    const _lcToday = new Date().toISOString().slice(0,10);
    const _lcYesterday = new Date(Date.now() - 86400000).toISOString().slice(0,10);
    const ourEvents = events.filter(e => e.source === 'agendaiq').sort((a,b) => (a.event_date||'').localeCompare(b.event_date||''));
    let _lcCurrentAppId = null;
    for (const oe of ourEvents) {
      if ((oe.event_date||'') > _lcYesterday) { _lcCurrentAppId = oe.appearance_id; break; }
    }
    if (!_lcCurrentAppId && ourEvents.length) _lcCurrentAppId = ourEvents[ourEvents.length-1].appearance_id;

    const rows = events.map(e=>{
      const isOurs = e.source==='agendaiq';
      const color  = isOurs ? '#2563eb' : '#64748b';
      const icon   = isOurs ? '📌' : '•';
      const isCurrent = isOurs && e.appearance_id === _lcCurrentAppId;
      const isPast = isOurs && (e.event_date||'') <= _lcYesterday && !isCurrent;
      const isFuture = isOurs && (e.event_date||'') > _lcToday && !isCurrent;
      const meta = [];
      if(e.committee_item_number) meta.push(`Cmte #${esc(e.committee_item_number)}`);
      if(e.bcc_item_number)       meta.push(`BCC #${esc(e.bcc_item_number)}`);
      if(e.agenda_stage)          meta.push(esc(e.agenda_stage));
      if(e.has_notes)             meta.push('★ notes');

      // Change detection between appearances
      let changeHtml = '';
      const chg = changeMap[e.appearance_id];
      if (chg) {
        if (chg.type === 'changed') {
          changeHtml = `<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:.35rem .55rem;margin-top:.3rem;font-size:.72rem;color:#991b1b">
            <strong>⚠ Changes detected</strong> from ${esc(chg.from)}
            ${chg.detail ? `<div style="margin-top:.2rem;white-space:pre-wrap;color:#7f1d1d">${esc(chg.detail)}</div>` : ''}
          </div>`;
        } else if (chg.type === 'nochange') {
          changeHtml = `<div style="font-size:.72rem;color:#16a34a;margin-top:.2rem">✓ No changes from ${esc(chg.from)}</div>`;
        } else if (chg.type === 'carried') {
          changeHtml = `<div style="font-size:.72rem;color:#92400e;margin-top:.2rem">↩ Carried forward from ${esc(chg.from)}</div>`;
        }
      }

      // Find matching appearance for "Open Appearance" button
      const matchApp = isOurs && e.appearance_id ? allApps.find(a => a.id === e.appearance_id) : null;
      const hasTx = matchApp && (matchApp.transcript_analysis || (matchApp.analyst_working_notes||'').includes('[Meeting Discussion'));
      const hasPdf = matchApp && (matchApp.item_pdf_url || matchApp.item_pdf_local_path);
      const hasNotes = matchApp && (matchApp.analyst_working_notes||'').trim();

      return `<div style="display:flex;gap:.75rem;padding:.65rem 0;border-bottom:1px dashed #e2e8f0;
          ${isCurrent?'background:#eff6ff;margin:0 -.5rem;padding-left:.5rem;padding-right:.5rem;border-radius:6px;border:1px solid #bfdbfe':''}">
        <div style="min-width:90px;color:${color};font-size:.78rem;font-weight:600">${esc(e.event_date||'')}</div>
        <div style="flex:1">
          <div style="font-size:.85rem;color:#1e293b">
            <span style="color:${color}">${icon}</span>
            <b>${esc(e.body_name||'')}</b>
            ${e.body_name && e.action ? ' — ' : ''}${esc(e.action||'')}
            ${isCurrent ? ' <span style="font-size:.68rem;color:#2563eb;font-weight:600">● CURRENT</span>' : ''}
            ${isPast ? ' <span style="font-size:.65rem;color:#94a3b8;font-weight:500">PRIOR</span>' : ''}
            ${isFuture ? ' <span style="font-size:.65rem;color:#7c3aed;font-weight:500">▶ UPCOMING</span>' : ''}
          </div>
          ${meta.length?`<div style="font-size:.72rem;color:#64748b;margin-top:.15rem">${meta.join(' · ')}</div>`:''}
          ${changeHtml}
          ${isOurs && e.appearance_id ? `
            <div style="margin-top:.35rem;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
              <a href="#" onclick="event.preventDefault();openDrawer('${esc(appData.file_number)}',${e.appearance_id})"
                class="btn btn-o btn-xs" style="font-size:.7rem;text-decoration:none">Open Appearance</a>
              ${hasTx ? '<span style="font-size:.68rem;color:#6d28d9">🎙 transcript</span>' : ''}
              ${hasPdf ? `<a href="/api/appearance/${e.appearance_id}/pdf" target="_blank" style="font-size:.68rem;color:#2563eb;text-decoration:none">📄 PDF</a>` : ''}
              ${hasNotes ? '<span style="font-size:.68rem;color:#059669">★ notes</span>' : ''}
            </div>` : ''}
        </div>
      </div>`;
    }).join('');
    body.innerHTML = `
      ${nextStepHtml}
      <div style="font-size:.78rem;color:#64748b;margin-bottom:.5rem">
        Full lifecycle for this matter — <span style="color:#2563eb">📌 blue pins</span> are our agenda appearances,
        <span style="color:#64748b">• gray dots</span> are from Legistar legislative history.
        Click <em>Open Appearance</em> to see that date's analysis, PDF, and transcript.
      </div>
      <div>${rows}</div>`;
  }catch(e){
    body.innerHTML = `<p style="color:#ef4444">Failed to load lifecycle: ${esc(e.message||e)}</p>`;
  }
}

function renderDrawerSummary(body, matter, app, saveBtn) {
  let ai = app?.ai_summary_for_appearance || matter?.latest_ai_summary_part1 || '';
  const wp = app?.watch_points_for_appearance || matter?.latest_watch_points || '';
  const cf = app?.carried_forward_from_prior;

  // Strip "[Carried from ...]" prefix from AI summary for cleaner display
  const carriedPrefixMatch = ai.match(/^\[Carried from ([^\]]+)\]\s*/);
  let carriedFromLabel = '';
  if (carriedPrefixMatch) {
    carriedFromLabel = carriedPrefixMatch[1];
    ai = ai.slice(carriedPrefixMatch[0].length);
  }

  // Parse change detection from analyst working notes for prominent display
  const notes = (app?.analyst_working_notes || '').trim();
  let changeDetectionHtml = '';
  const cdChanges = notes.match(/\[(Changes from ([^\]]+) to BCC version)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
  const cdNoChanges = notes.match(/\[(No changes from ([^\]]+) to BCC version)\]\s*([\s\S]*?)(?=\n\n\[|$)/);
  if (cdChanges) {
    const label = cdChanges[2];
    const detail = (cdChanges[3]||'').trim();
    changeDetectionHtml = `
      <div style="background:#fef2f2;border:2px solid #fca5a5;border-radius:10px;
        padding:.75rem 1rem;margin-bottom:.75rem">
        <div style="font-weight:700;color:#991b1b;font-size:.85rem;margin-bottom:.35rem">
          ⚠ CHANGES DETECTED — ${esc(label)} → BCC
        </div>
        <div style="white-space:pre-wrap;color:#7f1d1d;font-size:.82rem;line-height:1.55">
          ${esc(detail).slice(0,1200)}${detail.length>1200?'…':''}
        </div>
      </div>`;
  } else if (cdNoChanges) {
    const label = cdNoChanges[2];
    changeDetectionHtml = `
      <div style="background:#f0fdf4;border:2px solid #86efac;border-radius:10px;
        padding:.75rem 1rem;margin-bottom:.75rem">
        <div style="font-weight:700;color:#166534;font-size:.85rem">
          ✓ NO CHANGES — ${esc(label)} → BCC
        </div>
        <div style="color:#15803d;font-size:.8rem;margin-top:.2rem">
          No substantive changes detected between the committee and BCC versions of this item.
        </div>
      </div>`;
  } else if (cf && carriedFromLabel) {
    // Item was carried forward but no change detection ran (maybe no PDF available)
    changeDetectionHtml = `
      <div style="background:#fffbeb;border:2px solid #fde68a;border-radius:10px;
        padding:.65rem 1rem;margin-bottom:.75rem">
        <div style="font-weight:700;color:#92400e;font-size:.82rem">
          ↩ Carried from ${esc(carriedFromLabel)}
        </div>
        <div style="color:#78350f;font-size:.78rem;margin-top:.2rem">
          This debrief was carried forward from a prior committee appearance. Change detection was not available (PDF may not have been present at both stages).
        </div>
      </div>`;
  }

  // Gather ALL appearances for this matter (current + prior) so Research
  // Notes on the Part 1 deliverable show the full researcher trail.
  const allApps = (matter?.appearances||[]).slice().sort((a,b) =>
    (a.meeting_date||'').localeCompare(b.meeting_date||'')
  );
  const priorWithNotes = allApps.filter(p =>
    p.id !== (app?.id) && ((p.analyst_working_notes||'').trim() || (p.reviewer_notes||'').trim())
  );

  // Build Meeting Notes section: each entry shows stage + meeting date +
  // who/when it was updated, then the note body. Finalized_brief is NOT
  // included here — that's Deep Research (reference only).
  const researchNotesHtml = allApps.map(p => {
    const isCur = p.id === (app?.id);
    const stage = (p.agenda_stage||'').toLowerCase();
    const stageLabel = stage.includes('committee') ? 'Committee'
                     : stage.includes('bcc') ? 'BCC'
                     : (p.agenda_stage||'Appearance');
    const analyst  = (p.analyst_working_notes||'').trim();
    const reviewer = (p.reviewer_notes||'').trim();
    if (!analyst && !reviewer) return '';
    const whenA = p.analyst_notes_updated_at  || '';
    const whoA  = p.analyst_notes_updated_by  || '';
    const whenR = p.reviewer_notes_updated_at || '';
    const whoR  = p.reviewer_notes_updated_by || '';
    const fmt = d => { if(!d) return ''; const x=new Date(d); return isNaN(x)?d:x.toLocaleString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}); };
    return `
      <div style="border-left:3px solid ${isCur?'var(--blue2)':'var(--gray-300)'};
        padding:.5rem .75rem;margin-bottom:.55rem;background:${isCur?'#f8fbff':'var(--gray-50)'};
        border-radius:0 6px 6px 0">
        <div style="font-size:.72rem;font-weight:600;color:var(--gray-600);text-transform:uppercase;letter-spacing:.4px;margin-bottom:.35rem">
          ${esc(stageLabel)} · ${esc(fmtDate(p.meeting_date)||'?')}
          ${isCur?'<span style="color:var(--blue);margin-left:.35rem">● current</span>':''}
        </div>
        ${analyst ? `
          <div style="margin-bottom:.4rem">
            <div style="font-size:.68rem;color:var(--gray-500)">Analyst${whoA?` — ${esc(whoA)}`:''}${whenA?` · ${esc(fmt(whenA))}`:''}</div>
            <div style="white-space:pre-wrap;font-size:.8rem;color:var(--gray-800);line-height:1.5">${esc(analyst)}</div>
          </div>`:''}
        ${reviewer ? `
          <div>
            <div style="font-size:.68rem;color:var(--gray-500)">Reviewer${whoR?` — ${esc(whoR)}`:''}${whenR?` · ${esc(fmt(whenR))}`:''}</div>
            <div style="white-space:pre-wrap;font-size:.8rem;color:var(--gray-800);line-height:1.5">${esc(reviewer)}</div>
          </div>`:''}
      </div>`;
  }).filter(Boolean).join('');

  body.innerHTML = `
    <div style="background:#eef6ff;border:1px solid #bfdbfe;border-radius:8px;
      padding:.55rem .85rem;margin-bottom:.75rem;font-size:.74rem;color:#1e3a8a">
      <strong>Part 1 — Deliverable.</strong> This is what gets exported: Agenda Debrief, Watch Points, Legislative History, and Meeting Notes from every stage.
    </div>
    ${priorWithNotes.length ? `
      <div class="cf-banner" style="background:#dbeafe;border-color:#93c5fd;color:#1e40af;cursor:pointer"
        onclick="drTab('appearances',document.querySelectorAll('.dtab')[4])">
        ⓘ Prior notes exist — ${priorWithNotes.length} earlier appearance${priorWithNotes.length>1?'s':''} with notes. Scroll down to Meeting Notes or open the Appearances tab.
      </div>` : ''}
    ${changeDetectionHtml}
    <div class="ds">
      <div class="ds-title">
        <span>AGENDA DEBRIEF</span>
        <button class="btn btn-o btn-xs" onclick="toggleEdit('edit-ai')">Edit</button>
      </div>
      <div class="editable-field" id="edit-ai" contenteditable="false">${esc(ai)||'<span style="color:var(--gray-400)">No debrief yet.</span>'}</div>
    </div>
    <div class="ds">
      <div class="ds-title">
        <span>WATCH POINTS</span>
        <button class="btn btn-o btn-xs" onclick="toggleEdit('edit-wp')">Edit</button>
      </div>
      <div class="editable-field" id="edit-wp" contenteditable="false">${esc(wp)||'<span style="color:var(--gray-400)">None.</span>'}</div>
    </div>
    ${renderStatusLadder(matter, app)}
    ${app?.leg_history_summary ? `<div class="ds"><div class="ds-title">LEGISLATIVE HISTORY (AI summary)</div>
      <div class="editable-field" style="cursor:default">${esc(app.leg_history_summary)}</div></div>` : ''}
    ${matter.legislative_notes ? `<div class="ds"><div class="ds-title">LEGISLATIVE NOTES</div>
      <div class="editable-field" style="cursor:default">${esc(matter.legislative_notes)}</div></div>` : ''}
    <div class="ds">
      <div class="ds-title">
        <span>MEETING NOTES</span>
        <span style="font-size:.68rem;color:var(--gray-400);font-weight:400;text-transform:none;letter-spacing:0">
          across all appearances · add new in Notes tab
        </span>
      </div>
      ${researchNotesHtml || '<div style="color:var(--gray-400);font-size:.8rem;font-style:italic">No research notes yet. Add them in the Notes tab — they will appear here with timestamps.</div>'}
    </div>
  `;
  saveBtn.style.display='';
}

// Living "status ladder": derives the legislative-status progression from
// timeline events so users see how the item moved through the system and
// when. Current status (latest action) is highlighted at the top.
function renderStatusLadder(matter, app) {
  const events = (matter?.timeline || []).slice().sort((a,b) =>
    (b.event_date||'').localeCompare(a.event_date||'')
  );
  if (!events.length) {
    return `<div class="ds"><div class="ds-title">
      <span>LEGISLATIVE STATUS</span>
      <span style="font-size:.66rem;color:var(--gray-400);font-weight:400;text-transform:none;letter-spacing:0">
        live — updates every time backfill runs
      </span></div>
      <div style="font-size:.78rem;color:var(--gray-500);font-style:italic">
        No legislative events parsed yet. Run the backfill to pull this item's full history from Legistar.
      </div></div>`;
  }
  const latest = events[0];
  const fmt = d => { if(!d) return ''; const x=new Date(d); return isNaN(x)?d:x.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); };
  const rows = events.map((e,i) => {
    const isCur = i === 0;
    return `
      <div style="display:flex;gap:.55rem;align-items:flex-start;padding:.4rem .55rem;
        background:${isCur?'#eff6ff':'transparent'};border-left:3px solid ${isCur?'var(--blue2)':'var(--gray-200)'};
        border-radius:0 6px 6px 0;margin-bottom:.25rem">
        <div style="min-width:82px;font-size:.72rem;color:var(--gray-600);font-weight:600">
          ${esc(fmt(e.event_date))}
        </div>
        <div style="flex:1;font-size:.78rem;color:var(--gray-800);line-height:1.45">
          <div><strong>${esc(e.action||'—')}</strong>${e.agenda_item?` <span style="font-size:.7rem;color:var(--gray-500)">(#${esc(e.agenda_item)})</span>`:''}</div>
          <div style="font-size:.7rem;color:var(--gray-500)">${esc(e.body_name||'')}</div>
        </div>
        ${isCur?'<span style="font-size:.62rem;background:var(--blue2);color:#fff;padding:.1rem .4rem;border-radius:10px;font-weight:600;height:fit-content">CURRENT</span>':''}
      </div>`;
  }).join('');
  return `
    <div class="ds">
      <div class="ds-title">
        <span>LEGISLATIVE STATUS</span>
        <span style="font-size:.66rem;color:var(--gray-400);font-weight:400;text-transform:none;letter-spacing:0">
          live · ${events.length} event${events.length>1?'s':''} · updates on each backfill
        </span>
      </div>
      <div style="background:var(--gray-50);border:1px solid var(--gray-200);border-radius:8px;padding:.5rem">
        <div style="font-size:.7rem;color:var(--gray-500);margin-bottom:.35rem;text-transform:uppercase;letter-spacing:.4px">
          Current: <strong style="color:var(--gray-800)">${esc(latest.action||'—')}</strong>
          <span style="color:var(--gray-400)">as of ${esc(fmt(latest.event_date))}</span>
        </div>
        ${rows}
      </div>
    </div>`;
}

function toggleEdit(id) {
  const el=document.getElementById(id);
  const isEditing=el.contentEditable==='true';
  el.contentEditable=isEditing?'false':'true';
  if(!isEditing)el.focus();
}

function memberOptions(selectedVal, includeClear=true) {
  const members = (_cfg.team_members||[]).map(m=>m.name);
  const clearOpt = includeClear ? `<option value="">— Unassigned —</option>` : '';
  return clearOpt + members.map(n=>`<option${n===selectedVal?' selected':''}>${esc(n)}</option>`).join('');
}

function _renderTranscriptNotes(notesText) {
  // Extract [Meeting Discussion — ...] blocks from analyst_working_notes
  const regex = /\[Meeting Discussion — ([^\]]+)\]([\s\S]*?)(?=\n\n\[|$)/g;
  const blocks = [];
  let match;
  while ((match = regex.exec(notesText)) !== null) {
    blocks.push({label: match[1], body: match[2].trim()});
  }
  if (!blocks.length) return '';

  const sections = blocks.map(b => {
    // Parse structured fields from the body
    const lines = b.body.split('\n');
    let html = '';
    let inSection = false;

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) { html += '<div style="height:.3rem"></div>'; continue; }

      // Section headers (Sentiment:, Tone:, Speaker Positions:, etc.)
      if (/^(Sentiment|Tone|Speaker Positions|Intent & Implications|Concerns Raised|Public Comment|Vote|Amendments|Video|Speakers):/.test(trimmed)) {
        const [lbl, ...rest] = trimmed.split(':');
        const val = rest.join(':').trim();
        const color = lbl === 'Sentiment' ? (
          /supportive/i.test(val) ? '#15803d' :
          /opposed|contentious/i.test(val) ? '#dc2626' :
          /divided/i.test(val) ? '#d97706' : '#475569'
        ) : '#475569';

        if (lbl === 'Vote') {
          const voteColor = /pass|approv|adopt/i.test(val) ? '#15803d' : /fail|denied|defer/i.test(val) ? '#dc2626' : '#6b7280';
          html += `<div style="margin-top:.35rem;font-weight:700;color:${voteColor}">Vote: ${esc(val)}</div>`;
        } else if (lbl === 'Video') {
          const urlMatch = val.match(/(https:[^\s]+)/);
          if (urlMatch) {
            html += `<div style="margin-top:.3rem"><a href="${esc(urlMatch[1])}" target="_blank" style="font-size:.75rem;color:#2563eb">Watch this discussion in the recording</a></div>`;
          }
        } else if (lbl === 'Speaker Positions' || lbl === 'Intent & Implications' || lbl === 'Concerns Raised') {
          html += `<div style="margin-top:.4rem;font-weight:600;font-size:.75rem;color:#334155">${esc(lbl)}:</div>`;
          inSection = true;
        } else {
          html += `<div style="margin-top:.3rem"><span style="font-weight:600;font-size:.73rem;color:#475569">${esc(lbl)}:</span> <span style="color:${color}">${esc(val)}</span></div>`;
        }
      } else if (trimmed.startsWith('•') || trimmed.startsWith('-') || trimmed.startsWith('Sponsor:') || trimmed.startsWith('Opposition:') || trimmed.startsWith('Looking Ahead:') || trimmed.startsWith('Notable:')) {
        html += `<div style="padding-left:.8rem;font-size:.76rem;color:#475569">${esc(trimmed)}</div>`;
      } else {
        html += `<div style="font-size:.78rem;color:#334155;line-height:1.45">${esc(trimmed)}</div>`;
      }
    }

    return `<div style="margin-bottom:.5rem;padding:.6rem .75rem;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;border-left:3px solid #6366f1">
      <div style="font-size:.72rem;font-weight:700;color:#6366f1;margin-bottom:.3rem">MEETING DISCUSSION — ${esc(b.label)}</div>
      ${html}
    </div>`;
  }).join('');

  return `<hr style="border:none;border-top:1px solid var(--gray-200);margin:1rem 0">
    <div class="ds">
      <div class="ds-title" style="color:#6366f1">MEETING TRANSCRIPT ANALYSIS</div>
      ${sections}
    </div>`;
}

function renderDrawerNotes(body, app) {
  const wn = app?.analyst_working_notes || '';
  const status = app?.workflow_status || 'New';
  const reviewer = app?.reviewer || 'Rolando';
  const isReviewer = currentUser && currentUser === reviewer;
  const isAnalyst = currentUser && currentUser === (app?.assigned_to || '');
  const reviewerNotes = app?.reviewer_notes || '';
  const internalNotes = app?.internal_notes || '';
  const resubComment = app?.resubmission_comment || '';
  const debriefSnapshot = app?.debrief_snapshot_on_submit || '';
  const notesSnapshot = app?.analyst_notes_snapshot_on_submit || '';

  // Workflow state machine — determine what actions are available
  const analystCanEdit = ['New','Assigned','In Progress','Needs Revision'].includes(status);
  const canSubmit = ['In Progress','Needs Revision'].includes(status);
  const isNeedsRevision = status === 'Needs Revision';
  const canAcceptAssignment = status === 'Assigned' && isAnalyst;
  const reviewerCanAct = ['Draft Complete','In Review'].includes(status) && isReviewer;
  const canAcceptReview = status === 'Draft Complete' && isReviewer;
  const isFinalized = status === 'Finalized' || status === 'Archived';

  // Status badge colors — Draft Complete=orange, In Review=blue, Needs Revision=red
  const statusColors = {
    'New':'#64748b','Assigned':'#2563eb','In Progress':'#7c3aed',
    'Draft Complete':'#d97706','In Review':'#2563eb','Needs Revision':'#dc2626',
    'Finalized':'#059669','Archived':'#64748b'
  };
  const statusBg = {
    'New':'#f1f5f9','Assigned':'#eff6ff','In Progress':'#f5f3ff',
    'Draft Complete':'#fffbeb','In Review':'#eff6ff','Needs Revision':'#fef2f2',
    'Finalized':'#f0fdf4','Archived':'#f1f5f9'
  };

  // Change summary: if reviewer is looking and there's a snapshot, compute diff
  let changesSummaryHtml = '';
  if (reviewerCanAct && (debriefSnapshot || notesSnapshot)) {
    const currentDebrief = app?.ai_summary_for_appearance || '';
    const currentNotes = wn;
    const changes = [];
    if (debriefSnapshot && currentDebrief !== debriefSnapshot) {
      changes.push('Agenda Debrief was modified');
    } else if (debriefSnapshot) {
      changes.push('Agenda Debrief: no changes');
    }
    if (notesSnapshot && currentNotes !== notesSnapshot) {
      changes.push('Analyst Notes were modified');
    } else if (notesSnapshot) {
      changes.push('Analyst Notes: no changes');
    }
    if (resubComment) {
      changes.push('Analyst comment: "' + resubComment + '"');
    }
    if (changes.length) {
      changesSummaryHtml = `
      <div style="background:#eff6ff;border:2px solid #93c5fd;border-radius:10px;padding:.65rem .85rem;margin-bottom:.75rem">
        <div style="font-size:.78rem;font-weight:700;color:#1e40af;margin-bottom:.35rem">REVISION CHANGES SUMMARY</div>
        ${changes.map(c => {
          const isModified = c.includes('was modified');
          const isNoChange = c.includes('no changes');
          const color = isModified ? '#dc2626' : (isNoChange ? '#059669' : '#475569');
          const icon = isModified ? '✏' : (isNoChange ? '✓' : '💬');
          return '<div style="font-size:.8rem;color:'+color+';line-height:1.6">'+icon+' '+esc(c)+'</div>';
        }).join('')}
      </div>`;
    }
  }

  body.innerHTML = `
    <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.75rem;padding:.6rem .85rem;
      background:${statusBg[status]||'#f1f5f9'};border:2px solid ${statusColors[status]||'#94a3b8'};border-radius:10px">
      <div style="flex:1">
        <div style="font-size:.82rem;font-weight:700;color:${statusColors[status]||'#475569'}">
          ${esc(status)}
        </div>
        <div style="font-size:.72rem;color:#64748b;margin-top:.15rem">
          ${status==='New' ? 'Waiting for assignment by supervisor' : ''}
          ${status==='Assigned' ? 'Assigned to <b>'+esc(app?.assigned_to||'')+'</b> — accept to begin work' : ''}
          ${status==='In Progress' ? 'Being worked on by <b>'+esc(app?.assigned_to||'')+'</b>' : ''}
          ${status==='Draft Complete' ? 'Submitted for review by <b>'+esc(reviewer)+'</b>' : ''}
          ${status==='In Review' ? '<b>'+esc(reviewer)+'</b> is reviewing this item' : ''}
          ${status==='Needs Revision' ? '<b>'+esc(reviewer)+'</b> requested changes — see feedback below' : ''}
          ${status==='Finalized' ? 'Approved by <b>'+esc(reviewer)+'</b> — ready for export' : ''}
          ${status==='Archived' ? 'This item has been archived' : ''}
        </div>
      </div>
      <div style="display:flex;gap:.4rem;flex-wrap:wrap">
        ${canAcceptAssignment ? `<button class="btn btn-sm" style="background:#2563eb;color:#fff;border:none" onclick="acceptAssignment()">✓ Accept Assignment</button>` : ''}
        ${canSubmit && !isNeedsRevision ? `<button class="btn btn-sm" style="background:#059669;color:#fff;border:none" onclick="submitForReview()">📤 Submit for Review</button>` : ''}
        ${canAcceptReview ? `<button class="btn btn-sm" style="background:#2563eb;color:#fff;border:none" onclick="acceptReview()">📋 Begin Review</button>` : ''}
        ${status==='In Review' && isReviewer ? `
          <button class="btn btn-sm" style="background:#059669;color:#fff;border:none" onclick="reviewerAction('approve')">✓ Approve & Finalize</button>
          <button class="btn btn-sm" style="background:#dc2626;color:#fff;border:none" onclick="reviewerAction('revise')">↩ Request Revision</button>
        ` : ''}
      </div>
    </div>

    ${status === 'Needs Revision' && reviewerNotes ? `
    <div style="background:#fef2f2;border:2px solid #fca5a5;border-radius:10px;padding:.65rem .85rem;margin-bottom:.75rem">
      <div style="font-size:.78rem;font-weight:700;color:#991b1b;margin-bottom:.35rem">⚠ REVISION REQUESTED BY ${esc(reviewer).toUpperCase()}</div>
      <div style="font-size:.8rem;color:#7f1d1d;line-height:1.5;white-space:pre-wrap">${esc(reviewerNotes)}</div>
    </div>` : ''}

    ${isNeedsRevision ? `
    <div style="background:#fffbeb;border:2px solid #fde68a;border-radius:10px;padding:.65rem .85rem;margin-bottom:.75rem">
      <div style="font-size:.72rem;font-weight:600;color:#92400e;margin-bottom:.3rem;text-transform:uppercase;letter-spacing:.3px">YOUR RESPONSE TO REVIEWER</div>
      <textarea id="edit-resubmission-comment"
        style="min-height:80px;font-size:.82rem;line-height:1.55;margin-bottom:.5rem;border-color:#fde68a"
        placeholder="Describe what you changed and why...">${esc(resubComment)}</textarea>
      <button class="btn btn-sm" style="background:#059669;color:#fff;border:none" onclick="resubmitForReview()">📤 Resubmit for Review</button>
    </div>` : ''}

    ${changesSummaryHtml}

    ${reviewerCanAct ? `
    <div style="background:#f0fdf4;border:2px solid #22c55e;border-radius:10px;padding:.75rem 1rem;margin-bottom:.85rem">
      <div style="font-size:.82rem;font-weight:700;color:#166534;margin-bottom:.4rem">📋 REVIEWER PANEL</div>
      <div style="font-size:.74rem;color:#166534;margin-bottom:.6rem">
        Read the analyst notes below. Add feedback if needed, then approve or request revision.
      </div>
      <div style="font-size:.72rem;font-weight:600;color:#475569;margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.3px">REVIEWER COMMENT</div>
      <textarea id="edit-reviewer-notes" style="min-height:100px;font-size:.82rem;line-height:1.55;margin-bottom:.5rem;border-color:#22c55e"
        placeholder="Add your review comments, corrections, or feedback here...">${esc(reviewerNotes)}</textarea>
      <span id="rn-save-msg" style="font-size:.72rem;color:#059669;display:none"></span>
    </div>` : ''}

    <div style="font-size:.72rem;color:var(--gray-400);margin-bottom:.5rem;padding:0 .1rem">
      Assigned to: <strong>${esc(app?.assigned_to||'Unassigned')}</strong>
      ${app?.due_date ? ` · Due: <strong>${esc(app.due_date)}</strong>` : ''}
      · Reviewer: <strong>${esc(reviewer)}</strong>
    </div>
    <hr style="border:none;border-top:1px solid var(--gray-200);margin:.5rem 0 .75rem">
    <div class="ds">
      <div class="ds-title">
        <span>ANALYST NOTES</span>
        ${['Draft Complete','In Review'].includes(status) ? '<span style="font-size:.68rem;color:#d97706;font-weight:400;text-transform:none;letter-spacing:0">✓ Submitted</span>' : ''}
        ${status==='Needs Revision' ? '<span style="font-size:.68rem;color:#dc2626;font-weight:400;text-transform:none;letter-spacing:0">⚠ Revision requested</span>' : ''}
        ${isFinalized ? '<span style="font-size:.68rem;color:#059669;font-weight:400;text-transform:none;letter-spacing:0">✓ Finalized</span>' : ''}
      </div>
      <textarea id="edit-working-notes"
        style="min-height:200px;font-size:.82rem;line-height:1.55;margin-bottom:.5rem${!analystCanEdit?';background:#f8fafc;cursor:default':''}"
        ${!analystCanEdit ? 'readonly' : ''}
        placeholder="Write your debrief analysis, observations, and recommendations here...">${esc(wn)}</textarea>
      ${analystCanEdit ? `
      <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">
        <button class="btn btn-p btn-sm" onclick="saveFullNotes('working')">Save Draft</button>
        <button class="btn btn-s btn-sm" onclick="appendNotesToDebrief()">+ Append to Debrief</button>
        <span id="wn-save-msg" style="font-size:.72rem;color:#059669;display:none"></span>
      </div>` : ''}
    </div>
    <hr style="border:none;border-top:1px solid var(--gray-200);margin:.75rem 0">
    <div class="ds">
      <div class="ds-title">INTERNAL NOTES <span style="font-size:.65rem;color:#94a3b8;font-weight:400;text-transform:none;letter-spacing:0">(private — not shared in review)</span></div>
      <textarea id="edit-internal-notes" rows="4"
        style="min-height:80px;font-size:.82rem;line-height:1.55;margin-bottom:.5rem"
        placeholder="Private scratch pad — only you can see these notes...">${esc(internalNotes)}</textarea>
      <div style="display:flex;gap:.4rem;align-items:center">
        <button class="btn btn-o btn-sm" onclick="saveInternalNotes()">Save Note</button>
        <span id="in-save-msg" style="font-size:.72rem;color:#059669;display:none"></span>
      </div>
    </div>

    ${reviewerNotes && !reviewerCanAct && status !== 'Needs Revision' ? `
    <hr style="border:none;border-top:1px solid var(--gray-200);margin:.75rem 0">
    <div class="ds">
      <div class="ds-title" style="color:#059669">REVIEWER FEEDBACK</div>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:.55rem .75rem;font-size:.8rem;color:#166534;line-height:1.5;white-space:pre-wrap">${esc(reviewerNotes)}</div>
    </div>` : ''}
  `;
}

async function acceptAssignment() {
  if (!currentAppId) return;
  await fetch('/api/appearance/' + currentAppId + '/workflow', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status: 'In Progress', changed_by: currentUser})
  });
  toast('Assignment accepted — status changed to In Progress', 'ok');
  const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
  _drData.appData = ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
  loadDashboard();
}

async function submitForReview() {
  if (!currentAppId) return;
  const val = document.getElementById('edit-working-notes')?.value || '';
  // Save the analyst notes
  await fetch('/api/appearance/' + currentAppId + '/notes', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({analyst_working_notes: val, replace: true, changed_by: currentUser})
  });
  // Snapshot current state and change status
  await fetch('/api/appearance/' + currentAppId + '/submit-for-review', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({changed_by: currentUser})
  });
  toast('Submitted for review — ' + (_drData?.appData?.reviewer||'Rolando') + ' will be notified', 'ok');
  const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
  _drData.appData = ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
  loadDashboard();
}

async function acceptReview() {
  if (!currentAppId) return;
  await fetch('/api/appearance/' + currentAppId + '/workflow', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status: 'In Review', changed_by: currentUser})
  });
  toast('Review started — status changed to In Review', 'ok');
  const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
  _drData.appData = ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
}

async function reviewerAction(action) {
  if (!currentAppId) return;
  const reviewerNotes = document.getElementById('edit-reviewer-notes')?.value || '';
  await fetch('/api/appearance/' + currentAppId + '/notes', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reviewer_notes: reviewerNotes, replace: true, changed_by: currentUser})
  });

  if (action === 'approve') {
    await fetch('/api/appearance/' + currentAppId + '/workflow', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({status: 'Finalized', changed_by: currentUser})
    });
    toast('Item approved and finalized', 'ok');
  } else if (action === 'revise') {
    await fetch('/api/appearance/' + currentAppId + '/workflow', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({status: 'Needs Revision', changed_by: currentUser})
    });
    toast('Sent back for revision — analyst will be notified', 'ok');
  }

  const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
  _drData.appData = ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
  loadDashboard(); loadWorkflow();
}

// ─── Deep Research tab (formerly "Finalized Brief") ───────────
// This is reference-only material — NOT included in exports.
function renderDrawerDeepResearch(body, app) {
  let fb = app?.finalized_brief || '';
  const whenF = app?.finalized_brief_updated_at || '';
  const whoF  = app?.finalized_brief_updated_by || '';
  const drCarried = fb.match(/^\[Carried from ([^\]]+)\]\s*/);
  let drCarriedLabel = '';
  if (drCarried) { drCarriedLabel = drCarried[1]; fb = fb.slice(drCarried[0].length); }
  const fmt = d => { if(!d) return ''; const x=new Date(d); return isNaN(x)?d:x.toLocaleString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}); };

  // Gather transcript notes from all appearances
  const allApps = (_drData?.matter?.appearances || []).slice().sort((a,b) =>
    (b.meeting_date||'').localeCompare(a.meeting_date||'')
  );
  let transcriptSections = '';
  for (const a of allApps) {
    let txText = a.transcript_analysis || '';
    if (!txText) {
      const txMatch = (a.analyst_working_notes||'').match(/(\[Meeting Discussion[\s\S]*?)(?=\n\n\[(?!Meeting)|$)/);
      if (txMatch) txText = txMatch[1];
    }
    if (txText) {
      const bodyLabel = a.body_name || a.agenda_stage || 'Meeting';
      const dateLabel = a.meeting_date ? new Date(a.meeting_date).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';
      const isCurrent = a.id === app?.id;
      transcriptSections += `
        <div style="background:${isCurrent?'#eff6ff':'#f8fafc'};border:1px solid ${isCurrent?'#bfdbfe':'#e2e8f0'};
          border-radius:8px;padding:.6rem .85rem;margin-bottom:.6rem">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.35rem">
            <div style="font-size:.78rem;font-weight:600;color:#1e293b">
              🎙 ${esc(bodyLabel)} — ${esc(dateLabel)}${isCurrent?' <span style="color:#2563eb;font-size:.68rem">● CURRENT</span>':''}
            </div>
            <button class="btn btn-o btn-xs" onclick="appendTranscriptToNotes(${a.id})" title="Copy this transcript to your analyst notes">
              → Copy to Notes
            </button>
          </div>
          <div style="font-size:.78rem;color:var(--gray-600);line-height:1.5;white-space:pre-wrap;max-height:200px;overflow-y:auto">${esc(txText.trim())}</div>
        </div>`;
    }
  }

  body.innerHTML = `
    <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:8px;
      padding:.6rem .85rem;margin-bottom:.85rem;font-size:.74rem;color:#5b21b6;line-height:1.55">
      <strong>Deep Research — reference material.</strong> Part 2 notes (background research, memos, precedents)
      and meeting transcript analyses are gathered here. Use <em>Copy to Notes</em> to pull content into your analyst notes.
      <span style="display:block;margin-top:.2rem;color:#6d28d9;font-weight:600">⚠ This content is NOT exported in the Part 1 deliverable.</span>
    </div>
    ${drCarriedLabel ? `
    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
      padding:.5rem .75rem;margin-bottom:.75rem;font-size:.74rem;color:#92400e">
      ↩ Part 2 notes carried forward from <strong>${esc(drCarriedLabel)}</strong>. You can edit them for this appearance.
    </div>` : ''}

    ${transcriptSections ? `
    <div class="ds">
      <div class="ds-title">MEETING TRANSCRIPT ANALYSES</div>
      ${transcriptSections}
    </div>
    <hr style="border:none;border-top:1px solid var(--gray-200);margin:.75rem 0">
    ` : ''}

    <div class="ds">
      <div class="ds-title">
        <span>PART 2 — DEEP RESEARCH NOTES</span>
        ${whenF ? `<span style="font-size:.68rem;color:var(--gray-400);font-weight:400;text-transform:none;letter-spacing:0">
          last updated${whoF?` by ${esc(whoF)}`:''} · ${esc(fmt(whenF))}</span>`:''}
      </div>
      <textarea id="deep-research-field" style="min-height:220px;font-size:.82rem;line-height:1.55"
        placeholder="Paste or write deeper background research, source notes, prior memos, precedents, etc.">${esc(fb)}</textarea>
      <div style="margin-top:.55rem;display:flex;gap:.5rem;align-items:center">
        <button class="btn btn-p btn-sm" onclick="saveDeepResearch()">Save Part 2 Notes</button>
        <span id="deep-save-msg" style="font-size:.72rem;color:var(--green);display:none">✓ Saved</span>
      </div>
    </div>
  `;
}

function appendTranscriptToNotes(appId) {
  // Find the transcript text for this appearance
  const allApps = (_drData?.matter?.appearances || []);
  const a = allApps.find(x => x.id === appId);
  if (!a) { toast('Appearance not found', 'err'); return; }
  let txText = a.transcript_analysis || '';
  if (!txText) {
    const txMatch = (a.analyst_working_notes||'').match(/(\[Meeting Discussion[\s\S]*?)(?=\n\n\[(?!Meeting)|$)/);
    if (txMatch) txText = txMatch[1];
  }
  if (!txText) { toast('No transcript to copy', 'err'); return; }
  const bodyLabel = a.body_name || 'Meeting';
  const dateLabel = a.meeting_date || '';
  const block = `\n\n[Transcript from ${bodyLabel} ${dateLabel}]\n${txText.trim()}\n`;
  // If we're on the Notes tab, append to the textarea
  const notesField = document.getElementById('edit-working-notes');
  if (notesField) {
    notesField.value += block;
    toast('Transcript copied to notes field — remember to save', 'ok');
  } else {
    // Switch to notes tab and append
    drTab('notes', document.querySelectorAll('.dtab')[1]);
    setTimeout(() => {
      const nf = document.getElementById('edit-working-notes');
      if (nf) { nf.value += block; toast('Transcript copied to notes — remember to save', 'ok'); }
    }, 100);
  }
}

async function saveDeepResearch() {
  if(!currentAppId) return;
  const val = document.getElementById('deep-research-field').value;
  const r = await fetch(`/api/appearance/${currentAppId}/notes`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ finalized_brief: val, changed_by: currentUser, replace: true })
  });
  if (r.ok) {
    const m = document.getElementById('deep-save-msg');
    if(m){ m.style.display=''; setTimeout(()=>m.style.display='none',1500); }
    const ar = await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
    _drData.appData = ar.appearance;
  }
}

// ── AI Chat tab ─────────────────────────────────────────────
let _chatLoading = false;
async function renderDrawerChat(body, app) {
  if(!app?.id){ body.innerHTML='<p style="color:var(--gray-400)">No appearance.</p>'; return; }
  body.innerHTML = `
    <div class="chat-wrap">
      <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;
        padding:.5rem .75rem;margin-bottom:.5rem;font-size:.74rem;color:#1e40af;line-height:1.45">
        <strong>Private AI Chat</strong> — Ask questions about this item. Your chat history is private to you.
        You can append any AI response to your Working Notes or Part 1 summary.
      </div>
      <div class="chat-msgs" id="chat-msgs"></div>
      <div class="chat-ws-toggle">
        <label style="display:flex;align-items:center;gap:.3rem;cursor:pointer">
          <input type="checkbox" id="chat-ws" /> Enable web search
        </label>
        <span style="color:var(--gray-400)">(AI will search the web for current info)</span>
      </div>
      <div class="chat-input-row">
        <textarea id="chat-input" placeholder="Ask about this item..." rows="1"
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChatMsg();}"></textarea>
        <button class="chat-send-btn" id="chat-send-btn" onclick="sendChatMsg()">Send</button>
      </div>
    </div>`;
  // Load history
  try {
    const r = await fetch(`/api/chat/${app.id}/messages`);
    const d = await r.json();
    const container = document.getElementById('chat-msgs');
    (d.messages||[]).forEach(m => _appendChatBubble(container, m));
    container.scrollTop = container.scrollHeight;
  } catch(e) {}
}

function _appendChatBubble(container, msg) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + msg.role;
  let html = esc(msg.content);
  if(msg.role === 'assistant') {
    const appendedNotes = msg.appended_to === 'notes';
    const appendedDeep = msg.appended_to === 'deep_research';
    html += `<div class="chat-actions">
      <button onclick="appendChatToTarget(${msg.id},'notes',this)" ${appendedNotes?'class="appended" disabled':''}>
        ${appendedNotes ? '✓ Added to Analyst Notes' : '+ Analyst Notes'}</button>
      <button onclick="appendChatToTarget(${msg.id},'deep_research',this)" ${appendedDeep?'class="appended" disabled':''}>
        ${appendedDeep ? '✓ Saved to Deep Research' : '+ Deep Research'}</button>
    </div>`;
  }
  div.innerHTML = html;
  container.appendChild(div);
}

async function sendChatMsg() {
  if(_chatLoading || !currentAppId) return;
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if(!msg) return;
  const ws = document.getElementById('chat-ws')?.checked || false;
  const container = document.getElementById('chat-msgs');
  const btn = document.getElementById('chat-send-btn');

  // Show user bubble immediately
  _appendChatBubble(container, {role:'user', content:msg});
  input.value = '';
  container.scrollTop = container.scrollHeight;

  // Show typing indicator
  const typing = document.createElement('div');
  typing.className = 'chat-msg assistant';
  typing.style.opacity = '.5';
  typing.textContent = 'Thinking...';
  container.appendChild(typing);
  container.scrollTop = container.scrollHeight;

  _chatLoading = true;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/chat/${currentAppId}/send`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg, web_search: ws })
    });
    const d = await r.json();
    typing.remove();
    if(d.error) {
      _appendChatBubble(container, {role:'assistant', content:'Error: '+d.error});
    } else {
      _appendChatBubble(container, d);
    }
    container.scrollTop = container.scrollHeight;
  } catch(e) {
    typing.remove();
    _appendChatBubble(container, {role:'assistant', content:'Connection error.'});
  }
  _chatLoading = false;
  btn.disabled = false;
  input.focus();
}

async function appendChatToTarget(msgId, target, btnEl) {
  if(!currentAppId) return;
  try {
    const r = await fetch(`/api/chat/${currentAppId}/append`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message_id: msgId, target: target })
    });
    const d = await r.json();
    if(d.ok) {
      const labels = {notes:'✓ Added to Analyst Notes', deep_research:'✓ Saved to Deep Research'};
      btnEl.textContent = labels[target] || '✓ Added';
      btnEl.classList.add('appended');
      btnEl.disabled = true;
      // Refresh drawer data
      const ar = await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
      _drData.appData = ar.appearance;
    }
  } catch(e) {}
}

function showReviewQueue() {
  if (!currentUser) {
    alert('To see your review queue, first select your name from the "Acting as…" dropdown in the top-right header.');
    return;
  }
  document.getElementById('wf-f-status').value = 'Draft Complete';
  // We'll use a special convention: assigned filter "reviewer:me"
  const wfAssigned = document.getElementById('wf-f-assigned');
  // Check if reviewer option exists, add it if not
  let revOpt = wfAssigned.querySelector('option[value="reviewer:me"]');
  if (!revOpt) {
    revOpt = document.createElement('option');
    revOpt.value = 'reviewer:me';
    revOpt.textContent = 'My Reviews';
    wfAssigned.appendChild(revOpt);
  }
  wfAssigned.value = 'reviewer:me';
  loadWorkflow();
}

async function saveWorkflowFromDrawer() {
  if(!currentAppId)return;
  const payload = { changed_by: currentUser };
  const assignedEl = document.getElementById('nw-assigned');
  const priorityEl = document.getElementById('nw-priority');
  const dueEl = document.getElementById('nw-due');
  if(assignedEl) payload.assigned_to = assignedEl.value;
  if(priorityEl) payload.priority = priorityEl.value;
  if(dueEl) payload.due_date = dueEl.value;
  // Reviewer is always Rolando — set automatically
  payload.reviewer = 'Rolando';
  await fetch(`/api/appearance/${currentAppId}/workflow`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)
  });
  toast('Saved','ok');
  loadDashboard(); loadWorkflow();
  const ar=await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
  _drData.appData=ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
}

async function saveFullNotes(type) {
  if(!currentAppId)return;
  const field = type==='working' ? 'edit-working-notes' : 'edit-reviewer-notes';
  const msgEl = type==='working' ? 'wn-save-msg' : 'rn-save-msg';
  const val = document.getElementById(field)?.value || '';
  const key = type==='working' ? 'analyst_working_notes' : 'reviewer_notes';
  try {
    const r = await fetch(`/api/appearance/${currentAppId}/notes`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[key]:val,replace:true,changed_by:currentUser})
    });
    if (!r.ok) {
      const err = await r.json().catch(()=>({}));
      toast('Save failed: ' + (err.error || 'Server error ' + r.status), 'err');
      return;
    }

    // Auto-advance status: New/Assigned → In Progress when analyst saves notes
    if (type === 'working' && val.trim()) {
      const curStatus = _drData?.appData?.workflow_status || '';
      if (curStatus === 'New' || curStatus === 'Assigned') {
        await fetch(`/api/appearance/${currentAppId}/workflow`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({status: 'In Progress', changed_by: currentUser})
        });
      }
    }

    const m=document.getElementById(msgEl);
    if(m){m.textContent='✓ Saved';m.style.display='';setTimeout(()=>m.style.display='none',2500);}
    const ar=await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
    _drData.appData=ar.appearance;
  } catch(e) {
    toast('Save failed: ' + (e.message||e), 'err');
  }
}

async function appendNotesToDebrief() {
  if (!currentAppId) return;
  const val = document.getElementById('edit-working-notes')?.value?.trim() || '';
  if (!val) { toast('Nothing to append — write your notes first', 'err'); return; }
  try {
    // First save the notes
    const r1 = await fetch(`/api/appearance/${currentAppId}/notes`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({analyst_working_notes:val, replace:true, changed_by:currentUser})
    });
    if (!r1.ok) { toast('Save failed — check disk space', 'err'); return; }

    // Now append to the AI summary (agenda debrief)
    const ar = await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
    const existing = ar.appearance?.ai_summary_for_appearance || '';
    const separator = existing ? '\\n\\n--- Analyst Notes (appended by ' + (currentUser||'analyst') + ') ---\\n' : '';
    const newSummary = existing + separator + val;
    const r2 = await fetch(`/api/appearance/${currentAppId}/ai`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({summary:newSummary, changed_by:currentUser})
    });
    if (!r2.ok) { toast('Append failed — check disk space', 'err'); return; }

    toast('Notes saved and appended to Agenda Debrief', 'ok');
    _drData.appData = (await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json())).appearance;
    // Clear the textarea after successful append
    const el = document.getElementById('edit-working-notes');
    if (el) el.value = '';
  } catch(e) {
    toast('Error: ' + (e.message||e), 'err');
  }
}

async function addNote(type) {
  if(!currentAppId)return;
  const el=document.getElementById(`new-${type==='working'?'working':'reviewer'}-note`);
  const note=el.value.trim();
  if(!note)return;
  await fetch(`/api/appearance/${currentAppId}/notes`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      [type==='working'?'working_notes':'reviewer_notes']:note,
      changed_by:currentUser
    })
  });
  el.value='';
  showSaveMsg();
  const ar=await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
  _drData.appData=ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'),_drData.appData);
}

async function saveInternalNotes() {
  if(!currentAppId)return;
  const val = document.getElementById('edit-internal-notes')?.value || '';
  try {
    const r = await fetch(`/api/appearance/${currentAppId}/notes`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({internal_notes:val, replace:true, changed_by:currentUser})
    });
    if (!r.ok) { toast('Save failed — check disk space', 'err'); return; }
    const m=document.getElementById('in-save-msg');
    if(m){m.textContent='✓ Saved';m.style.display='';setTimeout(()=>m.style.display='none',2500);}
    const ar=await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
    _drData.appData=ar.appearance;
  } catch(e) { toast('Save failed: ' + (e.message||e), 'err'); }
}

async function resubmitForReview() {
  if (!currentAppId) return;
  const comment = document.getElementById('edit-resubmission-comment')?.value?.trim() || '';
  const val = document.getElementById('edit-working-notes')?.value || '';
  // Save the analyst notes
  await fetch('/api/appearance/' + currentAppId + '/notes', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({analyst_working_notes: val, replace: true, changed_by: currentUser})
  });
  // Save resubmission comment and snapshot, then change status
  await fetch('/api/appearance/' + currentAppId + '/resubmit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({resubmission_comment: comment, changed_by: currentUser})
  });
  toast('Resubmitted for review — ' + (_drData?.appData?.reviewer||'Rolando') + ' will be notified', 'ok');
  const ar = await fetch('/api/appearance/' + currentAppId).then(r=>r.json());
  _drData.appData = ar.appearance;
  renderDrawerNotes(document.getElementById('dr-body'), ar.appearance);
  loadDashboard(); loadWorkflow();
}

async function reanalyzeAllAppearances() {
  if (!currentFileNum) { toast('No item selected','err'); return; }
  const matter = _drData?.matter;
  if (!matter?.appearances?.length) { toast('No appearances found','err'); return; }
  const prog = document.getElementById('bf-item-progress');
  if(prog){ prog.style.display=''; prog.textContent='🤖 Queuing AI analysis for ALL appearances…'; }
  const btn = document.getElementById('bf-ai-all-btn');
  if(btn){ btn.disabled=true; btn.textContent='Analyzing…'; }
  try {
    const r = await fetch('/api/matter/' + matter.id + '/reanalyze-all', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      if(prog) prog.textContent='AI analysis started for ' + (d.count||'all') + ' appearances — this may take a few minutes…';
      // Poll for completion
      const startTime = Date.now();
      const pollAll = setInterval(async () => {
        try {
          const ar = await fetch('/api/matter/' + matter.id + '/reanalyze-progress').then(r=>r.json());
          if(prog) prog.textContent = '🤖 ' + (ar.completed||0) + '/' + (ar.total||0) + ' appearances analyzed…';
          if (ar.done) {
            clearInterval(pollAll);
            if(prog){ prog.textContent='✓ All appearances analyzed'; setTimeout(()=>prog.style.display='none', 3000); }
            if(btn){ btn.disabled=false; btn.textContent='🤖 Re-analyze ALL Appearances'; }
            if(currentFileNum) openDrawer(currentFileNum, currentAppId);
          } else if (Date.now() - startTime > 300000) {
            clearInterval(pollAll);
            if(prog) prog.textContent='⏳ Analysis still running — check back shortly';
            if(btn){ btn.disabled=false; btn.textContent='🤖 Re-analyze ALL Appearances'; }
          }
        } catch(e){ /* keep polling */ }
      }, 5000);
    } else {
      if(prog) prog.textContent='❌ ' + (d.error||'Failed');
      if(btn){ btn.disabled=false; btn.textContent='🤖 Re-analyze ALL Appearances'; }
    }
  } catch(e) {
    if(prog) prog.textContent='❌ Failed: ' + (e.message||e);
    if(btn){ btn.disabled=false; btn.textContent='🤖 Re-analyze ALL Appearances'; }
  }
}

async function renderDrawerHistory(body, app) {
  if(!app?.id){body.innerHTML='<p style="color:var(--gray-400)">No history.</p>';return;}
  body.innerHTML='<div style="color:var(--gray-400);font-size:.82rem">Loading…</div>';
  const r=await fetch(`/api/appearance/${app.id}/history`);
  const history=await r.json();
  if(!history.length){
    body.innerHTML='<p style="color:var(--gray-400);font-size:.82rem">No history yet.</p>';
    return;
  }
  const dotClass={status_change:'status',assigned:'assign',reviewer_set:'assign',
    working_note_added:'note',reviewer_note_added:'note',brief_finalized:'status',
    due_date_set:'status',ai_summary_edited:'note',priority_set:'status',export:'export'};
  const labels={status_change:'Status changed',assigned:'Assigned',reviewer_set:'Reviewer set',
    working_note_added:'Working note added',reviewer_note_added:'Reviewer note added',
    brief_finalized:'Brief finalized',due_date_set:'Due date set',
    ai_summary_edited:'AI summary edited',priority_set:'Priority set',export:'Files exported'};

  body.innerHTML=`<div class="timeline">
    ${history.map(h=>{
      const dt=new Date(h.changed_at);
      const ts=isNaN(dt)?h.changed_at:dt.toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      const by=h.changed_by&&h.changed_by!=='system'?` by <strong>${esc(h.changed_by)}</strong>`:'';
      const detail=h.old_value&&h.new_value
        ?`${esc(h.old_value)} → <strong>${esc(h.new_value)}</strong>`
        :(h.note?`<em>${esc(h.note.slice(0,120))}</em>`:'');
      return `<div class="tl-item">
        <div class="tl-dot ${dotClass[h.action]||''}"></div>
        <div class="tl-time">${ts}${by}</div>
        <div class="tl-action">${labels[h.action]||h.action}</div>
        ${detail?`<div class="tl-detail">${detail}</div>`:''}
      </div>`;
    }).join('')}
  </div>`;
}

function renderDrawerApps(body, matter) {
  const apps = (matter.appearances || []).slice().sort((a,b) =>
    (b.meeting_date||'').localeCompare(a.meeting_date||''));

  if(!apps.length){body.innerHTML='<p style="color:var(--gray-400);font-size:.82rem">No appearances.</p>';return;}

  // Legislative identity header
  const head = `
    <div style="background:var(--blue-lt);border:1px solid var(--blue-mid);border-radius:8px;
      padding:.75rem .95rem;margin-bottom:.9rem;font-size:.78rem">
      <div style="font-weight:600;color:var(--blue);margin-bottom:.3rem">
        File #${esc(matter.file_number||'')} — ${esc(matter.short_title||'')}
      </div>
      <div style="color:var(--gray-600);display:flex;flex-wrap:wrap;gap:.4rem 1.1rem">
        ${matter.file_type ? `<span><strong>Type:</strong> ${esc(matter.file_type)}</span>`:''}
        ${matter.current_status ? `<span><strong>Leg Status:</strong> ${esc(matter.current_status)}</span>`:''}
        ${matter.sponsor ? `<span><strong>Requester:</strong> ${esc(matter.sponsor)}</span>`:''}
        ${matter.department ? `<span><strong>Dept:</strong> ${esc(matter.department)}</span>`:''}
        ${matter.control_body ? `<span><strong>Control:</strong> ${esc(matter.control_body)}</span>`:''}
      </div>
      ${matter.legislative_notes ? `
        <div style="margin-top:.55rem;font-size:.76rem;color:var(--gray-600);
          border-top:1px dashed var(--blue-mid);padding-top:.45rem;line-height:1.5">
          <strong>Legislative Notes:</strong> ${esc(matter.legislative_notes).slice(0,400)}
        </div>`:''}
    </div>
    <div style="font-size:.72rem;color:var(--gray-400);text-transform:uppercase;
      letter-spacing:.5px;font-weight:600;margin:.4rem 0 .6rem">
      Case History · ${apps.length} appearance${apps.length>1?'s':''}
    </div>`;

  body.innerHTML = head + apps.map(a => {
    const isCurrent = a.id === currentAppId;
    const isSupp = /supplement/i.test(a.agenda_stage||'');
    const analyst = (a.analyst_working_notes||'').trim();
    const reviewer = (a.reviewer_notes||'').trim();
    const aiSum  = (a.ai_summary_for_appearance||'').trim();
    const watch  = (a.watch_points_for_appearance||'').trim();
    const hasAnyNotes = analyst||reviewer;

    const pill = (txt,bg,fg,title='') =>
      `<span class="badge" title="${esc(title)}" style="background:${bg};color:${fg}">${txt}</span>`;

    const pills = [
      isCurrent ? pill('● CURRENT','#dbeafe','#1e40af') : '',
      a.carried_forward_from_prior ? pill('↩ Carried Forward','#fef3c7','#92400e','Notes carried from prior appearance') : '',
      isSupp ? pill('+ Supplement','#ede9fe','#5b21b6') : '',
      badge(a.workflow_status||'New'),
    ].filter(Boolean).join(' ');

    // Separate change-detection notes from regular analyst notes
    // Supports both old format "[Changes from committee to BCC version]"
    // and new format "[Changes from Body Name 2026-04-13 to BCC version]"
    let changeBlock = '';
    let changeLabel = '';
    let noChanges = false;
    let regularNotes = analyst;
    // Check for changes block (with dynamic prior label)
    const chMatch = analyst.match(/\[(Changes from [^\]]+to BCC version)\]/);
    if (chMatch) {
      const fullTag = '[' + chMatch[1] + ']';
      changeLabel = chMatch[1].replace('Changes from ', '').replace(' to BCC version', '');
      changeBlock = analyst.slice(analyst.indexOf(fullTag) + fullTag.length).split('\n\n[')[0].trim();
      regularNotes = (analyst.slice(0, analyst.indexOf(fullTag)) + analyst.slice(analyst.indexOf(fullTag)).replace(/\[Changes from [^\]]+to BCC version\][^\[]*/, '')).trim();
    }
    // Check for "No changes" block
    const ncMatch = regularNotes.match(/\[(No changes from [^\]]+to BCC version)\]/);
    if (ncMatch) {
      noChanges = true;
      const ncTag = '[' + ncMatch[1] + ']';
      changeLabel = ncMatch[1].replace('No changes from ', '').replace(' to BCC version', '');
      changeBlock = regularNotes.slice(regularNotes.indexOf(ncTag) + ncTag.length).split('\n\n[')[0].trim();
      regularNotes = (regularNotes.slice(0, regularNotes.indexOf(ncTag)) + regularNotes.slice(regularNotes.indexOf(ncTag)).replace(/\[No changes from [^\]]+to BCC version\][^\[]*/, '')).trim();
    }
    // Also separate carried-forward notes (supports dynamic labels)
    let carriedBlock = '';
    let carriedLabel = '';
    const cfMatch = regularNotes.match(/\[(Carried from [^\]]+)\]/);
    if (cfMatch) {
      const cfTag = '[' + cfMatch[1] + ']';
      carriedLabel = cfMatch[1].replace('Carried from ', '');
      carriedBlock = regularNotes.slice(regularNotes.indexOf(cfTag) + cfTag.length).split('\n\n[')[0].trim();
      regularNotes = (regularNotes.slice(0, regularNotes.indexOf(cfTag)) + regularNotes.slice(regularNotes.indexOf(cfTag)).replace(/\[Carried from [^\]]+\][^\[]*/, '')).trim();
    }
    const hasContent = regularNotes||carriedBlock||changeBlock||noChanges||reviewer;

    const notesSection = hasContent ? `
      <div style="background:var(--gray-50);border:1px solid var(--gray-200);border-radius:7px;
        padding:.55rem .75rem;margin-top:.55rem;font-size:.78rem;line-height:1.55">
        ${(changeBlock && !noChanges) ? `
          <div style="margin-bottom:.5rem;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:.5rem .65rem">
            <div style="font-weight:600;color:#991b1b;font-size:.7rem;letter-spacing:.4px">⚠ CHANGES DETECTED${changeLabel ? ' ('+esc(changeLabel)+' → BCC)' : ' (COMMITTEE → BCC)'}</div>
            <div style="white-space:pre-wrap;color:#7f1d1d;margin-top:.25rem">${esc(changeBlock).slice(0,600)}${changeBlock.length>600?'…':''}</div>
          </div>`:''}
        ${noChanges ? `
          <div style="margin-bottom:.5rem;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:.5rem .65rem">
            <div style="font-weight:600;color:#166534;font-size:.7rem;letter-spacing:.4px">✓ NO CHANGES${changeLabel ? ' ('+esc(changeLabel)+' → BCC)' : ' (COMMITTEE → BCC)'}</div>
            <div style="color:#15803d;margin-top:.2rem;font-size:.76rem">No substantive changes detected between committee and BCC versions. The item appears identical.</div>
          </div>`:''}
        ${carriedBlock ? `
          <div style="margin-bottom:.5rem;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:.5rem .65rem">
            <div style="font-weight:600;color:#92400e;font-size:.7rem;letter-spacing:.4px">↩ NOTES FROM ${carriedLabel ? esc(carriedLabel).toUpperCase() : 'COMMITTEE'}</div>
            <div style="white-space:pre-wrap;color:#78350f;margin-top:.25rem">${esc(carriedBlock).slice(0,600)}${carriedBlock.length>600?'…':''}</div>
          </div>`:''}
        ${regularNotes ? `
          <div style="margin-bottom:.4rem">
            <div style="font-weight:600;color:#056f3a;font-size:.7rem;letter-spacing:.4px">📝 ANALYST NOTES (THIS APPEARANCE)</div>
            <div style="white-space:pre-wrap;color:var(--gray-800)">${esc(regularNotes).slice(0,800)}${regularNotes.length>800?'…':''}</div>
          </div>`:''}
        ${reviewer ? `
          <div style="margin-bottom:.4rem">
            <div style="font-weight:600;color:#7a1e1e;font-size:.7rem;letter-spacing:.4px">👁 REVIEWER NOTES</div>
            <div style="white-space:pre-wrap;color:var(--gray-800)">${esc(reviewer).slice(0,800)}${reviewer.length>800?'…':''}</div>
          </div>`:''}
        ${!isCurrent && hasContent ? `
          <div style="margin-top:.55rem;text-align:right">
            <button class="btn btn-o btn-xs" onclick="event.stopPropagation();copyPriorNotes(${a.id})">
              ⧉ Copy prior notes into current
            </button>
          </div>`:''}
      </div>` : (aiSum||watch) ? `
      <div style="background:var(--gray-50);border:1px solid var(--gray-200);border-radius:7px;
        padding:.45rem .75rem;margin-top:.55rem;font-size:.76rem;color:var(--gray-600);font-style:italic">
        AI summary only — no researcher notes recorded at this appearance.
      </div>` : '';

    return `
      <div class="app-row" style="flex-direction:column;align-items:stretch;padding:.75rem .85rem;
        ${isCurrent?'border-color:var(--blue2);background:#f8fbff':''}">
        <div style="display:flex;align-items:center;gap:.6rem;cursor:pointer"
             onclick="${isCurrent?'':`switchToApp(${a.id})`}">
          <div style="min-width:90px">
            <div style="font-weight:700;color:var(--gray-800);font-size:.85rem">${fmtDate(a.meeting_date)||'?'}</div>
            <div style="font-size:.7rem;color:var(--gray-400)">${a.committee_item_number? 'Cmte #'+esc(a.committee_item_number):(a.bcc_item_number?'BCC #'+esc(a.bcc_item_number):'')}</div>
          </div>
          <div style="flex:1">
            <div style="font-size:.82rem;font-weight:500;color:var(--gray-800)">${esc(a.body_name||'')}</div>
            <div style="font-size:.7rem;color:var(--gray-400)">${esc(a.agenda_stage||'')}</div>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:.25rem;justify-content:flex-end">${pills}</div>
        </div>
        ${notesSection}
      </div>`;
  }).join('');
}

async function copyPriorNotes(priorAppId) {
  if (!currentAppId) return;
  const r = await fetch(`/api/appearance/${priorAppId}`);
  const d = await r.json();
  const prior = d.appearance || {};
  const bits = [];
  if ((prior.analyst_working_notes||'').trim())
    bits.push(`[Copied from prior appearance ${fmtDate(prior.meeting_date)||''} — ${prior.body_name||''}]\n${prior.analyst_working_notes}`);
  if (!bits.length) { alert('Prior appearance has no analyst notes to copy.'); return; }
  if (!confirm('Append prior analyst notes to the current item?')) return;
  await fetch(`/api/appearance/${currentAppId}/notes`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ working_notes: bits.join('\n\n'), changed_by: currentUser })
  });
  const ar = await fetch(`/api/appearance/${currentAppId}`).then(r=>r.json());
  _drData.appData = ar.appearance;
  drTab('notes', document.querySelectorAll('.dtab')[1]);
}

async function switchToApp(appId) {
  currentAppId=appId;
  const ar=await fetch(`/api/appearance/${appId}`).then(r=>r.json());
  _drData.appData=ar.appearance;
  drTab('summary',document.querySelector('.dtab'));
}

async function saveSummaryEdits() {
  if(!currentAppId)return;
  const ai=document.getElementById('edit-ai').innerText;
  const wp=document.getElementById('edit-wp').innerText;
  await fetch(`/api/appearance/${currentAppId}/ai`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({summary:ai,watch_points:wp,changed_by:currentUser})
  });
  showSaveMsg();
  // Disable editable fields
  ['edit-ai','edit-wp'].forEach(id=>{
    const el=document.getElementById(id);
    if(el)el.contentEditable='false';
  });
  // Trigger re-export
  await fetch(`/api/appearance/${currentAppId}/export`,{method:'POST'});
}

async function exportAndDownload() {
  if(!currentAppId)return;
  document.getElementById('dr-export-btn').textContent='Exporting…';
  const r=await fetch(`/api/appearance/${currentAppId}/export`,{method:'POST'});
  const d=await r.json();
  document.getElementById('dr-export-btn').textContent='↓ Export Files';
  if(d.files&&d.files.length){
    // Create temporary links and click them
    d.files.forEach(f=>{
      const a=document.createElement('a');
      a.href=f.url; a.download=f.name||''; a.target='_self'; document.body.appendChild(a); a.click();
      document.body.removeChild(a);
    });
  }
  showSaveMsg();
  const h=document.getElementById('dr-save-msg');
  h.textContent='✓ Exported'; h.style.display='';
  setTimeout(()=>h.style.display='none',3000);
}

function showSaveMsg() {
  const m=document.getElementById('dr-save-msg');
  m.textContent='✓ Saved'; m.style.display='';
  setTimeout(()=>m.style.display='none',2500);
}

// ════════════════════════════════════════════════════════════
// Settings
// ════════════════════════════════════════════════════════════
let _cfg={};
async function loadConfig() {
  try {
    _cfg=await (await fetch('/api/config')).json();
    // Populate header "Acting as…" selector
    const sel=document.getElementById('current-user-sel');
    while(sel.options.length>1)sel.remove(1);
    (_cfg.team_members||[]).forEach(m=>{
      const opt=new Option(m.name,m.name);
      if(m.name===currentUser)opt.selected=true;
      sel.add(opt);
    });
    // Populate Settings "Who am I?" selector
    const ssel=document.getElementById('settings-user-sel');
    if(ssel){
      while(ssel.options.length>1)ssel.remove(1);
      (_cfg.team_members||[]).forEach(m=>{
        const opt=new Option(m.name,m.name);
        if(m.name===currentUser)opt.selected=true;
        ssel.add(opt);
      });
      const info=document.getElementById('settings-user-current');
      if(info) info.textContent = currentUser ? ('Currently acting as: '+currentUser) : 'Not set — "My Items" filter will not work until you pick your name.';
    }
    // Populate workflow Assigned-To filter with team members + Unassigned
    const wfa=document.getElementById('wf-f-assigned');
    if(wfa){
      // Wipe everything except first two fixed options (Anyone, My Items)
      while(wfa.options.length>2)wfa.remove(2);
      // Refresh the "My Items" label so the user can tell which name it maps to
      wfa.options[1].text = currentUser ? ('My Items ('+currentUser+')') : 'My Items (set your name ↓)';
      (_cfg.team_members||[]).forEach(m=>{
        wfa.add(new Option(m.name, m.name));
      });
      wfa.add(new Option('Unassigned','unassigned'));
    }
  } catch(e){}
}

function setCurrentUser(name) {
  currentUser=name||'';
  localStorage.setItem('oca_user',currentUser);
  // Keep header picker and settings picker in sync
  const hdr=document.getElementById('current-user-sel');
  if(hdr && hdr.value!==currentUser) hdr.value=currentUser;
  const ssel=document.getElementById('settings-user-sel');
  if(ssel && ssel.value!==currentUser) ssel.value=currentUser;
  const info=document.getElementById('settings-user-current');
  if(info) info.textContent = currentUser ? ('Currently acting as: '+currentUser) : 'Not set — "My Items" filter will not work until you pick your name.';
  const wfa=document.getElementById('wf-f-assigned');
  if(wfa && wfa.options.length>1){
    wfa.options[1].text = currentUser ? ('My Items ('+currentUser+')') : 'My Items (set your name ↓)';
  }
  if(typeof loadWorkflow==='function') loadWorkflow();
}

async function loadSettings() {
  await loadConfig();
  document.getElementById('email-enabled').checked=!!_cfg.email_enabled;
  document.getElementById('smtp-host').value=_cfg.smtp_host||'smtp.gmail.com';
  document.getElementById('smtp-port').value=_cfg.smtp_port||587;
  document.getElementById('smtp-user').value=_cfg.smtp_user||'';
  document.getElementById('smtp-pass').value=_cfg.smtp_password||'';
  document.getElementById('smtp-recip').value=(_cfg.notify_recipients||[]).join(', ');
  document.getElementById('reminder-days').value=_cfg.reminder_days||7;
  document.getElementById('webhook-enabled').checked=!!_cfg.webhook_enabled;
  document.getElementById('webhook-url').value=_cfg.webhook_url||'';
  renderTeamList();
}

function renderTeamList() {
  const el=document.getElementById('team-list');
  const members=_cfg.team_members||[];
  el.innerHTML=members.length?members.map((m,i)=>`
    <div class="team-row">
      <span><strong>${esc(m.name)}</strong> ${m.email?`&lt;${esc(m.email)}&gt;`:''}
      </span>
      <button class="btn btn-d btn-xs" onclick="removeTeamMember(${i})">Remove</button>
    </div>`).join(''):'<p style="color:var(--gray-400);font-size:.8rem">No team members yet.</p>';
}

async function addTeamMember() {
  const name=document.getElementById('new-member-name').value.trim();
  const email=document.getElementById('new-member-email').value.trim();
  if(!name)return;
  _cfg.team_members=_cfg.team_members||[];
  _cfg.team_members.push({name,email});
  document.getElementById('new-member-name').value='';
  document.getElementById('new-member-email').value='';
  await saveSettings(true);
  renderTeamList(); loadConfig();
}

async function removeTeamMember(idx) {
  _cfg.team_members.splice(idx,1);
  await saveSettings(true);
  renderTeamList();
}

async function saveSettings(silent=false) {
  _cfg.email_enabled=document.getElementById('email-enabled')?.checked||false;
  _cfg.smtp_host=document.getElementById('smtp-host')?.value||'smtp.gmail.com';
  _cfg.smtp_port=parseInt(document.getElementById('smtp-port')?.value)||587;
  _cfg.smtp_user=document.getElementById('smtp-user')?.value||'';
  _cfg.smtp_password=document.getElementById('smtp-pass')?.value||'';
  _cfg.notify_recipients=(document.getElementById('smtp-recip')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
  _cfg.reminder_days=parseInt(document.getElementById('reminder-days')?.value)||7;
  _cfg.webhook_enabled=document.getElementById('webhook-enabled')?.checked||false;
  _cfg.webhook_url=document.getElementById('webhook-url')?.value||'';
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_cfg)});
  if(!silent)toast('Settings saved','ok');
  loadConfig();
}

async function testEmail() {
  await saveSettings(true);
  const r=await fetch('/api/test-email',{method:'POST'});
  const d=await r.json();
  alert(d.ok?'Test email sent! Check your inbox.':'Failed: '+d.error);
}

async function testWebhook() {
  await saveSettings(true);
  const r=await fetch('/api/test-webhook',{method:'POST'});
  const d=await r.json();
  alert(d.ok?'Test message sent! Check your Teams/Slack channel.':'Failed: '+(d.error||'Unknown error'));
}

// ════════════════════════════════════════════════════════════
// Saved Meetings + Meeting Detail
// ════════════════════════════════════════════════════════════
let currentMeetingId = null;

let _bfPollTimer = null;

function _renderBfProgress(p) {
  const panel = document.getElementById('bf-progress');
  if (!panel) return;
  if (!p || (!p.running && !p.summary && !p.error)) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  const pct = p.percent || 0;
  const done = p.done || 0;
  const total = p.total || 0;
  const bar = document.getElementById('bf-bar');
  if (bar) bar.style.width = (p.running ? pct : (p.summary ? 100 : pct)) + '%';
  const txt = document.getElementById('bf-progress-text');
  if (txt) {
    if (p.running) {
      txt.innerHTML = `<b>${done}/${total}</b> matters · ${pct}% — <span style="color:var(--gray-600)">${esc(p.current||'')}</span>`;
    } else if (p.summary) {
      const s = p.summary;
      txt.innerHTML = `<span style="color:var(--green);font-weight:600">✓ Done</span> — ` +
        `<b>${s.matters||0}</b> matters · <b>${s.urls_filled||0}</b> links filled · ` +
        `<b>${s.pdfs_downloaded||0}</b> PDFs · <b>${s.timeline_events||0}</b> lifecycle events · ` +
        `<b>${s.stub_appearances||0}</b> committee stubs created`;
    } else if (p.error) {
      txt.innerHTML = `<span style="color:var(--red);font-weight:600">✗ Failed</span> — ${esc(p.error)}`;
    }
  }
  const logEl = document.getElementById('bf-log');
  if (logEl && p.events) {
    logEl.innerHTML = p.events.slice(-8).map(e =>
      `<div style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#64748b">` +
      `${e.ts ? e.ts.slice(11,19) : ''} · ${esc(e.msg||'')}</div>`).join('');
  }
}

async function runBackfill(onlyMissing) {
  // Disable all backfill buttons while running.
  ['bf-btn','bf-nudge-btn'].forEach(id => {
    const b = document.getElementById(id); if (b) b.disabled = true;
  });
  const msg = document.getElementById('bf-msg');
  if (msg) msg.textContent = onlyMissing ? 'Starting…' : 'Starting full refresh…';

  try{
    const r = await fetch('/api/backfill/urls-and-lifecycle',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({only_missing: !!onlyMissing})
    });
    const d = await r.json();
    if (!d.ok && d.error === 'Already running') {
      toast('Backfill already in progress — watch the progress bar.', 'ok');
    }
    // Show the progress panel and start polling.
    _renderBfProgress(d.progress || {running:true});
    if (_bfPollTimer) clearInterval(_bfPollTimer);
    _bfPollTimer = setInterval(async () => {
      try{
        const pr = await fetch('/api/backfill/progress');
        const pd = await pr.json();
        _renderBfProgress(pd);
        if (!pd.running) {
          clearInterval(_bfPollTimer); _bfPollTimer = null;
          ['bf-btn','bf-nudge-btn'].forEach(id => {
            const b = document.getElementById(id); if (b) b.disabled = false;
          });
          if (pd.summary) {
            const s = pd.summary;
            toast(`Backfill done · ${s.matters||0} matters · ${s.stub_appearances||0} stubs`, 'ok');
          } else if (pd.error) {
            toast('Backfill failed: '+pd.error, 'err');
          }
          loadSavedMeetings();
        }
      }catch(_){}
    }, 1500);
  }catch(e){
    if(msg){ msg.textContent = 'Failed: '+(e.message||e); }
    toast('Backfill failed to start: '+(e.message||e), 'err');
    ['bf-btn','bf-nudge-btn'].forEach(id => {
      const b = document.getElementById(id); if (b) b.disabled = false;
    });
  }
}

/* ── Transcript Backfill ─────────────────────────────────── */
let _txPollTimer = null;

function _txGetPanel() {
  let panel = document.getElementById('tx-progress');
  if (!panel) {
    const card = document.getElementById('md-artifacts');
    if (!card) return null;
    panel = document.createElement('div');
    panel.id = 'tx-progress';
    panel.style.cssText = 'margin-top:.6rem;padding:.6rem .75rem;border-radius:.5rem;background:#f0f4ff;border:1px solid #c7d2fe;font-size:.82rem;';
    card.prepend(panel);
  }
  panel.style.display = 'block';
  return panel;
}

async function backfillTranscript() {
  const btn = document.getElementById('md-transcript-btn');
  if (btn) btn.disabled = true;

  const mid = currentMeetingId;
  if (!mid) { toast('Open a meeting first.', 'err'); if (btn) btn.disabled = false; return; }

  const panel = _txGetPanel();
  if (!panel) { toast('UI element missing', 'err'); if (btn) btn.disabled = false; return; }

  // Show options: auto-fetch or paste
  panel.innerHTML = `<b>🎙 Transcript Backfill</b>
    <div style="margin-top:.5rem;display:flex;gap:.5rem;flex-wrap:wrap">
      <button class="btn btn-p btn-sm" onclick="_txAutoFetch()">🔍 Auto-fetch Transcript</button>
      <button class="btn btn-o btn-sm" onclick="_txShowPaste()">📋 Paste Transcript</button>
    </div>
    <div style="font-size:.72rem;color:#64748b;margin-top:.3rem">
      Auto-fetch searches the county archives for the meeting MP3 and transcribes it with AI.<br>
      Falls back to YouTube captions if no recording is found. You can also paste a transcript manually.
    </div>`;
}

async function _txAutoFetch(videoUrl) {
  const btn = document.getElementById('md-transcript-btn');
  const mid = currentMeetingId;
  const panel = _txGetPanel();
  panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>Searching for meeting recording…';

  try {
    const body = {meeting_id: mid};
    if (videoUrl) body.video_url = videoUrl;
    const r = await fetch('/api/backfill/transcript', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();

    if (!d.ok) {
      // Show error + paste fallback
      let errorHtml = esc(d.error || d.message || 'Failed');
      let candidatesHtml = '';
      if (d.candidates && d.candidates.length) {
        candidatesHtml = '<div style="margin-top:.4rem;font-size:.75rem"><b>Possible matches:</b><br>' +
          d.candidates.map((c, i) =>
            `<a href="#" onclick="event.preventDefault();_txAutoFetch(\'${esc(c.url)}\')" style="color:#2563eb">${i+1}. ${esc(c.title)} (${(c.match_score*100).toFixed(0)}%)</a>`
          ).join('<br>') + '</div>';
      }
      panel.innerHTML = `<b>🎙 Transcript Backfill</b><br>
        <span style="color:#dc2626">❌ ${errorHtml}</span>
        ${candidatesHtml}
        <div style="margin-top:.5rem;border-top:1px dashed #c7d2fe;padding-top:.4rem">
          <b>Workaround:</b> Open the YouTube video → click "…" → "Show transcript" → copy all text
          <button class="btn btn-o btn-sm" style="margin-top:.3rem" onclick="_txShowPaste()">📋 Paste Transcript Instead</button>
        </div>`;
      if (btn) btn.disabled = false;
      return;
    }

    _txStartPolling(panel, btn);
  } catch(e) {
    panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>❌ ' + esc(e.message || String(e));
    if (btn) btn.disabled = false;
  }
}

function _txShowPaste() {
  const panel = _txGetPanel();
  panel.innerHTML = `<b>🎙 Paste Meeting Transcript</b>
    <div style="font-size:.72rem;color:#475569;margin:.3rem 0">
      Open the YouTube video → click <b>"…" → "Show transcript"</b> → select all → copy → paste below.
      Or paste any other transcript source (Granicus, etc.)
    </div>
    <div style="margin-bottom:.3rem">
      <label style="font-size:.72rem;color:#64748b">Video URL (optional — for linking back):</label>
      <input type="text" id="tx-video-url" placeholder="https://youtube.com/watch?v=..." style="font-size:.78rem;padding:.3rem .5rem;width:100%">
    </div>
    <textarea id="tx-paste-area" rows="8" placeholder="Paste the full transcript here…"
      style="width:100%;font-size:.75rem;font-family:monospace;margin-bottom:.4rem"></textarea>
    <div style="display:flex;gap:.4rem">
      <button class="btn btn-p btn-sm" onclick="_txSubmitPaste()">🎙 Analyze Transcript</button>
      <button class="btn btn-o btn-sm" onclick="document.getElementById('tx-progress').style.display='none';document.getElementById('md-transcript-btn').disabled=false">Cancel</button>
    </div>`;
}

async function _txSubmitPaste() {
  const panel = _txGetPanel();
  const btn = document.getElementById('md-transcript-btn');
  const mid = currentMeetingId;
  const rawText = (document.getElementById('tx-paste-area')?.value || '').trim();
  const videoUrl = (document.getElementById('tx-video-url')?.value || '').trim();

  if (!rawText || rawText.length < 100) {
    toast('Transcript too short — paste the full transcript text.', 'err');
    return;
  }

  panel.innerHTML = `<b>🎙 Transcript Backfill</b><br>Analyzing ${rawText.length.toLocaleString()} chars of transcript…`;

  try {
    const r = await fetch('/api/backfill/transcript', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        meeting_id: mid,
        video_url: videoUrl || undefined,
        raw_transcript: rawText,
      })
    });
    const d = await r.json();
    if (!d.ok) {
      panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>❌ ' + esc(d.error || d.message || 'Failed');
      if (btn) btn.disabled = false;
      return;
    }
    _txStartPolling(panel, btn);
  } catch(e) {
    panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>❌ ' + esc(e.message || String(e));
    if (btn) btn.disabled = false;
  }
}

function _txStartPolling(panel, btn) {
  let _txPollCount = 0;
  let _lastMsg = '';
  let _stuckCount = 0;
  _txPollTimer = setInterval(async () => {
    _txPollCount++;
    try {
      const pr = await fetch('/api/backfill/transcript/progress');
      const pd = await pr.json();
      if (pd.done) {
        clearInterval(_txPollTimer); _txPollTimer = null;
        _handleTranscriptResult(pd.result, panel, btn);
        return;
      }
      const msg = pd.msg || 'Working…';
      panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>' +
        `<div style="margin:.3rem 0;background:#e0e7ff;border-radius:4px;height:6px;overflow:hidden">` +
        `<div style="width:${pd.pct||0}%;height:100%;background:#6366f1;transition:width .3s"></div></div>` +
        `<span style="color:#4338ca">${esc(msg)}</span>`;

      // Detect stuck state: same message for 60+ seconds
      if (msg === _lastMsg) { _stuckCount++; } else { _stuckCount = 0; _lastMsg = msg; }
      if (_stuckCount > 50) {  // ~60s at 1.2s interval
        clearInterval(_txPollTimer); _txPollTimer = null;
        panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>' +
          `<span style="color:#b45309">⚠ Appears stuck at "${esc(msg)}". This may be a disk space issue.</span><br>` +
          '<span style="font-size:.75rem;color:#64748b">Refresh the page and check item notes — they may already be saved.</span>';
        if (btn) btn.disabled = false;
        return;
      }
      // Safety: absolute max 10 min
      if (_txPollCount > 500) {
        clearInterval(_txPollTimer); _txPollTimer = null;
        panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>' +
          '<span style="color:#b45309">⚠ Taking longer than expected. Refresh the page and check item notes.</span>';
        if (btn) btn.disabled = false;
      }
    } catch(_) {}
  }, 1200);
}

function _handleTranscriptResult(d, panel, btn) {
  if (d && d.status === 'ok') {
    panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>' +
      `<span style="color:#15803d">✓ Done — ${d.items_updated || 0} items updated with discussion notes</span><br>` +
      `<span style="font-size:.75rem;color:#64748b">Video: ${esc(d.video_title || '')} · ` +
      `${(d.transcript_length||0).toLocaleString()} chars · ${d.items_segmented||0} segments</span>` +
      (d.video_url ? `<br><a href="${esc(d.video_url)}" target="_blank" style="font-size:.75rem">▶ Watch Recording</a>` : '');
    toast(`Transcript backfill complete — ${d.items_updated} items updated`, 'ok');
    // Refresh the meeting detail view to show new notes
    if (currentMeetingId) openMeeting(currentMeetingId);
  } else {
    panel.innerHTML = '<b>🎙 Transcript Backfill</b><br>❌ ' +
      esc((d && (d.message || d.error)) || 'Failed');
  }
  if (btn) btn.disabled = false;
}

async function loadSavedMeetings() {
  // If a backfill is already running (started in a previous page view),
  // immediately show the progress panel and resume polling.
  try {
    const bp = await fetch('/api/backfill/progress'); const bpd = await bp.json();
    if (bpd && bpd.running) {
      _renderBfProgress(bpd);
      if (!_bfPollTimer) {
        _bfPollTimer = setInterval(async () => {
          const r2 = await fetch('/api/backfill/progress');
          const d2 = await r2.json();
          _renderBfProgress(d2);
          if (!d2.running) { clearInterval(_bfPollTimer); _bfPollTimer=null; loadSavedMeetings(); }
        }, 1500);
      }
    } else if (bpd && (bpd.summary || bpd.error)) {
      _renderBfProgress(bpd);  // show last result line
    }
  } catch(_){}

  // Probe for missing-data and show the nudge banner if needed.
  try {
    const pr = await fetch('/api/backfill/status'); const pd = await pr.json();
    const nudge = document.getElementById('bf-nudge');
    const title = document.getElementById('bf-nudge-title');
    const bodyEl = document.getElementById('bf-nudge-body');
    if (pd.needs_backfill && pd.total_appearances > 0) {
      const missing = pd.missing_matter_url || 0;
      const unvisited = pd.matters_unvisited || 0;
      title.textContent = `Missing data detected — run the backfill to unlock links & lifecycle.`;
      bodyEl.innerHTML =
        `<b>${missing}</b> appearance(s) have no Legistar link · ` +
        `<b>${unvisited}</b> matter(s) haven't been checked for legislative history yet. ` +
        `Click <b>Fix now</b> to re-hit Legistar, parse each matter's history, ` +
        `and auto-create committee stubs so cross-stage Cmte Date/# populate.`;
      nudge.style.display = 'flex';
    } else {
      nudge.style.display = 'none';
    }
  } catch(_e){ /* banner is advisory — silent fail is OK */ }

  const r = await fetch('/api/meetings');
  const rows = await r.json();
  const tb = document.getElementById('mtg-tbody');
  document.getElementById('mtg-count').textContent = `${rows.length} meeting(s)`;
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="8">'+emptyState(
      '🗂️', 'No saved meetings yet',
      'Run a <b>Process</b> job on the Home tab to pull a committee or BCC agenda. Once items are analyzed, the meeting package will appear here with draft + final export controls.',
      '<button class="btn btn-p btn-sm" onclick="showPg(\'home\')">Go to Home →</button>'
    )+'</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(m => {
    const pct = m.total ? Math.round((m.finalized * 100) / m.total) : 0;
    const statusColor = {
      'Draft':'b-New','In Progress':'b-InProgress',
      'Final Ready':'b-DraftComplete','Final Generated':'b-Finalized',
      'Empty':'b-Archived'}[m.status] || 'b-New';
    return `<tr class="clickable" onclick="openMeeting(${m.id})">
      <td style="font-weight:600">${esc(m.body_name||'')}</td>
      <td style="white-space:nowrap">${fmtDate(m.meeting_date)}</td>
      <td style="font-size:.75rem">${esc(m.meeting_type||'—')}</td>
      <td>${m.total}</td>
      <td>
        <div style="display:flex;align-items:center;gap:.4rem">
          <div style="flex:1;background:var(--gray-100);border-radius:99px;height:6px;min-width:80px;overflow:hidden">
            <div style="height:100%;width:${pct}%;background:var(--green)"></div>
          </div>
          <span style="font-size:.72rem;color:var(--gray-600)">${m.finalized}/${m.total}</span>
        </div>
      </td>
      <td><span class="badge ${statusColor}">${m.status}</span></td>
      <td style="font-size:.72rem;color:var(--gray-600)">
        ${m.final_available ? '✓ Final available' : (m.total ? 'Draft only' : '—')}
      </td>
      <td onclick="event.stopPropagation()">
        <div style="display:flex;gap:.3rem;justify-content:flex-end;flex-wrap:wrap">
          <button class="btn btn-o btn-xs" onclick="openMeeting(${m.id})">Open →</button>
          <button class="btn btn-o btn-xs" onclick="regenDraft(${m.id},this)"
            title="Regenerate the draft Excel + Word from the latest DB state">⟳ Draft</button>
          <button class="btn btn-p btn-xs" onclick="genFinal(${m.id},this)"
            ${m.finalized === m.total && m.total > 0 ? '' : 'disabled title="All items must be Finalized"'}
            >★ Export Whole Agenda</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

async function openMeeting(meetingId) {
  currentMeetingId = meetingId;
  showPg('meeting-detail');
  document.getElementById('md-title').textContent = 'Loading…';
  document.getElementById('md-meta-body').innerHTML = '';
  document.getElementById('md-items').innerHTML = '';
  document.getElementById('md-artifacts').innerHTML = '';

  const r = await fetch(`/api/meeting/${meetingId}`);
  if (!r.ok) {
    document.getElementById('md-title').textContent = 'Not found';
    return;
  }
  const pkg = await r.json();
  renderMeetingDetail(pkg);
}

function renderMeetingDetail(pkg) {
  const m = pkg.meeting;
  const s = pkg.status;
  const items = pkg.items || [];
  const arts = pkg.artifacts || [];

  document.getElementById('md-title').textContent =
    `${m.body_name} — ${fmtDate(m.meeting_date)}`;

  const statusColor = {
    'Draft':'b-New','In Progress':'b-InProgress',
    'Final Ready':'b-DraftComplete','Final Generated':'b-Finalized',
    'Empty':'b-Archived'}[s.status] || 'b-New';
  document.getElementById('md-status-badge').innerHTML =
    `<span class="badge ${statusColor}" style="font-size:.75rem;padding:.28rem .75rem">${s.status}</span>`;

  document.getElementById('md-package-meta').textContent =
    `${s.total} items · ${s.finalized} finalized · ${s.in_progress} in progress · ${s.new} new`;

  // Meta with links
  const links = [];
  if (m.agenda_page_url) links.push(`<a class="btn btn-o btn-sm" target="_blank" href="${esc(m.agenda_page_url)}">📄 Open Agenda Page</a>`);
  if (m.final_agenda_url) links.push(`<a class="btn btn-o btn-sm" target="_blank" href="${esc(m.final_agenda_url)}">🔗 Final Agenda</a>`);
  if (m.agenda_pdf_url) links.push(`<a class="btn btn-o btn-sm" target="_blank" href="${esc(m.agenda_pdf_url)}">⬇ Agenda PDF</a>`);
  document.getElementById('md-meta-body').innerHTML = `
    <div style="display:flex;gap:.85rem;flex-wrap:wrap;align-items:center;font-size:.82rem;color:var(--gray-600)">
      <div><strong>Body:</strong> ${esc(m.body_name||'')}</div>
      <div><strong>Date:</strong> ${fmtDate(m.meeting_date)}</div>
      ${m.meeting_type ? `<div><strong>Type:</strong> ${esc(m.meeting_type)}</div>`:''}
      ${m.agenda_status ? `<div><strong>Agenda:</strong> ${esc(m.agenda_status)}</div>`:''}
      ${m.last_exported_at ? `<div><strong>Last export:</strong> ${m.last_exported_at.slice(0,16).replace('T',' ')}</div>`:''}
    </div>
    ${links.length ? `<div style="margin-top:.75rem;display:flex;gap:.4rem;flex-wrap:wrap">${links.join('')}</div>`:''}
  `;

  // Final export gating
  const finalBtn = document.getElementById('md-final-btn');
  if (s.total > 0 && s.finalized === s.total) {
    finalBtn.disabled = false;
    finalBtn.title = 'All items finalized — ready to generate final export';
  } else {
    finalBtn.disabled = true;
    finalBtn.title = `${s.finalized}/${s.total} items finalized — all must be finalized to generate final export`;
  }

  // Artifacts
  const mtgLevel = arts.filter(a => !a.appearance_id);
  document.getElementById('md-artifacts').innerHTML = mtgLevel.length ? mtgLevel.map(a => {
    const ext = (a.file_path || '').split('.').pop().toLowerCase();
    const fin = a.is_final ? 'FINAL' : 'DRAFT';
    const finColor = a.is_final ? 'var(--green)' : 'var(--gray-600)';
    return `<div class="fi">
      <div class="fi-info">
        <div class="ficon ${ext}">${ext.toUpperCase()}</div>
        <div>
          <div style="font-size:.82rem;font-weight:500">${esc(a.label || a.file_path.split('/').pop())}</div>
          <div style="font-size:.7rem;color:${finColor};font-weight:600">${fin}</div>
        </div>
      </div>
      <a class="dlbtn" href="/api/artifact/${a.id}/download" download="${esc(a.file_path?.split('/').pop()||'')}">↓ Download</a>
    </div>`;
  }).join('') : '<p style="color:var(--gray-400);font-size:.82rem">No exports yet. Click Regenerate Draft to create them.</p>';

  // Cache items for filtering
  _mdItems = items;
  _mdMeeting = m;
  console.log(`[meeting ${m.id}] API returned ${items.length} items; first keys:`,
    items[0] ? Object.keys(items[0]).slice(0,12) : '(none)');

  // If the API truly returned zero items, tell the user plainly — otherwise
  // the table just looks "broken".
  if (items.length === 0) {
    document.getElementById('md-items').innerHTML = `<tr><td colspan="20" style="padding:1.5rem;text-align:center">
      <div style="font-size:1.4rem;margin-bottom:.3rem">📭</div>
      <div style="font-weight:600;color:var(--ink)">This meeting has no appearances stored yet.</div>
      <div style="font-size:.78rem;color:var(--gray-600);margin-top:.35rem">
        Either scraping didn't save any items for <b>${esc(m.body_name||'')}</b> on <b>${esc(fmtDate(m.meeting_date)||'')}</b>,
        or they were created as a stub from a cross-stage reference.
        Try running a fresh <b>Process</b> job on the Home tab for this body/date.
      </div>
    </td></tr>`;
    document.getElementById('md-items-count').textContent = '0 items';
    return;
  }

  // Populate filter dropdowns from the data we have
  const legStatuses = [...new Set(items.map(i => i.current_status).filter(Boolean))].sort();
  const fileTypes   = [...new Set(items.map(i => i.file_type).filter(Boolean))].sort();
  const sponsors    = [...new Set(items.map(i => i.sponsor).filter(Boolean))].sort();
  function fill(id, vals) {
    const el = document.getElementById(id);
    while (el.options.length > 1) el.remove(1);
    vals.forEach(v => el.add(new Option(v, v)));
  }
  fill('md-f-legstatus', legStatuses);
  fill('md-f-filetype',  fileTypes);
  fill('md-f-sponsor',   sponsors);

  renderItemsGrid();
}

let _mdItems = [];
let _mdMeeting = {};

function clearItemFilters() {
  ['md-f-q','md-f-legstatus','md-f-filetype','md-f-sponsor','md-f-wf','md-f-special']
    .forEach(id => { const e=document.getElementById(id); if(e) e.value=''; });
  renderItemsGrid();
}

function renderItemsGrid() {
  try { return _renderItemsGrid(); }
  catch (e) {
    console.error('renderItemsGrid failed:', e);
    const tb = document.getElementById('md-items');
    if (tb) tb.innerHTML = `<tr><td colspan="20" style="padding:1rem;color:var(--red)">
      <b>Render error:</b> ${(e&&e.message)||e}<br>
      <span style="font-size:.72rem;color:#64748b">Open DevTools → Console for the full stack trace. Items are loaded (${_mdItems.length}) but couldn't render.</span>
    </td></tr>`;
  }
}
function _renderItemsGrid() {
  const q = (document.getElementById('md-f-q')?.value||'').toLowerCase().trim();
  const ls = document.getElementById('md-f-legstatus')?.value||'';
  const ft = document.getElementById('md-f-filetype')?.value||'';
  const sp = document.getElementById('md-f-sponsor')?.value||'';
  const wf = document.getElementById('md-f-wf')?.value||'';
  const sx = document.getElementById('md-f-special')?.value||'';

  const m = _mdMeeting || {};
  const cmteDate = m.meeting_date || '';
  const bodyLower = (m.body_name||'').toLowerCase();
  const isBCC = bodyLower.includes('bcc') || bodyLower.includes('board of county');

  const filtered = _mdItems.filter(it => {
    if (q && !((it.file_number||'').toLowerCase().includes(q) ||
               (it.short_title||it.appearance_title||'').toLowerCase().includes(q))) return false;
    if (ls && it.current_status !== ls) return false;
    if (ft && it.file_type !== ft) return false;
    if (sp && it.sponsor !== sp) return false;
    if (wf && it.workflow_status !== wf) return false;
    if (sx === 'cf' && !it.carried_forward_from_prior) return false;
    if (sx === 'supp' && !it.is_supplement) return false;
    if (sx === 'notes' && !it.has_prior_notes) return false;
    if (sx === 'unnotes' && (it.has_analyst_notes || it.has_reviewer_notes || it.has_finalized_brief)) return false;
    return true;
  });

  document.getElementById('md-items-count').textContent =
    `${filtered.length} of ${_mdItems.length} item(s)`;

  const tbody = document.getElementById('md-items');
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="18" style="padding:1rem;color:var(--gray-400)">No items match these filters.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(it => {
    const notesChips = [
      it.has_analyst_notes  ? '<span style="color:#056f3a">📝A</span>' : '',
      it.has_reviewer_notes ? '<span style="color:#7a1e1e">👁R</span>' : '',
      it.has_finalized_brief? '<span style="color:var(--green)">✓B</span>' : '',
    ].filter(Boolean).join(' ') || '—';

    const historyBits = [];
    if (it.carried_forward_from_prior)
      historyBits.push('<span class="badge b-cf" title="Notes carried forward">↩ CF</span>');
    if (it.prior_appearance_count > 0)
      historyBits.push(`<span class="badge" style="background:var(--blue-lt);color:var(--blue)" title="Prior appearances">↔ ${it.prior_appearance_count}</span>`);
    if (it.has_prior_notes)
      historyBits.push('<span class="badge" style="background:#fef3c7;color:#92400e" title="Prior analyst notes exist">★ Prior notes</span>');
    if (it.is_supplement)
      historyBits.push('<span class="badge" style="background:#ede9fe;color:#5b21b6" title="Supplement">+ Supp</span>');

    // Cross-stage date/number: always show Cmte Date/# AND BCC Date/# using
    // stored appearances first, then parsed Legistar lifecycle, then finally
    // the current-row fallback only if stage matches.
    const cd = it.committee_appearance_date || '';
    const cn = it.committee_item_number_x   || '';
    const bd = it.bcc_appearance_date       || '';
    const bn = it.bcc_item_number_x         || it.bcc_item_number || '';
    // Italic + gray means date came from parsed Legistar history (no stored
    // appearance in AgendaIQ yet). Solid dark means we have the appearance.
    const cmteCount = it.cmte_appearance_count || 0;
    const bccCount  = it.bcc_appearance_count || 0;
    const cmteBadge = cmteCount > 1 ? `<span style="font-size:.55rem;background:#bbf7d0;color:#166534;padding:0 3px;border-radius:3px;margin-left:2px" title="${cmteCount} committee appearances">×${cmteCount}</span>` : '';
    const bccBadge  = bccCount > 1 ? `<span style="font-size:.55rem;background:#bfdbfe;color:#1e40af;padding:0 3px;border-radius:3px;margin-left:2px" title="${bccCount} BCC appearances">×${bccCount}</span>` : '';
    // Date formatting: past dates muted, future dates bold blue with ▶ indicator
    const _today = new Date().toISOString().slice(0,10);
    const _fmtDate = (d, badge, src) => {
      if (!d) return '—';
      if (src === 'legistar') return `<span style="font-style:italic;color:#94a3b8" title="From Legistar legislative history">${d}</span>`;
      const isPast = d <= _today;
      if (isPast) return `<span style="color:#64748b" title="Past: ${d}">${d}</span>${badge}`;
      return `<span style="color:#2563eb;font-weight:600" title="Upcoming: ${d}">▶ ${d}</span>${badge}`;
    };
    const cdSrc = _fmtDate(cd, cmteBadge, it.committee_date_source);
    const bdSrc = _fmtDate(bd, bccBadge, it.bcc_date_source);

    // Inline Links column: ↗ Legistar item page, 📄 Item PDF (local or remote).
    const linkBits = [];
    if (it.matter_url) {
      linkBits.push(`<a href="${esc(it.matter_url)}" target="_blank" onclick="event.stopPropagation()" title="Open Legistar matter page" style="text-decoration:none;font-size:.95rem">↗</a>`);
    }
    if (it.item_pdf_url || it.item_pdf_local_path) {
      linkBits.push(`<a href="/api/appearance/${it.id}/pdf" target="_blank" onclick="event.stopPropagation()" title="Download/open Item PDF" style="text-decoration:none;font-size:.95rem">📄</a>`);
    }
    const linksCell = linkBits.length
      ? `<span style="display:inline-flex;gap:.4rem;align-items:center">${linkBits.join('')}</span>`
      : `<span style="color:var(--gray-400);font-size:.75rem" title="No Legistar link yet — run Backfill">—</span>`;

    // Explicit Prior Notes column with a checkmark (easier than hunting
    // through the History badges column).
    const priorCell = it.has_prior_notes
      ? `<span title="Prior analyst/reviewer notes exist for this matter" style="color:var(--green);font-weight:700;font-size:1rem">✓</span>`
      : `<span style="color:var(--gray-400)">—</span>`;

    return `<tr class="clickable" onclick="openDrawer('${esc(it.file_number)}',${it.id})">
      <td><span class="file-link">${it.file_number||''}</span></td>
      <td onclick="event.stopPropagation()">${linksCell}</td>
      <td style="white-space:nowrap;font-size:.73rem">${cdSrc}</td>
      <td style="font-size:.73rem">${esc(cn)||'—'}</td>
      <td style="white-space:nowrap;font-size:.73rem">${bdSrc}</td>
      <td style="font-size:.73rem">${esc(bn)||'—'}</td>
      <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(it.short_title||it.appearance_title||'')}</td>
      <td style="font-size:.72rem">${esc(it.file_type||'—')}</td>
      <td style="font-size:.72rem;font-weight:500">${esc(it.current_status||'—')}</td>
      <td style="font-size:.72rem">${esc(it.sponsor||'—')}</td>
      <td style="font-size:.72rem">${esc(it.control_body||'—')}</td>
      <td style="font-size:.72rem">${renderJourneyCell(it)}</td>
      <td style="font-size:.72rem">${renderNextStepCell(it)}</td>
      <td style="text-align:center">${priorCell}</td>
      <td>${historyBits.join(' ') || '—'}</td>
      <td>${badge(it.workflow_status)}</td>
      <td style="font-size:.73rem">${esc(it.assigned_to||'—')}</td>
      <td style="font-size:.73rem">${esc(it.reviewer||'—')}</td>
      <td style="font-size:.73rem">${it.due_date||'—'}</td>
      <td style="font-size:.8rem;white-space:nowrap">${notesChips}</td>
      <td onclick="event.stopPropagation()">
        <button class="btn btn-o btn-xs" onclick="exportItem(${it.id},this)">↓</button>
      </td>
    </tr>`;
  }).join('');
}

async function exportItem(appId, btn) {
  btn.disabled = true; btn.textContent = '…';
  const r = await fetch(`/api/appearance/${appId}/export`, {method:'POST'});
  const d = await r.json();
  btn.disabled = false; btn.textContent = '↓ Export Item';
  if (d.files && d.files.length) {
    d.files.forEach(f => {
      const a = document.createElement('a');
      a.href = f.url; a.download = f.name;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
    });
  }
  // Reload to pick up artifacts
  if (currentMeetingId) openMeeting(currentMeetingId);
}

async function regenDraft(meetingId, btnEl) {
  const id = meetingId || currentMeetingId;
  if (!id) return;
  const btn = btnEl || document.getElementById('md-regen-btn');
  const prev = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = '…'; }
  const r = await fetch(`/api/meeting/${id}/regenerate`, {method:'POST'});
  const d = await r.json().catch(()=>({}));
  if(btn){ btn.disabled = false; btn.textContent = prev || '⟳ Draft'; }
  if (d && d.artifacts && d.artifacts.length) {
    toast(`Draft regenerated (${d.artifacts.length} file${d.artifacts.length>1?'s':''})`, 'ok');
    d.artifacts.forEach(f => {
      const a = document.createElement('a');
      a.href = `/api/artifact/${f.id}/download`;
      a.download = f.name || f.label || ''; a.target='_self'; document.body.appendChild(a); a.click(); a.remove();
    });
  }
  if (currentMeetingId === id) openMeeting(id);
  else loadSavedMeetings();
}

async function genFinal(meetingId, btnEl) {
  const id = meetingId || currentMeetingId;
  if (!id) return;
  const btn = btnEl || document.getElementById('md-final-btn');
  const prev = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = 'Generating…'; }
  const r = await fetch(`/api/meeting/${id}/finalize`, {method:'POST'});
  const d = await r.json();
  if(btn){ btn.disabled = false; btn.textContent = prev || '★ Export Whole Agenda'; }
  if (!d.ok) { toast(d.message || 'Cannot generate final export.','err'); return; }
  toast('Final export generated — downloading…','ok');
  const files = d.artifacts || d.files || [];
  if (files.length) {
    files.forEach(f => {
      const a = document.createElement('a');
      a.href = `/api/artifact/${f.id}/download`;
      a.download = f.name || f.label || ''; a.target='_self'; document.body.appendChild(a); a.click(); a.remove();
    });
  }
  if (currentMeetingId === id) openMeeting(id);
  else loadSavedMeetings();
}

// ════════════════════════════════════════════════════════════
// My Items (researcher workload)
// ════════════════════════════════════════════════════════════
function initMyItemsFilters() {
  const rs = document.getElementById('mi-researcher');
  const st = document.getElementById('mi-status');
  while (rs.options.length) rs.remove(0);
  rs.add(new Option(currentUser ? `Me (${currentUser})` : 'Me', currentUser || ''));
  rs.add(new Option('All Unassigned', '__unassigned__'));
  (_cfg.team_members || []).forEach(m => rs.add(new Option(m.name, m.name)));
  while (st.options.length > 1) st.remove(1);
  ['New','Assigned','In Progress','Draft Complete','In Review','Needs Revision','Finalized','Archived']
    .forEach(s => st.add(new Option(s, s)));
}

async function loadMyItems() {
  const who = document.getElementById('mi-researcher').value;
  const status = document.getElementById('mi-status').value;
  const due = document.getElementById('mi-due').value;
  if (!who) {
    document.getElementById('mi-tbody').innerHTML =
      '<tr><td colspan="8" style="padding:1.25rem;color:var(--gray-400)">Pick a researcher (top-right user menu) to view your assigned items.</td></tr>';
    document.getElementById('mi-count').textContent = '';
    return;
  }
  let url = `/api/workflow?assigned=${encodeURIComponent(who)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;
  if (due) url += `&due=${encodeURIComponent(due)}`;
  const r = await fetch(url);
  const rows = await r.json();
  document.getElementById('mi-count').textContent = `${rows.length} item(s)`;
  const tb = document.getElementById('mi-tbody');
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="8" style="padding:1.25rem;color:var(--gray-400)">No items match these filters.</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(a => {
    const due_cls = a._due_class||'due-none';
    const due_lbl = a._due_label||a.due_date||'—';
    const hasNotes = (a.analyst_working_notes||'').trim() ? '📝' : '';
    return `<tr class="clickable" onclick="openDrawer('${esc(a.file_number)}',${a.id})">
      <td><span class="file-link">${a.file_number}</span></td>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.short_title||'')}</td>
      <td style="white-space:nowrap;font-size:.75rem">${fmtDate(a.meeting_date)}</td>
      <td style="font-size:.72rem">${esc(a.body_name||'')}</td>
      <td>${badge(a.workflow_status)}</td>
      <td style="font-size:.78rem">${hasNotes}</td>
      <td class="due-cell ${due_cls}">${due_lbl}</td>
      <td onclick="event.stopPropagation()">
        <button class="btn btn-o btn-xs" onclick="openDrawer('${esc(a.file_number)}',${a.id})">Open</button>
      </td>
    </tr>`;
  }).join('');
}

// ════════════════════════════════════════════════════════════
// Shared helpers
// ════════════════════════════════════════════════════════════
function badge(s) {
  const k={' ':' ','In Progress':'InProgress','Draft Complete':'DraftComplete','In Review':'InReview'};
  const cs=k[s]||(s||'').replace(/\s/g,'');
  return s?`<span class="badge b-${cs}">${s}</span>`:'';
}

// Render a compact Committee → BCC progress indicator for the grid.
// Filled dot = this stage has happened (we have a date for it), outlined = no
// activity recorded at that stage. Supplement marker appears if applicable.
function renderJourneyCell(it) {
  const steps = it.journey || [];
  if (!steps.length) return '<span style="color:var(--gray-400);font-size:.7rem">—</span>';
  return `<div style="display:flex;align-items:center;gap:0;font-size:.65rem;line-height:1;flex-wrap:nowrap;overflow:hidden">` +
    steps.map((s, i) => {
      let colors;
      if (s.is_current) {
        // Current appearance: bold, highlighted
        colors = s.stage === 'bcc'
          ? {bg: '#2563eb', fg: '#fff'}
          : {bg: '#16a34a', fg: '#fff'};
      } else if (s.is_past) {
        // Past appearance: muted
        colors = {bg: '#e2e8f0', fg: '#64748b'};
      } else {
        // Future appearance: outlined, bright
        colors = s.stage === 'bcc'
          ? {bg: '#dbeafe', fg: '#1e40af'}
          : {bg: '#dcfce7', fg: '#166534'};
      }
      const fromLeg = s.from_legistar ? 'border:1px dashed #94a3b8;' : '';
      const arrow = i < steps.length - 1
        ? '<span style="color:#94a3b8;font-size:.6rem;margin:0 1px">→</span>' : '';
      const statusTag = s.is_current ? ' ● CURRENT' : (s.is_past ? ' (prior)' : ' (upcoming)');
      const title = `${s.body_name || s.label} ${s.full_date || ''}${statusTag}${s.action ? ' — ' + s.action : ''}${s.from_legistar ? ' (from Legistar)' : ''}`;
      return `<span title="${esc(title)}" style="display:inline-flex;align-items:center;padding:1px 4px;border-radius:3px;background:${colors.bg};color:${colors.fg};white-space:nowrap;font-weight:${s.is_current?700:500};${fromLeg}">${esc(s.label)} ${esc(s.date)}</span>${arrow}`;
    }).join('') + '</div>';
}

function renderNextStepCell(it) {
  const ns = it.next_step || '';
  const nst = it.next_step_type || 'pending';
  if (!ns || ns === '—') return '<span style="color:var(--gray-400);font-size:.72rem">—</span>';
  const styles = {
    done:    'background:#f0fdf4;color:#15803d;border:1px solid #86efac',
    bcc:     'background:#eff6ff;color:#1d4ed8;border:1px solid #93c5fd',
    cmte:    'background:#f0fdf4;color:#166534;border:1px solid #86efac',
    pending: 'background:#fffbeb;color:#92400e;border:1px solid #fde68a',
  };
  const icons = {done: '✓', bcc: '→', cmte: '→', pending: '⏳'};
  return `<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:.68rem;font-weight:600;white-space:nowrap;${styles[nst]||styles.pending}">${icons[nst]||''} ${esc(ns)}</span>`;
}
function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDate(d){
  // Convert ISO 2026-04-13 → 4/13/2026 for display
  if(!d) return '';
  const m=d.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(!m) return d;
  return parseInt(m[2])+'/'+parseInt(m[3])+'/'+m[1];
}

// Toast helper — used for backfill / export feedback
let _toastT=null;
function toast(msg, kind){
  let el=document.getElementById('app-toast');
  if(!el){
    el=document.createElement('div');
    el.id='app-toast'; el.className='toast';
    document.body.appendChild(el);
  }
  el.className='toast '+(kind||'')+' show';
  el.textContent=msg;
  clearTimeout(_toastT);
  _toastT=setTimeout(()=>{ el.className='toast '+(kind||''); }, 2800);
}

// Empty-state renderer — returns HTML
function emptyState(icon, title, hint, ctaHtml){
  return `<div class="empty">
    <div class="icon">${icon||'📭'}</div>
    <div class="title">${esc(title||'Nothing here yet')}</div>
    <div class="hint">${hint||''}</div>
    ${ctaHtml?`<div class="cta">${ctaHtml}</div>`:''}
  </div>`;
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────

_db_initialized = False

@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
        except Exception as e:
            app.logger.error(f"init_db failed (continuing anyway): {e}")
        _db_initialized = True


@app.errorhandler(Exception)
def _json_error(e):
    """Return JSON for /api/* errors so the frontend never sees an HTML error
    page (which causes 'Unexpected token <' when parsed as JSON)."""
    from flask import request as _rq
    import traceback as _tb, logging as _logging
    _logging.getLogger("oca-agent").error(
        "Unhandled error on %s: %s\n%s", _rq.path, e, _tb.format_exc())
    if _rq.path.startswith("/api/"):
        return jsonify({"ok": False, "error": str(e)}), 500
    # Non-API routes: re-raise default behavior
    raise e


@app.route("/")
def index():
    from flask import make_response
    resp = make_response(HTML)
    # Prevent browser from serving stale JS/CSS during active development
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/favicon.ico")
def favicon():
    # Silence the 404 noise in the console
    return ("", 204)


@app.route("/api/committees")
def api_committees():
    from scraper import MiamiDadeScraper
    return jsonify(sorted(MiamiDadeScraper().get_committees().keys()))


@app.route("/api/stats")
def api_stats():
    from search import get_dashboard_stats
    from workflow import get_overdue_appearances, get_due_soon_appearances, get_unassigned_appearances
    d = get_dashboard_stats()
    d["overdue_count"]    = len(get_overdue_appearances())
    d["due_soon_count"]   = len(get_due_soon_appearances(7))
    d["unassigned_count"] = len(get_unassigned_appearances())
    # Count items pending review (Draft Complete status)
    d["pending_review_count"] = d.get("by_status", {}).get("Draft Complete", 0)
    d["needs_revision_count"] = d.get("by_status", {}).get("Needs Revision", 0)
    return jsonify(d)


@app.route("/api/search")
def api_search():
    from search import search_by_file_number, search_by_keyword, search_by_sponsor
    file_num = request.args.get("file")
    keyword  = request.args.get("keyword")
    sponsor  = request.args.get("sponsor")
    limit    = int(request.args.get("limit", 30))
    if file_num:
        result = search_by_file_number(file_num)
        return jsonify({"type": "matter", "data": result})
    elif keyword:
        return jsonify({"type": "list", "data": search_by_keyword(keyword, limit)})
    elif sponsor:
        return jsonify({"type": "list", "data": search_by_sponsor(sponsor, limit)})
    return jsonify({"type": "list", "data": []})


@app.route("/api/appearance/<int:app_id>")
def api_appearance(app_id):
    from repository import get_appearance_by_id, get_matter_by_file_number, get_meeting_by_id
    a = get_appearance_by_id(app_id)
    if not a:
        return jsonify({"error": "not found"}), 404
    m  = get_matter_by_file_number(a["file_number"]) or {}
    mt = get_meeting_by_id(a["meeting_id"]) or {}
    a["meeting_date"] = mt.get("meeting_date", "")
    a["body_name"]    = mt.get("body_name", "")
    # Attach all appearances for this matter so the Summary tab can render
    # Meeting Notes across every stage with timestamps.
    if m.get("id"):
        from repository import get_all_appearances_for_matter as _gaam
        apps = _gaam(m["id"]) or []
        # enrich each with its meeting date/body so the UI can label by stage
        for ap in apps:
            ap_mt = get_meeting_by_id(ap.get("meeting_id")) or {}
            ap["meeting_date"] = ap_mt.get("meeting_date", "")
            ap["body_name"]    = ap_mt.get("body_name", "")
        m["appearances"] = apps
        # Attach the living legislative-status timeline (parsed from Legistar)
        try:
            from lifecycle import get_timeline_for_matter as _gtm
            m["timeline"] = _gtm(m["id"]) or []
        except Exception:
            m["timeline"] = []
    return jsonify({"appearance": a, "matter": m})


@app.route("/api/appearance/<int:app_id>/history")
def api_appearance_history(app_id):
    from workflow import get_history
    return jsonify(get_history(app_id))


@app.route("/api/appearance/<int:app_id>/workflow", methods=["POST"])
def api_workflow_update(app_id):
    import workflow as wf
    from repository import get_appearance_by_id, get_meeting_by_id, get_matter_by_file_number
    d = request.get_json(force=True)
    by = d.get("changed_by") or "system"

    # Snapshot old values before changes (for notification logic)
    old_app = get_appearance_by_id(app_id) or {}
    old_assignee = old_app.get("assigned_to") or ""
    old_status   = old_app.get("workflow_status") or ""

    if d.get("status"):       wf.set_workflow_status(app_id, d["status"], by)
    if d.get("assigned_to") is not None:
        wf.assign_appearance(app_id, d["assigned_to"], by)
    if d.get("reviewer") is not None:
        wf.set_reviewer(app_id, d["reviewer"], by)
    # due_date: present key with "" means clear it; truthy sets it
    if "due_date" in d:        wf.set_due_date(app_id, d.get("due_date") or "", by)
    if d.get("priority"):     wf.set_priority(app_id, d["priority"], by)

    # ── Smart notifications ──────────────────────────────────────
    try:
        new_app = get_appearance_by_id(app_id) or {}
        mt = get_meeting_by_id(new_app.get("meeting_id", 0)) or {}
        matter = get_matter_by_file_number(new_app.get("file_number", "")) or {}
        # Enrich appearance dict for email templates
        enriched = dict(new_app)
        enriched.setdefault("short_title", matter.get("short_title", ""))
        enriched["meeting_date"] = mt.get("meeting_date", "")
        enriched["body_name"]    = mt.get("body_name", "")

        new_assignee = enriched.get("assigned_to") or ""
        new_status   = enriched.get("workflow_status") or ""

        # 1. New assignment → notify assignee
        if new_assignee and new_assignee != old_assignee:
            notifications.send_assignment_notification(enriched)

        # 2. Status → Draft Complete → notify reviewer
        if new_status == "Draft Complete" and old_status != "Draft Complete":
            notifications.send_draft_complete_notification(enriched)

        # 3. Status → Needs Revision → notify analyst
        if new_status == "Needs Revision":
            notifications.send_revision_notification(enriched)

        # 4. Status → Finalized → notify analyst of approval
        if new_status == "Finalized" and old_status in ("Draft Complete", "In Review"):
            notifications.send_approval_notification(enriched)

    except Exception as _e:
        app.logger.warning(f"Notification error: {_e}")

    return jsonify({"ok": True})


@app.route("/api/appearance/<int:app_id>/notes", methods=["POST"])
def api_notes_update(app_id):
    import workflow as wf
    d = request.get_json(force=True)
    by = d.get("changed_by") or "system"
    if d.get("replace"):
        # Full-replace mode: overwrite the entire field
        if "analyst_working_notes" in d:
            wf.replace_working_notes(app_id, d["analyst_working_notes"], changed_by=by)
        if "reviewer_notes" in d:
            wf.replace_reviewer_notes(app_id, d["reviewer_notes"], changed_by=by)
        if "internal_notes" in d:
            with get_db() as conn:
                conn.execute(
                    "UPDATE appearances SET internal_notes=?, updated_at=? WHERE id=?",
                    (d["internal_notes"], now_iso(), app_id)
                )
    else:
        if d.get("working_notes"):   wf.append_working_notes(app_id, d["working_notes"], changed_by=by)
        if d.get("reviewer_notes"):  wf.append_reviewer_notes(app_id, d["reviewer_notes"], changed_by=by)
    if "finalized_brief" in d:   wf.set_finalized_brief(app_id, d.get("finalized_brief") or "", changed_by=by)
    return jsonify({"ok": True})


@app.route("/api/appearance/<int:app_id>/submit-for-review", methods=["POST"])
def api_submit_for_review(app_id):
    """Snapshot current debrief + notes state, then change status to Draft Complete."""
    import workflow as wf
    d = request.get_json(force=True)
    by = d.get("changed_by") or "system"
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT ai_summary_for_appearance, analyst_working_notes FROM appearances WHERE id=?",
            (app_id,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE appearances SET
                   debrief_snapshot_on_submit=?,
                   analyst_notes_snapshot_on_submit=?,
                   resubmission_comment=NULL,
                   updated_at=?
                   WHERE id=?""",
                (row["ai_summary_for_appearance"] or "",
                 row["analyst_working_notes"] or "",
                 now, app_id)
            )
    wf.set_workflow_status(app_id, "Draft Complete", changed_by=by)
    return jsonify({"ok": True})


@app.route("/api/appearance/<int:app_id>/resubmit", methods=["POST"])
def api_resubmit_for_review(app_id):
    """Save resubmission comment, re-snapshot, change status to Draft Complete."""
    import workflow as wf
    d = request.get_json(force=True)
    by = d.get("changed_by") or "system"
    comment = d.get("resubmission_comment", "")
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT ai_summary_for_appearance, analyst_working_notes FROM appearances WHERE id=?",
            (app_id,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE appearances SET
                   resubmission_comment=?,
                   debrief_snapshot_on_submit=?,
                   analyst_notes_snapshot_on_submit=?,
                   updated_at=?
                   WHERE id=?""",
                (comment,
                 row["ai_summary_for_appearance"] or "",
                 row["analyst_working_notes"] or "",
                 now, app_id)
            )
    wf.set_workflow_status(app_id, "Draft Complete", changed_by=by)
    wf.log_history(app_id, "resubmit",
                   note=f"Resubmitted after revision. Comment: {comment[:200]}" if comment else "Resubmitted after revision",
                   changed_by=by)
    return jsonify({"ok": True})


@app.route("/api/appearance/<int:app_id>/ai", methods=["POST"])
def api_ai_update(app_id):
    import workflow as wf
    d = request.get_json(force=True)
    by = d.get("changed_by") or "system"
    wf.update_ai_summary(app_id, d.get("summary", ""), d.get("watch_points"), by)
    return jsonify({"ok": True})


@app.route("/api/appearance/<int:app_id>/export", methods=["POST"])
def api_export_appearance(app_id):
    """Per-item export: generate Excel + Word for JUST this appearance.
    Registers them as item-level artifacts."""
    from repository import get_appearance_by_id
    from exporters import export_for_appearance
    import artifacts as art
    import workflow as wf
    a = get_appearance_by_id(app_id)
    if not a:
        return jsonify({"error": "not found"}), 404

    output_dir = Path("output") / f"meeting_{a['meeting_id']}" / f"item_{app_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    files = export_for_appearance(app_id, output_dir)

    registered = []
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".xlsx":
            atype, label = "excel_draft", f"Item {a.get('file_number','')} — Tracking"
        elif suffix == ".docx":
            atype, label = "word_draft",  f"Item {a.get('file_number','')} — Research"
        else:
            continue
        aid = art.register_artifact(
            atype, f,
            meeting_id=a["meeting_id"],
            appearance_id=app_id,
            label=label,
            is_final=False,
            supersede_previous=True,
        )
        registered.append(art.get_artifact(aid))

    wf.log_history(app_id, "export", note=f"Regenerated item export ({len(registered)} artifacts)")
    out = [{"name": Path(r["file_path"]).name,
            "url":  f"/api/artifact/{r['id']}/download",
            "label": r.get("label"),
            "artifact_id": r["id"],
            "is_final": bool(r.get("is_final"))}
           for r in registered]
    return jsonify({"ok": True, "files": out, "meeting_id": a["meeting_id"]})


# ── Saved Meetings / Meeting package API ──────────────────────

@app.route("/api/meetings")
def api_meetings():
    import meeting_service
    return jsonify(meeting_service.list_saved_meetings())


@app.route("/api/meeting/<int:meeting_id>")
def api_meeting_detail(meeting_id):
    import meeting_service
    pkg = meeting_service.get_meeting_package(meeting_id)
    if not pkg:
        return jsonify({"error": "not found"}), 404
    return jsonify(pkg)


@app.route("/api/appearance/<int:app_id>/reanalyze", methods=["POST"])
def api_appearance_reanalyze(app_id):
    """Re-run AI analysis on a single appearance. Uses the item PDF text
    (or just the title if no PDF) and stores the result. Runs in a background
    thread so the UI doesn't block."""
    from repository import get_appearance_by_id, get_matter_by_file_number
    a = get_appearance_by_id(app_id)
    if not a:
        return jsonify({"error": "not found"}), 404

    def _run_reanalysis():
        try:
            from analyzer import AgendaAnalyzer
            from repository import get_meeting_by_id
            import db as _db

            matter = get_matter_by_file_number(a["file_number"]) or {}
            meeting = get_meeting_by_id(a["meeting_id"]) or {}
            title = a.get("appearance_title") or matter.get("short_title") or ""
            item_num = a.get("committee_item_number") or a.get("bcc_item_number") or a.get("raw_agenda_item_number") or ""
            committee = meeting.get("body_name") or ""

            # Try to get PDF text from local file
            pdf_text = ""
            pdf_path = a.get("item_pdf_local_path") or ""
            if pdf_path and Path(pdf_path).exists():
                try:
                    import fitz
                    doc = fitz.open(pdf_path)
                    pdf_text = "\n".join(page.get_text() for page in doc)
                    doc.close()
                except Exception as e:
                    app.logger.warning(f"PDF read failed for reanalysis: {e}")

            # Build prior context from earlier appearances
            prior = ""
            from repository import get_all_appearances_for_matter
            all_apps = get_all_appearances_for_matter(matter.get("id", 0))
            for pa in sorted(all_apps, key=lambda x: x.get("meeting_date", "")):
                if pa["id"] == app_id:
                    break
                if (pa.get("ai_summary_for_appearance") or "").strip():
                    prior += f"\n[Prior {pa.get('body_name','')} {pa.get('meeting_date','')}]\n"
                    prior += pa["ai_summary_for_appearance"][:1000] + "\n"

            analyzer = AgendaAnalyzer()
            part1, part2, full, meta = analyzer.analyze_item(
                item_number=item_num,
                title=title,
                pdf_text=pdf_text,
                committee_name=committee,
                prior_context=prior,
            )

            # Store results
            now = now_iso()
            with _db.get_db() as conn:
                conn.execute("""UPDATE appearances SET
                    ai_summary_for_appearance=?,
                    watch_points_for_appearance=?,
                    finalized_brief=CASE WHEN finalized_brief IS NULL OR finalized_brief='' THEN ? ELSE finalized_brief END,
                    analysis_input_hash=?,
                    analysis_tokens_in=?,
                    analysis_tokens_out=?,
                    analysis_cached_tokens=?,
                    analysis_at=?,
                    updated_at=? WHERE id=?""",
                    (part1,
                     "",  # watch points extracted separately if needed
                     part2,
                     meta.get("input_hash",""),
                     meta.get("usage",{}).get("in",0),
                     meta.get("usage",{}).get("out",0),
                     meta.get("usage",{}).get("cached",0),
                     now, now, app_id))

            # Extract watch points from part1 if present
            import re
            wp_match = re.search(r'(?:WATCH POINTS|Watch Points|POINTS TO WATCH)[:\s]*\n([\s\S]*?)(?:\n\n|\Z)', part1)
            if wp_match:
                wp_text = wp_match.group(1).strip()
                with _db.get_db() as conn:
                    conn.execute("UPDATE appearances SET watch_points_for_appearance=? WHERE id=?",
                                 (wp_text, app_id))

            app.logger.info(f"Reanalysis complete for appearance {app_id}: "
                           f"{meta.get('usage',{}).get('in',0)} in / {meta.get('usage',{}).get('out',0)} out tokens")
        except Exception as e:
            app.logger.error(f"Reanalysis failed for appearance {app_id}: {e}")

    import threading
    t = threading.Thread(target=_run_reanalysis, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Reanalysis started in background"})


# ── Batch reanalysis: all appearances for a matter ────────────
_batch_reanalysis = {}  # matter_id -> {total, completed, done}

@app.route("/api/matter/<int:matter_id>/reanalyze-all", methods=["POST"])
def api_matter_reanalyze_all(matter_id):
    """Re-run AI analysis on ALL appearances of a matter (item)."""
    from repository import get_all_appearances_for_matter
    all_apps = get_all_appearances_for_matter(matter_id)
    if not all_apps:
        return jsonify({"error": "no appearances found"}), 404

    _batch_reanalysis[matter_id] = {"total": len(all_apps), "completed": 0, "done": False}

    def _run_batch():
        try:
            from analyzer import AgendaAnalyzer
            from repository import get_appearance_by_id, get_matter_by_file_number, get_meeting_by_id
            import db as _db

            sorted_apps = sorted(all_apps, key=lambda x: x.get("meeting_date", ""))
            for idx, ap in enumerate(sorted_apps):
                try:
                    aid = ap["id"]
                    a = get_appearance_by_id(aid) or ap
                    matter = get_matter_by_file_number(a["file_number"]) or {}
                    meeting = get_meeting_by_id(a["meeting_id"]) or {}
                    title = a.get("appearance_title") or matter.get("short_title") or ""
                    item_num = a.get("committee_item_number") or a.get("bcc_item_number") or a.get("raw_agenda_item_number") or ""
                    committee = meeting.get("body_name") or ""

                    pdf_text = ""
                    pdf_path = a.get("item_pdf_local_path") or ""
                    if pdf_path and Path(pdf_path).exists():
                        try:
                            import fitz
                            doc = fitz.open(pdf_path)
                            pdf_text = "\n".join(page.get_text() for page in doc)
                            doc.close()
                        except Exception:
                            pass

                    # Build prior context from earlier analyzed appearances
                    prior = ""
                    for pa in sorted_apps[:idx]:
                        if (pa.get("ai_summary_for_appearance") or "").strip():
                            prior += f"\n[Prior {pa.get('body_name','')} {pa.get('meeting_date','')}]\n"
                            prior += pa["ai_summary_for_appearance"][:1000] + "\n"

                    analyzer = AgendaAnalyzer()
                    part1, part2, full, meta = analyzer.analyze_item(
                        item_number=item_num, title=title, pdf_text=pdf_text,
                        committee_name=committee, prior_context=prior,
                    )

                    now = now_iso()
                    with _db.get_db() as conn:
                        conn.execute("""UPDATE appearances SET
                            ai_summary_for_appearance=?,
                            watch_points_for_appearance=?,
                            finalized_brief=CASE WHEN finalized_brief IS NULL OR finalized_brief='' THEN ? ELSE finalized_brief END,
                            analysis_input_hash=?, analysis_tokens_in=?,
                            analysis_tokens_out=?, analysis_cached_tokens=?,
                            analysis_at=?, updated_at=? WHERE id=?""",
                            (part1, "", part2, meta.get("input_hash",""),
                             meta.get("usage",{}).get("in",0),
                             meta.get("usage",{}).get("out",0),
                             meta.get("usage",{}).get("cached",0),
                             now, now, aid))

                    # Update the in-memory copy for subsequent prior_context
                    ap["ai_summary_for_appearance"] = part1

                    import re
                    wp_match = re.search(r'(?:WATCH POINTS|Watch Points|POINTS TO WATCH)[:\s]*\n([\s\S]*?)(?:\n\n|\Z)', part1)
                    if wp_match:
                        with _db.get_db() as conn:
                            conn.execute("UPDATE appearances SET watch_points_for_appearance=? WHERE id=?",
                                         (wp_match.group(1).strip(), aid))

                    app.logger.info(f"Batch reanalysis: appearance {aid} done ({idx+1}/{len(sorted_apps)})")
                except Exception as e:
                    app.logger.error(f"Batch reanalysis failed for appearance {ap.get('id')}: {e}")

                _batch_reanalysis[matter_id]["completed"] = idx + 1
        except Exception as e:
            app.logger.error(f"Batch reanalysis error for matter {matter_id}: {e}")
        finally:
            _batch_reanalysis[matter_id]["done"] = True

    import threading
    t = threading.Thread(target=_run_batch, daemon=True)
    t.start()
    return jsonify({"ok": True, "count": len(all_apps)})


@app.route("/api/matter/<int:matter_id>/reanalyze-progress")
def api_matter_reanalyze_progress(matter_id):
    info = _batch_reanalysis.get(matter_id, {"total": 0, "completed": 0, "done": True})
    return jsonify(info)


@app.route("/api/meeting/<int:meeting_id>/reanalyze-all", methods=["POST"])
def api_meeting_reanalyze_all(meeting_id):
    """Re-run AI analysis on ALL items in a meeting."""
    from repository import get_meeting_by_id
    import db as _db
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({"error": "meeting not found"}), 404

    with _db.get_db() as conn:
        apps = conn.execute("SELECT id FROM appearances WHERE meeting_id=?", (meeting_id,)).fetchall()

    if not apps:
        return jsonify({"error": "no appearances in meeting"}), 404

    app_ids = [a["id"] for a in apps]

    def _run_meeting_batch():
        for aid in app_ids:
            try:
                from repository import get_appearance_by_id, get_matter_by_file_number, get_all_appearances_for_matter
                from analyzer import AgendaAnalyzer
                a = get_appearance_by_id(aid)
                if not a:
                    continue
                matter = get_matter_by_file_number(a["file_number"]) or {}
                mt = get_meeting_by_id(a["meeting_id"]) or {}
                title = a.get("appearance_title") or matter.get("short_title") or ""
                item_num = a.get("committee_item_number") or a.get("bcc_item_number") or a.get("raw_agenda_item_number") or ""
                committee = mt.get("body_name") or ""

                pdf_text = ""
                pdf_path = a.get("item_pdf_local_path") or ""
                if pdf_path and Path(pdf_path).exists():
                    try:
                        import fitz
                        doc = fitz.open(pdf_path)
                        pdf_text = "\n".join(page.get_text() for page in doc)
                        doc.close()
                    except Exception:
                        pass

                prior = ""
                all_apps = get_all_appearances_for_matter(matter.get("id", 0))
                for pa in sorted(all_apps, key=lambda x: x.get("meeting_date", "")):
                    if pa["id"] == aid:
                        break
                    if (pa.get("ai_summary_for_appearance") or "").strip():
                        prior += f"\n[Prior {pa.get('body_name','')} {pa.get('meeting_date','')}]\n"
                        prior += pa["ai_summary_for_appearance"][:1000] + "\n"

                analyzer = AgendaAnalyzer()
                part1, part2, full, meta = analyzer.analyze_item(
                    item_number=item_num, title=title, pdf_text=pdf_text,
                    committee_name=committee, prior_context=prior,
                )
                now = now_iso()
                with _db.get_db() as conn:
                    conn.execute("""UPDATE appearances SET
                        ai_summary_for_appearance=?, watch_points_for_appearance=?,
                        finalized_brief=CASE WHEN finalized_brief IS NULL OR finalized_brief='' THEN ? ELSE finalized_brief END,
                        analysis_input_hash=?, analysis_tokens_in=?, analysis_tokens_out=?,
                        analysis_cached_tokens=?, analysis_at=?, updated_at=? WHERE id=?""",
                        (part1, "", part2, meta.get("input_hash",""),
                         meta.get("usage",{}).get("in",0), meta.get("usage",{}).get("out",0),
                         meta.get("usage",{}).get("cached",0), now, now, aid))

                app.logger.info(f"Meeting batch reanalysis: appearance {aid} done")
            except Exception as e:
                app.logger.error(f"Meeting batch reanalysis failed for {aid}: {e}")

    import threading
    t = threading.Thread(target=_run_meeting_batch, daemon=True)
    t.start()
    return jsonify({"ok": True, "count": len(app_ids)})


@app.route("/api/meeting/<int:meeting_id>/regenerate", methods=["POST"])
def api_meeting_regenerate(meeting_id):
    import meeting_service
    registered = meeting_service.generate_draft_export(meeting_id, Path("output"))
    return jsonify({"ok": True, "artifacts": registered})


@app.route("/api/meeting/<int:meeting_id>/finalize", methods=["POST"])
def api_meeting_finalize(meeting_id):
    import meeting_service
    ok, msg, registered = meeting_service.generate_final_export(meeting_id, Path("output"))
    return jsonify({"ok": ok, "message": msg, "artifacts": registered})


@app.route("/api/meeting/<int:meeting_id>/artifacts")
def api_meeting_artifacts(meeting_id):
    import artifacts as art
    return jsonify(art.get_current_artifacts_for_meeting(meeting_id))


# ── Lifecycle / backfill endpoints ────────────────────────────

@app.route("/api/matter/<int:matter_id>/timeline")
def api_matter_timeline(matter_id):
    import lifecycle as lc
    return jsonify(lc.get_timeline_for_matter(matter_id))


@app.route("/api/appearance/<int:appearance_id>/timeline")
def api_appearance_timeline(appearance_id):
    """Return the full lifecycle for this appearance's matter + every
    appearance we have in our DB, merged and sorted chronologically."""
    import lifecycle as lc
    from repository import get_appearance_by_id, get_all_appearances_for_matter
    app_row = get_appearance_by_id(appearance_id)
    if not app_row:
        return jsonify({"error": "not found"}), 404
    mid = app_row["matter_id"]
    history = lc.get_timeline_for_matter(mid)
    apps = get_all_appearances_for_matter(mid)
    our_events = []
    for a in apps:
        our_events.append({
            "event_date": a.get("meeting_date", ""),
            "body_name":  a.get("body_name", ""),
            "action":     f"On agenda — {a.get('workflow_status','New')}",
            "source":     "agendaiq",
            "appearance_id": a["id"],
            "committee_item_number": a.get("committee_item_number", ""),
            "bcc_item_number":       a.get("bcc_item_number", ""),
            "agenda_stage":          a.get("agenda_stage", ""),
            "has_notes": bool((a.get("analyst_working_notes") or "").strip()
                               or (a.get("finalized_brief") or "").strip()),
        })
    merged = sorted(history + our_events,
                    key=lambda e: (e.get("event_date") or "", e.get("source") or ""))
    return jsonify({"matter_id": mid, "events": merged})


@app.route("/api/appearance/<int:appearance_id>/pdf")
def api_appearance_pdf(appearance_id):
    """Serve the locally-cached item PDF if we have one, otherwise redirect
    to the remote item_pdf_url."""
    from flask import send_file, redirect
    from repository import get_appearance_by_id
    a = get_appearance_by_id(appearance_id)
    if not a:
        return "Not found", 404
    lp = a.get("item_pdf_local_path") or ""
    if lp and Path(lp).exists():
        return send_file(lp, as_attachment=True,
                         download_name=f"file_{a.get('file_number','item')}.pdf")
    remote = a.get("item_pdf_url") or ""
    if remote:
        return redirect(remote)
    return "No PDF available", 404


@app.route("/api/backfill/status")
def api_backfill_status():
    """Quick probe: how many appearances are missing Legistar URLs or have no
    lifecycle rows. Used by the UI to decide whether to show a nudge banner."""
    with database.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM appearances").fetchone()["c"]
        missing_url = conn.execute(
            "SELECT COUNT(*) c FROM appearances WHERE matter_url IS NULL OR matter_url=''"
        ).fetchone()["c"]
        missing_pdf = conn.execute(
            "SELECT COUNT(*) c FROM appearances WHERE (item_pdf_url IS NULL OR item_pdf_url='') "
            "AND (item_pdf_local_path IS NULL OR item_pdf_local_path='')"
        ).fetchone()["c"]
        matters = conn.execute("SELECT COUNT(*) c FROM matters").fetchone()["c"]
        matters_with_timeline = conn.execute(
            "SELECT COUNT(DISTINCT matter_id) c FROM matter_timeline"
        ).fetchone()["c"]
        # A matter is "unvisited" if we've never attempted lifecycle parse.
        # Once attempted, lifecycle_refreshed_at is set — even if no events
        # were found (rare but possible for empty/withdrawn matters).
        matters_unvisited = conn.execute(
            """SELECT COUNT(*) c FROM matters
               WHERE lifecycle_refreshed_at IS NULL OR lifecycle_refreshed_at=''"""
        ).fetchone()["c"]
    return jsonify({
        "total_appearances":      total,
        "missing_matter_url":     missing_url,
        "missing_item_pdf":       missing_pdf,
        "total_matters":          matters,
        "matters_with_timeline":  matters_with_timeline,
        "matters_unvisited":      matters_unvisited,
        "needs_backfill":         (missing_url > 0) or (matters_unvisited > 0),
    })


# ── Background backfill with live progress ────────────────────
# The backfill re-hits Legistar once per matter and can take several minutes
# for a full DB. Running it synchronously inside a request blocks every other
# API call on Flask's single dev thread, making the whole UI appear dead. We
# run it in a background thread and expose progress via a polled endpoint
# plus an SSE stream so the UI can show a live progress bar.
import threading as _threading

_bf_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "only_missing": None,
    "total": 0,
    "done": 0,
    "current": "",
    "events": [],       # rolling log of status lines (last ~30)
    "summary": None,    # dict when finished
    "error": None,
}
_bf_lock = _threading.Lock()


def _bf_log(msg: str):
    with _bf_lock:
        _bf_state["current"] = msg
        _bf_state["events"].append({"ts": now_iso(), "msg": msg})
        if len(_bf_state["events"]) > 60:
            _bf_state["events"] = _bf_state["events"][-60:]


def _bf_progress(msg: str):
    # Expected format: "[i/total] …File# XXXX"
    try:
        if msg.startswith("[") and "/" in msg and "]" in msg:
            head = msg[1:msg.index("]")]
            i, total = head.split("/")
            with _bf_lock:
                _bf_state["done"] = int(i)
                _bf_state["total"] = int(total)
    except Exception:
        pass
    _bf_log(msg)


def _run_backfill(only_missing: bool):
    import lifecycle as lc
    try:
        from paths import PDF_CACHE_DIR
        pdf_dir = PDF_CACHE_DIR
        _bf_log(f"Starting backfill (only_missing={only_missing})…")
        summary = lc.backfill_urls_and_lifecycle(
            pdf_dir,
            only_missing_urls=only_missing,
            progress_callback=_bf_progress,
        )
        with _bf_lock:
            _bf_state["summary"] = summary
            _bf_state["finished_at"] = now_iso()
            _bf_state["running"] = False
        _bf_log(
            f"Done — {summary.get('matters',0)} matters · "
            f"{summary.get('urls_filled',0)} links · "
            f"{summary.get('pdfs_downloaded',0)} PDFs · "
            f"{summary.get('timeline_events',0)} lifecycle events · "
            f"{summary.get('stub_appearances',0)} committee stubs"
        )
    except Exception as e:
        with _bf_lock:
            _bf_state["error"] = str(e)
            _bf_state["finished_at"] = now_iso()
            _bf_state["running"] = False
        _bf_log(f"Backfill failed: {e}")


@app.route("/api/backfill/urls-and-lifecycle", methods=["POST"])
def api_backfill():
    """Kick off the backfill in a background thread. Returns immediately.
    Poll /api/backfill/progress or subscribe to /api/backfill/stream for
    live updates."""
    payload = request.get_json(silent=True) or {}
    only_missing = bool(payload.get("only_missing", True))
    with _bf_lock:
        if _bf_state["running"]:
            return jsonify({"ok": False, "error": "Already running",
                            "progress": _bf_public_state()}), 409
        _bf_state.update({
            "running": True, "started_at": now_iso(), "finished_at": None,
            "only_missing": only_missing, "total": 0, "done": 0,
            "current": "queued…", "events": [], "summary": None, "error": None,
        })
    t = _threading.Thread(target=_run_backfill, args=(only_missing,), daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "progress": _bf_public_state()})


def _bf_public_state():
    with _bf_lock:
        return {
            "running":     _bf_state["running"],
            "started_at":  _bf_state["started_at"],
            "finished_at": _bf_state["finished_at"],
            "total":       _bf_state["total"],
            "done":        _bf_state["done"],
            "current":     _bf_state["current"],
            "percent":     round((_bf_state["done"] / _bf_state["total"]) * 100)
                              if _bf_state["total"] else 0,
            "events":      list(_bf_state["events"][-20:]),
            "summary":     _bf_state["summary"],
            "error":       _bf_state["error"],
        }


@app.route("/api/backfill/progress")
def api_backfill_progress():
    return jsonify(_bf_public_state())


# ── Transcript Backfill ──────────────────────────────────────────
_tx_lock = _threading.Lock()
_tx_state = {
    "running": False, "phase": None, "pct": 0, "msg": "",
    "done": False, "result": None,
}

def _tx_emit(msg, phase="transcript", pct=0):
    with _tx_lock:
        _tx_state["msg"] = msg
        _tx_state["phase"] = phase
        _tx_state["pct"] = pct

@app.route("/api/backfill/transcript", methods=["POST"])
def api_backfill_transcript():
    data = request.get_json(force=True)
    meeting_id = data.get("meeting_id")
    video_url = data.get("video_url")
    raw_transcript = data.get("raw_transcript")
    if not meeting_id:
        return jsonify({"ok": False, "error": "meeting_id required"}), 400

    with _tx_lock:
        if _tx_state["running"]:
            return jsonify({"ok": False, "error": "Transcript backfill already running"}), 409

    # Start the async pipeline — Granicus (Strategy 1) and YouTube (Strategy 2)
    # are both handled inside backfill_transcript()
    with _tx_lock:
        _tx_state.update({
            "running": True, "phase": "transcript", "pct": 5,
            "msg": "Searching for meeting recording…",
            "done": False, "result": None,
        })

    def _run():
        try:
            import transcript as tx
            result = tx.backfill_transcript(
                meeting_id,
                video_url=video_url,
                raw_transcript=raw_transcript,
                emit=_tx_emit,
            )
            with _tx_lock:
                _tx_state["result"] = result
                _tx_state["done"] = True
                _tx_state["running"] = False
                _tx_state["pct"] = 100
                _tx_state["msg"] = "Done"
        except Exception as e:
            log.exception("Transcript backfill error")
            with _tx_lock:
                _tx_state["result"] = {"status": "error", "message": str(e)}
                _tx_state["done"] = True
                _tx_state["running"] = False
                _tx_state["msg"] = f"Error: {e}"

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/backfill/transcript/progress")
def api_transcript_progress():
    with _tx_lock:
        return jsonify({
            "phase": _tx_state["phase"],
            "pct": _tx_state["pct"],
            "msg": _tx_state["msg"],
            "done": _tx_state["done"],
            "result": _tx_state["result"],
        })


@app.route("/api/artifact/<int:artifact_id>/download")
def api_artifact_download(artifact_id):
    import artifacts as art
    row = art.get_artifact(artifact_id)
    if not row:
        return "Not found", 404
    fp = Path(row["file_path"])
    if not fp.exists():
        return "File missing on disk", 404
    return send_file(str(fp.resolve()), as_attachment=True, download_name=fp.name)


@app.route("/api/workflow")
def api_workflow():
    from datetime import datetime, timedelta
    from search import list_appearances_by_status
    import db as _db

    status   = request.args.get("status")
    assigned = request.args.get("assigned")
    reviewer = request.args.get("reviewer")
    due_filter = request.args.get("due")

    with _db.get_db() as conn:
        where = ["1=1"]
        params = []
        if status:
            where.append("a.workflow_status=?"); params.append(status)
        if reviewer:
            where.append("a.reviewer=?"); params.append(reviewer)
        if assigned == "__unassigned__":
            where.append("(a.assigned_to IS NULL OR a.assigned_to='')")
        elif assigned:
            where.append("a.assigned_to=?"); params.append(assigned)

        today = datetime.utcnow().date()
        if due_filter == "overdue":
            where.append("a.due_date < ? AND a.due_date != '' AND a.due_date IS NOT NULL")
            params.append(today.strftime("%Y-%m-%d"))
        elif due_filter and due_filter.isdigit():
            cutoff = (today + timedelta(days=int(due_filter))).strftime("%Y-%m-%d")
            where.append("a.due_date >= ? AND a.due_date <= ? AND a.due_date != ''")
            params.extend([today.strftime("%Y-%m-%d"), cutoff])

        rows = conn.execute(
            f"""SELECT a.*, m.file_number, m.short_title, m.sponsor,
                       mt.meeting_date, mt.body_name
                FROM appearances a
                JOIN matters m ON m.id=a.matter_id
                JOIN meetings mt ON mt.id=a.meeting_id
                WHERE {' AND '.join(where)}
                ORDER BY mt.meeting_date DESC, a.id DESC
                LIMIT 200""",
            params
        ).fetchall()

    today_str = today.strftime("%Y-%m-%d")
    result = []
    for r in rows:
        row = dict(r)
        due = row.get("due_date") or ""
        if due:
            if due < today_str: row["_due_class"]="due-over"; row["_due_label"]=f"⚠ {due}"
            elif due <= (today + timedelta(days=7)).strftime("%Y-%m-%d"):
                row["_due_class"]="due-soon"; row["_due_label"]=f"⏰ {due}"
            else: row["_due_class"]="due-ok"; row["_due_label"]=due
        result.append(row)
    return jsonify(result)


@app.route("/api/config")
def api_config_get():
    cfg = notifications.load_config()
    # Don't send password over wire
    safe = {k: v for k, v in cfg.items() if k != "smtp_password"}
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    cfg = notifications.load_config()
    new = request.get_json(force=True)
    cfg.update(new)
    notifications.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    try:
        cfg = notifications.load_config()
        notifications._send_email(cfg, "[AgendaIQ] Test Email",
            "<p>This is a test email from AgendaIQ. Email notifications are working!</p>")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/test-webhook", methods=["POST"])
def api_test_webhook():
    try:
        cfg = notifications.load_config()
        result = notifications.test_webhook(cfg)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _persist_progress(output_dir: Path, msg: dict):
    """Append one progress/complete/error event to progress.jsonl for resume.
    Silent on failure — in-memory queue is the authoritative source."""
    try:
        with open(output_dir / "progress.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")
    except Exception:
        pass


def _read_persisted_progress(output_dir: Path) -> list[dict]:
    """Read back all events written so far for this job. Used when a user
    reloads the browser and we need to replay what they missed."""
    p = output_dir / "progress.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return out
    return out


@app.route("/api/run", methods=["POST"])
def api_run():
    from oca_agenda_agent_v6 import process_committees
    from scraper import MiamiDadeScraper
    from analyzer import AgendaAnalyzer

    data = request.get_json(force=True)
    date_str      = data.get("date")
    from_date_str = data.get("from_date")
    selected      = data.get("committees", [])

    job_id     = str(uuid.uuid4())
    output_dir = Path("output") / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    # subscribers: list of queue.Queue, one per SSE connection. The cb
    # broadcasts to all of them so reconnecting clients get their own copy.
    JOBS[job_id] = {
        "status":     "running",
        "subscribers": [],
        "output_dir": output_dir,
        "files":      [],
        "started_at": now_iso(),
        "label":      ", ".join(selected) if selected else "All committees",
        "date":       from_date_str or date_str or "",
        "mode":       "range" if from_date_str else "single",
    }
    # Persist job metadata so a future process can still describe it
    try:
        (output_dir / "job.json").write_text(json.dumps({
            "job_id":     job_id,
            "started_at": JOBS[job_id]["started_at"],
            "label":      JOBS[job_id]["label"],
            "date":       JOBS[job_id]["date"],
            "mode":       JOBS[job_id]["mode"],
            "committees": selected,
        }), encoding="utf-8")
    except Exception:
        pass

    def _broadcast(payload):
        """Push an event to every connected SSE listener AND to disk."""
        _persist_progress(output_dir, payload)
        dead = []
        for i, sq in enumerate(JOBS[job_id]["subscribers"]):
            try:
                sq.put_nowait(payload)
            except Exception:
                dead.append(i)
        # Prune dead subscribers (iterate in reverse to keep indices stable)
        for i in reversed(dead):
            try:
                JOBS[job_id]["subscribers"].pop(i)
            except Exception:
                pass

    def _run():
        try:
            api_key  = load_api_key()
            analyzer = AgendaAnalyzer(api_key)
            scraper  = MiamiDadeScraper()
            all_c    = scraper.get_committees()
            matched  = {k: v for k, v in all_c.items() if k in selected} if selected else all_c
            parsed, mode = (parse_date_arg(from_date_str), "range") if from_date_str \
                      else (parse_date_arg(date_str), "single")

            def cb(msg, phase=None, pct=None):
                payload = {
                    "type":    "progress",
                    "message": msg,
                    "phase":   phase,
                    "pct":     pct,
                    "ts":      now_iso(),
                }
                _broadcast(payload)

            results = process_committees(matched, parsed, mode, output_dir, analyzer, scraper, cb)
            files = [f.name for f in sorted(output_dir.glob("*Part1*.xlsx")) +
                                      sorted(output_dir.glob("*Part2*.docx"))]
            JOBS[job_id].update({"status": "complete", "files": files})
            _broadcast({"type": "complete", "results": results, "files": files})
        except Exception as exc:
            JOBS[job_id]["status"] = "error"
            _broadcast({"type": "error", "message": str(exc)})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/active")
def api_jobs_active():
    """Return jobs that are still running or just finished, so the browser
    can reattach after a reload. Sorted newest first."""
    out = []
    for jid, meta in JOBS.items():
        out.append({
            "job_id":     jid,
            "status":     meta.get("status"),
            "started_at": meta.get("started_at"),
            "label":      meta.get("label"),
            "date":       meta.get("date"),
            "mode":       meta.get("mode"),
            "files":      meta.get("files", []),
        })
    out.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return jsonify({"jobs": out})


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    if job_id not in JOBS:
        return jsonify({"error": "not found"}), 404
    replay = request.args.get("replay", "0") == "1"
    meta   = JOBS[job_id]
    output_dir = meta["output_dir"]

    # Register a fresh queue for this SSE connection so multiple
    # tabs / reconnects each get their own copy of live events.
    my_q = queue.Queue()
    meta["subscribers"].append(my_q)

    def generate():
        try:
            if replay:
                for event in _read_persisted_progress(output_dir):
                    event = dict(event)
                    event["replay"] = True
                    yield f"data: {json.dumps(event)}\n\n"
                # If job already finished, we're done
                if meta.get("status") in ("complete", "error"):
                    return
            while True:
                try:
                    msg = my_q.get(timeout=30)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg["type"] in ("complete", "error"):
                        break
                except queue.Empty:
                    yield f"data: {json.dumps({'type':'ping'})}\n\n"
        finally:
            # Unsubscribe when the connection closes
            try:
                meta["subscribers"].remove(my_q)
            except (ValueError, KeyError):
                pass
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/download/<job_id>/<filename>")
def api_download(job_id, filename):
    if job_id not in JOBS:
        return "Not found", 404
    fp = JOBS[job_id]["output_dir"] / Path(filename).name
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp.resolve()), as_attachment=True, download_name=fp.name)


# ═══════════════════════════════════════════════════════════════
# AI Chat endpoints — private per-user conversations about items
# ═══════════════════════════════════════════════════════════════

@app.route("/api/chat/<int:appearance_id>/messages")
def api_chat_history(appearance_id):
    """Return chat history for (appearance, user). Private per user."""
    user = _current_user()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, role, content, web_search, appended_to, created_at
               FROM chat_messages
               WHERE appearance_id=? AND username=?
               ORDER BY created_at ASC""",
            (appearance_id, user)
        ).fetchall()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/api/chat/<int:appearance_id>/send", methods=["POST"])
def api_chat_send(appearance_id):
    """Send a user message and get an AI response. Optionally use web search."""
    import httpx
    from anthropic import Anthropic

    user = _current_user()
    data = request.get_json(force=True)
    user_msg = (data.get("message") or "").strip()
    use_web  = bool(data.get("web_search", False))
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # Single DB query: fetch item context + history together
    with get_db() as conn:
        app_row = conn.execute(
            """SELECT a.*, m.short_title, m.full_title, m.file_number as matter_file_number
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               WHERE a.id=?""",
            (appearance_id,)
        ).fetchone()
        if not app_row:
            return jsonify({"error": "Appearance not found"}), 404
        app_row = dict(app_row)

        history = conn.execute(
            """SELECT role, content FROM chat_messages
               WHERE appearance_id=? AND username=?
               ORDER BY created_at ASC""",
            (appearance_id, user)
        ).fetchall()
        history = [dict(r) for r in history][-20:]

        # Save user message immediately (same transaction)
        conn.execute(
            "INSERT INTO chat_messages (appearance_id, username, role, content, web_search, created_at) VALUES (?,?,?,?,?,?)",
            (appearance_id, user, "user", user_msg, 0, now_iso())
        )

    # Build system prompt (keep it lean for speed)
    title = app_row.get("appearance_title") or app_row.get("short_title") or app_row.get("full_title") or ""
    file_num = app_row.get("matter_file_number") or app_row.get("file_number") or ""
    ai_summary = app_row.get("ai_summary_for_appearance") or ""
    leg_hist = app_row.get("leg_history_summary") or ""

    sys_prompt = (
        "You are a research assistant for the Office of the Commission Auditor (OCA) at Miami-Dade County. "
        "You are helping a researcher analyze a specific agenda item. Be concise, factual, and professional. "
        "Do NOT use markdown formatting. Use plain text only. For bullets, use dashes.\n\n"
        f"ITEM: {file_num} — {title}\n"
    )
    if ai_summary:
        sys_prompt += f"\nANALYSIS:\n{ai_summary[:1500]}\n"
    if leg_hist:
        sys_prompt += f"\nLEGISLATIVE HISTORY:\n{leg_hist[:500]}\n"

    # Build messages array
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": user_msg})

    # Call Claude
    try:
        api_key = load_api_key()
        client = Anthropic(api_key=api_key, http_client=httpx.Client(verify=False))
        kw = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,
            "system": sys_prompt,
            "messages": messages,
        }
        if use_web:
            kw["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

        resp = client.messages.create(**kw)
        ai_text = "\n".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if not ai_text:
            ai_text = "(No response generated)"
    except Exception as e:
        ai_text = f"[Error: {e}]"

    # Save assistant message
    now2 = now_iso()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (appearance_id, username, role, content, web_search, created_at) VALUES (?,?,?,?,?,?)",
            (appearance_id, user, "assistant", ai_text, 1 if use_web else 0, now2)
        )

    return jsonify({"role": "assistant", "content": ai_text, "created_at": now2})


@app.route("/api/chat/<int:appearance_id>/append", methods=["POST"])
def api_chat_append(appearance_id):
    """Append a chat message's content to working notes or Part 1."""
    user = _current_user()
    data = request.get_json(force=True)
    msg_id = data.get("message_id")
    target = data.get("target", "notes")  # 'notes', 'part1', or 'deep_research'

    if target not in ("notes", "part1", "deep_research"):
        return jsonify({"error": "Invalid target"}), 400

    # Fetch the chat message
    with get_db() as conn:
        msg_row = conn.execute(
            "SELECT * FROM chat_messages WHERE id=? AND appearance_id=? AND username=?",
            (msg_id, appearance_id, user)
        ).fetchone()
    if not msg_row:
        return jsonify({"error": "Message not found"}), 404

    content = msg_row["content"]
    now = now_iso()

    with get_db() as conn:
        if target == "notes":
            existing = conn.execute(
                "SELECT analyst_working_notes FROM appearances WHERE id=?", (appearance_id,)
            ).fetchone()
            old_notes = (existing["analyst_working_notes"] or "") if existing else ""
            separator = "\n\n--- AI Chat Note (added by {}) ---\n".format(user)
            new_notes = old_notes + separator + content
            conn.execute(
                "UPDATE appearances SET analyst_working_notes=?, analyst_notes_updated_at=?, analyst_notes_updated_by=?, updated_at=? WHERE id=?",
                (new_notes, now, user, now, appearance_id)
            )
        elif target == "deep_research":
            existing = conn.execute(
                "SELECT finalized_brief FROM appearances WHERE id=?", (appearance_id,)
            ).fetchone()
            old_text = (existing["finalized_brief"] or "") if existing else ""
            separator = "\n\n--- AI Chat Research (added by {}) ---\n".format(user)
            new_text = old_text + separator + content
            conn.execute(
                "UPDATE appearances SET finalized_brief=?, finalized_brief_updated_at=?, finalized_brief_updated_by=?, updated_at=? WHERE id=?",
                (new_text, now, user, now, appearance_id)
            )
        else:  # part1
            existing = conn.execute(
                "SELECT ai_summary_for_appearance FROM appearances WHERE id=?", (appearance_id,)
            ).fetchone()
            old_text = (existing["ai_summary_for_appearance"] or "") if existing else ""
            separator = "\n\n--- Additional Context (added by {}) ---\n".format(user)
            new_text = old_text + separator + content
            conn.execute(
                "UPDATE appearances SET ai_summary_for_appearance=?, updated_at=? WHERE id=?",
                (new_text, now, appearance_id)
            )

        # Mark the chat message as appended
        conn.execute(
            "UPDATE chat_messages SET appended_to=? WHERE id=?", (target, msg_id)
        )

    return jsonify({"ok": True, "target": target})


@app.route("/api/maintenance/vacuum", methods=["POST"])
def api_maintenance_vacuum():
    """Reclaim disk space: checkpoint WAL, vacuum, and report DB size."""
    import os
    try:
        with get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # VACUUM must run outside a transaction
        c = get_connection()
        c.execute("VACUUM")
        c.close()
        db_size = os.path.getsize(str(DB_PATH))
        wal_path = str(DB_PATH) + "-wal"
        wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        return jsonify({
            "ok": True,
            "db_size_mb": round(db_size / 1048576, 2),
            "wal_size_mb": round(wal_size / 1048576, 2),
            "total_mb": round((db_size + wal_size) / 1048576, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maintenance/db-size")
def api_maintenance_db_size():
    """Report current database file sizes."""
    import os
    db_size = os.path.getsize(str(DB_PATH)) if DB_PATH.exists() else 0
    wal_path = str(DB_PATH) + "-wal"
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    shm_path = str(DB_PATH) + "-shm"
    shm_size = os.path.getsize(shm_path) if os.path.exists(shm_path) else 0
    return jsonify({
        "db_size_mb": round(db_size / 1048576, 2),
        "wal_size_mb": round(wal_size / 1048576, 2),
        "shm_size_mb": round(shm_size / 1048576, 2),
        "total_mb": round((db_size + wal_size + shm_size) / 1048576, 2),
    })


@app.route("/api/maintenance/diagnose")
def api_maintenance_diagnose():
    """Full diagnostic: disk, DB integrity, row counts."""
    import os
    diag = {"disk": {}, "db": {}, "counts": {}, "errors": []}
    try:
        st = os.statvfs(str(DB_PATH.parent))
        diag["disk"]["free_mb"] = round((st.f_bavail * st.f_frsize) / 1048576, 2)
        diag["disk"]["total_mb"] = round((st.f_blocks * st.f_frsize) / 1048576, 2)
        diag["disk"]["used_pct"] = round(100 * (1 - st.f_bavail / st.f_blocks), 1)
    except Exception as e:
        diag["errors"].append(f"Disk check failed: {e}")

    try:
        db_size = os.path.getsize(str(DB_PATH)) if DB_PATH.exists() else 0
        wal_path = str(DB_PATH) + "-wal"
        wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        diag["db"]["db_mb"] = round(db_size / 1048576, 2)
        diag["db"]["wal_mb"] = round(wal_size / 1048576, 2)
        diag["db"]["exists"] = DB_PATH.exists()
    except Exception as e:
        diag["errors"].append(f"DB size check failed: {e}")

    try:
        import sqlite3
        c = sqlite3.connect(str(DB_PATH))
        result = c.execute("PRAGMA integrity_check").fetchone()
        diag["db"]["integrity"] = result[0] if result else "unknown"
        diag["db"]["journal_mode"] = c.execute("PRAGMA journal_mode").fetchone()[0]

        for table in ["meetings", "appearances", "matters", "matter_timeline",
                      "workflow_history", "chat_messages", "artifacts"]:
            try:
                cnt = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                diag["counts"][table] = cnt
            except Exception as e:
                diag["counts"][table] = f"ERROR: {e}"
        c.close()
    except Exception as e:
        diag["errors"].append(f"DB query failed: {e}")

    return jsonify(diag)


@app.route("/api/maintenance/vacuum", methods=["POST"])
def api_maintenance_vacuum():
    """Reclaim disk space: WAL checkpoint, VACUUM, clear caches."""
    results = []
    try:
        # 1. Reclaim via db.py helper
        from db import _try_reclaim_disk_space
        freed = _try_reclaim_disk_space()
        results.append(f"Reclaim freed {freed / 1048576:.1f} MB")

        # 2. VACUUM to compact the DB file
        import sqlite3 as _sq
        c = _sq.connect(str(DB_PATH))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        results.append("WAL checkpoint done")
        try:
            c.execute("VACUUM")
            results.append("VACUUM done")
        except Exception as ve:
            results.append(f"VACUUM failed (need ~2x DB free space): {ve}")
        c.close()

        # 3. Report new free space
        st = os.statvfs(str(DB_PATH.parent))
        free_mb = round((st.f_bavail * st.f_frsize) / 1048576, 2)
        results.append(f"Free space now: {free_mb} MB")
    except Exception as e:
        results.append(f"Error: {e}")

    return jsonify({"ok": True, "results": results})


@app.route("/api/maintenance/sample-data")
def api_maintenance_sample_data():
    """Quick peek at actual data — confirms whether rows exist and have content."""
    sample = {}
    try:
        with db.get_db() as conn:
            # Recent meetings
            rows = conn.execute(
                "SELECT id, body_name, meeting_date, agenda_status FROM meetings ORDER BY meeting_date DESC LIMIT 5"
            ).fetchall()
            sample["recent_meetings"] = [dict(r) for r in rows]

            # Recent appearances
            rows = conn.execute(
                "SELECT a.id, a.file_number, a.workflow_status, m.meeting_date, m.body_name "
                "FROM appearances a JOIN meetings m ON a.meeting_id=m.id "
                "ORDER BY m.meeting_date DESC LIMIT 5"
            ).fetchall()
            sample["recent_appearances"] = [dict(r) for r in rows]

            # Check if any meetings have appearances
            row = conn.execute(
                "SELECT COUNT(DISTINCT meeting_id) as meetings_with_items FROM appearances"
            ).fetchone()
            sample["meetings_with_items"] = row["meetings_with_items"]
    except Exception as e:
        sample["error"] = str(e)

    return jsonify(sample)


# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    init_db()
    notifications.start_background_checker(interval_hours=1)
    print(f"\n  AgendaIQ v6 → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
