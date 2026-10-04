"""Now Scrobbling V2 Phase 0+1: standalone Streamlit app, V1-compatible history."""
import html
import json
import os
import random
from textwrap import dedent
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Now Scrobbling", page_icon="🎧", layout="wide", initial_sidebar_state="collapsed")

DATA_DIR = Path(os.getenv("NOW_SCROBBLING_DATA_DIR", str(Path(__file__).resolve().parent)))
CACHE_FILE = DATA_DIR / "leaderboard_cache.json"
HISTORY_FILE = DATA_DIR / "leaderboard_history.json"
META_FILE = DATA_DIR / "artist_meta.json"
# On Streamlit Cloud, supply LASTFM_API_KEY through App Settings > Secrets.
# Environment variables remain supported for local hosting.
try:
    API_KEY = os.getenv("LASTFM_API_KEY", "") or st.secrets.get("LASTFM_API_KEY", "")
except (FileNotFoundError, KeyError):
    API_KEY = os.getenv("LASTFM_API_KEY", "")
V2_ENABLED = os.getenv("NOW_SCROBBLING_V2", "1") == "1"
REFRESH_MS = 60000
CHASE_THRESHOLDS = {"one_session": 5, "within_reach": 25, "closing": 100}
API_URL = "https://ws.audioscrobbler.com/2.0/"


def api_call(method, username, **kwargs):
    if not API_KEY:
        raise RuntimeError("Set LASTFM_API_KEY in your environment or Streamlit secrets.")
    response = requests.get(API_URL, params={"method": method, "user": username,
                        "api_key": API_KEY, "format": "json", **kwargs}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload.get("message", "Last.fm API error"))
    return payload


def fetch_live(username):
    recent = api_call("user.getrecenttracks", username, limit=1)
    tracks = recent.get("recenttracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks:
        raise RuntimeError("No recent tracks returned.")
    track = tracks[0]
    artist_field = track.get("artist", {})
    artist = artist_field.get("#text", "") if isinstance(artist_field, dict) else str(artist_field)
    title = track.get("name", "")
    images = track.get("image") or []
    art = images[-1].get("#text", "") if images else ""
    artists = api_call("user.gettopartists", username, period="overall", limit=500).get("topartists", {}).get("artist", [])
    if isinstance(artists, dict):
        artists = [artists]
    if not artists:
        raise RuntimeError("No Top 500 artists returned. Existing history was not modified.")
    return artist, title, art, artists


def load_history_entries():
    """Stable V1 JSONL reader. Intentionally does not rewrite or migrate history."""
    if not HISTORY_FILE.exists():
        return []
    entries = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or not (line.startswith("{") and '"timestamp"' in line and '"data"' in line):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
                    entries.append(obj)
    except OSError as exc:
        st.warning(f"Could not read historical data: {exc}")
    return entries


def load_previous_leaderboard():
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as handle:
            obj = json.load(handle)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def as_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def load_long_term_baseline(entries):
    if not entries:
        return load_previous_leaderboard()
    baselines = {}
    for entry in sorted(entries, key=lambda item: str(item.get("timestamp") or "")):
        snapshot = entry.get("data", {})
        if not isinstance(snapshot, dict) or not snapshot:
            continue
        for artist, info in snapshot.items():
            if artist not in baselines and isinstance(info, dict) and as_int(info.get("rank")) is not None and as_int(info.get("playcount")) is not None:
                baselines[artist] = {"rank": as_int(info["rank"]), "playcount": as_int(info["playcount"]), "first_seen": entry.get("timestamp")}
    return baselines


def load_artist_meta():
    try:
        with META_FILE.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_artist_meta(meta):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
    os.replace(tmp, META_FILE)


def append_history(snapshot):
    """V1-style neighborhood logging; dedupe repeated Streamlit reruns in session."""
    signature = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    if st.session_state.get("last_logged_signature") == signature:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now().isoformat(timespec="milliseconds"), "data": snapshot}
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    st.session_state["last_logged_signature"] = signature
    # Cache remains a fallback only; never a source of overriding historical baselines.
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    os.replace(tmp, CACHE_FILE)


