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


def build_standings_and_matches(events):
    # init empty standings table
    groups = {
        letter: {team: {"mp": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0} for team in teams}
        for letter, teams in GROUP_ROSTER.items()
    }

    matches = []
    live_teams = []
    today_et = datetime.now(ET_ZONE).date()

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
    groups, matches, live_teams = build_standings_and_matches(events)
    news = fetch_news()

    output = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "matches": matches,
        "groups": groups,
        "liveTeams": sorted(set(live_teams)),
        "news": news,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote data.json: {len(matches)} matches today, {len(news)} headlines, {len(live_teams)} live teams")


if __name__ == "__main__":
    main()
