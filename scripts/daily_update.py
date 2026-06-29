"""
Mindful Joga — Daily Automation
---------------------------------
Runs once a day (via GitHub Actions). Does four things in order:

  1. Fetch the most recent World Cup match results from API-Football (looks
     back a few days if needed, so knockout-stage rest days still have a
     real result to recap)
  2. Turn those results into a calm script using Claude (house style baked in)
  3. Turn that script into audio using ElevenLabs (Ryan's voice)
  4. Save the audio file into audio/today-male.mp3, overwriting the old one

All three API keys (API_FOOTBALL_KEY, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY)
are read from environment variables — GitHub Actions injects these from
encrypted repository Secrets, they are never written to disk or logged.

This script is intentionally a single file: easier to reason about and
debug for a small, single-purpose daily job like this one.
"""

import os
import re
import sys
import json
import subprocess
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
WORLD_CUP_LEAGUE_ID = 1
WORLD_CUP_SEASON = 2026

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"

VOICE_ID = "jbEI5QkrMSKWeDlP27MV"  # Ryan — Deep and Meditative
TTS_MODEL_ID = "eleven_v3"            # premium expressive model — used for the
                                       # main recap, where [tags]/pacing matter most
UPCOMING_TTS_MODEL_ID = "eleven_v3"   # NOTE: previously eleven_flash_v2_5 to
                                       # save credits, but Flash doesn't
                                       # understand eleven_v3's [bracketed]
                                       # tags — it just reads them aloud as
                                       # literal text, plus a real quality
                                       # drop. Reverted to v3 for both scripts;
                                       # cost control comes from script length
                                       # instead (see UPCOMING_STYLE_PROMPT).

# ffmpeg's atempo filter is a SPEED multiplier, not a slowdown percentage.
# 0.90 means "play at 90% speed" = 10% slower overall. Pitch is preserved.
SLOWDOWN_FACTOR = 0.90

VOICE_SETTINGS = {
    "stability": 0.32,  # lowered further from 0.40 — encourages slower, more
                         # deliberate pacing and more natural variance in rhythm
    "similarity_boost": 0.75,
    "style": 0.35,
    "use_speaker_boost": True,
}

HOUSE_STYLE_PROMPT = """You are writing the daily script for "Mindful Joga" — \
a mindful, ASMR-toned daily MORNING audio recap of World Cup football. It drops once \
each morning so the listener can start their day caught up, calmly. During the group \
stage this almost always means the matches that finished the prior day — but during \
knockout rounds, with single-game days and multi-day gaps between rounds, it may \
instead be recapping the most recent result from a few days back. The user message \
will always tell you exactly how recent the matches are — follow that note precisely \
for how you talk about timing, rather than assuming "yesterday." Follow these rules \
exactly:

VOICE & TONE:
- Open with "Good morning. Welcome to [today's date], Mindful Joga's World Cup
  recap." Use the date provided to you in the user message, spoken naturally
  (e.g. "Welcome to June twenty-second, Mindful Joga's World Cup recap" — not
  the numeric format, say it the way a person would say it out loud). This is
  specifically a morning ritual, and the listener should know exactly where
  they are within the first few seconds.
- If the recency note says the matches are NOT from yesterday (a knockout-stage
  rest day with nothing new), gently acknowledge the quiet stretch instead of
  implying these are fresh results — something like "[softly] it's been a
  quiet couple of days in the tournament... so let's sit with the last result
  a little longer." This keeps the script honest without making the lack of
  new matches feel like a problem to apologize for.
- Warm, slow, unhurried — slower than feels natural at first. Short sentences. Generous room to breathe between thoughts, not just between sections.
- Address the listener directly as "you" at least 3-4 times in the script.
- Never use score-anxiety language (no "DESTROYED," "STUNNING," "SMASHED," etc.)
  even for blowout results. A 6-0 win and a 0-0 draw get the same calm register.
- This script will be read by ElevenLabs' v3 model, which understands bracketed
  audio tags like [softly], [whispers], [slowly], [gently], [sighs], [breathes in].
  Use [slowly] and [gently] more liberally than feels necessary — at least once per
  2-3 sentences, not just once per paragraph. Favor calm/breathy tags: [softly],
  [gently], [slowly], [whispers]. Do NOT use unrelated emotional tags like
  [excited] or [happy] — this is a meditative read, not a performance.
- Use ellipses (...) generously for natural hesitant pauses — more than feels
  strictly necessary on the page. Between major beats (after a score, before
  moving to a new match), insert a standalone [pause] or [long pause] line.
- Vary sentence length deliberately: a short, plain sentence after a longer
  flowing one gives the reader's breath somewhere to land.

MINDFULNESS LAYER (roughly 10-15% of total script — present but not dominant):
- Open with a brief "settle in" moment: shoulders, jaw, permission to not be ready.
- Include 2-3 breathing reminders across the script, but VARY how they're delivered —
  don't repeat the same "breathe in... breathe out" phrasing each time. Mix:
    (a) a literal, instructional cue, written so the model performs an ACTUAL
        breath sound rather than just saying the words. Use the audio tags
        [inhales] and [exhales] directly in the line, e.g.:
        "Breathe in... [inhales] ...and let it go. [exhales]"
        The tag should sit right where the breath itself happens, not just
        near it, so the model has the best chance of actually performing it
        rather than reading the bracketed word aloud.
    (b) an immersive, woven-in cue that doesn't sound like an instruction
        (e.g. "Notice the air in the room before the next line. [inhales]
        That's yours to keep, however the match went. [exhales]")
    (c) a cue tied to the football itself (e.g. "Even the players take a
        breath before a free kick. [inhales] Take yours now. [exhales]")
  At least one breathing moment per script should NOT use the literal words
  "breathe in" / "breathe out" — find a more textured, sensory way in, but
  still include the [inhales]/[exhales] tags so the actual sound is there.
- Give every LOSING team a short (1-2 sentence) genuine consolation beat tied to
  football's actual rhythm — never generic, never minimizing. Something like:
  "there's almost always another match coming."
- Close with a short grounding line that turns back toward the listener, not just
  the tournament (e.g. "the tournament will still be there. So will you.")

STRUCTURE:
- Brief warm opening + settle-in moment + first breath cue
- Go through each match in turn, with venue named
- After each match with a clear loser, include the consolation beat
- Second breath cue
- Short closing summary + grounding line

PLAYER NAMES (use sparingly — this is the new part):
- The match data may include goal scorers by name. You do NOT need to mention
  every scorer in every match — that would turn this into a box score, which
  is exactly what this show is not.
- Pick 1-3 moments across the whole script where naming a player adds warmth
  or color, not just information. Good candidates: a player who scored more
  than once in a match, a goalkeeper's team kept a clean sheet (0 conceded),
  a late/dramatic-minute goal, an own goal (handle this one gently and kindly,
  never mockingly).
- When you do name a player, keep it brief and human — "Eduardo found the net
  again in the second half" not a stat-sheet recitation of minute and method.
- It is completely fine, and often better, for a script to mention zero or one
  player by name. Do not force a name into every match just because the data
  is there.

QUALIFICATION STATUS (advancing / eliminated / still waiting):
- When provided, qualification status data tells you whether a team is
  through to the next round, out of the tournament, or still waiting because
  their group isn't finished. Weave this in naturally where it fits — after
  describing a match's result is the most natural spot.
- "Advancing" gets a warm, simple acknowledgment — "Brazil moves on to the
  next round" — no extra fanfare needed, the result already carries that.
- "Eliminated" needs the same gentle care as a losing-team consolation beat,
  arguably more so, since this is the end of their tournament, not just one
  bad day. Never say "eliminated," "knocked out," or "out" as a blunt fact
  and move on. Soften it: "their World Cup ends here, for now" or "they'll
  be watching the rest of this from home, same as a lot of us." Always pair
  it with the same warmth you'd give any consolation beat.
- "Waiting" should be framed as genuinely suspenseful in a calm way, not
  anxious — "their fate isn't decided yet, and won't be for a couple more
  days" — this is good material for naming the unresolved feeling without
  trying to resolve it.
- Do NOT force this into every match. If the status data isn't provided, or
  doesn't add something genuinely worth saying, leave it out entirely. This
  is seasoning, like the player names — most matches don't need it spelled
  out, especially mid-group-stage when most teams are still just "waiting."

LENGTH: Aim for roughly 500-700 words total (about 3-4 minutes spoken slowly).

Output ONLY the finished script. No headers, no notes, no explanation — just the
words to be spoken, with [tags] and ... inline as described above."""


