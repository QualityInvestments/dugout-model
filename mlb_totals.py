#!/usr/bin/env python3
"""
DUGOUT MODEL - local edition
============================================================================
Pulls today's MLB slate plus every team's runs/game and ERA and each
starting pitcher's season line from the free MLB Stats API (server-side, so
no CORS and no bot-blocking), runs the total-runs model, and writes a
self-contained dugout_report.html you open in any browser. You type the
posted total per game to get the lean. Re-run each morning to refresh.

Usage:
    pip install requests
    python mlb_totals.py            # today's games
    python mlb_totals.py 2026-06-10 # a specific date (YYYY-MM-DD)

The HTML is fully offline once generated - all stats are baked in.
============================================================================
"""

import os
import sys
import json
import datetime as dt
import webbrowser
from pathlib import Path

import requests

# ---- model constants (edit here) ------------------------------------------
SP_SHARE       = 0.60   # starter's share of a game's innings
SP_REGRESS_IP  = 25     # innings to regress a starter's RA/9 toward lg avg
DEFAULT_LG     = 4.45   # fallback league runs / team / game
TIMEOUT        = 15

# ---- park factors (approx, runs basis; 1.00 = neutral) by MLB team id ------
PARK = {
    115: 1.20, 113: 1.08, 111: 1.07, 109: 1.04, 118: 1.03, 143: 1.03,
    140: 1.02, 110: 1.02, 108: 1.01, 141: 1.01, 117: 1.01, 158: 1.00,
    120: 1.00, 144: 1.00, 112: 1.00, 142: 1.00, 147: 0.99, 145: 1.00,
    138: 0.99, 119: 0.98, 114: 0.98, 121: 0.97, 116: 0.97, 134: 0.97,
    133: 0.98, 139: 0.96, 146: 0.96, 135: 0.95, 136: 0.93, 137: 0.92,
}

API = "https://statsapi.mlb.com/api/v1"
ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "dugout-model/1.0 (personal research)"})

# Canonical nicknames, used to match ESPN team names to MLB team names so we
# can attach each game's posted over/under line. Substring match handles the
# "Oakland Athletics" / "Athletics Athletics" and Red/White Sox cases.
NICKS = [
    "Diamondbacks", "Braves", "Orioles", "Red Sox", "Cubs", "White Sox", "Reds",
    "Guardians", "Rockies", "Tigers", "Astros", "Royals", "Angels", "Dodgers",
    "Marlins", "Brewers", "Twins", "Mets", "Yankees", "Athletics", "Phillies",
    "Pirates", "Padres", "Giants", "Mariners", "Cardinals", "Rays", "Rangers",
    "Blue Jays", "Nationals",
]


def nick(name):
    n = (name or "").lower()
    for k in NICKS:
        if k.lower() in n:
            return k
    return n.strip()


def fetch_espn_lines(date_str):
    """Map each matchup to its posted over/under from ESPN's public scoreboard."""
    data = get_json(f"{ESPN_SB}?dates={date_str.replace('-', '')}")
    lines = {}
    for ev in (data or {}).get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        sides = {}
        for c in comp.get("competitors", []):
            nm = c.get("team", {}).get("displayName") or c.get("team", {}).get("name")
            sides[c.get("homeAway")] = nick(nm)
        ou = None
        for o in comp.get("odds") or []:
            if o.get("overUnder") is not None:
                ou = o["overUnder"]
                break
        if "home" in sides and "away" in sides and ou is not None:
            lines[frozenset((sides["home"], sides["away"]))] = ou
    return lines


# ---- helpers ---------------------------------------------------------------
def get_json(url):
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("  ! fetch failed:", url.split("?")[0], "-", e)
        return None


def parse_ip(ip):
    """MLB innings-pitched strings use .1 = 1/3, .2 = 2/3."""
    if ip is None:
        return 0.0
    whole, _, frac = str(ip).partition(".")
    w = int(whole) if whole.lstrip("-").isdigit() else 0
    f = int(frac) / 3 if frac.isdigit() else 0.0
    return w + f


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def prevention(sp, team, lg):
    """Blend a starter's RA/9 with the team staff RA/9."""
    if sp is not None and team is not None:
        return SP_SHARE * sp + (1 - SP_SHARE) * team
    return sp if sp is not None else (team if team is not None else lg)


def project(away_rg, home_rg, away_prev, home_prev, park, lg):
    """Expected runs = RS/G * opponent run prevention / lg avg, x ballpark."""
    away_exp = (away_rg * home_prev / lg) * park   # away bats vs home arms
    home_exp = (home_rg * away_prev / lg) * park   # home bats vs away arms
    return away_exp, home_exp, away_exp + home_exp


