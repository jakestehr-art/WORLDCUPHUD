"""
Pulls FIFA World Cup 2026 match data from ESPN's public (no-auth) scoreboard API
and headlines from Google News RSS (also no-auth), then writes data.json for the
HUD page to consume.

No API key or signup required for either source.
"""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

ET_ZONE = timezone(timedelta(hours=-4))  # EDT — correct for the June/July World Cup window

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
NEWS_URL = "https://news.google.com/rss/search"
YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml"
FIFA_YOUTUBE_CHANNEL_ID = "UCpcTrCXblq78GZrTUTLWeBw"  # FIFA's official channel

# Static 2026 World Cup group assignments (group stage is fixed for the whole tournament)
GROUP_ROSTER = {
    "A": ["Mexico", "South Africa", "South Korea", "Czechia"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["United States", "Paraguay", "Australia", "Turkey"],
    "E": ["Germany", "Curacao", "Ivory Coast", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "Iran", "New Zealand"],
    "H": ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}

# Map ESPN's display names (lowercase) to the canonical names used above
ALIASES = {
    "korea republic": "south korea",
    "czech republic": "czechia",
    "côte d'ivoire": "ivory coast",
    "cote d'ivoire": "ivory coast",
    "türkiye": "turkey",
    "cabo verde": "cape verde",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "congo dr": "dr congo",
}


def normalize(name):
    return ALIASES.get(name.strip().lower(), name.strip().lower())


# Build lookup: normalized name -> (group letter, canonical name)
TEAM_INFO = {}
for letter, teams in GROUP_ROSTER.items():
    for t in teams:
        TEAM_INFO[normalize(t)] = (letter, t)


def fetch_events():
    resp = requests.get(SCOREBOARD_URL, params={"dates": "20260611-20260719", "limit": 200}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("events", [])


def build_standings_and_matches(events, today_et):
    # init empty standings table
    groups = {
        letter: {team: {"mp": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0} for team in teams}
        for letter, teams in GROUP_ROSTER.items()
    }

    matches = []
    live_teams = []

    for event in events:
        comp = event["competitions"][0]
        status = comp["status"]
        state = status["type"]["state"]  # 'pre', 'in', 'post'

        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        home_norm = normalize(home["team"]["displayName"])
        away_norm = normalize(away["team"]["displayName"])

        if home_norm not in TEAM_INFO or away_norm not in TEAM_INFO:
            continue  # not a group-stage match we're tracking (e.g. future knockout placeholder)

        home_group, home_name = TEAM_INFO[home_norm]
        away_group, away_name = TEAM_INFO[away_norm]

        # ---- Standings (completed matches only) ----
        if state == "post":
            hs, as_ = int(home["score"]), int(away["score"])
            ht = groups[home_group][home_name]
            at = groups[away_group][away_name]
            ht["mp"] += 1; at["mp"] += 1
            ht["gf"] += hs; ht["ga"] += as_
            at["gf"] += as_; at["ga"] += hs
            if hs > as_:
                ht["w"] += 1; ht["pts"] += 3
                at["l"] += 1
            elif hs < as_:
                at["w"] += 1; at["pts"] += 3
                ht["l"] += 1
            else:
                ht["d"] += 1; ht["pts"] += 1
                at["d"] += 1; at["pts"] += 1

        # ---- Today's matches ----
        kickoff_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        kickoff_et = kickoff_utc.astimezone(ET_ZONE)
        if kickoff_et.date() != today_et:
            continue

        if state == "in":
            match_status = "live"
        elif state == "post":
            match_status = "ft"
        else:
            match_status = "upcoming"

        entry = {
            "id": f"wc26-{event['id']}",
            "when": kickoff_et.strftime("%-I:%M %p"),
            "_sort": kickoff_et,
            "grp": f"GROUP {home_group}",
            "a": home_name,
            "b": away_name,
            "status": match_status,
        }
        if match_status in ("ft", "live"):
            entry["scoreA"] = int(home["score"])
            entry["scoreB"] = int(away["score"])
        if match_status == "live":
            entry["minute"] = status.get("displayClock", "LIVE")
            live_teams.append(home_name)
            live_teams.append(away_name)

        matches.append(entry)

    matches.sort(key=lambda m: m["_sort"])
    for m in matches:
        del m["_sort"]

    # Convert standings dicts -> the [name, mp, w, d, l, gf, ga, pts] format the page expects
    groups_out = {}
    for letter, teams in groups.items():
        groups_out[letter] = {"teams": [
            [name, t["mp"], t["w"], t["d"], t["l"], t["gf"], t["ga"], t["pts"]]
            for name, t in teams.items()
        ]}

    return groups_out, matches, live_teams


def implied_prob_from_moneyline(ml):
    """American moneyline -> raw implied probability (0-1), before removing the vig."""
    if ml is None:
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return -ml / (-ml + 100.0)


def fetch_match_probabilities(events, groups_out, today_et):
    """Win probability for each team in today's matches.

    Prefers ESPN's odds (winPercentage if provided, else moneyline converted
    and de-vigged). Falls back to a simple standings-based estimate — each
    team starts at 50% and is nudged by the points gap with their opponent —
    when no odds are available for that match.
    """
    # quick lookup: canonical team name -> current points
    points_by_team = {}
    for letter, g in groups_out.items():
        for row in g["teams"]:
            points_by_team[row[0]] = row[7]

    results = []
    for event in events:
        comp = event["competitions"][0]
        kickoff_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        kickoff_et = kickoff_utc.astimezone(ET_ZONE)
        if kickoff_et.date() != today_et:
            continue

        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        home_norm, away_norm = normalize(home["team"]["displayName"]), normalize(away["team"]["displayName"])
        if home_norm not in TEAM_INFO or away_norm not in TEAM_INFO:
            continue
        _, home_name = TEAM_INFO[home_norm]
        _, away_name = TEAM_INFO[away_norm]

        home_pct = away_pct = None

        odds_list = comp.get("odds") or []
        o = odds_list[0] if odds_list else None
        if o:
            home_odds = o.get("homeTeamOdds", {}) or {}
            away_odds = o.get("awayTeamOdds", {}) or {}
            draw_odds = o.get("drawOdds", {}) or {}

            # 1) ESPN sometimes provides a pre-computed implied win percentage directly
            hwp, awp = home_odds.get("winPercentage"), away_odds.get("winPercentage")
            if hwp is not None and awp is not None:
                home_pct, away_pct = hwp * 100, awp * 100
            else:
                # 2) otherwise derive from moneylines, de-vigging across the 3-way market
                hp = implied_prob_from_moneyline(home_odds.get("moneyLine"))
                ap = implied_prob_from_moneyline(away_odds.get("moneyLine"))
                dp = implied_prob_from_moneyline(draw_odds.get("moneyLine")) or 0
                if hp is not None and ap is not None:
                    total = hp + ap + dp
                    home_pct, away_pct = (hp / total) * 100, (ap / total) * 100

        if home_pct is None:
            # Fallback: nudge 50/50 by current group-stage points gap (5 pts per point of gap)
            gap = points_by_team.get(home_name, 0) - points_by_team.get(away_name, 0)
            home_pct = max(10, min(90, 50 + gap * 5))
            away_pct = 100 - home_pct

        results.append({"code": home_name, "pct": round(home_pct)})
        results.append({"code": away_name, "pct": round(away_pct)})

    return results


def fetch_highlights(limit=6):
    """Returns recent FIFA YouTube uploads as [{videoId, title}, ...]."""
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    try:
        resp = requests.get(YOUTUBE_RSS_URL, params={"channel_id": FIFA_YOUTUBE_CHANNEL_ID}, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        highlights = []
        for entry in root.findall("atom:entry", ns)[:limit]:
            video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
            title = entry.findtext("atom:title", default="", namespaces=ns)
            if video_id:
                highlights.append({"videoId": video_id, "title": title})
        return highlights
    except Exception as e:
        print("Highlights fetch failed:", e)
        return []


def fetch_news(limit=8):
    try:
        resp = requests.get(NEWS_URL, params={"q": "World Cup 2026", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        headlines = []
        for item in root.findall("./channel/item")[:limit]:
            title = item.findtext("title") or ""
            # Google News appends " - Source Name"; trim it for a cleaner ticker
            headlines.append(title.rsplit(" - ", 1)[0])
        return headlines
    except Exception as e:
        print("News fetch failed:", e)
        return []


def main():
    events = fetch_events()
    today_et = datetime.now(ET_ZONE).date()
    groups, matches, live_teams = build_standings_and_matches(events, today_et)
    probabilities = fetch_match_probabilities(events, groups, today_et)
    news = fetch_news()
    highlights = fetch_highlights()

    output = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "matches": matches,
        "groups": groups,
        "liveTeams": sorted(set(live_teams)),
        "probabilities": probabilities,
        "news": news,
        "highlights": highlights,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote data.json: {len(matches)} matches today, {len(probabilities)} probability entries, "
          f"{len(news)} headlines, {len(highlights)} highlight videos, {len(live_teams)} live teams")


if __name__ == "__main__":
    main()