def log(message):
    print(f"[mindful-joga] {message}", flush=True)


# ---------------------------------------------------------------------------
# ELEVENLABS CREDIT MONITORING
# ---------------------------------------------------------------------------
# Tuned for the World Cup's remaining run: both scripts together cost roughly
# 6,000 characters/day, both on eleven_v3 (same model for recap and upcoming —
# Flash was tried briefly for the cheaper Upcoming script but couldn't
# interpret the [bracketed] emotional tags, so both are back on v3). These
# thresholds give a multi-day heads-up before a hard quota_exceeded failure,
# rather than finding out the same way we did the first time — a crashed
# Action mid-run.
LOW_CREDIT_DAYS_THRESHOLD = 5     # warn (but still run normally) below this many
                                   # projected days of runway at current usage
CRITICAL_CREDIT_BUFFER = 500      # if remaining credits are under
                                   # (today's estimated need + this buffer),
                                   # treat it as critical for today's run


def fetch_elevenlabs_credit_status(api_key):
    """Check remaining ElevenLabs credits and estimate days of runway at the
    recent daily average. Returns a dict, or None if the check itself fails
    (in which case the caller should proceed normally rather than block the
    whole run over a monitoring hiccup)."""
    try:
        response = requests.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": api_key},
        )
        if response.status_code != 200:
            log(f"Could not check ElevenLabs subscription status: {response.status_code}")
            return None

        data = response.json()
        used = data.get("character_count", 0)
        limit = data.get("character_limit", 0)
        remaining = limit - used
        reset_unix = data.get("next_character_count_reset_unix")

        days_until_reset = None
        if reset_unix:
            seconds_left = reset_unix - datetime.now(timezone.utc).timestamp()
            days_until_reset = max(seconds_left / 86400, 0)

        log(f"ElevenLabs credits: {remaining:,} remaining of {limit:,} "
            f"({used:,} used). Resets in "
            f"{days_until_reset:.1f} day(s)." if days_until_reset is not None
            else f"ElevenLabs credits: {remaining:,} remaining of {limit:,} ({used:,} used).")

        return {
            "remaining": remaining,
            "limit": limit,
            "days_until_reset": days_until_reset,
        }
    except Exception as e:
        # Monitoring should never be the reason the whole run fails.
        log(f"ElevenLabs credit check failed (non-fatal): {e}")
        return None


# ---------------------------------------------------------------------------
# STEP 1 — Fetch the most recent match results
# ---------------------------------------------------------------------------

