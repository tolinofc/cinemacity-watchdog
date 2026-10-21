#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
LANG = "cs_CZ"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FILM_PATTERN = os.environ.get("FILM_PATTERN", "dun").lower()
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "imax").lower()
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "180"))
HINT_ATTR = os.environ.get("HINT_ATTR", "70-mm")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))
TICKETS_BASE = "https://tickets.cinemacity.cz/api"
AVAILABILITY_CHECK = os.environ.get("AVAILABILITY_CHECK", "1") != "0"
MIN_FREED_SEATS = int(os.environ.get("MIN_FREED_SEATS", "1"))

CZ_DAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]
CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    """Aktuální čas v zóně kina, bez tzinfo — porovnatelný s daty z API."""
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    """GET na data-api-service; vrací obsah klíče "body"."""
    url = f"{BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["body"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise SystemExit(f"API selhalo po 4 pokusech: {url}\n{last}")


def horizon():
    return (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()


def fetch_cinemas():
    body = api(f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}")
    return {c["id"]: c for c in body["cinemas"]}


def fetch_dates(cinema_id):
    time.sleep(DELAY)
    return api(f"/dates/in-cinema/{cinema_id}/until/{horizon()}?attr=&lang={LANG}")["dates"]


def fetch_day(cinema_id, day):
    time.sleep(DELAY)
    body = api(f"/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}")
    films = {f["id"]: f for f in body.get("films", [])}
    return films, body.get("events", [])


def hint_cinema_ids():
    """Kina, která podle API mají představení s atributem HINT_ATTR."""
    if not HINT_ATTR:
        return set()
    body = api(f"/cinemas/with-event/until/{horizon()}?attr={HINT_ATTR}&lang={LANG}")
    return {c["id"] for c in body["cinemas"]}


def is_target_hall(event):
    return AUDITORIUM_PATTERN in (event.get("auditorium") or "").lower()


def collect():
    cinemas = fetch_cinemas()
    dates_by_cinema = {cid: fetch_dates(cid) for cid in cinemas}

    candidates = hint_cinema_ids() & set(cinemas)
    day_cache = {}
    for cid, days in dates_by_cinema.items():
        if not days:
            continue
        probe = days[0]
        day_cache[(cid, probe)] = fetch_day(cid, probe)
        if any(is_target_hall(e) for e in day_cache[(cid, probe)][1]):
            candidates.add(cid)

    found = {}
    for cid in sorted(candidates):
        for day in dates_by_cinema.get(cid, []):
            films, events = day_cache.get((cid, day)) or fetch_day(cid, day)
            for e in events:
                film = films.get(e["filmId"], {})
                if FILM_PATTERN not in film.get("name", "").lower():
                    continue
                if not is_target_hall(e):
                    continue
                found[e["id"]] = {
                    "id": e["id"],
                    "film": film.get("name", e["filmId"]),
                    "filmLink": film.get("link"),
                    "cinema": cinemas[cid]["displayName"],
                    "cinemaId": cid,
                    "datetime": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "attrs": e.get("attributeIds", []),
                    "booking": f"https://tickets.cinemacity.cz/order/{e.get('presentationCode') or e['id']}",
                    "soldOut": bool(e.get("soldOut")),
                    "presentationCode": e.get("presentationCode") or e["id"],
                    "availabilityRatio": e.get("availabilityRatio"),
                }
    return found


_capacity_cache = {}


def tickets_api(path, method="GET"):
    """Volání veřejných (nechráněných) endpointů tickets.cinemacity.cz."""
    url = f"{TICKETS_BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"tickets API selhalo po 4 pokusech: {url}\n{last}")


def seatplan_capacity(venue_id, seatplan_id):
    """Celkový počet sedadel v sále podle veřejného plánu sálu."""
    key = (venue_id, seatplan_id)
    if key not in _capacity_cache:
        body = tickets_api(
            f"/seats/seatplanV2?venueId={venue_id}&seatplanId={seatplan_id}", method="POST"
        )
        _capacity_cache[key] = sum(
            len(row.get("S", {}))
            for section in body.get("S", {}).values()
            for group in section.get("G", {}).values()
            for row in group.get("R", {}).values()
        )
    return _capacity_cache[key]


def enrich_availability(event, known_event=None):
    """Doplní capacity a freeSeats. Kapacita se bere ze stavu, ať se nevolá pořád.

    Fail-soft: když se něco nepovede, event zůstane s ratio a hlídání poběží dál.
    """
    ratio = event.get("availabilityRatio")
    if ratio is None:
        return
    capacity = (known_event or {}).get("capacity")
    try:
        if not capacity:
            time.sleep(DELAY)
            presentation = tickets_api(
                f"/presentations/{event['presentationCode']}?referralMiniSiteId=0"
            )["presentation"]
            time.sleep(DELAY)
            capacity = seatplan_capacity(presentation["venueId"], presentation["seatplanId"])
        if capacity:
            event["capacity"] = capacity
            event["freeSeats"] = round(ratio * capacity)
    except Exception as exc:
        print(f"Odhad volných míst selhal u {event['id']}: {exc}")


def seats_gained(old, new):
    """O kolik míst přibylo oproti minulému běhu (None = nejde určit)."""
    old_free, new_free = old.get("freeSeats"), new.get("freeSeats")
    if old_free is not None and new_free is not None:
        return new_free - old_free
    old_ratio, new_ratio = old.get("availabilityRatio"), new.get("availabilityRatio")
    if old_ratio is not None and new_ratio is not None:
        return 1 if new_ratio - old_ratio > 0.002 else 0
    if "soldOut" in old:
        return 1 if old["soldOut"] and not new["soldOut"] else 0
    return None


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"updated": None, "events": {}}


