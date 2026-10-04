"""Now Scrobbling V2 Phase 0+1: standalone Streamlit app, V1-compatible history."""
import json
import os
import random
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent


import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Now Scrobbling", page_icon="🎧", layout="wide")

DATA_DIR = Path(__file__).resolve().parent
CACHE_FILE = DATA_DIR / "leaderboard_cache.json"
HISTORY_FILE = DATA_DIR / "leaderboard_history.json"
META_FILE = DATA_DIR / "artist_meta.json"
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


def render_command_center(artist, track, rank, plays, baseline, ahead, behind):
    st.markdown("### 🎧 Now Scrobbling · Command Center")
    base = baseline.get(artist, {})
    first_rank = as_int(base.get("rank"))
    change = first_rank - rank if first_rank is not None else None
    movement = f"{change:+d} places" if change is not None else "Not tracked yet"
    target_name, target_plays = ahead if ahead else (None, None)
    gap = max(0, target_plays - plays) if ahead else None
    needed = gap + 1 if gap is not None else None
    cushion = max(0, plays - behind[1]) if behind else None

    st.caption(f"**{artist}** · {track}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current position", f"#{rank}")
    c2.metric("Since first observed", movement, help=f"First recorded rank: #{first_rank}" if first_rank else "No baseline yet")
    c3.metric("All-time scrobbles", f"{plays:,}")
    c4.metric("Cushion below", f"{cushion:,} plays" if cushion is not None else "—", help="Current playcount lead over the next artist below; ties may affect ranking")

    if ahead:
        st.markdown(f"**Next target:** {target_name} (#{rank-1}) · {target_plays:,} scrobbles")
        st.markdown(f"**Gap:** {gap:,} plays · **Estimated to pass:** {needed:,} additional plays · **{chase_status(gap)}**")
        # Relative gap visual: fraction of this matchup's original gap closed in this session.
        # Do not fabricate historical matchup starts: show current relative playcount ratio instead.
        fraction = min(1.0, plays / target_plays) if target_plays else 1.0
        st.progress(fraction, text=f"Chase meter · {fraction:.1%} of target's total playcount")
        st.caption("The meter shows current playcount proximity, not progress since the matchup began. Tie ordering may change the exact pass threshold.")
    else:
        st.markdown("**Next target:** Already at #1 — nobody ahead.")
    st.divider()


def render_chart(artists, ranks, plays, current, baseline):
    labels, colors, hover = [], [], []
    for name in artists:
        rank = ranks[name]
        base_rank = as_int(baseline.get(name, {}).get("rank"))
        color = "gray" if base_rank is None or rank == base_rank else ("green" if rank < base_rank else "red")
        if name.casefold() == current.casefold():
            color = "deepskyblue"  # Non-negotiable override, applied LAST.
        colors.append(color)
        labels.append(f"#{rank} {name}" + (" · NOW PLAYING" if name.casefold() == current.casefold() else ""))
        movement = "No baseline" if base_rank is None else f"{base_rank-rank:+d} positions since first observed"
        hover.append(f"{name}<br>Rank #{rank}<br>{plays[name]:,} scrobbles<br>{movement}")
    fig = go.Figure(go.Bar(x=[plays[n] for n in artists][::-1], y=labels[::-1],
                           orientation="h", marker_color=colors[::-1],
                           text=[f"{plays[n]:,}" for n in artists][::-1], textposition="outside",
                           customdata=hover[::-1], hovertemplate="%{customdata}<extra></extra>"))
    fig.update_layout(title="Surrounding Artists · Movement Since First Observed",
                      xaxis_title="All-time scrobbles", yaxis_title="",
                      margin=dict(l=20, r=45, t=55, b=35), height=490)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("🔵 Now playing  ·  🟢 Above first-observed rank  ·  🔴 Below first-observed rank  ·  ⚪ Unchanged / unknown")


def render_meta(current, ranks):
    meta = load_artist_meta()
    info = meta.get(current, {})
    all_tags = sorted({str(t) for m in meta.values() if isinstance(m, dict) for t in m.get("tags", [])})
    all_moods = sorted({str(t) for m in meta.values() if isinstance(m, dict) for t in m.get("moods", [])})
    st.subheader("🏷️ Tag and Rate This Artist")
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
    st.subheader("Rank / Playcount Over Time")
    if len(series) < 2:
        st.caption("Not enough recorded appearances for a historical chart.")
        return
    metric = st.radio("View over time", ["Rank", "Playcount"], horizontal=True)
    fig = go.Figure(go.Scatter(x=[s[0] for s in series], y=[s[1] if metric == "Rank" else s[2] for s in series], mode="lines+markers"))
    if metric == "Rank": fig.update_yaxes(autorange="reversed")
    fig.update_layout(title=f"{current} · {metric} Over Time", xaxis_title="Recorded snapshot time", yaxis_title=metric, height=350)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Only actual recorded neighborhood appearances are plotted; missing periods are not inferred.")


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
    st.subheader("🎧 Top 50 Playcount Gainers Since First Appearance")
    st.dataframe(sorted(rows, key=lambda r: r["Scrobbles gained"], reverse=True)[:50], hide_index=True, use_container_width=True)


def main():
    st.title("Now Scrobbling")
    username = st.text_input("Last.fm username", value=os.getenv("LASTFM_USERNAME", ""), placeholder="your_username_here").strip()
    st_autorefresh(interval=REFRESH_MS, limit=None, key="refresh")
    if not username: return
    try:
        with st.spinner("Fetching your current listening data..."):
            current, track, art, top = fetch_live(username)
        st.session_state["last_valid_live"] = (username, current, track, art, top)
        stale = False
    except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
        saved = st.session_state.get("last_valid_live")
        if not saved or saved[0] != username:
            st.error(f"Could not fetch Last.fm data: {exc}")
            return
        _, current, track, art, top = saved
        stale = True
        st.warning(f"Showing last valid data; latest Last.fm request failed: {exc}")
    ranks, plays = top_maps(top)
    current = next((name for name in ranks if name.casefold() == current.casefold()), current)
    if current not in ranks:
        st.info(f"{current} is outside your Top 500. No neighborhood available.")
        return
    rank = ranks[current]
    names = [a.get("name") for a in top[:500] if a.get("name")]
    pos = names.index(current)
    neighborhood = names[max(0, pos-5):min(len(names), pos+6)]
    entries = load_history_entries()
    baseline = load_long_term_baseline(entries)  # BEFORE logging current neighborhood.
    if V2_ENABLED:
        ahead = (names[pos-1], plays[names[pos-1]]) if pos > 0 else None
        behind = (names[pos+1], plays[names[pos+1]]) if pos+1 < len(names) else None
        render_command_center(current, track, rank, plays[current], baseline, ahead, behind)
    if not stale:
        snapshot = {name: {"rank": ranks[name], "playcount": plays[name]} for name in neighborhood}
        try:
            append_history(snapshot)
        except OSError as exc:
            st.warning(f"Could not append history (existing history was preserved): {exc}")
    render_chart(neighborhood, ranks, plays, current, baseline)
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"**Track:** {track}")
        st.markdown(f"**Artist:** {current} · **Playcount:** {plays[current]:,}")
    with col2:
        if art: st.image(art, width=145)
    st.divider()
    render_meta(current, ranks)
    st.divider()
    render_history(current, entries, baseline)
    st.divider()
    render_movers(top, baseline)


if __name__ == "__main__":
    main()