def fetch_standings(api_key):
    """Fetch group-stage standings for all 12 groups and classify each team
    as advancing, eliminated, or still waiting on other results — using the
    actual World Cup 2026 format (top 2 of each group, plus the 8 best
    third-placed teams across all groups, advance to the Round of 32)."""
    headers = {"x-apisports-key": api_key}
    params = {"league": WORLD_CUP_LEAGUE_ID, "season": WORLD_CUP_SEASON}

    response = requests.get(f"{API_FOOTBALL_BASE}/standings", headers=headers, params=params)
    if response.status_code != 200:
        log(f"Standings fetch error: {response.status_code}")
        return []

    data = response.json()
    raw_groups = data.get("response", [{}])[0].get("league", {}).get("standings", [])
    if not raw_groups:
        log("No standings data returned (group stage may not have started yet).")
        return []

    # Each group's rows, with played-match count, to know if the group is
    # actually finished (all teams played all 3 group matches) before we
    # treat a 3rd-place finish as final rather than "still waiting."
    groups = []
    third_placed = []
    for group_rows in raw_groups:
        group_name = group_rows[0]["group"] if group_rows else "Unknown"
        group_complete = all(row["all"]["played"] >= 3 for row in group_rows)
        team_rows = []
        for row in group_rows:
            team_rows.append({
                "team": row["team"]["name"],
                "rank": row["rank"],
                "points": row["points"],
                "played": row["all"]["played"],
                "goal_diff": row["goalsDiff"],
                "goals_for": row["all"]["goals"]["for"],
            })
            if row["rank"] == 3:
                third_placed.append({
                    "team": row["team"]["name"],
                    "group": group_name,
                    "points": row["points"],
                    "goal_diff": row["goalsDiff"],
                    "goals_for": row["all"]["goals"]["for"],
                    "group_complete": group_complete,
                })
        groups.append({"group": group_name, "complete": group_complete, "teams": team_rows})

    # Determine the 8 best 3rd-placed teams, but ONLY once enough groups are
    # complete to make that ranking meaningful — otherwise every 3rd place
    # team should show as "waiting," not prematurely eliminated.
    # IMPORTANT: compare against the real tournament-wide group count (12),
    # not just len(groups) — a partial API response could otherwise make a
    # handful of finished groups look like "all groups," which would
    # incorrectly eliminate 3rd-place teams before the full picture is in.
    TOTAL_WORLD_CUP_GROUPS = 12
    complete_groups = [g for g in groups if g["complete"]]
    all_groups_done = len(complete_groups) >= TOTAL_WORLD_CUP_GROUPS

    best_third_teams = set()
    if all_groups_done:
        ranked_thirds = sorted(
            third_placed,
            key=lambda t: (t["points"], t["goal_diff"], t["goals_for"]),
            reverse=True,
        )
        best_third_teams = {t["team"] for t in ranked_thirds[:8]}

    classified = []
    for g in groups:
        for row in g["teams"]:
            if not g["complete"]:
                status = "waiting"  # group still has matches left to play
            elif row["rank"] in (1, 2):
                status = "advancing"
            elif row["rank"] == 3:
                status = "advancing" if row["team"] in best_third_teams else (
                    "waiting" if not all_groups_done else "eliminated"
                )
            else:
                status = "eliminated"
            classified.append({
                "team": row["team"],
                "group": g["group"],
                "rank": row["rank"],
                "status": status,
            })

    return classified


# Round names (lowercased) that are knockout rounds where a loss means
# elimination outright -- i.e. NOT the semifinals, where losers get one more
# match (the 3rd place playoff) before actually going home, and NOT the
# 3rd-place playoff or Final themselves, which are handled separately.
STRAIGHT_ELIMINATION_ROUNDS = ("round of 32", "round of 16", "quarter")


def fetch_knockout_eliminations(api_key):
    """Fetch every knockout-stage fixture played so far and determine which
    teams have actually been eliminated by an on-pitch result, as distinct
    from the group-stage-only classification fetch_standings() produces.

    Single elimination applies from the Round of 32 through the
    Quarterfinals: the loser of any of those matches is eliminated
    immediately. The Semifinals are the one exception -- losing a semifinal
    does NOT eliminate a team; they instead move on to play the other
    semifinal loser in the 3rd place match. Only losing THAT match (or the
    Final) ends a team's tournament.

    Returns a dict of {team_name: "eliminated" | "playing for 3rd place" |
    "advancing"} for any team found in a finished knockout fixture. Teams
    not present in this dict simply haven't played a knockout match yet --
    callers should fall back to the group-stage status from fetch_standings()
    for those teams."""
    headers = {"x-apisports-key": api_key}
    params = {"league": WORLD_CUP_LEAGUE_ID, "season": WORLD_CUP_SEASON}

    response = requests.get(f"{API_FOOTBALL_BASE}/fixtures", headers=headers, params=params)
    if response.status_code != 200:
        log(f"Knockout fixtures fetch error: {response.status_code}")
        return {}

    data = response.json()
    fixtures = data.get("response", [])

    # Sort by kickoff time before processing. The API does not guarantee
    # fixtures are returned in chronological order, and this function relies
    # on later results overriding earlier ones (e.g. a team's 3rd-place-match
    # result must override their earlier "playing for 3rd place" status from
    # the semifinal they lost) -- so processing out of order could silently
    # leave a team's status stuck on a stale intermediate state.
    fixtures.sort(key=lambda fx: fx["fixture"].get("timestamp") or 0)

    knockout_status = {}
    for fx in fixtures:
        status_short = fx["fixture"]["status"]["short"]
        if status_short not in ("FT", "AET", "PEN"):
            continue  # only finished matches carry a real result

        round_name = (fx.get("league", {}).get("round") or "").strip().lower()
        is_group_stage = "group" in round_name
        if is_group_stage:
            continue  # fetch_standings() already handles the group stage

        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        home_winner = fx["teams"]["home"].get("winner")
        away_winner = fx["teams"]["away"].get("winner")

        is_bronze_match = "3rd" in round_name or "third" in round_name
        is_final = bool(re.search(r"\bfinal\b", round_name)) and not is_bronze_match
        is_semifinal = "semi" in round_name
        is_straight_elimination = any(r in round_name for r in STRAIGHT_ELIMINATION_ROUNDS)

        if home_winner is None and away_winner is None:
            continue  # draw recorded without a shootout winner yet (shouldn't
                       # happen in knockouts, but don't guess if it does)

        winner = home if home_winner else away
        loser = away if home_winner else home

        if is_final:
            knockout_status[winner] = "champion"
            knockout_status[loser] = "runner-up"
        elif is_bronze_match:
            knockout_status[winner] = "third place"
            knockout_status[loser] = "fourth place"
        elif is_semifinal:
            # The exception to single elimination: semifinal losers are NOT
            # eliminated yet -- they play each other for 3rd place first.
            knockout_status[winner] = "advancing"
            knockout_status[loser] = "playing for 3rd place"
        elif is_straight_elimination:
            knockout_status[winner] = "advancing"
            knockout_status[loser] = "eliminated"

    return knockout_status


