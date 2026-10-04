"""Now Scrobbling • PULSE edition.

A standalone Streamlit dashboard for the Top 500 all-time Last.fm artists.
The historical first-observed baseline is immutable. All other calculations are
read-only analyses of those saved observations and the current Top 500.
"""
from __future__ import annotations

import html
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Now Scrobbling", page_icon="🎧", layout="wide", initial_sidebar_state="collapsed")

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("NOW_SCROBBLING_DATA_DIR", str(ROOT)))
HISTORY_FILE = DATA_DIR / "leaderboard_history.json"
CACHE_FILE = DATA_DIR / "leaderboard_cache.json"
META_FILE = DATA_DIR / "artist_meta.json"
LEGACY_BASELINE_FILES = [DATA_DIR / "artist_baseline.json", DATA_DIR / "baseline_import.json"]
API_URL = "https://ws.audioscrobbler.com/2.0/"
REFRESH_MS = 60_000
CHART_COLORS = {"up": "#28CE92", "down": "#FF526B", "same": "#596574", "current": "#21B9F7"}
LOOKBACK_CHOICES = [7, 28, 90, 180]
CHASE_THRESHOLDS = {"session": 5, "reach": 25, "closing": 100}
TZ_NAME = os.getenv("NOW_SCROBBLING_TZ", "America/New_York")
try:
    API_KEY = os.getenv("LASTFM_API_KEY", "") or st.secrets.get("LASTFM_API_KEY", "")
except (FileNotFoundError, KeyError):
    API_KEY = os.getenv("LASTFM_API_KEY", "")


def local_now():
    try:
        return datetime.now(ZoneInfo(TZ_NAME)).replace(tzinfo=None)
    except (ValueError, KeyError):
        return datetime.now()


def numeric(value):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def artist_key(value):
    return " ".join(str(value or "").casefold().split())


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(ZoneInfo(TZ_NAME)).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


def date_display(value, include_time=False):
    dt = value if isinstance(value, datetime) else parse_time(value)
    if not dt:
        return "—"
    date = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    if include_time:
        return f"{date} · {dt.strftime('%I:%M %p').lstrip('0')}"
    return date


def api_call(method, username, **params):
    if not API_KEY:
        raise RuntimeError("Add LASTFM_API_KEY to Streamlit app Secrets.")
    r = requests.get(API_URL, params={"method": method, "user": username, "api_key": API_KEY,
                                     "format": "json", **params}, timeout=22)
    r.raise_for_status()
    obj = r.json()
    if isinstance(obj, dict) and "error" in obj:
        raise RuntimeError(str(obj.get("message", "Last.fm API error")))
    return obj


