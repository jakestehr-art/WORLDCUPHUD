"""
Pulls FIFA World Cup 2026 match data from ESPN's public (no-auth) scoreboard API,
headlines from Google News RSS, match photos from Wikimedia Commons, and
tournament-winner odds from Polymarket's public Gamma API — then writes
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
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"

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


def fetch_photos(matches, limit=6):
    """Photos related to the 2026 World Cup from Wikimedia Commons (CC-licensed, attributed).

    Tries an increasingly general set of plain-text searches — today's
    specific matchups first, then the tournament generally — combining
    results until `limit` is reached. (An earlier version restricted
    searches with `deepcat:`, which is slow/unreliable on Commons and
    often returns nothing even when matching files exist.)
    """
    def search_commons(query, n):
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,  # File namespace
            "gsrlimit": n,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": 640,
        }
        resp = requests.get(
            COMMONS_API_URL, params=params, timeout=15,
            headers={"User-Agent": "WorldCupHUD/1.0 (GitHub Pages dashboard; no contact on file)"},
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})

        photos = []
        for page in pages.values():
            info = (page.get("imageinfo") or [None])[0]
            if not info:
                continue
            if not info.get("mime", "").startswith("image/"):
                continue  # skip videos, PDFs, etc.
            thumb = info.get("thumburl") or info.get("url")
            if not thumb:
                continue
            meta = info.get("extmetadata", {})
            artist = meta.get("Artist", {}).get("value", "")
            artist = re.sub(r"<[^>]+>", "", artist).strip()  # strip embedded HTML links
            license_name = meta.get("LicenseShortName", {}).get("value", "")
            photos.append({
                "title": page.get("title", "").replace("File:", ""),
                "thumbUrl": thumb,
                "pageUrl": info.get("descriptionurl", ""),
                "attribution": artist,
                "license": license_name,
            })
        return photos

    seen = set()
    results = []

    queries = [f"{m['a']} {m['b']} 2026 FIFA World Cup" for m in matches[:4]]
    queries.append("2026 FIFA World Cup")
    queries.append("FIFA World Cup 2026 stadium fans")

    for q in queries:
        if len(results) >= limit:
            break
        try:
            for photo in search_commons(q, limit - len(results) + 2):
                if photo["title"] not in seen:
                    seen.add(photo["title"])
                    results.append(photo)
        except Exception as e:
            print(f"Photo search failed for '{q}':", e)

    return results[:limit]


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
    news = fetch_news()
    photos = fetch_photos(matches)

    output = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "matches": matches,
        "groups": groups,
        "liveTeams": sorted(set(live_teams)),
        "probabilities": probabilities,
        "groupOdds": group_odds,
        "news": news,
        "highlights": photos,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote data.json: {len(matches)} matches today, {len(probabilities)} title-odds entries, "
          f"group odds for {len(group_odds)} groups, {len(news)} headlines, {len(photos)} photos, "
          f"{len(live_teams)} live teams")


if __name__ == "__main__":
    main()