def fetch_goal_scorers(fixture_id, api_key):
    """Fetch the events for one fixture and return a short list of goal
    scorers (and own-goal info), used to add a couple of player names into
    the script — not a full play-by-play."""
    headers = {"x-apisports-key": api_key}
    params = {"fixture": fixture_id}

    response = requests.get(f"{API_FOOTBALL_BASE}/fixtures/events", headers=headers, params=params)
    if response.status_code != 200:
        log(f"Could not fetch events for fixture {fixture_id}: {response.status_code}")
        return []

    data = response.json()
    events = data.get("response", [])

    scorers = []
    for ev in events:
        if ev.get("type") == "Goal":
            player = ev.get("player", {}).get("name")
            team = ev.get("team", {}).get("name")
            minute = ev.get("time", {}).get("elapsed")
            detail = ev.get("detail", "")  # e.g. "Normal Goal", "Own Goal", "Penalty"
            if player:
                scorers.append({
                    "player": player,
                    "team": team,
                    "minute": minute,
                    "detail": detail,
                })
    return scorers


RECAP_LOOKBACK_DAYS = 4  # how many days back to search for the most recent
                         # date with any match, so knockout-stage rest days
                         # (no game yesterday, or even for several days) don't
                         # leave the recap with nothing to talk about


def _fetch_fixtures_for_date(date_str, api_key):
    """Fetch raw fixtures for a single date. Returns the list of fixture
    dicts from API-Football (empty list on a clean no-fixtures response)."""
    headers = {"x-apisports-key": api_key}
    params = {
        "league": WORLD_CUP_LEAGUE_ID,
        "season": WORLD_CUP_SEASON,
        "date": date_str,
        "timezone": "America/New_York",
    }

    response = requests.get(f"{API_FOOTBALL_BASE}/fixtures", headers=headers, params=params)
    log(f"Request URL: {response.url}")
    log(f"Response status: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(f"API-Football error {response.status_code}: {response.text}")

    data = response.json()

    if data.get("errors"):
        log(f"API-Football reported errors: {data['errors']}")
    log(f"API-Football 'results' count: {data.get('results')}")
    log(f"Raw response (first 1500 chars): {str(data)[:1500]}")

    return data.get("response", [])


def _parse_fixtures(fixtures, api_key):
    """Turn raw API-Football fixture dicts into our simplified match dicts,
    attaching goal scorers for any finished match."""
    matches = []
    for fx in fixtures:
        status_short = fx["fixture"]["status"]["short"]
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        home_score = fx["goals"]["home"]
        away_score = fx["goals"]["away"]
        venue = fx["fixture"]["venue"].get("name") or "the stadium"
        fixture_id = fx["fixture"]["id"]
        round_name = fx.get("league", {}).get("round", "")

        is_final = status_short in ("FT", "AET", "PEN")

        scorers = []
        if is_final:
            scorers = fetch_goal_scorers(fixture_id, api_key)
            log(f"  Fixture {fixture_id} ({home} v {away}): {len(scorers)} goal event(s) found.")

        matches.append({
            "home": home,
            "away": away,
            "home_score": home_score if home_score is not None else 0,
            "away_score": away_score if away_score is not None else 0,
            "venue": venue,
            "status": "final" if is_final else "in_progress",
            "scorers": scorers,
            "round": round_name,
        })

    return matches


def fetch_recent_matches(api_key, lookback_days=RECAP_LOOKBACK_DAYS):
    """Find the most recent date (starting from yesterday and scanning
    backward up to `lookback_days`) that had any matches, and return that
    date's matches along with how many days ago it was.

    During the group stage this almost always resolves on the very first
    check (yesterday). During knockout rounds, with single-game days and
    multi-day gaps between rounds, this walks back far enough to find the
    last real result so the recap always has something true to say —
    instead of going silent the moment "yesterday" happens to be empty.

    Returns (matches, days_ago). If nothing is found in the whole window,
    returns ([], None).
    """
    now_et = datetime.now(EASTERN)

    for days_ago in range(1, lookback_days + 1):
        check_date = (now_et - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        log(f"Checking for matches on {check_date} (days_ago={days_ago}, America/New_York)...")

        fixtures = _fetch_fixtures_for_date(check_date, api_key)
        log(f"Found {len(fixtures)} fixture(s) on {check_date}.")

        if fixtures:
            matches = _parse_fixtures(fixtures, api_key)
            return matches, days_ago

    log(f"No matches found in the last {lookback_days} day(s).")
    return [], None


# ---------------------------------------------------------------------------
# STEP 2 — Write the script via Claude
# ---------------------------------------------------------------------------

def describe_recency(days_ago):
    """Turn a days_ago count into a natural phrase for the script prompt,
    so Claude's house style can phrase timing honestly instead of always
    saying 'yesterday' even when results are a few days old."""
    if days_ago is None:
        return "in the near future"  # not used in practice; matches is empty
    if days_ago == 1:
        return "yesterday"
    if days_ago == 2:
        return "two days ago"
    if days_ago == 3:
        return "a few days ago"
    return f"{days_ago} days ago"


def build_match_summary(matches, days_ago=1):
    if not matches:
        return "No World Cup matches were played recently."
    recency = describe_recency(days_ago)
    lines = [f"(These matches were played {recency}.)"]
    for m in matches:
        if m["status"] == "final":
            line = f"- {m['home']} {m['home_score']} - {m['away_score']} {m['away']} (venue: {m['venue']})"
            scorers = m.get("scorers", [])
            if scorers:
                scorer_strs = []
                for s in scorers:
                    detail_note = f", {s['detail']}" if s.get("detail") and s["detail"] != "Normal Goal" else ""
                    scorer_strs.append(f"{s['player']} ({s['team']}, {s['minute']}'{detail_note})")
                line += f"\n  Goals: {'; '.join(scorer_strs)}"
            lines.append(line)
        else:
            lines.append(f"- IN PROGRESS: {m['home']} {m['home_score']} - {m['away_score']} {m['away']} (venue: {m['venue']})")
    return "\n".join(lines)


def build_standings_context(matches, standings, knockout_status=None):
    """Build a short qualification-status note for just the teams in the
    matches being recapped, so Claude can mention it naturally without
    dumping all 48 teams' statuses into the script.

    knockout_status (from fetch_knockout_eliminations) takes priority over
    the group-stage `standings` classification whenever a team appears in
    both -- group-stage status is a snapshot frozen at the end of the group
    stage, so once a team has actually played (and possibly lost) a
    knockout match, that real result is what's true, not the old group
    placement."""
    if not standings and not knockout_status:
        return ""

    teams_in_play = set()
    for m in matches:
        teams_in_play.add(m["home"])
        teams_in_play.add(m["away"])

    status_by_team = {row["team"]: row["status"] for row in (standings or [])}
    if knockout_status:
        status_by_team.update(knockout_status)  # knockout results win on overlap

    lines = []
    for team in teams_in_play:
        status = status_by_team.get(team)
        if status:
            lines.append(f"- {team}: {status}")

    if not lines:
        return ""

    return (
        "\n\nQualification status for teams in these matches "
        "(advancing = through to the next round; eliminated = out of the "
        "tournament; playing for 3rd place = lost their semifinal but plays "
        "one more match for third, NOT eliminated yet; champion/runner-up/"
        "third place/fourth place = tournament has concluded for that team; "
        "waiting = group stage not finished yet, status not yet "
        "determined):\n" + "\n".join(lines)
    )


def find_final_match(matches):
    """If the World Cup Final is among these (finished) matches, return it.
    Otherwise return None. This is the single trigger for the tournament's
    one-time closing episode and the permanent stop of daily updates after.

    Matched case-insensitively against variations like "Final" or "Final - 1"
    (API-Football's group-stage rounds are phrased "Group Stage - 3", so the
    Final may similarly carry a suffix). Uses a word-boundary match on
    "final" rather than a plain substring check — "Quarter-finals" and
    "Semi-finals" both contain the substring "final" too, and a naive check
    would wrongly trigger the tournament-ending logic weeks early. Also
    explicitly excludes the 3rd-place playoff ("3rd Place Final"), which is
    played a day or two BEFORE the actual Final."""
    for m in matches:
        round_name = (m.get("round") or "").strip().lower()
        is_bronze_match = "3rd" in round_name or "third" in round_name
        is_final = re.search(r"\bfinal\b", round_name) and not is_bronze_match
        if is_final and m.get("status") == "final":
            return m
    return None


FINALE_STYLE_PROMPT = """You are writing the final-ever script for "Mindful Joga" —
a mindful, ASMR-toned daily audio recap of World Cup football. This is a
one-time closing episode, published the morning after the World Cup Final.
There will be no more daily episodes after this one until the next World Cup.

Follow the same calm, ASMR house style as the daily recaps — soft pacing,
gentle pauses, [bracketed] emotional/delivery tags supported by the eleven_v3
voice model (e.g. [warmly], [softly], a literal "..." for a pause) — but this
episode should feel like a genuine send-off, not just another day's recap.

CONTENT, in this order:
1. Open warmly, acknowledging this is the last episode of the tournament.
2. Recap the Final itself: the score, the champion, a couple of standout
   moments or goal-scorers if provided. Let this breathe — it's the biggest
   match of the tournament, give it a touch more weight than an ordinary recap.
3. A short, genuine reflection on the tournament as a whole — its spirit, the
   shared ritual of these morning check-ins, gratitude to the listener for
   spending these quiet mornings together.
4. Close with a warm, specific send-off: thank the listener, and tell them
   you'll see them in Uruguay in 2030, when the next World Cup kicks off.
   Keep this line sincere, not corny — it should feel like saying goodbye to
   a friend after a shared season, not a marketing tagline.

LENGTH: Aim for roughly 450-650 words — a little more spacious than a typical
recap, since this is a one-time send-off, not a routine update.

Output ONLY the finished script. No headers, no notes — just the words to be
spoken, with [tags] and ... inline as described above."""


def generate_finale_script(final_match, api_key, days_ago=1):
    """Generate the one-time closing episode for the morning after the
    World Cup Final."""
    recency = describe_recency(days_ago)
    home, away = final_match["home"], final_match["away"]
    home_score, away_score = final_match["home_score"], final_match["away_score"]
    champion = home if home_score > away_score else away
    runner_up = away if home_score > away_score else home

    scorer_lines = ""
    scorers = final_match.get("scorers", [])
    if scorers:
        scorer_strs = []
        for s in scorers:
            detail_note = f", {s['detail']}" if s.get("detail") and s["detail"] != "Normal Goal" else ""
            scorer_strs.append(f"{s['player']} ({s['team']}, {s['minute']}'{detail_note})")
        scorer_lines = f"\nGoals: {'; '.join(scorer_strs)}"

    today_str = datetime.now(EASTERN).strftime("%B %-d, %Y")
    user_prompt = (
        f"Today's date is {today_str}.\n\n"
        f"The World Cup Final was played {recency}, at {final_match['venue']}:\n"
        f"{home} {home_score} - {away_score} {away}{scorer_lines}\n\n"
        f"{champion} are the 2026 World Cup champions. {runner_up} were the runners-up.\n\n"
        f"Write the final, closing Mindful Joga episode."
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "system": FINALE_STYLE_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    log("Generating FINALE script via Claude...")
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text}")

    data = response.json()
    script_text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    log(f"Finale script generated ({len(script_text)} characters).")
    return script_text.strip()




def generate_script(matches, api_key, api_football_key, days_ago=1):
    match_summary = build_match_summary(matches, days_ago)
    standings = fetch_standings(api_football_key)
    knockout_status = fetch_knockout_eliminations(api_football_key)
    standings_context = build_standings_context(matches, standings, knockout_status)
    today_str = datetime.now(EASTERN).strftime("%B %-d, %Y")  # e.g. "June 22, 2026"
    recency = describe_recency(days_ago)
    user_prompt = (
        f"Today's date is {today_str}.\n\n"
        f"Here are the most recent World Cup matches, played {recency} "
        f"(NOT necessarily yesterday — check the recency note below and "
        f"phrase the script's timing language honestly, e.g. if it's been "
        f"a few days, don't say 'yesterday'):\n\n{match_summary}"
        f"{standings_context}\n\n"
        f"Write today's Mindful Joga script."
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "system": HOUSE_STYLE_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    log("Generating script via Claude...")
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text}")

    data = response.json()
    script_text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    log(f"Script generated ({len(script_text)} characters).")
    return script_text.strip()


# ---------------------------------------------------------------------------
# STEP 3 — Turn the script into audio via ElevenLabs
# ---------------------------------------------------------------------------

def generate_audio(script_text, api_key, output_path, model_id=TTS_MODEL_ID):
    url = f"{ELEVENLABS_API_URL}/{VOICE_ID}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": script_text,
        "model_id": model_id,
        "voice_settings": VOICE_SETTINGS,
    }

    log(f"Generating audio via ElevenLabs (model: {model_id})...")
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"ElevenLabs API error {response.status_code}: {response.text}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Save the raw ElevenLabs output to a temp file first, then slow it down
    # by exactly SLOWDOWN_FACTOR using ffmpeg's pitch-preserving atempo filter.
    # This gives a precise, guaranteed speed change — voice_settings like
    # 'stability' nudge pacing unpredictably but don't give an exact percentage.
    raw_path = output_path + ".raw.mp3"
    with open(raw_path, "wb") as f:
        f.write(response.content)

    log(f"Slowing audio by {(1 - SLOWDOWN_FACTOR) * 100:.0f}% (atempo={SLOWDOWN_FACTOR})...")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", raw_path,
            "-filter:a", f"atempo={SLOWDOWN_FACTOR}",
            "-vn", output_path,
        ],
        capture_output=True, text=True,
    )
    os.remove(raw_path)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg slowdown failed: {result.stderr}")

    log(f"Audio saved to {output_path}.")