def top_maps(top):
    rank_map, play_map = {}, {}
    for index, artist in enumerate(top[:500], start=1):
        name = artist.get("name", "")
        if name:
            rank_map[name] = index
            play_map[name] = as_int(artist.get("playcount")) or 0
    return rank_map, play_map


def auto_favorite_from_rank(rank):
    if rank is None: return 40
    for bound, score in [(10, 100), (15, 95), (30, 90), (75, 80), (150, 70), (350, 60), (500, 50)]:
        if rank <= bound: return score
    return 40


def chase_status(gap):
    if gap <= CHASE_THRESHOLDS["one_session"]: return "One session away"
    if gap <= CHASE_THRESHOLDS["within_reach"]: return "Within reach"
    if gap <= CHASE_THRESHOLDS["closing"]: return "Closing"
    return "Distant"


CSS = """
<style>
/* Minimal, dark, information-first UI. No ranking logic lives in CSS. */
:root {color-scheme: dark;}
.stApp {background: #090d13; color: #eef3fa;}
.block-container {max-width: 1120px; padding-top: 1.2rem; padding-bottom: 3rem;}
header[data-testid="stHeader"] {background: rgba(9,13,19,.86);}
[data-testid="stSidebar"] {background: #0d131d;}
[data-testid="stMarkdownContainer"] p {margin-bottom: .3rem;}
[data-testid="stPlotlyChart"] {background: transparent;}
[data-testid="stHorizontalBlock"] {align-items: start;}
div[data-testid="stTabs"] button {font-size: .85rem; color: #8495a9;}
div[data-testid="stTabs"] button[aria-selected="true"] {color: #f3f7fc;}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {background-color: #2fc1ff;}
div[data-testid="stExpander"] {background: #0e151f; border: 1px solid #202a37; border-radius: 12px;}
.ns-shell {font-family: Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;}
.ns-topline {display:flex; align-items:center; justify-content:space-between; gap:12px; margin: 2px 0 14px;}
.ns-live {font-weight: 750; font-size: 10px; letter-spacing: .17em; color:#c5d4e8; display:flex; align-items:center; gap:8px;}
.ns-dot {display:inline-block; width:7px; height:7px; border-radius:50%; background:#37c1ff; box-shadow:0 0 12px rgba(55,193,255,.36);}
.ns-tag {font-size:10px; letter-spacing:.1em; color:#64758d; font-weight:650;}
.ns-main {display:flex; align-items:center; gap:15px; padding:0 0 17px;}
.ns-cover {width:66px; height:66px; flex:0 0 66px; border-radius:11px; overflow:hidden; border:1px solid #253246; background:linear-gradient(140deg, #192639, #0d151f); display:flex; align-items:center; justify-content:center;}
.ns-cover img {width:100%; height:100%; object-fit:cover;}
.ns-cover-fallback {color:#617189; font-size:25px;}
.ns-ident {flex:1; min-width:0;}
.ns-artist {font-size:clamp(25px,3.4vw,39px); font-weight:780; line-height:1.12; color:#f1f5fb; letter-spacing:-.055em; overflow-wrap:anywhere;}
.ns-song {margin-top:6px; font-size:13px; font-weight:450; color:#8a9cb2; overflow-wrap:anywhere;}
.ns-rank {padding-left:16px; text-align:right; border-left:1px solid #263143; flex:0 0 auto;}
.ns-rank small {display:block; color:#7b8ba2; font-size:10px; letter-spacing:.12em; font-weight:700;}
.ns-rank strong {display:block; margin-top:1px; font-size:clamp(34px,4.1vw,54px); font-weight:750; color:#f2f6fc; letter-spacing:-.06em; line-height:1.12; font-variant-numeric:tabular-nums;}
.ns-stat-grid {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-top:1px solid #202c3b; border-bottom:1px solid #202c3b; padding:15px 0 17px; margin:0 0 3px;}
.ns-stat {padding:0 18px; border-right:1px solid #202b39; min-width:0;}
.ns-stat:first-child {padding-left:0;}
.ns-stat:last-child {border-right:0;}
.ns-stat-label {font-size:10px; text-transform:uppercase; letter-spacing:.12em; font-weight:650; color:#778ba2; margin-bottom:6px;}
.ns-stat-value {font-size:clamp(21px,2.2vw,27px); color:#edf4fa; font-weight:700; letter-spacing:-.045em; line-height:1.13; font-variant-numeric:tabular-nums;}
.ns-stat-note {color:#7c8da3; font-weight:450; font-size:11px; margin-left:5px; letter-spacing:0;}
.ns-up {color:#23bb7a;}
.ns-down {color:#ec7779;}
.ns-quiet {color:#a3afbf;}
.ns-chase {margin: 0 0 21px; padding:14px 16px 15px; border:1px solid #273244; border-radius:12px; background:#0d141e;}
.ns-chase-head {display:flex; align-items:center; justify-content:space-between; gap:10px;}
.ns-chase-left {min-width:0; display:flex; flex-direction:column; gap:4px;}
.ns-chase-kicker {font-size:9px; letter-spacing:.14em; color:#75889d; font-weight:750;}
.ns-chase-name {font-size:15px; font-weight:650; letter-spacing:-.025em; color:#eef3fa; overflow-wrap:anywhere;}
.ns-chase-name span {font-weight:450; color:#7990a8; margin-left:5px; font-size:12px;}
.ns-chase-number {font-size:24px; font-weight:750; color:#dfe9f4; font-variant-numeric:tabular-nums; letter-spacing:-.045em; white-space:nowrap;}
.ns-chase-number span {font-size:11px; font-weight:500; color:#8899ae; letter-spacing:0; margin-left:5px;}
.ns-trackline {height:4px; width:100%; margin-top:13px; overflow:hidden; border-radius:9px; background:#263242;}
.ns-trackfill {height:100%; border-radius:9px; background:#4186b7;}
.ns-chase-foot {display:flex; justify-content:space-between; gap:8px; margin-top:7px; font-size:10px; color:#6f8299;}
.ns-caption {font-size:11px; color:#72859c; margin:-4px 0 9px;}
.ns-hidden {display:none;}
@media (max-width:700px) {
 .block-container {padding: .8rem .7rem 2.1rem;}
 .ns-topline {margin:0 0 12px;}
 .ns-main {gap:11px; padding-bottom:15px;}
 .ns-cover {width:54px; height:54px; flex-basis:54px; border-radius:9px;}
 .ns-artist {font-size:clamp(21px,6vw,28px);}
 .ns-song {font-size:12px; margin-top:4px;}
 .ns-rank {padding-left:10px;}
 .ns-rank strong {font-size:33px;}
 .ns-stat-grid {padding:12px 0 13px;}
 .ns-stat {padding:0 10px;}
 .ns-stat-label {font-size:9px; letter-spacing:.04em;}
 .ns-stat-value {font-size:20px;}
 .ns-stat-note {display:none;}
 .ns-chase {padding:12px; margin-bottom:15px;}
 .ns-chase-name {font-size:13px;}
 .ns-chase-number {font-size:21px;}
 .ns-tag {font-size:9px;}
}
</style>
"""