def save_state(path, events):
    if events == load_state(path).get("events", {}):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": now().replace(microsecond=0).isoformat(),
        "events": dict(sorted(events.items(), key=lambda kv: kv[1]["datetime"])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    return True


def prune_past(events):
    """Zahodí ze stavu představení, která už proběhla — ať soubor neroste."""
    cutoff = (now() - timedelta(days=1)).isoformat()
    return {k: v for k, v in events.items() if v["datetime"] >= cutoff}


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)
    return f"{CZ_DAYS[dt.weekday()]} {dt.day}. {dt.month}. {dt.year} v {dt:%H:%M}"


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.day}. {dt.month}."


def render(new_events, gone_events, freed_events=()):
    """Markdown tělo hlášení."""
    lines = []
    if freed_events:
        lines.append(f"### Uvolnila se místa ({len(freed_events)})\n")
        for cinema, group in group_by_cinema(freed_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                if e.get("freeSeats") is not None and e.get("was") is not None:
                    detail = f" — ~{e['freeSeats']} volných (bylo ~{e['was']}, +{e['gained']})"
                else:
                    detail = " — přibyla volná místa"
                link = f" — [koupit]({e['booking']})" if e["booking"] else ""
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{detail}{link}")
            lines.append("")
    if new_events:
        lines.append(f"### Nově vypsáno ({len(new_events)})\n")
        for cinema, group in group_by_cinema(new_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                flags = []
                if "70-mm" in e["attrs"]:
                    flags.append("70mm")
                if "subbed" in e["attrs"]:
                    flags.append("titulky")
                if "dubbed" in e["attrs"]:
                    flags.append("dabing")
                if e["soldOut"]:
                    flags.append("**vyprodáno**")
                elif e.get("freeSeats") is not None:
                    flags.append(f"~{e['freeSeats']} volných")
                suffix = f" — {', '.join(flags)}" if flags else ""
                link = f" — [koupit]({e['booking']})" if e["booking"] else ""
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{suffix}{link}")
            lines.append("")
    if gone_events:
        lines.append(f"### Zmizelo z rozpisu ({len(gone_events)})\n")
        for cinema, group in group_by_cinema(gone_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}")
            lines.append("")
    film_link = next(
        (e["filmLink"] for e in list(new_events) + list(gone_events) + list(freed_events) if e.get("filmLink")),
        None,
    )
    if film_link:
        lines.append(f"[Stránka filmu na Cinema City]({film_link})")
    lines.append("")
    lines.append(
        f"<sub>Zkontrolováno {now():%d. %m. %Y %H:%M} · "
        f"film ~ `{FILM_PATTERN}` · sál ~ `{AUDITORIUM_PATTERN}`</sub>"
    )
    return "\n".join(lines)


def group_by_cinema(events):
    order = {}
    for e in sorted(events, key=lambda x: (x["cinema"], x["datetime"])):
        order.setdefault(e["cinema"], []).append(e)
    return order.items()


def title_for(new_events):
    film = new_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in new_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    n = len(new_events)
    word = "nový termín" if n == 1 else ("nové termíny" if n < 5 else "nových termínů")
    return f"🎬 {film} v IMAXu: {n} {word} ({span})"


def title_for_freed(freed_events):
    film = freed_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in freed_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    return f"🎟️ {film} v IMAXu: uvolněná místa ({span})"


def gh_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state/seen.json", help="soubor se stavem")
    ap.add_argument("--seed", action="store_true", help="jen ulož stav, nic nehlas")
    ap.add_argument("--force-report", action="store_true", help="nahlas vše, i známé")
    ap.add_argument("--report", default="report.md", help="kam zapsat markdown hlášení")
    ap.add_argument("--title", default="title.txt", help="kam zapsat titulek issue")
    args = ap.parse_args()

    current = collect()
    state = load_state(args.state)
    known = state.get("events", {})

    if AVAILABILITY_CHECK:
        for k, e in current.items():
            enrich_availability(e, known.get(k))

    print(f"Nalezeno {len(current)} hlídaných představení, ve stavu {len(known)}.")

    if args.seed:
        save_state(args.state, prune_past(current))
        print(f"Stav zapsán do {args.state} (seed, nic se nehlásí).")
        gh_output(has_news="false")
        return

    if args.force_report:
        new_events = sorted(current.values(), key=lambda e: e["datetime"])
        gone = []
        freed = []
    else:
        new_events = sorted(
            (v for k, v in current.items() if k not in known),
            key=lambda e: e["datetime"],
        )
        freed = []
        for k, v in current.items():
            old = known.get(k)
            if not old:
                continue
            gained = seats_gained(old, v)
            if gained is not None and gained >= MIN_FREED_SEATS:
                freed.append({**v, "gained": gained, "was": old.get("freeSeats")})
        freed.sort(key=lambda e: e["datetime"])
        future = now().isoformat()
        gone = sorted(
            (v for k, v in known.items() if k not in current and v["datetime"] > future),
            key=lambda e: e["datetime"],
        )

    save_state(args.state, prune_past(current))

    if not new_events and not gone and not freed:
        print("Nic nového.")
        gh_output(has_news="false")
        return

    body = render(new_events, gone, freed)
    if freed:
        title = title_for_freed(freed)
        if new_events:
            title += f" + {len(new_events)} nových termínů"
    elif new_events:
        title = title_for(new_events)
    else:
        title = "🎬❌ Zrušené termíny"
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(args.title, "w", encoding="utf-8") as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)
    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