def archive_audio_copy(source_path, archive_subdir, date_str, label):
    """Copy a freshly-generated audio file into a dated archive path,
    e.g. audio/archive/2026-06-24-recap.mp3. This is purely additive — the
    live site's player keeps reading today-male.mp3 / today-upcoming.mp3
    exactly as before; this just preserves a permanent, browsable copy of
    every day's episode alongside it, named so files sort naturally by date
    in a GitHub folder listing.

    `date_str` should be the date the episode was actually PUBLISHED (today),
    not necessarily the date of the match being discussed -- this guarantees
    a unique filename every single day even on a knockout rest day that
    re-narrates an older result, since the archive's job is "what did the
    site publish on this date," not "what match is this about."""
    try:
        archive_dir = os.path.join("audio", archive_subdir)
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, f"{date_str}-{label}.mp3")
        with open(source_path, "rb") as src, open(archive_path, "wb") as dst:
            dst.write(src.read())
        log(f"Archived a copy to {archive_path}.")
    except Exception as e:
        # Archiving should never take down the actual daily publish — if
        # this fails for any reason, log it and move on; today-male.mp3 /
        # today-upcoming.mp3 (the only files the live site reads) are
        # already written successfully by this point regardless.
        log(f"⚠️ Archiving failed (non-fatal, live site is unaffected): {e}")