def render_styles():
    st.markdown(CSS, unsafe_allow_html=True)


def render_command_center(artist, track, art, rank, plays, baseline, behind):
    base = baseline.get(artist, {})
    first_rank = as_int(base.get("rank"))
    movement = first_rank - rank if first_rank is not None else None
    if movement is None:
        move_text, move_class = "—", "ns-quiet"
    elif movement > 0:
        move_text, move_class = f"+{movement}", "ns-up"
    elif movement < 0:
        move_text, move_class = str(movement), "ns-down"
    else:
        move_text, move_class = "0", "ns-quiet"
    cushion = plays - behind[1] if behind else None
    cushion_text = f"{cushion:,}" if cushion is not None else "—"
    cover = (f'<img src="{html.escape(art, quote=True)}" alt="Album art">'
             if art and art.startswith(("https://", "http://"))
             else '<span class="ns-cover-fallback">♫</span>')
    st.markdown(dedent(f"""
    <div class="ns-shell">
      <div class="ns-topline"><div class="ns-live"><span class="ns-dot"></span> NOW SCROBBLING</div>
        <div class="ns-tag">ALL-TIME RANKINGS</div></div>
      <div class="ns-main">
        <div class="ns-cover">{cover}</div>
        <div class="ns-ident"><div class="ns-artist">{html.escape(artist)}</div>
          <div class="ns-song">{html.escape(track or 'Latest scrobble')}</div></div>
        <div class="ns-rank"><small>RANK</small><strong>#{rank}</strong></div>
      </div>
      <div class="ns-stat-grid">
        <div class="ns-stat"><div class="ns-stat-label">Since first seen</div>
          <div class="ns-stat-value {move_class}">{move_text}<span class="ns-stat-note">ranks</span></div></div>
        <div class="ns-stat"><div class="ns-stat-label">Scrobbles</div>
          <div class="ns-stat-value">{plays:,}</div></div>
        <div class="ns-stat"><div class="ns-stat-label">Cushion</div>
          <div class="ns-stat-value">{cushion_text}<span class="ns-stat-note">plays</span></div></div>
      </div>
    </div>
    """), unsafe_allow_html=True)