def fetch_live(username):
    recent = api_call("user.getrecenttracks", username, limit=1)
    tracks = recent.get("recenttracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks:
        raise ValueError("No recent tracks available")
    track = tracks[0]
    field = track.get("artist", {})
    current = field.get("#text", "") if isinstance(field, dict) else str(field)
    images = track.get("image") or []
    cover = images[-1].get("#text", "") if images and isinstance(images[-1], dict) else ""
    title = str(track.get("name", ""))
    top = api_call("user.gettopartists", username, period="overall", limit=500).get("topartists", {}).get("artist", [])
    if isinstance(top, dict):
        top = [top]
    if len(top) < 11:  # Do not overwrite good snapshots if the API is incomplete.
        raise ValueError("Last.fm returned an incomplete artist ranking")
    return current, title, cover, top[:500]


def _normalize_entry(obj):
    if not isinstance(obj, dict):
        return []
    if "data" in obj:
        return [{"timestamp": str(obj.get("timestamp") or ""), "data": obj["data"]}] if isinstance(obj["data"], dict) else []
    result = []
    for timestamp, snapshot in obj.items():
        if isinstance(snapshot, dict) and parse_time(timestamp):
            result.append({"timestamp": str(timestamp), "data": snapshot})
    return result


def _decode_history(raw):
    """Retain V1's line-by-line recovery; also salvage whole JSON and mixed eras.

    No mutation, migration, truncation, or automatic replacement occurs here.
    """
    entries = []
    stripped = raw.strip()
    if not stripped:
        return entries
    # A historic version could be a single JSON dict, list or entry.
    try:
        whole = json.loads(stripped)
        if isinstance(whole, list):
            for item in whole:
                entries.extend(_normalize_entry(item))
        else:
            entries.extend(_normalize_entry(whole))
    except (ValueError, TypeError):
        pass
    # V1 recovery technique: parse valid JSONL lines and ignore malformed/empty lines.
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        entries.extend(_normalize_entry(obj))
    # Mixed whole-file JSON followed by JSONL, including truncated/malformed prefixes.
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{\s*"timestamp"\s*:', raw):
        try:
            obj, _ = decoder.raw_decode(raw, match.start())
        except ValueError:
            continue
        entries.extend(_normalize_entry(obj))
    # Some older snapshots were stored as timestamp-keyed JSON dictionaries.
    for match in re.finditer(r'"(\d{4}-\d\d-\d\d[T ][^"\n]{4,40})"\s*:\s*(\{)', raw):
        if not parse_time(match.group(1)):
            continue
        try:
            snapshot, _ = decoder.raw_decode(raw, match.start(2))
        except ValueError:
            continue
        if isinstance(snapshot, dict):
            entries.append({"timestamp": match.group(1), "data": snapshot})
    # Deduplicate copies found through multiple decoding paths.
    seen, result = set(), []
    for entry in entries:
        snapshot = entry.get("data", {})
        ts = entry.get("timestamp", "")
        if not isinstance(snapshot, dict):
            continue
        signature = (ts, json.dumps(snapshot, sort_keys=True, ensure_ascii=False))
        if signature in seen:
            continue
        seen.add(signature)
        result.append(entry)
    result.sort(key=lambda e: (parse_time(e.get("timestamp")) or datetime.max, str(e.get("timestamp"))))
    return result


@st.cache_data(show_spinner=False, max_entries=4)
def read_history_cached(path, file_mtime_ns, file_size):
    # Signatures above make the cache refresh automatically after a history write.
    try:
        return _decode_history(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return []


def load_history_entries():
    try:
        stat = HISTORY_FILE.stat()
    except OSError:
        return []
    return read_history_cached(str(HISTORY_FILE), stat.st_mtime_ns, stat.st_size)


def safe_load_dict(path):
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_long_term_baseline(entries):
    baseline = {}
    for entry in entries:
        time = entry.get("timestamp")
        if not parse_time(time):
            continue
        for name, info in entry.get("data", {}).items():
            if not isinstance(info, dict):
                continue
            rank, plays = numeric(info.get("rank")), numeric(info.get("playcount"))
            key = artist_key(name)
            if key and key not in baseline and rank is not None and plays is not None:
                baseline[key] = {"rank": rank, "playcount": plays, "first_seen": time, "source": "history"}
    # Optional legacy references fill MISSING artists only; never overwrite history.
    for path in LEGACY_BASELINE_FILES:
        data = safe_load_dict(path)
        for name, info in data.items():
            if not isinstance(info, dict):
                continue
            rank, plays = numeric(info.get("rank")), numeric(info.get("playcount", info.get("baseline_playcount")))
            key = artist_key(name)
            if key and key not in baseline and rank is not None and plays is not None:
                baseline[key] = {"rank": rank, "playcount": plays,
                                 "first_seen": info.get("timestamp", info.get("first_seen", "")),
                                 "source": "legacy"}
    if not baseline and not entries:
        for name, info in safe_load_dict(CACHE_FILE).items():
            if isinstance(info, dict):
                rank, plays = numeric(info.get("rank")), numeric(info.get("playcount"))
                if rank is not None and plays is not None:
                    baseline[artist_key(name)] = {"rank": rank, "playcount": plays, "source": "cache", "first_seen": ""}
    return baseline


def top_maps(top):
    names, ranks, plays = [], {}, {}
    for i, obj in enumerate(top[:500], 1):
        name = str(obj.get("name") or "").strip()
        if name and artist_key(name) not in ranks:
            key = artist_key(name)
            names.append(name)
            ranks[key] = i
            plays[key] = numeric(obj.get("playcount")) or 0
    return names, ranks, plays


def semantic_color(name, rank, current, baseline):
    if artist_key(name) == artist_key(current):
        return CHART_COLORS["current"]
    first = numeric(baseline.get(artist_key(name), {}).get("rank"))
    if first is None or first == rank:
        return CHART_COLORS["same"]
    return CHART_COLORS["up"] if rank < first else CHART_COLORS["down"]


def write_atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_neighborhood(snapshot, username):
    """Append when ranking/neighborhood changes; never modify original history lines."""
    sig = json.dumps({"username": username, "data": snapshot}, sort_keys=True, ensure_ascii=False)
    if st.session_state.get("last_logged_signature") == sig:
        return False
    entries = load_history_entries()
    if entries and entries[-1].get("data") == snapshot:
        st.session_state["last_logged_signature"] = sig
        return False
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Protect a final historical entry without a trailing newline.
    needs_separator = False
    if HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size:
        with HISTORY_FILE.open("rb") as existing:
            existing.seek(-1, 2)
            needs_separator = existing.read(1) not in (b"\n", b"\r")
    with HISTORY_FILE.open("a", encoding="utf-8") as out:
        if needs_separator:
            out.write("\n")
        out.write(json.dumps({"timestamp": local_now().isoformat(timespec="milliseconds"), "data": snapshot}, ensure_ascii=False) + "\n")
    st.session_state["last_logged_signature"] = sig
    write_atomic_json(CACHE_FILE, snapshot)
    return True


def observed_series(entries):
    """Recorded points only. No made-up intervening scrobbles or ranks."""
    points = defaultdict(list)
    for entry in entries:
        dt = parse_time(entry.get("timestamp"))
        if not dt:
            continue
        for name, info in entry.get("data", {}).items():
            if not isinstance(info, dict):
                continue
            rank, plays = numeric(info.get("rank")), numeric(info.get("playcount"))
            if rank is not None or plays is not None:
                points[artist_key(name)].append((dt, rank, plays, name))
    result = {}
    for key, series in points.items():
        deduped = {}
        for row in sorted(series, key=lambda x: x[0]):
            deduped[row[0]] = row
        result[key] = list(deduped.values())
    return result


def momentum_rows(series_map, names, ranks, plays, days, reference=None):
    """Return gains observed within the requested calendar lookback, no interpolation."""
    reference = reference or local_now()
    cutoff = reference - timedelta(days=days)
    results = []
    for name in names:
        key = artist_key(name)
        pts = [p for p in series_map.get(key, []) if cutoff <= p[0] <= reference]
        valid = [p for p in pts if p[2] is not None]
        if len(valid) < 2:
            continue
        first, last = valid[0], valid[-1]
        elapsed = max((last[0] - first[0]).total_seconds()/86400, 1/24)
        change = max(0, last[2] - first[2])
        rank_delta = first[1] - last[1] if first[1] is not None and last[1] is not None else None
        results.append({"Artist": name, "Plays gained": change, "Per day": round(change/elapsed, 2),
                        "Rank Δ": rank_delta, "First seen": first[0], "Last seen": last[0],
                        "Snapshots": len(valid), "Current rank": ranks.get(key), "Current plays": plays.get(key)})
    return sorted(results, key=lambda row: (row["Plays gained"], row["Per day"]), reverse=True)


def progress_rows(names, ranks, plays, baselines, series_map):
    result = []
    for name in names:
        key = artist_key(name)
        base = baselines.get(key)
        if not base:
            continue
        first_rank, first_plays = numeric(base.get("rank")), numeric(base.get("playcount"))
        if first_rank is None or first_plays is None:
            continue
        last = series_map.get(key, [])
        result.append({"Artist": name, "Rank": ranks[key], "From": first_rank,
                       "Movement": first_rank-ranks[key], "Plays": plays[key],
                       "Gained": plays[key]-first_plays, "First observed": date_display(base.get("first_seen")),
                       "Last recorded": date_display(last[-1][0]) if last else "—",
                       "Baseline source": base.get("source", "history")})
    return result


def binge_events(series_map, limit=20):
    bursts = []
    for points in series_map.values():
        for before, after in zip(points, points[1:]):
            dt = (after[0] - before[0]).total_seconds() / 3600
            if dt <= 0 or dt > 48 or before[2] is None or after[2] is None:
                continue
            gain = after[2] - before[2]
            if gain > 0:
                bursts.append({"Artist": after[3], "Jump": gain, "Hours": round(dt, 1),
                               "When": date_display(after[0], include_time=True)})
    return sorted(bursts, key=lambda b: b["Jump"], reverse=True)[:limit]


def period_leaders(series_map, grain="quarter"):
    bucket_points = defaultdict(lambda: defaultdict(list))
    for key, pts in series_map.items():
        for time, _, plays, name in pts:
            if plays is None:
                continue
            if grain == "year": bucket = f"{time.year}"
            elif grain == "month": bucket = f"{time.year}-{time.month:02d}"
            else: bucket = f"{time.year} Q{(time.month-1)//3+1}"
            bucket_points[bucket][key].append((time, plays, name))
    result = {}
    for bucket, artists in bucket_points.items():
        ranked = []
        for vals in artists.values():
            vals.sort()
            if len(vals) < 2:
                continue
            gain = vals[-1][1] - vals[0][1]
            if gain > 0:
                ranked.append({"Artist": vals[-1][2], "Observed gain": gain, "Records": len(vals)})
        if ranked:
            result[bucket] = sorted(ranked, key=lambda v: v["Observed gain"], reverse=True)
    return dict(sorted(result.items(), reverse=True))


def milestone_rows(names, plays, ranks):
    parked, one_away = [], []
    for name in names:
        p = plays[artist_key(name)]
        if p < 50:
            continue
        def tier(n):
            return "1,000" if n % 1000 == 0 else "500" if n % 500 == 0 else "100" if n % 100 == 0 else "50"
        if p % 50 == 0:
            parked.append({"Artist": name, "Rank": ranks[artist_key(name)], "Plays": p, "Milestone": f"{p:,} · {tier(p)}"})
        if (p+1) % 50 == 0:
            one_away.append({"Artist": name, "Rank": ranks[artist_key(name)], "Plays": p,
                             "Next milestone": f"{p+1:,}"})
    return parked, one_away


def top50_candidates(names, plays, ranks, rates, max_days=183):
    if len(names) < 51:
        return []
    threshold = plays[artist_key(names[49])]
    by_key = {artist_key(r["Artist"]): r for r in rates}
    result = []
    for name in names[50:300]:
        k = artist_key(name)
        row = by_key.get(k)
        if not row or row["Per day"] <= 0:
            continue
        # +1 makes the ranking cutoff conservative in ties.
        gap = max(0, threshold - plays[k] + 1)
        days = gap / row["Per day"]
        if days <= max_days:
            result.append({"Artist": name, "Rank": ranks[k], "Plays": plays[k], "Gap": gap,
                           "Observed/day": row["Per day"], "Est. days": int(days+0.999),
                           "Projected (6m)": int(plays[k]+row["Per day"]*max_days)})
    return sorted(result, key=lambda x: (x["Est. days"], x["Rank"]))


def gap_state(gap):
    if gap <= CHASE_THRESHOLDS["session"]: return "ONE SESSION"
    if gap <= CHASE_THRESHOLDS["reach"]: return "IN RANGE"
    if gap <= CHASE_THRESHOLDS["closing"]: return "CLOSING"
    return "LONG CHASE"


def escape(s):
    return html.escape(str(s or ""), quote=True)


CSS = """
<style>
:root {color-scheme:dark}
.stApp {background: #080d15; color: #eaf1fa;}
.block-container {max-width:1080px; padding-top:1rem; padding-bottom:3rem;}
header[data-testid="stHeader"] {background:rgba(8,13,21,.8)}
[data-testid="stSidebar"] {background:#0c131e;}
[data-testid="stPlotlyChart"] {background:transparent;}
[data-testid="stMarkdownContainer"] p {margin-bottom:.25rem;}
[data-testid="stTabs"] [data-baseweb="tab-list"] {gap:1.4rem; border-bottom:1px solid #223042;}
[data-testid="stTabs"] button {color:#8798ac; font-weight:600; font-size:.84rem;}
[data-testid="stTabs"] button[aria-selected="true"] {color:#ebf4ff;}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {background:#23b9f7;}
div[data-testid="stExpander"] {border:1px solid #263449; background:#0d1521; border-radius:12px;}
div[data-testid="stDataFrame"] {border-radius:10px; overflow:hidden;}
.ns-shell {font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;}
.ns-utility {display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;}
.ns-signal {display:flex;align-items:center;gap:8px; color:#d0ddeb; font-size:9px; letter-spacing:.18em;font-weight:800;}
.ns-live-dot {height:7px;width:7px;border-radius:50%;background:#22bffb;box-shadow:0 0 12px #21b9f777;}
.ns-minor {font-size:9px; font-weight:700; letter-spacing:.1em; color:#607891;}
.ns-top {display:flex;gap:14px;align-items:center;padding:0 0 15px;}
.ns-art {width:66px;height:66px;flex:0 0 66px;border-radius:13px;object-fit:cover;background:linear-gradient(140deg,#183453,#101927);border:1px solid #28425d;}
.ns-art-fallback {display:flex;align-items:center;justify-content:center;font-size:24px;color:#75a5c5;}
.ns-info {flex:1;min-width:0;}
.ns-name {color:#f4f8ff;font-weight:790;font-size:clamp(25px,3vw,36px);letter-spacing:-.05em;line-height:1.07;overflow-wrap:anywhere;}
.ns-track {margin-top:6px;color:#8ca4bd;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ns-rank {padding-left:16px;border-left:1px solid #263547;min-width:94px;text-align:right;}
.ns-rank small {font-size:9px; color:#81a2c2;letter-spacing:.13em;font-weight:750;}
.ns-rank strong {font-weight:760;font-size:clamp(38px,5vw,58px);letter-spacing:-.07em;color:#f5f8ff;display:block;line-height:1.1;font-variant-numeric:tabular-nums;}
.ns-stats {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid #263243;border-bottom:1px solid #263243;margin:0 0 2px;padding:13px 0;}
.ns-cell {padding:0 16px;border-right:1px solid #233246;}
.ns-cell:first-child {padding-left:0;}.ns-cell:last-child {border:0;}
.ns-label {color:#6c89a6;font-size:9px;letter-spacing:.14em;font-weight:700;margin-bottom:5px;}
.ns-num {color:#f2f7fd;font-size:clamp(20px,2.3vw,26px);font-weight:780;letter-spacing:-.035em;line-height:1.1;font-variant-numeric:tabular-nums;}
.ns-unit {font-size:10px;font-weight:450;margin-left:4px;color:#8499ab;}
.ns-up {color:#28ce92;}.ns-down {color:#ff526b;}.ns-flat {color:#b8c5d4;}
.ns-chase {background:linear-gradient(115deg,#101d2d,#0d1723);border:1px solid #2a4057;padding:13px 16px 12px;border-radius:13px;margin:7px 0 22px;}
.ns-chase-top {display:flex;justify-content:space-between;align-items:center;gap:10px;}
.ns-target {font-size:13px;font-weight:750;color:#edf4fc;overflow-wrap:anywhere;}
.ns-target small {display:block;font-size:9px;letter-spacing:.14em;color:#7b96ae;margin-bottom:5px;}
.ns-distance {font-size:24px;font-weight:800;letter-spacing:-.055em;white-space:nowrap;color:#f4f8fc;}
.ns-distance span {font-weight:450;font-size:10px;letter-spacing:0;color:#91a3b4;margin-left:5px;}
.ns-track-bg {width:100%;height:5px;border-radius:20px;background:#243246;overflow:hidden;margin-top:11px;}
.ns-track-bar {height:100%;border-radius:20px;background:linear-gradient(90deg,#176b9b,#2bc7ff);}
.ns-chase-bottom {display:flex;justify-content:space-between;gap:10px;margin-top:7px;color:#8ba2b7;font-size:10px;}
.ns-section {margin:10px 0 9px;display:flex;align-items:center;justify-content:space-between;gap:12px;}
.ns-section h3 {margin:0;font-size:16px;letter-spacing:-.025em;font-weight:760;color:#e9f1fa;}
.ns-section span {font-size:10px;color:#6d849e;}
.ns-tiny-pills {display:flex;gap:7px;flex-wrap:wrap;margin:7px 0 15px;}
.ns-pill {border:1px solid #253a50;background:#0d1928;border-radius:7px;padding:6px 9px;font-size:10px;color:#9cb2c8;}
.ns-pill b {color:#e6f1ff;}
.ns-annot {font-size:11px;color:#6c8099; margin-top:-2px;}
@media(max-width:700px){
 .block-container{padding:.7rem .65rem 2rem;}
 .ns-art{height:51px;width:51px;flex-basis:51px;border-radius:9px;}
 .ns-top{gap:9px;}.ns-name{font-size:clamp(21px,6vw,28px);}.ns-track{font-size:11px;}
 .ns-rank{padding-left:9px;min-width:69px;}.ns-rank strong{font-size:36px;}
 .ns-rank small{font-size:8px;}.ns-stats{padding:11px 0;}
 .ns-cell{padding:0 9px;}.ns-num{font-size:20px;}.ns-label{letter-spacing:.035em;font-size:8px;}
 .ns-unit{display:none;}.ns-chase{padding:11px 12px;}.ns-distance{font-size:22px;}
 .ns-minor{font-size:8px;}
}
</style>
"""


def render_hero(current, title, cover, rank, playcount, baseline, cushion):
    base = baseline.get(artist_key(current), {})
    start = numeric(base.get("rank"))
    diff = start-rank if start is not None else None
    if diff is None: movement, cls = "—", "ns-flat"
    elif diff > 0: movement, cls = f"+{diff}", "ns-up"
    elif diff < 0: movement, cls = str(diff), "ns-down"
    else: movement, cls = "0", "ns-flat"
    img = f'<img class="ns-art" alt="Album cover" src="{escape(cover)}">' if cover.startswith(("https://", "http://")) else '<div class="ns-art ns-art-fallback">♫</div>'
    cush = f"{cushion:,}" if cushion is not None else "—"
    st.markdown(f"""
<div class="ns-shell">
 <div class="ns-utility"><span class="ns-signal"><i class="ns-live-dot"></i> NOW SCROBBLING</span><span class="ns-minor">ALL-TIME · TOP 500</span></div>
 <div class="ns-top">{img}<div class="ns-info"><div class="ns-name">{escape(current)}</div><div class="ns-track">{escape(title)}</div></div><div class="ns-rank"><small>RANK</small><strong>#{rank}</strong></div></div>
 <div class="ns-stats">
   <div class="ns-cell"><div class="ns-label">SINCE FIRST SEEN</div><div class="ns-num {cls}">{movement}<span class="ns-unit">places</span></div></div>
   <div class="ns-cell"><div class="ns-label">SCROBBLES</div><div class="ns-num">{playcount:,}</div></div>
   <div class="ns-cell"><div class="ns-label">CUSHION</div><div class="ns-num">{cush}<span class="ns-unit">plays</span></div></div>
 </div>
</div>""", unsafe_allow_html=True)


def render_neighborhood(neighbors, current, ranks, plays, baseline):
    # Plotly normally interprets categorical y labels; unique position labels avoid collisions.
    ordered = list(reversed(neighbors))
    keys = [artist_key(n) for n in ordered]
    y_labels = [f"{ranks[k]:>3}  {n}" for n, k in zip(ordered, keys)]
    colors = [semantic_color(n, ranks[k], current, baseline) for n,k in zip(ordered, keys)]
    custom = []
    for name,k in zip(ordered,keys):
        base = baseline.get(k, {})
        old = numeric(base.get("rank"))
        move = f"{old-ranks[k]:+d} places since first seen" if old is not None else "Not yet tracked"
        custom.append([name, ranks[k], move])
    fig = go.Figure(go.Bar(
        y=y_labels, x=[plays[k] for k in keys], orientation="h", marker_color=colors,
        marker_line=dict(color=["#b3edff" if k == artist_key(current) else "rgba(0,0,0,0)" for k in keys], width=[1.3 if k == artist_key(current) else 0 for k in keys]),
        text=[f"{plays[k]:,}" for k in keys], textposition="outside", cliponaxis=False,
        textfont=dict(size=11, color="#dceaf6"),
        customdata=custom, hovertemplate="<b>%{customdata[0]}</b> · #%{customdata[1]}<br>%{x:,} plays<br>%{customdata[2]}<extra></extra>"
    ))
    fig.update_layout(
        height=max(300, len(ordered)*34+14), margin=dict(l=4,r=60,t=8,b=6), bargap=.21,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
        font=dict(family="Inter, Arial, sans-serif",color="#b5cbe0"),
        xaxis=dict(visible=False, range=[0,max(plays[k] for k in keys)*1.11], fixedrange=True),
        yaxis=dict(showgrid=False, tickfont=dict(size=11, color="#b7c8dc"), automargin=True, fixedrange=True, title=None),
        hoverlabel=dict(bgcolor="#142131",font_color="#f2f6ff"), dragmode=False
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False, "scrollZoom":False, "responsive":True})


def render_chase(target, target_rank, target_plays, current_plays):
    if target is None:
        st.markdown('<div class="ns-chase"><div class="ns-target"><small>POSITION</small>TOP OF THE BOARD · #1</div></div>', unsafe_allow_html=True)
        return
    gap = max(0, target_plays-current_plays)
    filled = (current_plays/target_plays*100) if target_plays else 100
    filled = min(100,max(0,filled))
    st.markdown(f"""
<div class="ns-shell ns-chase">
 <div class="ns-chase-top"><div class="ns-target"><small>NEXT · #{target_rank}</small>{escape(target)}</div><div class="ns-distance">{gap:,}<span>behind</span></div></div>
 <div class="ns-track-bg"><div class="ns-track-bar" style="width:{filled:.2f}%"></div></div>
 <div class="ns-chase-bottom"><span>{gap_state(gap)}</span><span>≈{gap+1:,} to pass</span></div>
</div>""",unsafe_allow_html=True)


def section(title, label=""):
    st.markdown(f'<div class="ns-section"><h3>{escape(title)}</h3><span>{escape(label)}</span></div>', unsafe_allow_html=True)


def render_frame_table(data, columns=None, limit=None):
    if not data:
        st.caption("Not enough recorded observations yet.")
        return
    show = data[:limit] if limit else data
    if columns:
        show = [{k: row.get(k) for k in columns} for row in show]
    st.dataframe(show, hide_index=True, use_container_width=True)


def history_chart(current, entries, baselines, neighbor_keys=None, metric="Rank"):
    names = [current] if not neighbor_keys else neighbor_keys
    per_artist = observed_series(entries)
    fig = go.Figure()
    available = 0
    palette = ["#21B9F7", "#cdb4ff", "#ffc978", "#54d9c3", "#fd94c8", "#92b9ff", "#a5df74", "#f5917e", "#f3cfff", "#b9c6d8", "#ffd45d"]
    for i,name in enumerate(names):
        key = artist_key(name)
        points = [x for x in per_artist.get(key, []) if x[1 if metric=="Rank" else 2] is not None]
        if len(points) < 2:
            continue
        available += 1
        color = "#21B9F7" if artist_key(name)==artist_key(current) else palette[i % len(palette)]
        # A large gap indicates genuinely missing observations: do not connect it as a continuous trace.
        xs, ys = [], []
        for j,p in enumerate(points):
            if j and p[0]-points[j-1][0] > timedelta(days=14):
                xs.append(None); ys.append(None)
            xs.append(p[0]); ys.append(p[1] if metric=="Rank" else p[2])
        fig.add_trace(go.Scatter(x=xs,y=ys, mode="lines+markers", name=name,
                                 line=dict(color=color,width=2.6 if key==artist_key(current) else 1.5),
                                 marker=dict(size=5 if key==artist_key(current) else 3),
                                 connectgaps=False,
                                 hovertemplate=f"{escape(name)}<br>%{{x|%b %d, %Y}}<br>%{{y:,}}<extra></extra>"))
    if not available:
        st.caption("Historical chart will appear after at least two recorded observations.")
        return
    fig.update_layout(height=350,margin=dict(l=6,r=12,t=5,b=26),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#a9bbd0",size=11),hovermode="closest",
                      legend=dict(orientation="h",y=-.23,font=dict(size=10)) if available<=5 else dict(font=dict(size=10)),
                      xaxis=dict(title=None,gridcolor="#1c2838",tickformat="%b '%y"),
                      yaxis=dict(title=None,gridcolor="#1c2838",autorange="reversed" if metric=="Rank" else True))
    st.plotly_chart(fig,use_container_width=True,config={"displayModeBar":False})


def rank_gravity_chart(names, plays):
    names = names[:200]
    vals = [plays[artist_key(n)] for n in names]
    gaps = [max(0,vals[i]-vals[i+1]) for i in range(len(vals)-1)]+[0]
    colors = ["#234c6d" if i<10 else "#16473d" if i<25 else "#60422c" if i<50 else "#493348" if i<100 else "#343c4b" for i in range(1,len(vals)+1)]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=list(range(1,len(vals)+1)),y=gaps, name="Gap to next rank", marker_color=colors,
                         opacity=.55, yaxis="y2",hovertemplate="#%{x} → next: %{y:,} plays<extra></extra>"))
    fig.add_trace(go.Scatter(x=list(range(1,len(vals)+1)),y=vals,name="All-time plays",line=dict(color="#a79cff",width=2.6),
                                hovertemplate="#%{x}: %{y:,} plays<extra></extra>"))
    for mark in [10,25,50,100,200]:
        if mark<=len(vals):
            fig.add_vline(x=mark,line_dash="dot",line_color="#4a6685",opacity=.5)
            fig.add_annotation(x=mark,y=1.03,yref="paper",text=f"#{mark}",showarrow=False,font=dict(size=10,color="#9fb6d0"))
    fig.update_layout(height=350,margin=dict(l=6,r=10,t=24,b=24),paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",font=dict(color="#abb9cd",size=11),
                      legend=dict(orientation="h",y=-.2),barmode="overlay",
                      xaxis=dict(title="Rank",gridcolor="#1d2939"),
                      yaxis=dict(title="Scrobbles",gridcolor="#1d2939"),
                      yaxis2=dict(title="Gap",overlaying="y",side="right",showgrid=False))
    st.plotly_chart(fig,use_container_width=True,config={"displayModeBar":False})


