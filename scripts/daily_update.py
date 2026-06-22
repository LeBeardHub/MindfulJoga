"""
Mindful Joga — Daily Automation
---------------------------------
Runs once a day (via GitHub Actions). Does four things in order:

  1. Fetch yesterday's World Cup match results from API-Football
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
import sys
import json
import subprocess
import requests
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
WORLD_CUP_LEAGUE_ID = 1
WORLD_CUP_SEASON = 2026

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"

VOICE_ID = "jbEI5QkrMSKWeDlP27MV"  # Ryan — Deep and Meditative
TTS_MODEL_ID = "eleven_v3"

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
each morning and recaps the matches that finished the prior day, so the listener can \
start their day caught up, calmly. Follow these rules exactly:

VOICE & TONE:
- Open with "Good morning. Welcome to [today's date], Mindful Joga's World Cup
  recap." Use the date provided to you in the user message, spoken naturally
  (e.g. "Welcome to June twenty-second, Mindful Joga's World Cup recap" — not
  the numeric format, say it the way a person would say it out loud). This is
  specifically a morning ritual, and the listener should know exactly where
  they are within the first few seconds.
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

LENGTH: Aim for roughly 500-700 words total (about 3-4 minutes spoken slowly).

Output ONLY the finished script. No headers, no notes, no explanation — just the
words to be spoken, with [tags] and ... inline as described above."""


def log(message):
    print(f"[mindful-joga] {message}", flush=True)


# ---------------------------------------------------------------------------
# STEP 1 — Fetch yesterday's match results
# ---------------------------------------------------------------------------

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


def fetch_yesterdays_matches(api_key):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"Fetching matches for {yesterday}...")

    headers = {"x-apisports-key": api_key}
    params = {
        "league": WORLD_CUP_LEAGUE_ID,
        "season": WORLD_CUP_SEASON,
        "date": yesterday,
    }

    response = requests.get(f"{API_FOOTBALL_BASE}/fixtures", headers=headers, params=params)
    log(f"Request URL: {response.url}")
    log(f"Response status: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(f"API-Football error {response.status_code}: {response.text}")

    data = response.json()

    # Debug: show any API-level errors/warnings and the raw result count
    if data.get("errors"):
        log(f"API-Football reported errors: {data['errors']}")
    log(f"API-Football 'results' count: {data.get('results')}")
    log(f"Raw response (first 1500 chars): {str(data)[:1500]}")

    fixtures = data.get("response", [])
    log(f"Found {len(fixtures)} fixture(s).")

    matches = []
    for fx in fixtures:
        status_short = fx["fixture"]["status"]["short"]
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        home_score = fx["goals"]["home"]
        away_score = fx["goals"]["away"]
        venue = fx["fixture"]["venue"].get("name") or "the stadium"
        fixture_id = fx["fixture"]["id"]

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
        })

    return matches


# ---------------------------------------------------------------------------
# STEP 2 — Write the script via Claude
# ---------------------------------------------------------------------------

def build_match_summary(matches):
    if not matches:
        return "No World Cup matches were played yesterday."
    lines = []
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


def generate_script(matches, api_key):
    match_summary = build_match_summary(matches)
    today_str = datetime.now(timezone.utc).strftime("%B %-d, %Y")  # e.g. "June 22, 2026"
    user_prompt = (
        f"Today's date is {today_str}.\n\n"
        f"Here are yesterday's World Cup matches:\n\n{match_summary}\n\n"
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

def generate_audio(script_text, api_key, output_path):
    url = f"{ELEVENLABS_API_URL}/{VOICE_ID}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": script_text,
        "model_id": TTS_MODEL_ID,
        "voice_settings": VOICE_SETTINGS,
    }

    log("Generating audio via ElevenLabs...")
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

    for offset in range(0, days_ahead + 1):
        check_date = (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")
        params = {
            "league": WORLD_CUP_LEAGUE_ID,
            "season": WORLD_CUP_SEASON,
            "date": check_date,
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
    today_str = datetime.now(timezone.utc).strftime("%B %-d, %Y")
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

    # ---------------------- RECAP (existing, unchanged) ----------------------
    matches = fetch_yesterdays_matches(api_football_key)

    # Always write the scores file, even if empty, so the live site never
    # shows yesterday's (or older) stale placeholder data.
    save_scores_json(matches)

    if not matches:
        log("No matches found for yesterday — skipping recap script/audio generation.")
    else:
        script_text = generate_script(matches, anthropic_key)

        # Save the script alongside the audio for reference/debugging in the repo
        script_path = "scripts/latest-script.txt"
        os.makedirs("scripts", exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_text)
        log(f"Recap script also saved to {script_path} for reference.")

        generate_audio(script_text, elevenlabs_key, "audio/today-male.mp3")

    # ---------------------- UPCOMING (new) ----------------------
    upcoming_matches, matchday_date = fetch_upcoming_matches(api_football_key)

    # Always write upcoming.json, even if empty, so the site never shows stale data
    save_upcoming_json(upcoming_matches, matchday_date)

    if not upcoming_matches:
        log("No upcoming matches found — skipping upcoming script/audio generation.")
    else:
        upcoming_script_text = generate_upcoming_script(upcoming_matches, matchday_date, anthropic_key)

        upcoming_script_path = "scripts/latest-upcoming-script.txt"
        with open(upcoming_script_path, "w", encoding="utf-8") as f:
            f.write(upcoming_script_text)
        log(f"Upcoming script also saved to {upcoming_script_path} for reference.")

        generate_audio(upcoming_script_text, elevenlabs_key, "audio/today-upcoming.mp3")

    log("Done.")


if __name__ == "__main__":
    main()