def render_chart(artists, ranks, plays, current, baseline):
    labels, colors, hover = [], [], []
    for name in artists:
        rank = ranks[name]
        base_rank = as_int(baseline.get(name, {}).get("rank"))
        # PERMANENT baseline color semantics. Do not compare to prior refresh.
        color = "gray" if base_rank is None or rank == base_rank else ("green" if rank < base_rank else "red")
        if name.casefold() == current.casefold():
            color = "deepskyblue"  # Current artist override LAST.
        colors.append(color)
        labels.append(f"{rank:>3}   {name}")
        move = "Not yet observed" if base_rank is None else f"{base_rank-rank:+d} ranks since first seen"
        hover.append(f"<b>{html.escape(name)}</b><br>#{rank} · {plays[name]:,} plays<br>{move}")

    count = len(artists)
    height = max(300, 35 + count * 47)
    fig = go.Figure(go.Bar(
        x=[plays[n] for n in artists][::-1],
        y=labels[::-1],
        orientation="h", marker_color=colors[::-1],
        marker_line_width=[1.6 if n.casefold() == current.casefold() else 0
                           for n in artists][::-1],
        marker_line_color=["#8ee1ff" if n.casefold() == current.casefold() else "rgba(0,0,0,0)"
                           for n in artists][::-1],
        text=[f"{plays[n]:,}" for n in artists][::-1],
        textposition="outside", cliponaxis=False,
        customdata=hover[::-1], hovertemplate="%{customdata}<extra></extra>",
        textfont=dict(family="Inter, Arial, sans-serif", size=12, color="#eaf1f9")))
    fig.update_layout(
        title=None,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Arial, sans-serif", color="#d9e4f0", size=12),
        margin=dict(l=6, r=76, t=12, b=10),
        height=height,
        bargap=0.33,
        showlegend=False,
        xaxis=dict(visible=False, fixedrange=True, range=[0, max(plays[n] for n in artists)*1.09]),
        yaxis=dict(title=None, showgrid=False, ticks="", tickfont=dict(size=11, color="#abbdd1"),
                   automargin=True, fixedrange=True),
        hoverlabel=dict(bgcolor="#162231", font_color="#eff5fc", bordercolor="#2a3d52", font_size=12),
        dragmode=False)
    config = {"displayModeBar": False, "scrollZoom": False, "responsive": True}
    st.plotly_chart(fig, use_container_width=True, config=config)


def render_chase(ahead, rank, plays):
    if not ahead:
        st.markdown('<div class="ns-chase"><span class="ns-chase-kicker">NEXT UP</span>'
                    '<div class="ns-chase-name">#1 · TOP OF THE BOARD</div></div>', unsafe_allow_html=True)
        return
    target, target_plays = ahead
    gap = max(0, target_plays - plays)
    needed = gap + 1
    fraction = max(0, min(100, plays / target_plays * 100)) if target_plays else 100
    st.markdown(dedent(f"""
    <div class="ns-shell ns-chase">
      <div class="ns-chase-head">
        <div class="ns-chase-left">
          <div class="ns-chase-kicker">NEXT UP · #{rank - 1}</div>
          <div class="ns-chase-name">{html.escape(target)} <span>{target_plays:,} plays</span></div>
        </div>
        <div class="ns-chase-number">{gap:,}<span>behind</span></div>
      </div>
      <div class="ns-trackline"><div class="ns-trackfill" style="width:{fraction:.2f}%"></div></div>
      <div class="ns-chase-foot"><span>{chase_status(gap)}</span><span>≈{needed:,} to pass</span></div>
    </div>
    """), unsafe_allow_html=True)