def render_head_to_head(names, plays, series_map, rate_rows):
    pick1,pick2=st.columns(2)
    with pick1:
        a=st.selectbox("Artist A", names, index=min(30,len(names)-1),key="h2h_a")
    with pick2:
        b=st.selectbox("Artist B", names, index=min(80,len(names)-1),key="h2h_b")
    if a==b:
        st.caption("Choose two different artists.")
        return
    fig=go.Figure()
    rates={artist_key(row["Artist"]): row["Per day"] for row in rate_rows}
    now=local_now()
    for name,color in [(a,"#b7a1ff"),(b,"#ff896d")]:
        key=artist_key(name)
        series=[p for p in series_map.get(key,[]) if p[2] is not None]
        if series:
            fig.add_trace(go.Scatter(x=[p[0] for p in series],y=[p[2] for p in series],name=name,
                                     mode="lines+markers",line=dict(color=color,width=2.2)))
        rate=rates.get(key)
        if rate and rate>0:
            fig.add_trace(go.Scatter(x=[now,now+timedelta(days=90)],
                                     y=[plays[key],plays[key]+rate*90],mode="lines",name=f"{name} · 90d pace",
                                     line=dict(color=color,dash="dash",width=1.4),opacity=.75))
    if not fig.data:
        st.caption("No recorded points for this comparison.")
        return
    fig.update_layout(height=330,margin=dict(l=6,r=6,t=12,b=25),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(size=11,color="#a3b7cc"),xaxis=dict(gridcolor="#1d2938",tickformat="%b '%y"),
                      yaxis=dict(title="Scrobbles",gridcolor="#1d2938"),legend=dict(orientation="h",y=-.22))
    st.plotly_chart(fig,use_container_width=True,config={"displayModeBar":False})
    st.caption("Dashed lines extend each artist's observed pace by 90 days; not a prediction of future listening.")


