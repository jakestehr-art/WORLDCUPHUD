"""
Pulls FIFA World Cup 2026 match data from ESPN's public (no-auth) scoreboard API,
headlines from Google News RSS, tournament/group odds from Polymarket's public
Gamma API, and host-city weather from Open-Meteo (also no-auth) — then writes
data.json for the HUD page to consume.

No API key or signup required for any of these sources.
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

ET_ZONE = timezone(timedelta(hours=-4))  # EDT — correct for the June/July World Cup window

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
NEWS_URL = "https://news.google.com/rss/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Group stage runs through June 27, 2026 — used to distinguish "remaining group
# fixtures" from later knockout-round placeholder matches in the events feed.
GROUP_STAGE_CUTOFF = datetime(2026, 6, 28, tzinfo=timezone.utc)

# The 16 confirmed 2026 World Cup host venues, keyed by the city name as it's
# likely to appear in ESPN's venue.address.city field. Several cities map to
# the same stadium under different common names.
HOST_VENUES = {
    "atlanta": {"stadium": "Mercedes-Benz Stadium", "lat": 33.7554, "lon": -84.4008},
    "foxborough": {"stadium": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643},
    "boston": {"stadium": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643},
    "arlington": {"stadium": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945},
    "dallas": {"stadium": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945},
    "houston": {"stadium": "NRG Stadium", "lat": 29.6847, "lon": -95.4107},
    "kansas city": {"stadium": "Arrowhead Stadium", "lat": 39.0489, "lon": -94.4839},
    "inglewood": {"stadium": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "los angeles": {"stadium": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "miami gardens": {"stadium": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389},
    "miami": {"stadium": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389},
    "east rutherford": {"stadium": "MetLife Stadium", "lat": 40.8135, "lon": -74.0744},
    "new york": {"stadium": "MetLife Stadium", "lat": 40.8135, "lon": -74.0744},
    "new york/new jersey": {"stadium": "MetLife Stadium", "lat": 40.8135, "lon": -74.0744},
    "philadelphia": {"stadium": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675},
    "santa clara": {"stadium": "Levi's Stadium", "lat": 37.4032, "lon": -121.9697},
    "san francisco bay area": {"stadium": "Levi's Stadium", "lat": 37.4032, "lon": -121.9697},
    "seattle": {"stadium": "Lumen Field", "lat": 47.5952, "lon": -122.3316},
    "toronto": {"stadium": "BMO Field", "lat": 43.6332, "lon": -79.4187},
    "vancouver": {"stadium": "BC Place", "lat": 49.2768, "lon": -123.1119},
    "guadalajara": {"stadium": "Estadio Akron", "lat": 20.6822, "lon": -103.4625},
    "mexico city": {"stadium": "Estadio Banorte", "lat": 19.3029, "lon": -99.1505},
    "monterrey": {"stadium": "Estadio BBVA", "lat": 25.6628, "lon": -100.2453},
}

# WMO weather codes (used by Open-Meteo) -> short description
WMO_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}

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


def venue_info(comp):
    """Extract (city, stadium full name) from an ESPN competition object."""
    venue = comp.get("venue", {}) or {}
    address = venue.get("address", {}) or {}
    return address.get("city", ""), venue.get("fullName", "")


def lookup_venue(city):
    return HOST_VENUES.get((city or "").strip().lower())


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


def fetch_title_odds(limit=16):
    """World Cup outright-winner odds from Polymarket's public Gamma API.

    Each sub-market in the 'world-cup-winner' event is a Yes/No question
    ("Will <team> win the 2026 FIFA World Cup?"); the Yes price is the
    market's implied probability (0-1). Returns the top `limit` teams by
    probability as [{code, pct}, ...].
    """
    try:
        resp = requests.get(POLYMARKET_EVENTS_URL, params={"slug": "world-cup-winner"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        event = data[0] if isinstance(data, list) else data
        if not event:
            return []

        results = []
        for m in event.get("markets", []):
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = json.loads(m.get("outcomePrices", "[]"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not outcomes or not prices or len(outcomes) != len(prices):
                continue

            yes_idx = outcomes.index("Yes") if "Yes" in outcomes else 0
            pct = float(prices[yes_idx]) * 100

            team = m.get("groupItemTitle") or m.get("question", "")
            team = re.sub(r"^Will\s+", "", team)
            team = re.sub(r"\s+win the.*$", "", team, flags=re.IGNORECASE).strip()
            if not team:
                continue

            results.append({"code": team, "pct": round(pct, 1)})

        results.sort(key=lambda r: r["pct"], reverse=True)
        return results[:limit]
    except Exception as e:
        print("Polymarket title odds fetch failed:", e)
        return []


def fetch_weather(lat, lon):
    """Current conditions at a venue from Open-Meteo (no key required)."""
    try:
        resp = requests.get(OPEN_METEO_URL, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
        }, timeout=15)
        resp.raise_for_status()
        cur = resp.json().get("current", {})
        code = cur.get("weather_code")
        return {
            "tempF": round(cur["temperature_2m"]) if "temperature_2m" in cur else None,
            "windMph": round(cur["wind_speed_10m"]) if "wind_speed_10m" in cur else None,
            "description": WMO_WEATHER_CODES.get(code, "—"),
        }
    except Exception as e:
        print("Weather fetch failed:", e)
        return None


def fetch_top_scorers(events, limit=8):
    """Golden Boot leaderboard, aggregated from ESPN's per-match scoring-play
    'details' feed.

    ESPN's field names for this feed aren't formally documented, so this is
    written defensively: each event is processed independently (one bad
    entry doesn't break the rest), and if the expected fields aren't present
    at all, this simply returns an empty list rather than failing the run.
    """
    tally = {}  # (player, team canonical name) -> goal count

    for event in events:
        try:
            comp = event["competitions"][0]
            for d in (comp.get("details") or []):
                if not d.get("scoringPlay"):
                    continue
                type_text = ((d.get("type") or {}).get("text") or "").lower()
                if "goal" not in type_text or "own goal" in type_text:
                    continue

                team_id = (d.get("team") or {}).get("id")
                team_name = ""
                for c in comp["competitors"]:
                    if c.get("team", {}).get("id") == team_id:
                        team_name = c["team"].get("displayName", "")
                        break
                norm = normalize(team_name)
                canonical = TEAM_INFO[norm][1] if norm in TEAM_INFO else team_name

                athletes = d.get("athletesInvolved") or []
                player = ""
                if athletes:
                    player = athletes[0].get("displayName") or athletes[0].get("shortName") or ""
                if not player:
                    continue

                key = (player, canonical)
                tally[key] = tally.get(key, 0) + 1
        except Exception:
            continue

    scorers = [{"player": p, "team": t, "goals": g} for (p, t), g in tally.items()]
    scorers.sort(key=lambda r: r["goals"], reverse=True)
    return scorers[:limit]


def compute_next_match_and_fixtures(events):
    """Returns (next_match, remaining_fixtures).

    next_match: info (teams, kickoff time, venue) for the soonest upcoming
    match across the whole tournament — used for the "Next Up" countdown
    and venue weather, independent of "today's matches".

    remaining_fixtures: {team_name: ["vs Opponent (Jun 18)", ...]} for each
    team's still-to-be-played group-stage matches.
    """
    now_utc = datetime.now(timezone.utc)
    next_match = None
    fixtures = {}

    for event in events:
        comp = event["competitions"][0]
        state = comp["status"]["type"]["state"]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        home_norm, away_norm = normalize(home["team"]["displayName"]), normalize(away["team"]["displayName"])
        if home_norm not in TEAM_INFO or away_norm not in TEAM_INFO:
            continue
        home_group, home_name = TEAM_INFO[home_norm]
        _, away_name = TEAM_INFO[away_norm]

        kickoff_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))

        if state == "pre" and kickoff_utc <= GROUP_STAGE_CUTOFF:
            date_str = kickoff_utc.astimezone(ET_ZONE).strftime("%b %-d")
            fixtures.setdefault(home_name, []).append(f"vs {away_name} ({date_str})")
            fixtures.setdefault(away_name, []).append(f"vs {home_name} ({date_str})")

        if state == "pre" and kickoff_utc > now_utc:
            if next_match is None or kickoff_utc < next_match["_kickoff_utc"]:
                city, stadium = venue_info(comp)
                next_match = {
                    "_kickoff_utc": kickoff_utc,
                    "a": home_name,
                    "b": away_name,
                    "grp": f"GROUP {home_group}",
                    "kickoffISO": kickoff_utc.isoformat().replace("+00:00", "Z"),
                    "when": kickoff_utc.astimezone(ET_ZONE).strftime("%a %-I:%M %p ET"),
                    "venue": stadium,
                    "city": city,
                }

    for team in fixtures:
        fixtures[team] = fixtures[team][:3]

    if next_match:
        del next_match["_kickoff_utc"]

    return next_match, fixtures


def annotate_upsets(matches, group_odds):
    """Flags completed matches today where the team favored by Polymarket's
    group-winner odds lost. Mutates `matches` in place, adding 'upset',
    'favored', and 'favoredPct' to flagged entries.
    """
    for m in matches:
        if m.get("status") != "ft":
            continue
        letter = m["grp"].replace("GROUP ", "").strip()
        odds = {o["code"]: o["pct"] for o in group_odds.get(letter, [])}
        a_pct, b_pct = odds.get(m["a"]), odds.get(m["b"])
        if a_pct is None or b_pct is None:
            continue

        sa, sb = m["scoreA"], m["scoreB"]
        if sa == sb:
            continue  # draws aren't flagged

        if sa > sb:
            loser, loser_pct, winner_pct = m["b"], b_pct, a_pct
        else:
            loser, loser_pct, winner_pct = m["a"], a_pct, b_pct

        if loser_pct > winner_pct:  # the loser entered as the group favorite
            m["upset"] = True
            m["favored"] = loser
            m["favoredPct"] = loser_pct


def fetch_group_odds():
    """Group-stage winner odds from Polymarket, one event per group (A-L).

    Returns {"A": [{code, pct}, ...], ...}, team names normalized to the
    canonical GROUP_ROSTER names so they line up with the standings table.
    """
    group_odds = {}
    for letter in GROUP_ROSTER:
        slug = f"world-cup-group-{letter.lower()}-winner"
        try:
            resp = requests.get(POLYMARKET_EVENTS_URL, params={"slug": slug}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            event = data[0] if isinstance(data, list) and data else None
            if not event:
                group_odds[letter] = []
                continue

            entries = []
            for m in event.get("markets", []):
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                except (TypeError, json.JSONDecodeError):
                    continue
                if not outcomes or not prices or len(outcomes) != len(prices):
                    continue

                yes_idx = outcomes.index("Yes") if "Yes" in outcomes else 0
                pct = float(prices[yes_idx]) * 100

                team_raw = m.get("groupItemTitle") or m.get("question", "")
                team_raw = re.sub(r"^Will\s+", "", team_raw)
                team_raw = re.sub(r"\s+win.*$", "", team_raw, flags=re.IGNORECASE).strip()

                norm = normalize(team_raw)
                canonical = TEAM_INFO[norm][1] if norm in TEAM_INFO else team_raw
                entries.append({"code": canonical, "pct": round(pct, 1)})

            entries.sort(key=lambda r: r["pct"], reverse=True)
            group_odds[letter] = entries
        except Exception as e:
            print(f"Group {letter} odds fetch failed:", e)
            group_odds[letter] = []

    return group_odds


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

    probabilities = fetch_title_odds()
    group_odds = fetch_group_odds()
    annotate_upsets(matches, group_odds)

    news = fetch_news()
    top_scorers = fetch_top_scorers(events)
    next_match, remaining_fixtures = compute_next_match_and_fixtures(events)

    if next_match:
        venue = lookup_venue(next_match.get("city"))
        if venue:
            next_match["stadium"] = next_match["venue"] or venue["stadium"]
            next_match["weather"] = fetch_weather(venue["lat"], venue["lon"])
        else:
            next_match["stadium"] = next_match["venue"]
            next_match["weather"] = None

    output = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "matches": matches,
        "groups": groups,
        "liveTeams": sorted(set(live_teams)),
        "probabilities": probabilities,
        "groupOdds": group_odds,
        "news": news,
        "nextMatch": next_match,
        "remainingFixtures": remaining_fixtures,
        "topScorers": top_scorers,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    upsets = sum(1 for m in matches if m.get("upset"))
    print(f"Wrote data.json: {len(matches)} matches today ({upsets} upsets), "
          f"{len(probabilities)} title-odds entries, group odds for {len(group_odds)} groups, "
          f"{len(news)} headlines, {len(top_scorers)} top scorers, "
          f"next match: {next_match['a'] + ' vs ' + next_match['b'] if next_match else 'none found'}, "
          f"{len(live_teams)} live teams")


if __name__ == "__main__":
    main()