def render_meta(current, ranks):
    meta = load_artist_meta()
    info = meta.get(current, {})
    all_tags = sorted({str(t) for m in meta.values() if isinstance(m, dict) for t in m.get("tags", [])})
    all_moods = sorted({str(t) for m in meta.values() if isinstance(m, dict) for t in m.get("moods", [])})
    st.markdown("**Tags & preferences**")
    left, right = st.columns(2)
    with left:
        tags = st.multiselect("Tags", sorted(set(all_tags) | set(info.get("tags", []))), default=info.get("tags", []), key=f"tags_{current}")
        new_tag = st.text_input("New tag", key=f"newtag_{current}")
        moods = st.multiselect("Moods", sorted(set(all_moods) | set(info.get("moods", []))), default=info.get("moods", []), key=f"moods_{current}")
        new_mood = st.text_input("New mood", key=f"newmood_{current}")
    with right:
        auto_fav = auto_favorite_from_rank(ranks.get(current))
        override = st.checkbox("Override auto favorite score", value=info.get("favorite_override") is not None, key=f"override_{current}")
        fav = st.slider("Favorite score", 1, 100, value=int(info.get("favorite_override") or auto_fav), key=f"fav_{current}") if override else None
        if not override: st.caption(f"Automatic favorite score: {auto_fav}")
        energy = st.slider("Energy", 1, 5, value=int(info.get("energy", 3)), key=f"energy_{current}")
        eras = ["", "60s", "70s", "80s", "90s", "00s", "10s", "2020s"]
        era = st.selectbox("Era", eras, index=eras.index(info.get("era", "")) if info.get("era", "") in eras else 0, key=f"era_{current}")
        listen_more = st.checkbox("Want to listen to more", value=bool(info.get("listen_more", False)), key=f"more_{current}")
        notes = st.text_area("Notes", value=info.get("notes", ""), key=f"notes_{current}")
    if st.button("Save artist tags and preferences", key=f"save_{current}"):
        meta[current] = {"tags": list(dict.fromkeys(tags + ([new_tag.strip()] if new_tag.strip() else []))),
                         "moods": list(dict.fromkeys(moods + ([new_mood.strip()] if new_mood.strip() else []))),
                         "energy": energy, "era": era, "favorite_override": fav,
                         "listen_more": listen_more, "notes": notes.strip()}
        save_artist_meta(meta)
        st.success("Saved artist preferences.")


def render_history(current, entries, baseline):
    series = []
    for entry in entries:
        info = entry.get("data", {}).get(current)
        if not isinstance(info, dict): continue
        try:
            stamp = datetime.fromisoformat(str(entry.get("timestamp", "")))
        except ValueError:
            continue
        rank, play = as_int(info.get("rank")), as_int(info.get("playcount"))
        if rank is not None or play is not None:
            series.append((stamp, rank, play))
    series.sort(key=lambda item: item[0])
    st.markdown("**Recorded history**")
    if len(series) < 2:
        st.caption("Not enough recorded appearances for a historical chart.")
        return
    metric = st.radio("View over time", ["Rank", "Playcount"], horizontal=True)
    fig = go.Figure(go.Scatter(x=[s[0] for s in series], y=[s[1] if metric == "Rank" else s[2] for s in series], mode="lines+markers"))
    if metric == "Rank": fig.update_yaxes(autorange="reversed")
    fig.update_traces(line=dict(color="#53b9ff", width=2), marker=dict(size=4, color="#53b9ff"))
    fig.update_layout(title=None, height=310, showlegend=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#9fb1c7"), margin=dict(l=0, r=10, t=10, b=22),
                      xaxis=dict(title=None, gridcolor="#1f2938", zeroline=False),
                      yaxis=dict(title=None, gridcolor="#1f2938", zeroline=False,
                                 autorange="reversed" if metric == "Rank" else True))
    st.plotly_chart(fig, use_container_width=True)