@st.cache_data(ttl=900,show_spinner=False,max_entries=8)
def recent_listening_weeks(username, weeks):
    """Actual scrobbles, not neighboring rank snapshots. Lazy cached external fetch."""
    start = local_now() - timedelta(weeks=weeks)
    start_ts=int(start.replace(tzinfo=ZoneInfo(TZ_NAME)).timestamp())
    rows=[]
    truncated=False
    for page in range(1,13):  # upper bound: 2400 tracks in 12 API calls
        obj=api_call("user.getrecenttracks",username,limit=200,page=page,**{"from":start_ts})
        resp=obj.get("recenttracks",{})
        tracks=resp.get("track",[])
        if isinstance(tracks,dict): tracks=[tracks]
        for item in tracks:
            field=item.get("artist",{})
            name=field.get("#text", "") if isinstance(field,dict) else str(field)
            dateobj=item.get("date",{})
            uts=numeric(dateobj.get("uts")) if isinstance(dateobj,dict) else None
            if not name or not uts: continue
            dt=datetime.fromtimestamp(uts,ZoneInfo(TZ_NAME)).replace(tzinfo=None)
            if dt>=start:
                rows.append((artist_key(name), dt.isocalendar().year, dt.isocalendar().week, name))
        attr=resp.get("@attr",{})
        last_page=numeric(attr.get("totalPages")) or page
        if page>=last_page:
            break
        if page==12:
            truncated=True
    return rows,truncated