def save_scores_json(matches, output_path="scores.json"):
    """Write a simple JSON file the live site can fetch to show real scores
    in the cycling date/score pill, replacing the old hardcoded placeholder."""
    simplified = []
    for m in matches:
        simplified.append({
            "home": m["home"],
            "away": m["away"],
            "home_score": m["home_score"],
            "away_score": m["away_score"],
            "status": m["status"],  # "final" or "in_progress"
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(simplified, f, indent=2)
    log(f"Scores saved to {output_path} ({len(simplified)} match(es)).")


# ---------------------------------------------------------------------------
# UPCOMING MATCHES (forward-looking companion to the recap)
# ---------------------------------------------------------------------------

def fetch_upcoming_matches(api_key, days_ahead=5):
    """Find the nearest upcoming matchday — today if matches are scheduled,
    otherwise the closest future date with scheduled fixtures within
    `days_ahead` days. Returns (matches, matchday_date) or ([], None)."""
    headers = {"x-apisports-key": api_key}
    now_et = datetime.now(EASTERN)

    for offset in range(0, days_ahead + 1):
        check_date = (now_et + timedelta(days=offset)).strftime("%Y-%m-%d")
        params = {
            "league": WORLD_CUP_LEAGUE_ID,
            "season": WORLD_CUP_SEASON,
            "date": check_date,
            "timezone": "America/New_York",
        }
        response = requests.get(f"{API_FOOTBALL_BASE}/fixtures", headers=headers, params=params)
        if response.status_code != 200:
            log(f"Upcoming-fetch error on {check_date}: {response.status_code}")
            continue

        data = response.json()
        fixtures = data.get("response", [])
        # Only count fixtures that haven't started yet
        scheduled = [fx for fx in fixtures if fx["fixture"]["status"]["short"] == "NS"]

        if scheduled:
            log(f"Found {len(scheduled)} upcoming fixture(s) on {check_date} (offset {offset}).")
            matches = []
            for fx in scheduled:
                matches.append({
                    "home": fx["teams"]["home"]["name"],
                    "away": fx["teams"]["away"]["name"],
                    "venue": fx["fixture"]["venue"].get("name") or "the stadium",
                    "kickoff_utc": fx["fixture"]["date"],
                })
            return matches, check_date

    log("No upcoming fixtures found in the lookahead window.")
    return [], None


def build_upcoming_summary(matches, matchday_date):
    if not matches:
        return "No upcoming World Cup matches were found in the near future."
    lines = [f"Matchday: {matchday_date}"]
    for m in matches:
        lines.append(f"- {m['home']} vs {m['away']} (venue: {m['venue']}, kickoff: {m['kickoff_utc']})")
    return "\n".join(lines)


UPCOMING_STYLE_PROMPT = """You are writing the "Upcoming Matches" script for \
"Mindful Joga" — the forward-looking companion to the daily recap. Where the \
recap looks back at yesterday's results, this one looks ahead to today's (or \
the nearest) matchday. Same calm, breathy, ASMR-toned house voice — same \
[tags], same slow pacing — but a DIFFERENT emotional register: anticipation \
and calm readiness, not resolution. Follow these rules:

VOICE & TONE:
- Open with "Good morning. Welcome to [today's date], Mindful Joga's
  Upcoming Matches." Speak the date naturally, the way a person would say it.
- Same slow, breathy delivery as the recap: [softly], [gently], [slowly],
  ellipses, [inhales]/[exhales] tags placed at the actual breath moment.
- This is NOT a hype/preview show. No "get ready for the action," no
  countdown-clock energy, no score predictions. The tone is closer to
  reading someone their schedule for the day in a way that feels like
  permission to look forward to something, not pressure to be excited.
- There are no scores yet, so there is nothing to be anxious about — lean
  into that. A line like "nothing has happened yet, and that's the nicest
  part of today" is the kind of thing this show is for.

STRUCTURE:
- Brief settle-in moment + one breath cue (same style as the recap, varied
  phrasing, real [inhales]/[exhales] tags)
- Name each match today/this matchday: who's playing, where, and roughly when
  (translate the kickoff time into a simple, calm phrase like "this afternoon"
  or "tonight" rather than reading a precise UTC timestamp aloud)
- No predictions, no odds, no "X is the favorite" — just the facts of who and
  where, delivered gently
- Close with a grounding line that hands the day back to the listener, e.g.
  "Whatever happens today, you'll hear about it gently, tomorrow morning."

LENGTH: Aim for roughly 300-450 words (shorter than the recap — this is a
simple heads-up, not a story with an arc).

Output ONLY the finished script. No headers, no notes — just the words to be
spoken, with [tags] and ... inline as described above."""


def generate_upcoming_script(matches, matchday_date, api_key):
    summary = build_upcoming_summary(matches, matchday_date)
    today_str = datetime.now(EASTERN).strftime("%B %-d, %Y")
    user_prompt = (
        f"Today's date is {today_str}.\n\n"
        f"Here is the nearest upcoming World Cup matchday:\n\n{summary}\n\n"
        f"Write today's Mindful Joga Upcoming Matches script."
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "system": UPCOMING_STYLE_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    log("Generating Upcoming Matches script via Claude...")
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text}")

    data = response.json()
    script_text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    log(f"Upcoming script generated ({len(script_text)} characters).")
    return script_text.strip()


def save_upcoming_json(matches, matchday_date, output_path="upcoming.json"):
    simplified = {
        "matchday": matchday_date,
        "matches": [
            {"home": m["home"], "away": m["away"], "kickoff_utc": m["kickoff_utc"]}
            for m in matches
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(simplified, f, indent=2)
    log(f"Upcoming matches saved to {output_path} ({len(matches)} match(es)).")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    api_football_key = os.environ.get("API_FOOTBALL_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")

    missing = [name for name, val in [
        ("API_FOOTBALL_KEY", api_football_key),
        ("ANTHROPIC_API_KEY", anthropic_key),
        ("ELEVENLABS_API_KEY", elevenlabs_key),
    ] if not val]
    if missing:
        sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")

    # Sortable date string for this run, used to name archived audio copies
    # (e.g. "2026-06-24") -- distinct from the human-readable date strings
    # used elsewhere in scripts, since filenames need to sort naturally.
    publish_date_str = datetime.now(EASTERN).strftime("%Y-%m-%d")

    # ---------------------- TOURNAMENT-OVER CHECK ----------------------
    # FINALE_MARKER_PATH is committed to the repo the day the finale episode
    # is published. Every run after that checks for it first and does
    # nothing — no API calls, no audio, no cost — until the scheduler
    # (cron-job.org) is turned off by hand. The very first run after the
    # marker exists fails once (visible via the same GitHub email used for
    # low credits) as a one-time nudge to go disable the cron job; every run
    # after that is a fully silent no-op.
    FINALE_MARKER_PATH = "scripts/.finale-published"
    FINALE_REMINDER_SENT_PATH = "scripts/.finale-reminder-sent"

    if os.path.exists(FINALE_MARKER_PATH):
        if not os.path.exists(FINALE_REMINDER_SENT_PATH):
            os.makedirs("scripts", exist_ok=True)
            with open(FINALE_REMINDER_SENT_PATH, "w") as f:
                f.write("Reminder sent.\n")
            log("The World Cup finale episode was already published. "
                "Mindful Joga's daily run is now permanently retired until "
                "the 2030 World Cup. This is a one-time reminder to disable "
                "the cron-job.org scheduler — after this, every future run "
                "will be a silent no-op.")
            sys.exit(
                "Mindful Joga has already published its tournament finale "
                "episode. No further daily episodes will be generated. "
                "Please disable the cron-job.org trigger — this is the only "
                "reminder you'll get; future runs will exit silently."
            )
        else:
            # Reminder already sent on a previous run — true silent no-op.
            return

    # ---------------------- CREDIT CHECK ----------------------
    # Checked once up front so both the recap and upcoming sections below can
    # make an informed decision, rather than discovering the problem mid-run
    # (as happened the first time, with an uncaught quota_exceeded crash).
    credit_status = fetch_elevenlabs_credit_status(elevenlabs_key)

    # Rough per-script cost estimate, used only for the runway projection —
    # actual cost depends on the day's script length, this is a planning
    # estimate, not an exact figure.
    ESTIMATED_RECAP_COST = 3800       # eleven_v3, ~500-700 word script
    ESTIMATED_UPCOMING_COST = 2200    # eleven_v3, ~300-450 word script (shorter
                                       # than recap, same per-character model cost)
    estimated_daily_cost = ESTIMATED_RECAP_COST + ESTIMATED_UPCOMING_COST

    credit_tier = "healthy"  # healthy | low | critical
    if credit_status:
        remaining = credit_status["remaining"]
        projected_days = remaining / estimated_daily_cost if estimated_daily_cost else float("inf")

        if remaining < ESTIMATED_RECAP_COST + CRITICAL_CREDIT_BUFFER:
            credit_tier = "critical"
            log(f"⚠️ CRITICAL: only {remaining:,} ElevenLabs credits left — "
                f"not enough headroom for today's recap. Will attempt recap "
                f"only and skip the Upcoming script.")
        elif projected_days < LOW_CREDIT_DAYS_THRESHOLD:
            credit_tier = "low"
            log(f"⚠️ LOW CREDITS: {remaining:,} remaining, projected to last "
                f"~{projected_days:.1f} more day(s) at current usage. "
                f"Running normally today, but consider upgrading soon — "
                f"this is a heads-up, not a failure.")
        else:
            log(f"Credits healthy: {remaining:,} remaining, "
                f"~{projected_days:.1f} projected day(s) of runway.")
    else:
        log("Proceeding without a credit-status check (monitoring call failed).")

    # ---------------------- RECAP ----------------------
    # Looks back up to RECAP_LOOKBACK_DAYS to find the most recent date with
    # any matches. This keeps daily audio flowing through knockout-stage rest
    # days (single-game days, multi-day gaps between rounds) by re-narrating
    # the last real result with honest "how long ago" phrasing, rather than
    # going silent the moment "yesterday" happens to be empty.
    matches, days_ago = fetch_recent_matches(api_football_key)

    # Always write the scores file, even if empty, so the live site never
    # shows stale placeholder data.
    save_scores_json(matches)

    recap_audio_failed = False
    is_tournament_finale = False
    if not matches:
        # Only true if there were truly zero matches across the entire
        # lookback window — should be rare even in knockouts, since the
        # window is generous. Genuinely nothing honest to recap yet.
        log(f"No matches found in the last {RECAP_LOOKBACK_DAYS} day(s) — skipping recap script/audio generation.")
    else:
        final_match = find_final_match(matches)

        if final_match:
            is_tournament_finale = True
            log(f"🏆 World Cup Final detected: {final_match['home']} "
                f"{final_match['home_score']}-{final_match['away_score']} "
                f"{final_match['away']}. Generating the one-time closing episode.")
            script_text = generate_finale_script(final_match, anthropic_key, days_ago)

            script_path = "scripts/latest-script.txt"
            os.makedirs("scripts", exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_text)
            log(f"Finale script also saved to {script_path} for reference.")

            try:
                generate_audio(script_text, elevenlabs_key, "audio/today-male.mp3", TTS_MODEL_ID)
                archive_audio_copy("audio/today-male.mp3", "archive", publish_date_str, "finale")
            except RuntimeError as e:
                recap_audio_failed = True
                log(f"⚠️ Finale audio generation failed: {e}")

            # Write the marker now, regardless of audio success/failure above —
            # the Final has been played and recapped (text script exists either
            # way); we don't want to re-trigger this branch tomorrow and
            # generate a second, possibly conflicting "finale" episode.
            with open(FINALE_MARKER_PATH, "w") as f:
                f.write(f"Finale published. Champion match: {final_match['home']} "
                        f"{final_match['home_score']}-{final_match['away_score']} "
                        f"{final_match['away']}.\n")
            log(f"Wrote {FINALE_MARKER_PATH} — daily updates will stop after today.")
        else:
            log(f"Recapping matches from {days_ago} day(s) ago.")
            script_text = generate_script(matches, anthropic_key, api_football_key, days_ago)

            # Save the script alongside the audio for reference/debugging in the repo
            script_path = "scripts/latest-script.txt"
            os.makedirs("scripts", exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_text)
            log(f"Recap script also saved to {script_path} for reference.")

            try:
                generate_audio(script_text, elevenlabs_key, "audio/today-male.mp3", TTS_MODEL_ID)
                archive_audio_copy("audio/today-male.mp3", "archive", publish_date_str, "recap")
            except RuntimeError as e:
                # Even in the critical-credit case, attempt this — if it still
                # fails (e.g. credits ran out between the check and now), don't
                # let it take down the upcoming section or the JSON files we've
                # already written below. Surface it clearly and keep going.
                recap_audio_failed = True
                log(f"⚠️ Recap audio generation failed: {e}")

    # ---------------------- UPCOMING ----------------------
    upcoming_audio_skipped_for_credits = False
    if is_tournament_finale:
        # Nothing left to preview — the tournament just ended. Write an
        # empty upcoming.json so the site doesn't show stale fixtures, and
        # skip script/audio generation entirely.
        save_upcoming_json([], None)
        log("Tournament finale published — skipping Upcoming section entirely (nothing left to preview until 2030).")
    else:
        upcoming_matches, matchday_date = fetch_upcoming_matches(api_football_key)

        # Always write upcoming.json, even if empty, so the site never shows stale data
        save_upcoming_json(upcoming_matches, matchday_date)

        if not upcoming_matches:
            log("No upcoming matches found — skipping upcoming script/audio generation.")
        elif credit_tier == "critical":
            # Prioritize the recap (the part people actually wait for) over the
            # forward-looking preview when credits are this tight. The countdown
            # angle picks back up automatically once credits reset or the plan
            # is upgraded — nothing is lost, just deferred a day.
            upcoming_audio_skipped_for_credits = True
            log("Skipping Upcoming script/audio today — reserving remaining "
                "credits for the recap due to critically low balance.")
        else:
            upcoming_script_text = generate_upcoming_script(upcoming_matches, matchday_date, anthropic_key)

            upcoming_script_path = "scripts/latest-upcoming-script.txt"
            with open(upcoming_script_path, "w", encoding="utf-8") as f:
                f.write(upcoming_script_text)
            log(f"Upcoming script also saved to {upcoming_script_path} for reference.")

            try:
                generate_audio(upcoming_script_text, elevenlabs_key, "audio/today-upcoming.mp3", UPCOMING_TTS_MODEL_ID)
                archive_audio_copy("audio/today-upcoming.mp3", "archive", publish_date_str, "upcoming")
            except RuntimeError as e:
                log(f"⚠️ Upcoming audio generation failed: {e}")

    log("Done.")

    if is_tournament_finale and not recap_audio_failed:
        # A clean, expected stop — not an error. Still exits non-zero so the
        # existing GitHub failure-email fires once, letting you know it's
        # time to retire the cron-job.org schedule. No further action needed
        # from the script itself after this.
        sys.exit(
            "🏆 The World Cup Final has been recapped — this was the last "
            "scheduled Mindful Joga episode of the tournament. A marker file "
            "has been committed so future runs will no-op automatically. "
            "See you in Uruguay in 2030!"
        )

    # Deliberately fail the Action when credits are critical, so the existing
    # GitHub failure-email notification fires — even though the recap itself
    # may have succeeded. This is the "warning" surfaced to a human, distinct
    # from a genuine crash: the commit step below still runs either way.
    if credit_tier == "critical" or recap_audio_failed:
        sys.exit(
            "ElevenLabs credits are critically low (or recap/finale audio "
            "failed) — this run is flagged so it reaches you by email, even "
            "though the script/scores were generated normally. Check your "
            "ElevenLabs balance."
        )


if __name__ == "__main__":
    main()