def render_movers(top, baseline):
    rows = []
    for rank, artist in enumerate(top[:500], start=1):
        name = artist.get("name")
        base = baseline.get(name, {})
        br, bp = as_int(base.get("rank")), as_int(base.get("playcount"))
        cp = as_int(artist.get("playcount"))
        if br is None or bp is None or cp is None: continue
        rows.append({"Artist": name, "Baseline rank": br, "Current rank": rank,
                     "Rank change": br-rank, "Scrobbles gained": cp-bp,
                     "First observed": base.get("first_seen", "Unknown")})
    with st.expander("🏆 Biggest Movers Since First Appearance"):
        st.markdown("**Biggest risers**")
        st.dataframe(sorted(rows, key=lambda r: r["Rank change"], reverse=True)[:10], hide_index=True, use_container_width=True)
        st.markdown("**Biggest fallers**")
        st.dataframe(sorted(rows, key=lambda r: r["Rank change"])[:10], hide_index=True, use_container_width=True)
    st.markdown("**Top 50 playcount gainers**")
    st.dataframe(sorted(rows, key=lambda r: r["Scrobbles gained"], reverse=True)[:50], hide_index=True, use_container_width=True)


def main():
    render_styles()
    with st.sidebar:
        st.markdown("**NOW SCROBBLING**")
        username = st.text_input("Last.fm username", value=os.getenv("LASTFM_USERNAME", "troycapybara"),
                                 placeholder="Last.fm username").strip()
        st.caption("Updates every 60 seconds")
    st_autorefresh(interval=REFRESH_MS, limit=None, key="refresh")
    if not username:
        st.info("Enter a Last.fm username in the sidebar.")
        return
    try:
        with st.spinner("Updating rankings…"):
            current, track, art, top = fetch_live(username)
        st.session_state["last_valid_live"] = (username, current, track, art, top)
        stale = False
    except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
        saved = st.session_state.get("last_valid_live")
        if not saved or saved[0] != username:
            st.error(f"Last.fm unavailable: {exc}")
            return
        _, current, track, art, top = saved
        stale = True
        st.caption("Live update unavailable · showing last good rankings")
    ranks, plays = top_maps(top)
    current = next((name for name in ranks if name.casefold() == current.casefold()), current)
    if current not in ranks:
        st.info(f"{current} is outside your Top 500.")
        return
    rank = ranks[current]
    names = [a.get("name") for a in top[:500] if a.get("name")]
    pos = names.index(current)
    neighborhood = names[max(0, pos-5):min(len(names), pos+6)]
    entries = load_history_entries()
    baseline = load_long_term_baseline(entries)  # BEFORE logging current neighborhood.
    ahead = (names[pos-1], plays[names[pos-1]]) if pos > 0 else None
    behind = (names[pos+1], plays[names[pos+1]]) if pos+1 < len(names) else None

    # Compact intelligence header; bar chart stays the visual centerpiece.
    if V2_ENABLED:
        render_command_center(current, track, art, rank, plays[current], baseline, behind)
    else:
        st.markdown(f"**{current}** · {track} · #{rank}")
    if not stale:
        snapshot = {name: {"rank": ranks[name], "playcount": plays[name]} for name in neighborhood}
        try:
            append_history(snapshot)
        except OSError:
            # Existing imported history remains readable even if storage is read-only.
            st.sidebar.caption("History is read-only; new activity may not persist.")

    # No chart title, axis title, caption, or always-visible legend.
    render_chart(neighborhood, ranks, plays, current, baseline)
    if V2_ENABLED:
        render_chase(ahead, rank, plays[current])

    # Secondary screens: available without occupying the everyday leaderboard.
    history_tab, movers_tab, artist_tab = st.tabs(["History", "Movers", "Artist"])
    with history_tab:
        render_history(current, entries, baseline)
    with movers_tab:
        render_movers(top, baseline)
    with artist_tab:
        render_meta(current, ranks)


if __name__ == "__main__":
    main()