def weekly_associations(rows, anchor):
    target=artist_key(anchor)
    per_artist=defaultdict(lambda: defaultdict(int))
    for key, year, week, name in rows:
        per_artist[key][(year,week)] += 1
    anchor_weeks=set(per_artist[target])
    ranking=[]
    for key, weeks in per_artist.items():
        common=set(weeks)&anchor_weeks
        if key==target or not common: continue
        ranking.append({"Artist":next((n for k,_,_,n in rows if k==key),key),
                        "Shared weeks":len(common), "Plays in those weeks":sum(weeks[w] for w in common)})
    return sorted(ranking,key=lambda v:(v["Shared weeks"],v["Plays in those weeks"]),reverse=True)


def auto_favorite(rank):
    if not rank:return 40
    for cap,v in [(10,100),(15,95),(30,90),(75,80),(150,70),(350,60),(500,50)]:
        if rank<=cap:return v
    return 40


def render_meta(current, rank):
    meta=safe_load_dict(META_FILE)
    key=next((k for k in meta if artist_key(k)==artist_key(current)),current)
    record=meta.get(key, {}) if isinstance(meta.get(key),dict) else {}
    taglist=sorted({str(x) for item in meta.values() if isinstance(item,dict) for x in item.get("tags",[])})
    moodlist=sorted({str(x) for item in meta.values() if isinstance(item,dict) for x in item.get("moods",[])})
    left,right=st.columns(2)
    with left:
        tags=st.multiselect("Tags",sorted(set(taglist)|set(record.get("tags",[]))),default=record.get("tags",[]),key=f"tags:{key}")
        extra_tag=st.text_input("Add tag",key=f"extra_tags:{key}")
        moods=st.multiselect("Moods",sorted(set(moodlist)|set(record.get("moods",[]))),default=record.get("moods",[]),key=f"moods:{key}")
        extra_mood=st.text_input("Add mood",key=f"extra_moods:{key}")
        listen=st.checkbox("Listen more",value=bool(record.get("listen_more",False)),key=f"listen:{key}")
    with right:
        override=st.checkbox("Custom favorite score",value=record.get("favorite_override") is not None,key=f"override:{key}")
        fav=st.slider("Favorite",1,100,value=numeric(record.get("favorite_override")) or auto_favorite(rank),key=f"favorite:{key}") if override else None
        energy=st.slider("Energy",1,5,value=numeric(record.get("energy")) or 3,key=f"energy:{key}")
        eras=["","60s","70s","80s","90s","00s","10s","2020s"]
        era=st.selectbox("Era",eras,index=eras.index(record.get("era","")) if record.get("era","") in eras else 0,key=f"era:{key}")
        notes=st.text_area("Notes",value=str(record.get("notes","")),key=f"notes:{key}")
    if st.button("Save preferences",type="primary"):
        new={"tags":list(dict.fromkeys(tags+([extra_tag.strip()] if extra_tag.strip() else []))),
             "moods":list(dict.fromkeys(moods+([extra_mood.strip()] if extra_mood.strip() else []))),
             "favorite_override":fav,"energy":energy,"era":era,"listen_more":listen,"notes":notes.strip()}
        meta[key]=new
        try:
            write_atomic_json(META_FILE,meta)
            st.success("Saved")
        except OSError:
            st.warning("This deployment cannot save edits permanently. Download your data before restarting.")