# ---- data load -------------------------------------------------------------
def fetch_team_stats(team_id, season):
    url = f"{API}/teams/{team_id}/stats?season={season}&stats=season&group=hitting,pitching"
    data = get_json(url)
    rg = ra9 = None
    if data:
        for grp in data.get("stats", []):
            name = grp.get("group", {}).get("displayName")
            splits = grp.get("splits") or []
            if not splits:
                continue
            stat = splits[0].get("stat", {})
            if name == "hitting":
                gp = stat.get("gamesPlayed") or 0
                runs = stat.get("runs")
                if gp and runs is not None:
                    rg = runs / gp
            elif name == "pitching":
                ip = parse_ip(stat.get("inningsPitched"))
                runs = stat.get("runs")
                if ip and runs is not None:
                    ra9 = runs / ip * 9
    return {"rg": rg, "ra9": ra9}


def fetch_pitcher(pid, season):
    url = f"{API}/people/{pid}/stats?season={season}&stats=season&group=pitching"
    data = get_json(url)
    if not data:
        return {"ip": 0.0, "ra9": None}
    try:
        stat = data["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError):
        return {"ip": 0.0, "ra9": None}
    ip = parse_ip(stat.get("inningsPitched"))
    runs = stat.get("runs")
    ra9 = (runs / ip * 9) if ip and runs is not None else None
    return {"ip": ip, "ra9": ra9}


def stabilize(pp, lg):
    """Regress a starter's RA/9 toward league average for small samples."""
    if not pp or not pp.get("ip") or pp.get("ra9") is None:
        return {"ra9": lg, "has": False, "raw": None}
    ip, ra9 = pp["ip"], pp["ra9"]
    reg = (ip * ra9 + SP_REGRESS_IP * lg) / (ip + SP_REGRESS_IP)
    return {"ra9": reg, "has": True, "raw": ra9}


def build_slate(date_str):
    season = date_str[:4]
    print(f"Fetching slate for {date_str} ...")
    sched = get_json(
        f"{API}/schedule?sportId=1&date={date_str}"
        f"&hydrate=probablePitcher,team,venue"
    )
    games = (sched or {}).get("dates", [{}])
    games = games[0].get("games", []) if games else []
    if not games:
        return [], DEFAULT_LG

    team_ids, pp_ids = set(), set()
    for g in games:
        team_ids.add(g["teams"]["away"]["team"]["id"])
        team_ids.add(g["teams"]["home"]["team"]["id"])
        for side in ("away", "home"):
            pp = g["teams"][side].get("probablePitcher")
            if pp:
                pp_ids.add(pp["id"])

    print(f"  {len(games)} games, {len(team_ids)} teams, {len(pp_ids)} starters")
    team_map = {tid: fetch_team_stats(tid, season) for tid in team_ids}
    pp_map = {pid: fetch_pitcher(pid, season) for pid in pp_ids}
    espn_lines = fetch_espn_lines(date_str)
    print(f"  {len(espn_lines)} over/under lines from ESPN")

    rgs = [t["rg"] for t in team_map.values() if t["rg"] is not None]
    lg = clamp(sum(rgs) / len(rgs), 3.8, 5.2) if rgs else DEFAULT_LG

    rows = []
    for g in games:
        a, h = g["teams"]["away"], g["teams"]["home"]
        a_id, h_id = a["team"]["id"], h["team"]["id"]
        at, ht = team_map.get(a_id, {}), team_map.get(h_id, {})
        a_pp = a.get("probablePitcher")
        h_pp = h.get("probablePitcher")
        a_sp = stabilize(pp_map.get(a_pp["id"]) if a_pp else None, lg)
        h_sp = stabilize(pp_map.get(h_pp["id"]) if h_pp else None, lg)

        away_rg = at.get("rg") or lg
        home_rg = ht.get("rg") or lg
        away_prev = prevention(a_sp["ra9"], at.get("ra9") or lg, lg)
        home_prev = prevention(h_sp["ra9"], ht.get("ra9") or lg, lg)
        park = PARK.get(h_id, 1.0)
        a_exp, h_exp, total = project(away_rg, home_rg, away_prev, home_prev, park, lg)

        when = ""
        if g.get("gameDate"):
            try:
                t = dt.datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
                when = t.astimezone().strftime("%-I:%M %p")
            except Exception:
                when = ""

        rows.append({
            "away": a["team"]["name"], "home": h["team"]["name"],
            "away_rg": round(away_rg, 2), "home_rg": round(home_rg, 2),
            "away_sp": _sp_label(a_sp, a_pp), "home_sp": _sp_label(h_sp, h_pp),
            "away_pn": (a_pp or {}).get("fullName", "Probable TBD"),
            "home_pn": (h_pp or {}).get("fullName", "Probable TBD"),
            "away_exp": round(a_exp, 1), "home_exp": round(h_exp, 1),
            "park": round(park, 2), "total": round(total, 1),
            "time": when, "status": g.get("status", {}).get("detailedState", ""),
            "line": espn_lines.get(frozenset((nick(a["team"]["name"]),
                                              nick(h["team"]["name"])))),
        })
    return rows, lg


def _sp_label(sp, pp):
    if sp["has"]:
        return f"{sp['ra9']:.2f} RA/9"
    return "no data - lg avg" if pp else "TBD - lg avg"


# ---- HTML report -----------------------------------------------------------
def render_html(games, lg, date_str):
    stamp = dt.datetime.now().strftime("%b %-d, %Y at %-I:%M %p")
    cards = "".join(_card_html(g) for g in games) or \
        f'<div class="dm-msg">No games scheduled for {date_str}.</div>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dugout Model - {date_str}</title>
<style>{_CSS}</style></head>
<body><div class="dm-wrap">
  <header class="dm-header">
    <div>
      <h1 class="dm-title">DUGOUT<span>MODEL</span></h1>
      <p class="dm-tag">Projected totals, not predictions. You bring the line; the math brings the lean.</p>
    </div>
    <div class="dm-meta">
      <div class="dm-date">{date_str}</div>
      <div class="dm-stamp">generated {stamp}</div>
    </div>
  </header>
  <div class="dm-bar">
    <div class="dm-thr"><span>edge threshold</span>
      <input id="thr" type="range" min="0.25" max="2" step="0.25" value="0.75">
      <b class="dm-mono" id="thrv">0.75 runs</b></div>
    <div class="dm-lg">lg avg <b class="dm-mono">{lg:.2f}</b> R/G &middot; {len(games)} games</div>
  </div>
  {cards}
  <section class="dm-method">
    <h2>How the number is built</h2>
    <p>Each team's expected runs = its season runs-per-game x the opponent's run prevention / league
       average, then scaled by the ballpark. Opponent run prevention blends the listed starter (~60%,
       regressed toward league average for small samples) with the team's full-staff RA/9 (~40%). Add
       the two sides for the projected total. Anything inside your edge threshold is a PASS.</p>
    <h2>What it does not know</h2>
    <p>Weather and wind, today's lineups and injuries, bullpen fatigue, umpire tendencies, and whatever
       the market has already priced in. A larger gap usually means the model is missing something the
       book is not. Treat it as one input, not an answer.</p>
  </section>
  <footer class="dm-foot">Break-even at standard -110 juice is about <b>52.4%</b>. No model reliably
    beats an efficient market - this is a research tool, not financial advice. 21+, and only wager what
    you can afford to lose. If gambling stops being fun: 1-800-GAMBLER.</footer>
</div>
<script>{_JS}</script>
</body></html>"""


def _card_html(g):
    has_line = g.get("line") is not None
    val = f' value="{g["line"]}"' if has_line else ""
    src = " &middot; ESPN" if has_line else ""
    return f"""
  <div class="dm-card" data-total="{g['total']}">
    <div class="dm-card-top">
      <span class="dm-matchup">{g['away']} <em>@</em> {g['home']}</span>
      <span class="dm-time">{(g['status'] + ' &middot; ') if g['status'] and g['status'] != 'Scheduled' else ''}{g['time']}</span>
    </div>
    <div class="dm-grid">
      <div class="dm-side">
        <div class="dm-team">{g['away']}</div>
        <div class="dm-stat"><span>R/G</span><b class="dm-num">{g['away_rg']:.2f}</b></div>
        <div class="dm-stat"><span>SP</span><b class="dm-num">{g['away_sp']}</b></div>
        <div class="dm-pn">{g['away_pn']}</div>
      </div>
      <div class="dm-center">
        <div class="dm-proj-label">proj. total</div>
        <div class="dm-proj">{g['total']:.1f}</div>
        <div class="dm-split">{g['away_exp']:.1f} &ndash; {g['home_exp']:.1f}</div>
        <div class="dm-park">park &times;{g['park']:.2f}</div>
      </div>
      <div class="dm-side dm-right">
        <div class="dm-team">{g['home']}</div>
        <div class="dm-stat"><span>R/G</span><b class="dm-num">{g['home_rg']:.2f}</b></div>
        <div class="dm-stat"><span>SP</span><b class="dm-num">{g['home_sp']}</b></div>
        <div class="dm-pn">{g['home_pn']}</div>
      </div>
    </div>
    <div class="dm-card-foot">
      <label class="dm-linewrap">posted total{src}
        <input class="dm-input dm-mono dm-line" inputmode="decimal" placeholder="8.5"{val}></label>
      <div class="dm-result"><span class="dm-gap"></span><span class="dm-badge is-empty">enter line</span></div>
    </div>
    <div class="dm-note"></div>
  </div>"""


_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap');
:root{--field:#0F1714;--panel:#16211C;--panel2:#1C2A23;--line:#2A3A31;--chalk:#ECE8DA;--muted:#8A998E;--amber:#F4A93B;--over:#E07A3E;--under:#5FB0C9;}
*{box-sizing:border-box;}
body{background:var(--field);color:var(--chalk);font-family:Inter,system-ui,sans-serif;margin:0;padding:22px 14px 60px;-webkit-font-smoothing:antialiased;}
.dm-wrap{max-width:980px;margin:0 auto;}
.dm-mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums;}
.dm-num{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums;font-weight:500;}
.dm-header{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid var(--line);}
.dm-title{font-family:'Saira Condensed',sans-serif;font-weight:700;font-size:36px;letter-spacing:.04em;margin:0;line-height:.95;}
.dm-title span{color:var(--amber);margin-left:8px;}
.dm-tag{color:var(--muted);font-size:13.5px;margin:7px 0 0;max-width:430px;line-height:1.4;}
.dm-meta{text-align:right;}
.dm-date{font-family:'JetBrains Mono',monospace;font-size:15px;color:var(--chalk);}
.dm-stamp{font-size:11.5px;color:var(--muted);margin-top:3px;}
.dm-input{background:var(--panel);color:var(--chalk);border:1px solid var(--line);border-radius:7px;padding:8px 10px;font-size:14px;font-family:inherit;}
.dm-input:focus{outline:none;border-color:var(--amber);}
.dm-bar{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 2px;}
.dm-thr{display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;}
.dm-thr input[type=range]{accent-color:var(--amber);width:130px;}
.dm-thr b,.dm-lg b{color:var(--chalk);}
.dm-lg{font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;}
.dm-msg{padding:34px 16px;text-align:center;color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:12px;}
.dm-card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:13px;padding:14px 16px;margin-bottom:13px;box-shadow:inset 0 1px 0 rgba(255,255,255,.03);}
.dm-card-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding-bottom:11px;border-bottom:1px solid var(--line);margin-bottom:13px;}
.dm-matchup{font-family:'Saira Condensed',sans-serif;font-weight:600;font-size:19px;letter-spacing:.02em;}
.dm-matchup em{color:var(--muted);font-style:normal;margin:0 3px;}
.dm-time{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--muted);white-space:nowrap;}
.dm-grid{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center;}
.dm-right{text-align:right;}
.dm-team{font-family:'Saira Condensed',sans-serif;font-weight:600;font-size:16px;margin-bottom:8px;letter-spacing:.02em;}
.dm-stat{display:flex;justify-content:space-between;gap:10px;font-size:13px;color:var(--muted);padding:2px 0;}
.dm-right .dm-stat{flex-direction:row-reverse;}
.dm-stat span{text-transform:uppercase;letter-spacing:.05em;font-size:11px;padding-top:1px;}
.dm-pn{font-size:11.5px;color:var(--muted);margin-top:6px;opacity:.85;}
.dm-center{text-align:center;padding:0 6px;}
.dm-proj-label{font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);}
.dm-proj{font-family:'Saira Condensed',sans-serif;font-weight:700;font-size:52px;line-height:1;color:var(--amber);text-shadow:0 0 22px rgba(244,169,59,.45);margin:2px 0;}
.dm-split{font-family:'JetBrains Mono',monospace;font-size:13px;}
.dm-park{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);margin-top:3px;}
.dm-card-foot{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;margin-top:13px;padding-top:12px;border-top:1px solid var(--line);}
.dm-linewrap{display:flex;flex-direction:column;gap:5px;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);}
.dm-linewrap .dm-input{width:84px;text-align:center;}
.dm-result{display:flex;align-items:center;gap:11px;}
.dm-gap{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:17px;}
.dm-gap.pos{color:var(--over);}.dm-gap.neg{color:var(--under);}
.dm-badge{font-family:'Saira Condensed',sans-serif;font-weight:700;font-size:15px;letter-spacing:.06em;padding:7px 15px;border-radius:7px;border:1px solid var(--line);}
.dm-badge.is-over{background:rgba(224,122,62,.16);color:var(--over);border-color:rgba(224,122,62,.4);}
.dm-badge.is-under{background:rgba(95,176,201,.16);color:var(--under);border-color:rgba(95,176,201,.4);}
.dm-badge.is-pass{background:rgba(138,153,142,.12);color:var(--muted);}
.dm-badge.is-empty{color:var(--muted);font-family:Inter;font-weight:500;font-size:12px;letter-spacing:0;text-transform:lowercase;}
.dm-note{font-size:11.5px;color:var(--muted);margin-top:9px;text-align:right;min-height:1px;}
.dm-method{margin-top:26px;padding:20px;background:var(--panel);border:1px solid var(--line);border-radius:13px;}
.dm-method h2{font-family:'Saira Condensed',sans-serif;font-weight:600;font-size:15px;letter-spacing:.04em;text-transform:uppercase;color:var(--amber);margin:0 0 7px;}
.dm-method h2+p+h2{margin-top:18px;}
.dm-method p{font-size:13.5px;line-height:1.55;color:var(--muted);margin:0;}
.dm-method b{color:var(--chalk);}
.dm-foot{margin-top:18px;font-size:12px;line-height:1.55;color:var(--muted);text-align:center;padding:0 8px;}
.dm-foot b{color:var(--chalk);}
@media(max-width:640px){.dm-header{flex-direction:column;align-items:stretch;}.dm-meta{text-align:left;}.dm-grid{grid-template-columns:1fr;gap:10px;text-align:center;}.dm-right{text-align:center;}.dm-right .dm-stat{flex-direction:row;}.dm-stat{justify-content:center;gap:8px;}.dm-proj{font-size:46px;}}
"""

_JS = """
function recompute(){
  var thr=parseFloat(document.getElementById('thr').value);
  document.getElementById('thrv').textContent=thr.toFixed(2)+' runs';
  document.querySelectorAll('.dm-card').forEach(function(c){
    var total=parseFloat(c.getAttribute('data-total'));
    var inp=c.querySelector('.dm-line');
    var gapEl=c.querySelector('.dm-gap');
    var badge=c.querySelector('.dm-badge');
    var note=c.querySelector('.dm-note');
    var line=parseFloat(inp.value);
    if(isNaN(line)){gapEl.textContent='';gapEl.className='dm-gap';
      badge.className='dm-badge is-empty';badge.textContent='enter line';note.textContent='';return;}
    var gap=total-line;
    gapEl.textContent=(gap>=0?'+':'')+gap.toFixed(1);
    gapEl.className='dm-gap '+(gap>=0?'pos':'neg');
    var pick='PASS',cls='is-pass',msg='No edge against this number';
    if(gap>=thr){pick='OVER';cls='is-over';}
    else if(gap<=-thr){pick='UNDER';cls='is-under';}
    if(pick!=='PASS')msg=Math.abs(gap)<1.5?'Slight lean - thin edge':'Larger gap - verify weather, lineups, bullpen';
    badge.className='dm-badge '+cls;badge.textContent=pick;note.textContent=msg;
  });
}
document.getElementById('thr').addEventListener('input',recompute);
document.querySelectorAll('.dm-line').forEach(function(i){i.addEventListener('input',recompute);});
recompute();
"""


# ---- main ------------------------------------------------------------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    date_str = args[0] if args else dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print("Date must be YYYY-MM-DD, e.g. 2026-06-10")
        sys.exit(1)

    games, lg = build_slate(date_str)

    # Output path: DUGOUT_OUTFILE env wins (used by scheduled / cloud runs),
    # otherwise dugout_report.html next to this script.
    outfile = os.environ.get("DUGOUT_OUTFILE")
    if outfile:
        out = Path(outfile).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(__file__).resolve().parent / "dugout_report.html"
    out.write_text(render_html(games, lg, date_str), encoding="utf-8")
    print(f"\nWrote {out}  ({len(games)} games, lg avg {lg:.2f} R/G)")

    # Open a browser only for interactive runs (not cron / CI).
    if "--no-open" not in flags and not os.environ.get("CI"):
        try:
            webbrowser.open(out.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
