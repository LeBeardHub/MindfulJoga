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
- Open with "Good morning" — this is specifically a morning ritual.
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
    (a) a literal, instructional cue ("Breathe in... and let it go.")
    (b) an immersive, woven-in cue that doesn't sound like an instruction
        (e.g. "Notice the air in the room before the next line. That's yours
        to keep, however the match went.")
    (c) a cue tied to the football itself (e.g. "Even the players take a
        breath before a free kick. Take yours now.")
  At least one breathing moment per script should NOT use the literal words
  "breathe in" / "breathe out" — find a more textured, sensory way in.
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

LENGTH: Aim for roughly 500-700 words total (about 3-4 minutes spoken slowly).

Output ONLY the finished script. No headers, no notes, no explanation — just the
words to be spoken, with [tags] and ... inline as described above."""


def log(message):
    print(f"[mindful-joga] {message}", flush=True)


# ---------------------------------------------------------------------------
# STEP 1 — Fetch yesterday's match results
# ---------------------------------------------------------------------------

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

        is_final = status_short in ("FT", "AET", "PEN")

        matches.append({
            "home": home,
            "away": away,
            "home_score": home_score if home_score is not None else 0,
            "away_score": away_score if away_score is not None else 0,
            "venue": venue,
            "status": "final" if is_final else "in_progress",
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
            lines.append(f"- {m['home']} {m['home_score']} - {m['away_score']} {m['away']} (venue: {m['venue']})")
        else:
            lines.append(f"- IN PROGRESS: {m['home']} {m['home_score']} - {m['away_score']} {m['away']} (venue: {m['venue']})")
    return "\n".join(lines)


def generate_script(matches, api_key):
    match_summary = build_match_summary(matches)
    user_prompt = f"Here are yesterday's World Cup matches:\n\n{match_summary}\n\nWrite today's Mindful Joga script."

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
    with open(output_path, "wb") as f:
        f.write(response.content)
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

    matches = fetch_yesterdays_matches(api_football_key)

    # Always write the scores file, even if empty, so the live site never
    # shows yesterday's (or older) stale placeholder data.
    save_scores_json(matches)

    if not matches:
        log("No matches found for yesterday — skipping script/audio generation.")
        return

    script_text = generate_script(matches, anthropic_key)

    # Save the script alongside the audio for reference/debugging in the repo
    script_path = "scripts/latest-script.txt"
    os.makedirs("scripts", exist_ok=True)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    log(f"Script also saved to {script_path} for reference.")

    generate_audio(script_text, elevenlabs_key, "audio/today-male.mp3")

    log("Done.")


if __name__ == "__main__":
    main()