def sidebar(username, entries, baseline):
    with st.sidebar:
        st.markdown("### NOW SCROBBLING")
        st.caption("Live Top 500 · 60s refresh")
        st.markdown("---")
        st.caption(f"{len(entries):,} recovered snapshots · {len(baseline):,} baselines")
        if HISTORY_FILE.exists():
            st.download_button("Back up history",data=HISTORY_FILE.read_bytes(),file_name="leaderboard_history.json",mime="application/json")
        if META_FILE.exists():
            st.download_button("Back up tags",data=META_FILE.read_bytes(),file_name="artist_meta.json",mime="application/json")
        if not HISTORY_FILE.exists():
            st.warning("leaderboard_history.json is missing from this deployment.")
        st.caption("Streamlit Cloud files may reset after redeploy. Export backups or connect durable storage before treating new observations as permanent.")


def main():
    st.markdown(CSS,unsafe_allow_html=True)
    with st.sidebar:
        username=st.text_input("Last.fm username",value=os.getenv("LASTFM_USERNAME","troycapybara"),key="lastfm_user").strip()
    st_autorefresh(interval=REFRESH_MS,limit=None,key="live_poll")
    if not username:
        st.info("Enter your Last.fm username in the sidebar")
        return
    try:
        with st.spinner("Updating…"):
            current,track,cover,top=fetch_live(username)
        st.session_state["last_good"]=(username,current,track,cover,top)
        stale=False
    except (requests.RequestException,ValueError,RuntimeError,KeyError) as exc:
        saved=st.session_state.get("last_good")
        if not saved or saved[0]!=username:
            st.error(f"Last.fm unavailable: {exc}")
            return
        _,current,track,cover,top=saved
        stale=True
    names,ranks,plays=top_maps(top)
    current=next((n for n in names if artist_key(n)==artist_key(current)),current)
    current_key=artist_key(current)
    if current_key not in ranks:
        st.caption(f"{current} is currently outside the Top 500.")
        return
    pos=names.index(current)
    neighborhood=names[max(0,pos-5):min(len(names),pos+6)]
    entries=load_history_entries()
    baselines=load_long_term_baseline(entries)
    ahead=names[pos-1] if pos>0 else None
    behind=names[pos+1] if pos+1<len(names) else None
    cushion=max(0,plays[current_key]-plays[artist_key(behind)]) if behind else None
    # Preserve the earliest established baseline. Snapshot writing never changes it.
    if not stale:
        snapshot={n:{"rank":ranks[artist_key(n)],"playcount":plays[artist_key(n)]} for n in neighborhood}
        try:
            wrote=append_neighborhood(snapshot,username)
            if wrote:
                # Newly seen artists can display an honest 0 immediately.
                now=local_now().isoformat(timespec="milliseconds")
                for n in neighborhood:
                    k=artist_key(n)
                    baselines.setdefault(k,{"rank":ranks[k],"playcount":plays[k],"first_seen":now,"source":"history"})
                entries=load_history_entries()
        except OSError:
            st.sidebar.caption("Live history writes unavailable on this server.")
    if stale:st.caption("Connection interrupted · showing last valid rankings")
    render_hero(current,track,cover,ranks[current_key],plays[current_key],baselines,cushion)
    render_neighborhood(neighborhood,current,ranks,plays,baselines)
    render_chase(ahead,pos if ahead else None,plays[artist_key(ahead)] if ahead else None,plays[current_key])
    # Defer heavy analytical calculations into secondary navigation.
    series_map=observed_series(entries)
    pulse,history,explore,artist=st.tabs(["Pulse","History","Discover","Artist"])
    with pulse:
        colleft,colright=st.columns([1,1],gap="large")
        with colleft:
            section("Momentum","RECORDED ACTIVITY")
            window=st.segmented_control("Lookback",LOOKBACK_CHOICES,default=28,format_func=lambda x:f"{x}d",key="momentum_days",label_visibility="collapsed")
            window=window or 28
            rows=momentum_rows(series_map,names,ranks,plays,window)
            render_frame_table([{**r,"First seen":date_display(r["First seen"]),"Last seen":date_display(r["Last seen"])} for r in rows],
                               ["Artist","Plays gained","Per day","Rank Δ","Last seen"],limit=12)
        with colright:
            section("Top movers","SINCE FIRST OBSERVED")
            prog=progress_rows(names,ranks,plays,baselines,series_map)
            up=sorted(prog,key=lambda v:v["Movement"],reverse=True)
            render_frame_table(up,["Artist","Rank","Movement","Gained"],limit=12)
        section("Playcount gainers","TOP 100 · SINCE FIRST SEEN")
        render_frame_table(sorted(prog,key=lambda v:v["Gained"],reverse=True),["Artist","From","Rank","Movement","Gained","First observed"],limit=100)
    with history:
        section("Listening history",date_display(local_now()))
        metric=st.segmented_control("Metric",["Rank","Playcount"],default="Rank",label_visibility="collapsed",key="history_metric") or "Rank"
        show_neighbors=st.toggle("Compare nearby artists",value=False)
        history_chart(current,entries,baselines,neighborhood if show_neighbors else None,metric)
        section("Period leaders","OBSERVED IN SNAPSHOTS")
        grain=st.segmented_control("Periods",["month","quarter","year"],default="quarter",label_visibility="collapsed",key="period_grain") or "quarter"
        periods=period_leaders(series_map,grain)
        if not periods:st.caption("Waiting for repeated records within a calendar period.")
        for i,(label,items) in enumerate(list(periods.items())[:8]):
            with st.expander(label,expanded=i==0):
                render_frame_table(items,["Artist","Observed gain","Records"],limit=12)
        section("Binge radar","JUMPS WITHIN 48 HOURS")
        render_frame_table(binge_events(series_map),["Artist","Jump","Hours","When"],limit=15)
    with explore:
        section("Milestone watch","ROUND NUMBERS & ONE AWAY")
        parked,almost=milestone_rows(names,plays,ranks)
        mleft,mright=st.columns(2,gap="large")
        with mleft:
            st.markdown("**One away**")
            render_frame_table(almost,["Artist","Rank","Plays","Next milestone"],limit=20)
        with mright:
            st.markdown("**On the number**")
            render_frame_table(parked,["Artist","Rank","Plays","Milestone"],limit=20)
        section("Top 50 contenders","6-MONTH PACE")
        rates=momentum_rows(series_map,names,ranks,plays,180)
        render_frame_table(top50_candidates(names,plays,ranks,rates),
                           ["Artist","Rank","Plays","Gap","Observed/day","Est. days"],limit=25)
        # Adjacent ties are the clearest one-play rank overtake opportunities.
        one_play_pass = []
        for i in range(1, len(names)):
            artist_name, ahead_name = names[i], names[i-1]
            if plays[artist_key(artist_name)] == plays[artist_key(ahead_name)]:
                one_play_pass.append({"Artist": artist_name, "Rank": ranks[artist_key(artist_name)],
                                      "Passes": ahead_name, "Tied at": plays[artist_key(artist_name)]})
        if one_play_pass:
            with st.expander(f"One play from passing · {len(one_play_pass)} matchups"):
                render_frame_table(one_play_pass,["Artist","Rank","Passes","Tied at"],limit=40)
        section("Rank gravity","TOP 200")
        rank_gravity_chart(names,plays)
        section("Head-to-head","90-DAY PACE EXTENSION")
        render_head_to_head(names,plays,series_map,rates)
        section("Weekly listening companions","ACTUAL SCROBBLES")
        st.caption("Which artists show up in the same calendar weeks as the current artist?")
        weeks=st.select_slider("Lookback",options=[4,8,12,16],value=8,format_func=lambda w:f"{w} weeks",key="assoc_weeks")
        if st.button("Analyze weekly listens",key="weekly_query"):
            st.session_state["weekly_requested"]=(username,current,weeks)
        if st.session_state.get("weekly_requested")== (username,current,weeks):
            try:
                tracks,truncated=recent_listening_weeks(username,weeks)
                assoc=weekly_associations(tracks,current)
                render_frame_table(assoc,["Artist","Shared weeks","Plays in those weeks"],limit=15)
                if truncated:st.caption("Partial sample: Last.fm returned more than 2,400 tracks in the period.")
            except (requests.RequestException,ValueError,RuntimeError) as exc:
                st.caption(f"Weekly listening unavailable: {exc}")
    with artist:
        section("Artist preferences",current)
        render_meta(current,ranks[current_key])
    sidebar(username,entries,baselines)


if __name__=="__main__":
    main()
