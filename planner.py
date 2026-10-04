"""Japan trip agent: filter, route, and repair a city plan.

The decision loop is the same for the rules policy and the LLM policy.
Tools do the factual work. A stop is kept only when rating, walking time,
opening hours, and the running budget all pass. Rejected stops stay in the
trace so a later step can pick something else.

ponytail: each city has a few dozen places, so the search tries every legal
candidate. Past a few hundred places, shortlist by neighborhood first.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "pois.json"

WALK_KMH = 4.8
BUS_KMH = 15
TRAIN_KMH = 32
DAY_START = 10 * 60

# ponytail: one flat bus fare and a distance-based train fare. Real IC-card fares
# vary by operator; swap this for a timetable if the cache grows past one city.
SLOTS = (
    ("attraction", 10 * 60, False),
    ("restaurant", 12 * 60, False),
    ("attraction", 14 * 60, True),
    ("attraction", 16 * 60, True),
    ("restaurant", 18 * 60 + 30, False),
)


def load_catalog(path: Path | None = None) -> dict:
    raw = json.loads((path or DATA).read_text())
    return raw


def haversine_km(a: dict, b: dict) -> float:
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dphi = math.radians(b["lat"] - a["lat"])
    dlmb = math.radians(b["lon"] - a["lon"])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def walk_minutes(a: dict, b: dict) -> int:
    return int(round(haversine_km(a, b) / WALK_KMH * 60))


def say(lang: str, key: str, **kw) -> str:
    lang = "zh" if lang == "zh" else "en"
    return _TEXT[key][lang].format(**kw)


_TEXT = {
    "walk_over": {
        "en": "walk {minutes} min exceeds {limit}",
        "zh": "步行 {minutes} 分鐘，超過上限 {limit} 分鐘",
    },
    "from_anchor": {
        "en": "{minutes} min from {name} exceeds {limit}",
        "zh": "距離{name} {minutes} 分鐘，超過上限 {limit} 分鐘",
    },
    "transit_over": {
        "en": "{mode} {minutes} min exceeds the {limit} min ride limit",
        "zh": "{mode} {minutes} 分鐘，超過車程上限 {limit} 分鐘",
    },
    "money": {
        "en": "¥{left} left cannot cover ¥{cost} and ¥{reserve} for later meals",
        "zh": "餘額 ¥{left} 不夠付 ¥{cost}，之後餐費還要預留 ¥{reserve}",
    },
    "closed": {"en": "closed at that time", "zh": "那個時段沒有營業"},
    "splurge": {
        "en": "¥{cost} for the party is a splurge the day can skip",
        "zh": "這頓 ¥{cost} 太貴，這一天可以先跳過",
    },
    "skip": {
        "en": "no optional stop inside the walk and budget",
        "zh": "步行範圍同預算內沒有可選的下一站",
    },
    "no_meal": {
        "en": "no {kind} within {limit} min of {name}; try another area",
        "zh": "{name} 附近 {limit} 分鐘內沒有{kind}，改試下一個地區",
    },
    "added": {
        "en": "rating, hours, and budget passed; {mode} {minutes} min",
        "zh": "評分、營業時間同預算都過關；{mode} {minutes} 分鐘",
    },
    "added_here": {
        "en": "rating, hours, and budget passed",
        "zh": "評分、營業時間同預算都過關",
    },
    "repair_budget": {"en": "free budget for the remaining days", "zh": "騰出預算給後面的日子"},
    "no_day": {
        "en": "Day {day} has no route that satisfies travel, rating, and budget.",
        "zh": "第 {day} 天沒有同時符合交通、評分同預算的路線。",
    },
    "disclaimer": {
        "en": "Ratings, prices, and ride times are a frozen sample, not live Tabelog, Google, or a transit timetable.",
        "zh": "評分、價錢同車程都係固定樣本，不是即時的 Tabelog、Google 或時刻表。",
    },
    "search": {"en": "load cached Tabelog and Google ratings", "zh": "讀取暫存的 Tabelog 同 Google 評分"},
    "mode_walk": {"en": "walk", "zh": "步行"},
    "mode_bus": {"en": "bus", "zh": "巴士"},
    "mode_train": {"en": "train", "zh": "電車"},
    "kind_restaurant": {"en": "restaurant", "zh": "餐廳"},
    "checked_stop": {
        "en": "checker kept it: rating, hours, and budget passed; {mode} {minutes} min",
        "zh": "檢查通過先留下：評分、營業時間同預算都過關；{mode} {minutes} 分鐘",
    },
    "checked_here": {
        "en": "checker kept it: rating, hours, and budget passed",
        "zh": "檢查通過先留下：評分、營業時間同預算都過關",
    },
    "prompt_unused": {
        "en": "That note was not sent. Set LLM_API_KEY and LLM_BASE_URL, or the rules route ignores it.",
        "zh": "這句話未送到模型。要設 LLM_API_KEY 同 LLM_BASE_URL，否則規則行程會忽略它。",
    },
    "prompt_failed": {
        "en": "The model call failed, so this route ignored the note: {error}",
        "zh": "模型呼叫失敗，所以這條路線忽略了那句話：{error}",
    },
    "prompt_ignored": {
        "en": "The model did not return a legal plan, so this route ignored the note.",
        "zh": "模型沒有交到合法行程，所以這條路線忽略了那句話。",
    },
}


def ride(a: dict, b: dict, req: dict) -> dict | None:
    """Walk if it fits. Otherwise one bus or train hop when the user allowed transit."""
    walk = walk_minutes(a, b)
    if walk <= req["max_walk_min"]:
        return {"mode": "walk", "minutes": walk, "fare": 0}
    if req.get("transport") != "transit":
        return None
    km = haversine_km(a, b)
    if km <= 3:
        minutes = max(10, int(round(km / BUS_KMH * 60 + 6)))
        mode, fare = "bus", 220
    else:
        minutes = max(12, int(round(km / TRAIN_KMH * 60 + 10)))
        mode, fare = "train", int(round((180 + km * 40) / 10) * 10)
    if minutes > req["max_transit_min"]:
        return None
    return {"mode": mode, "minutes": minutes, "fare": fare}


def party_cost(poi: dict, party_size: int) -> int:
    return int(poi["price_yen"]) * party_size


def rating_ok(poi: dict, req: dict) -> str | None:
    google = poi.get("google_rating")
    if google is None or google < req["min_google"]:
        return f"Google {google} is below {req['min_google']}"
    if poi["kind"] == "restaurant":
        tabelog = poi.get("tabelog")
        if tabelog is None or tabelog < req["min_tabelog"]:
            return f"Tabelog {tabelog} is below {req['min_tabelog']}"
    return None


def interest_ok(poi: dict, interests: list[str]) -> bool:
    # Meals stay available on a temple day or a museum day.
    if poi["kind"] == "restaurant":
        return True
    if not interests:
        return True
    return bool(set(poi["tags"]) & set(interests))


def hours_ok(poi: dict, start_min: int, lang: str = "en") -> str | None:
    end = start_min + int(poi["duration_min"])
    if start_min < poi["open_min"] or end > poi["close_min"]:
        return say(lang, "closed")
    return None


def pool_for(pois: list[dict], req: dict, used: set[str]) -> list[dict]:
    out = []
    for poi in pois:
        if poi["city"] != req["city"] or poi["id"] in used:
            continue
        if not interest_ok(poi, req.get("interests") or []):
            continue
        if rating_ok(poi, req):
            continue
        out.append(poi)
    return out


def _cheapest(cands: list[dict], party_size: int) -> int:
    if not cands:
        return 0
    return min(party_cost(p, party_size) for p in cands)


def _future_reserve(slot_index: int, pool: list[dict], party_size: int) -> int:
    """Hold back the cheapest later meal so an early splurge cannot empty the day."""
    meals_left = sum(1 for kind, _start, optional in SLOTS[slot_index + 1 :] if kind == "restaurant" and not optional)
    if meals_left == 0:
        return 0
    restaurants = [p for p in pool if p["kind"] == "restaurant"]
    return _cheapest(restaurants, party_size) * meals_left


def _leg_cost(poi, prev, req) -> tuple[dict | None, int]:
    hop = None if prev is None else ride(prev, poi, req)
    fare = 0 if hop is None else hop["fare"] * req["party_size"]
    return hop, party_cost(poi, req["party_size"]) + fare


def _problems(poi, prev, budget_left, start_min, req, reserve, anchor=None) -> list[str]:
    lang = req.get("lang") or "en"
    reasons = []
    hop, cost = _leg_cost(poi, prev, req)
    if prev is not None and hop is None:
        if req.get("transport") == "transit":
            minutes = _transit_minutes_unchecked(prev, poi)
            reasons.append(say(lang, "transit_over", mode=say(lang, "mode_train"), minutes=minutes, limit=req["max_transit_min"]))
        else:
            reasons.append(say(lang, "walk_over", minutes=walk_minutes(prev, poi), limit=req["max_walk_min"]))
    if req.get("transport") != "transit" and anchor is not None and poi["id"] != anchor["id"]:
        away = walk_minutes(anchor, poi)
        if away > req["max_walk_min"]:
            reasons.append(say(lang, "from_anchor", minutes=away, name=anchor["name"], limit=req["max_walk_min"]))
    if cost + reserve > budget_left:
        reasons.append(say(lang, "money", left=budget_left, cost=cost, reserve=reserve))
    hour = hours_ok(poi, start_min, lang)
    if hour:
        reasons.append(hour)
    return reasons


def _transit_minutes_unchecked(a: dict, b: dict) -> int:
    km = haversine_km(a, b)
    if km <= 3:
        return max(10, int(round(km / BUS_KMH * 60 + 6)))
    return max(12, int(round(km / TRAIN_KMH * 60 + 10)))


def _log_rejects(trace: list[dict], rejected: list[tuple[dict, list[str]]]) -> None:
    for poi, reasons in rejected[:2]:
        trace.append(
            {
                "action": "reject",
                "poi_id": poi["id"],
                "name": poi["name"],
                "name_zh": poi.get("name_zh") or poi["name"],
                "reason": reasons[0],
            }
        )


def assemble_day(pool: list[dict], req: dict, budget_left: int, trace: list[dict]) -> dict | None:
    """Try morning anchors until one day can reach a legal lunch on foot."""
    attractions = [p for p in pool if p["kind"] == "attraction"]
    attractions.sort(key=lambda p: (-(p.get("google_rating") or 0), p["price_yen"]))
    best: dict | None = None
    seed_rejects: list[dict] = []
    last_fail: list[dict] = []
    for seed in attractions:
        seed_problems = _problems(seed, None, budget_left, SLOTS[0][1], req, _future_reserve(0, pool, req["party_size"]))
        if seed_problems:
            seed_rejects.append(
                {
                    "action": "reject",
                    "poi_id": seed["id"],
                    "name": seed["name"],
                    "name_zh": seed.get("name_zh") or seed["name"],
                    "reason": seed_problems[0],
                }
            )
            continue
        day_trace: list[dict] = []
        stops = []
        used: set[str] = set()
        prev = None
        money = budget_left
        failed = False
        for index, (kind, start, optional) in enumerate(SLOTS):
            reserve = _future_reserve(index, pool, req["party_size"])
            if index == 0:
                chosen = seed
                rejected: list[tuple[dict, list[str]]] = []
            else:
                ranked = [p for p in pool if p["kind"] == kind and p["id"] not in used]
                ranked.sort(key=lambda p: (-_rank(p, prev, req, kind), p["price_yen"]))
                # The 16:00 slot is the one ride to another neighborhood.
                if req.get("transport") == "transit" and index == 3:
                    ranked = _excursion_first(ranked, prev, pool, used, req)
                chosen = None
                rejected = []
                for cand in ranked:
                    reasons = _problems(cand, prev, money, start, req, reserve, seed)
                    if reasons:
                        rejected.append((cand, reasons))
                        continue
                    if cand["kind"] == "restaurant" and cand["price_yen"] >= 8000:
                        splurge = party_cost(cand, req["party_size"])
                        rejected.append((cand, [say(req.get("lang") or "en", "splurge", cost=splurge)]))
                        continue
                    chosen = cand
                    break
            _log_rejects(day_trace, rejected)
            if chosen is None:
                if optional:
                    day_trace.append({"action": "skip_slot", "reason": say(req.get("lang") or "en", "skip")})
                    continue
                failed = True
                if kind == "restaurant" and prev is not None:
                    day_trace.append(
                        {
                            "action": "repair",
                            "reason": say(
                                req.get("lang") or "en",
                                "no_meal",
                                kind=say(req.get("lang") or "en", "kind_restaurant"),
                                limit=req["max_walk_min"],
                                name=prev["name"],
                            ),
                        }
                    )
                break
            hop, cost = _leg_cost(chosen, prev, req)
            money -= cost
            used.add(chosen["id"])
            stop = _stop(chosen, start, hop, cost, req)
            stops.append(stop)
            mode_key = "mode_walk" if stop["mode"] == "walk" else "mode_" + stop["mode"]
            lang = req.get("lang") or "en"
            reason = (
                say(lang, "added_here")
                if stop["walk_min"] == 0
                else say(lang, "added", mode=say(lang, mode_key), minutes=stop["walk_min"])
            )
            day_trace.append(
                {
                    "action": "add",
                    "poi_id": chosen["id"],
                    "name": chosen["name"],
                    "name_zh": chosen.get("name_zh") or chosen["name"],
                    "walk_min": stop["walk_min"],
                    "mode": stop["mode"],
                    "cost_yen": cost,
                    "reason": reason,
                }
            )
            prev = chosen
        if failed or not _day_usable(stops):
            last_fail = [step for step in day_trace if step["action"] != "add"]
            continue
        built = {"stops": stops, "spent_yen": budget_left - money, "trace": day_trace}
        if best is None or _day_score(built) > _day_score(best):
            best = built
    if best:
        trace.extend(best["trace"])
        return best
    trace.extend(seed_rejects[:4])
    trace.extend(last_fail)
    return None


def _rank(poi: dict, prev: dict, req: dict, kind: str) -> float:
    hop = ride(prev, poi, req)
    if hop is None:
        return -1e9
    if hop["mode"] == "walk":
        return _blend(poi) - hop["minutes"] * 0.02
    # Meals stay near the last stop. Sights may take one ride.
    if kind == "restaurant":
        return _blend(poi) - 3 - hop["minutes"] * 0.02
    return _blend(poi) - 0.15 - hop["minutes"] * 0.01


def _excursion_first(ranked: list[dict], prev: dict, pool: list[dict], used: set[str], req: dict) -> list[dict]:
    """Put a bus or train hop to another area first when a meal is walkable there."""
    front, back = [], []
    meals = [p for p in pool if p["kind"] == "restaurant" and p["id"] not in used]
    for poi in ranked:
        hop = ride(prev, poi, req)
        away = hop is not None and hop["mode"] != "walk" and poi.get("area") != prev.get("area")
        meal_ok = any(ride(poi, meal, req) and ride(poi, meal, req)["mode"] == "walk" for meal in meals)
        if away and meal_ok:
            front.append(poi)
        else:
            back.append(poi)
    front.sort(key=lambda p: (-(p.get("google_rating") or 0), p["price_yen"]))
    return front + back


def _blend(poi: dict) -> float:
    google = poi.get("google_rating") or 0
    tabelog = poi.get("tabelog")
    if tabelog is None:
        return google
    # Tabelog 3.5 is already strong. Stretch it onto a 5-point scale for sorting only.
    return (google + (tabelog / 5) * 5) / 2


def _day_usable(stops: list[dict]) -> bool:
    kinds = {s["kind"] for s in stops}
    return "attraction" in kinds and "restaurant" in kinds


def _day_score(day: dict) -> float:
    ratings = sum(s["google_rating"] or 0 for s in day["stops"])
    walk = sum(s["walk_min"] for s in day["stops"])
    return len(day["stops"]) * 2 + ratings - walk * 0.02


def _stop(poi: dict, start_min: int, hop: dict | None, cost: int, req: dict) -> dict:
    mode = "walk" if hop is None else hop["mode"]
    minutes = 0 if hop is None else hop["minutes"]
    fare = 0 if hop is None else hop["fare"] * req["party_size"]
    return {
        "id": poi["id"],
        "name": poi["name"],
        "name_zh": poi.get("name_zh") or poi["name"],
        "kind": poi["kind"],
        "area": poi["area"],
        "area_zh": poi.get("area_zh") or poi["area"],
        "lat": poi["lat"],
        "lon": poi["lon"],
        "start_min": start_min,
        "duration_min": poi["duration_min"],
        "walk_min": minutes,
        "mode": mode,
        "fare_yen": fare,
        "cost_yen": cost,
        "price_yen": poi["price_yen"],
        "google_rating": poi.get("google_rating"),
        "google_count": poi.get("google_count"),
        "tabelog": poi.get("tabelog"),
        "tags": poi["tags"],
        "note": poi.get("note") or "",
        "note_zh": poi.get("note_zh") or poi.get("note") or "",
        "party_size": req["party_size"],
    }


def _rebuild_without(day: dict, drop_id: str, pool: list[dict], req: dict, budget_left: int, trace: list[dict]) -> dict | None:
    """Drop one pricey stop and fill the day again from the places still around it."""
    trace.append({"action": "repair", "poi_id": drop_id, "reason": say(req.get("lang") or "en", "repair_budget")})
    kept_ids = [s["id"] for s in day["stops"] if s["id"] != drop_id]
    by_id = {p["id"]: p for p in pool}
    if not kept_ids or kept_ids[0] not in by_id:
        return None
    # Reuse the normal search so the replacement still has to pass the tools.
    blocked = {drop_id}
    reduced = [p for p in pool if p["id"] not in blocked]
    return assemble_day(reduced, req, budget_left, trace)


def plan_rules(req: dict, pois: list[dict]) -> dict:
    trace: list[dict] = [{"action": "search", "city": req["city"], "reason": say(req.get("lang") or "en", "search")}]
    used: set[str] = set()
    budget_left = int(req["budget_yen"])
    days = []
    repairs = 0
    warnings: list[str] = []
    for day_number in range(1, int(req["days"]) + 1):
        pool = pool_for(pois, req, used)
        trace.append({"action": "filter", "day": day_number, "candidates": len(pool)})
        built = assemble_day(pool, req, budget_left, trace)
        if built is None and days:
            pricey = max(days[-1]["stops"], key=lambda s: s["cost_yen"])
            if pricey["cost_yen"] > 0:
                repairs += 1
                prev_pool = pool_for(pois, req, used - {s["id"] for s in days[-1]["stops"]})
                # Budget that day was allowed to spend includes what it already spent.
                prev_budget = budget_left + days[-1]["spent_yen"]
                rebuilt = _rebuild_without(days[-1], pricey["id"], prev_pool, req, prev_budget, trace)
                if rebuilt and rebuilt["spent_yen"] < days[-1]["spent_yen"]:
                    used -= {s["id"] for s in days[-1]["stops"]}
                    budget_left += days[-1]["spent_yen"]
                    days[-1] = _public_day(len(days), rebuilt)
                    for stop in rebuilt["stops"]:
                        used.add(stop["id"])
                    budget_left -= rebuilt["spent_yen"]
                    pool = pool_for(pois, req, used)
                    built = assemble_day(pool, req, budget_left, trace)
        if built is None:
            warnings.append(say(req.get("lang") or "en", "no_day", day=day_number))
            continue
        days.append(_public_day(day_number, built))
        for stop in built["stops"]:
            used.add(stop["id"])
        budget_left -= built["spent_yen"]
    spent = int(req["budget_yen"]) - budget_left
    return _result(req, days, spent, repairs, warnings, trace, "rules")


def _public_day(number: int, built: dict) -> dict:
    return {"day": number, "stops": built["stops"], "spent_yen": built["spent_yen"]}


def _result(req, days, spent, repairs, warnings, trace, mode: str) -> dict:
    return {
        "city": req["city"],
        "mode": mode,
        "budget_yen": int(req["budget_yen"]),
        "spent_yen": spent,
        "party_size": int(req["party_size"]),
        "repairs": repairs,
        "warnings": warnings,
        "days": days,
        "trace": trace,
        "transport": req.get("transport") or "walk",
        "disclaimer": say(req.get("lang") or "en", "disclaimer"),
    }


def validate_itinerary(days: list[list[dict]], req: dict, by_id: dict[str, dict]) -> list[str]:
    """Shared checker for a model proposal and for the eval."""
    errors = []
    seen: set[str] = set()
    spent = 0
    for day in days:
        prev = None
        for poi in day:
            if poi["id"] in seen:
                errors.append(f"duplicate {poi['name']}")
            seen.add(poi["id"])
            if poi["city"] != req["city"]:
                errors.append(f"{poi['name']} is not in {req['city']}")
            bad_rating = rating_ok(poi, req)
            if bad_rating:
                errors.append(f"{poi['name']}: {bad_rating}")
            if prev is not None:
                hop = ride(prev, poi, req)
                if hop is None:
                    errors.append(f"no legal ride between {prev['name']} and {poi['name']}")
                else:
                    spent += hop["fare"] * req["party_size"]
            spent += party_cost(poi, req["party_size"])
            prev = poi
    if spent > req["budget_yen"]:
        errors.append(f"budget ¥{spent} exceeds ¥{req['budget_yen']}")
    return errors


def load_dotenv() -> None:
    """Read KEY=value lines from .env. A variable already set in the shell wins."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def baseline_clause(req: dict) -> str:
    text = (req.get("baseline_brief") or "").strip()
    if not text:
        return ""
    return (
        "Rules route with the same constraints, before the traveler note:\n"
        + text
        + "\nChange it only to follow the note.\n"
    )


def _stop_label(stop: dict, lang: str) -> str:
    return stop["name_zh"] if lang == "zh" else stop["name"]


def _day_lines(days: list[dict], lang: str, with_ids: bool = False) -> str:
    lines = []
    for day in days:
        labels = []
        for stop in day["stops"]:
            label = _stop_label(stop, lang)
            if with_ids:
                label += f" ({stop['id']})"
            labels.append(label)
        title = f"第{day['day']}日" if lang == "zh" else f"Day {day['day']}"
        lines.append(title + "：" + " → ".join(labels))
    return "\n".join(lines)


def _thought(before: dict, after: list[dict], why: str, lang: str) -> dict:
    before_names = [_stop_label(stop, lang) for day in before["days"] for stop in day["stops"]]
    after_names = [_stop_label(stop, lang) for day in after for stop in day["stops"]]
    return {
        "before": _day_lines(before["days"], lang),
        "after": _day_lines(after, lang),
        "removed": [name for name in before_names if name not in after_names],
        "added": [name for name in after_names if name not in before_names],
        "why": why.strip()[:600],
    }


def propose_with_llm(req: dict, pool: list[dict], errors: list[str] | None) -> dict | None:
    load_dotenv()
    key = os.environ.get("LLM_API_KEY")
    base = os.environ.get("LLM_BASE_URL")
    if not key or not base:
        return None
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    brief = [
        {
            "id": p["id"],
            "name": p["name"],
            "kind": p["kind"],
            "area": p["area"],
            "price_yen": p["price_yen"],
            "google_rating": p.get("google_rating"),
            "tabelog": p.get("tabelog"),
            "tags": p["tags"],
            "note": p.get("note") or "",
            "name_zh": p.get("name_zh") or "",
        }
        for p in pool
    ]
    fix = ""
    if errors:
        fix = "The previous plan was rejected:\n" + "\n".join(errors) + "\nPropose a different plan.\n"
    note = (req.get("prompt") or "").strip()
    wish = ""
    if note:
        wish = (
            "Traveler note, follow it when it does not break the constraints: "
            + note
            + "\nUse only ids from the list.\n"
        )
    prompt = (
    "Plan a Japan city trip. Use only ids from the list. Do not invent places.\n"
        f"Constraints: city={req['city']}, days={req['days']}, budget_yen={req['budget_yen']} "
        f"for the whole party of {req['party_size']}, min_google={req['min_google']}, "
        f"min_tabelog={req['min_tabelog']} for restaurants, max_walk_min={req['max_walk_min']} "
        "between consecutive stops. Prices are per person.\n"
        f"{wish}{fix}{baseline_clause(req)}"
        'Reply with JSON only, no markdown: {"days": [["poi_id", "..."]], "why": "..."}\n'
        "why is 2 to 4 sentences. Say which rules-route places you kept, which you changed because of the traveler note, and why the kept places fit that note. "
        + ("Write why in Traditional Chinese.\n" if req.get("lang") == "zh" else "Write why in English.\n")
        + f"Places:\n{json.dumps(brief, ensure_ascii=False)}"
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You output JSON only. You never invent place ids."},
                {"role": "user", "content": prompt},
            ],
        }
    ).encode()
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    text = payload["choices"][0]["message"]["content"]
    start, end = text.find("{"), text.rfind("}")
    parsed = json.loads(text[start : end + 1])
    days = parsed.get("days") or []
    return {
        "days": [[str(pid) for pid in day] for day in days][: int(req["days"])],
        "why": str(parsed.get("why") or "")[:600],
    }


def plan_llm(req: dict, pois: list[dict], propose=None) -> dict:
    """Ask for a plan, check it, and send the errors back. Fall back to the rules agent."""
    pool = pool_for(pois, req, set())
    proposer = propose or propose_with_llm
    errors: list[str] | None = None
    asked = False
    call_error = ""
    trace: list[dict] = [{"action": "search", "city": req["city"], "candidates": len(pool)}]
    if req.get("prompt"):
        trace.append({"action": "note", "reason": req["prompt"]})
    by_id = {p["id"]: p for p in pool}
    baseline = plan_rules(req, pois)
    req["baseline_brief"] = _day_lines(baseline["days"], req.get("lang") or "en", with_ids=True)
    for attempt in range(3):
        try:
            proposed = proposer(req, pool, errors)
        except Exception as exc:
            call_error = str(exc)[:180]
            trace.append({"action": "repair", "reason": f"model call failed: {call_error}"})
            proposed = None
        if not proposed:
            break
        asked = True
        why = ""
        if isinstance(proposed, dict):
            why = str(proposed.get("why") or "")
            proposed = proposed.get("days") or []
        days = [[by_id[pid] for pid in day if pid in by_id] for day in proposed]
        missing = [pid for day in proposed for pid in day if pid not in by_id]
        errors = validate_itinerary(days, req, by_id)
        if missing:
            errors.append("unknown ids: " + ", ".join(missing))
        trace.append({"action": "check", "attempt": attempt + 1, "errors": errors})
        if not errors and any(days):
            public = []
            spent = 0
            for number, day in enumerate(days, start=1):
                stops = []
                prev = None
                clock = DAY_START
                for poi in day:
                    hop, cost = _leg_cost(poi, prev, req)
                    start = DAY_START if prev is None else clock + (0 if hop is None else hop["minutes"])
                    stops.append(_stop(poi, start, hop, cost, req))
                    clock = start + int(poi["duration_min"])
                    prev = poi
                    spent += cost
                public.append({"day": number, "stops": stops, "spent_yen": sum(s["cost_yen"] for s in stops)})
                for stop in stops:
                    lang = req.get("lang") or "en"
                    mode_key = "mode_walk" if stop["mode"] == "walk" else "mode_" + stop["mode"]
                    reason = (
                        say(lang, "checked_here")
                        if stop["walk_min"] == 0
                        else say(lang, "checked_stop", mode=say(lang, mode_key), minutes=stop["walk_min"])
                    )
                    trace.append(
                        {
                            "action": "add",
                            "poi_id": stop["id"],
                            "name": stop["name"],
                            "name_zh": stop["name_zh"],
                            "reason": reason,
                        }
                    )
            result = _result(req, public, spent, attempt, [], trace, "llm")
            result["thought"] = _thought(baseline, public, why, req.get("lang") or "en")
            return result
        trace.append({"action": "repair", "reason": "; ".join(errors[:3])})
    fallback = plan_rules(req, pois)
    if req.get("prompt"):
        if asked:
            notice = say(req.get("lang") or "en", "prompt_ignored")
        elif call_error:
            notice = say(req.get("lang") or "en", "prompt_failed", error=call_error)
        else:
            notice = say(req.get("lang") or "en", "prompt_unused")
        fallback["warnings"] = [notice] + list(fallback["warnings"])
    fallback["mode"] = "rules_after_rejected_draft"
    fallback["repairs"] = fallback["repairs"] + len([step for step in trace if step["action"] == "repair"])
    fallback["trace"] = trace + fallback["trace"]
    return fallback


def normalize_request(raw: dict) -> dict:
    interests = [str(item) for item in (raw.get("interests") or [])]
    city = str(raw.get("city") or "tokyo").lower()
    if city not in {"tokyo", "kyoto", "osaka"}:
        raise ValueError("city must be tokyo, kyoto, or osaka")
    days = int(raw.get("days") or 1)
    party = int(raw.get("party_size") or 1)
    budget = int(raw.get("budget_yen") or 0)
    if not 1 <= days <= 3:
        raise ValueError("days must be 1 to 3")
    if not 1 <= party <= 8:
        raise ValueError("party_size must be 1 to 8")
    if budget < 0 or budget > 2_000_000:
        raise ValueError("budget_yen is out of range")
    transport = str(raw.get("transport") or "walk")
    if transport not in {"walk", "transit"}:
        raise ValueError("transport must be walk or transit")
    max_transit = int(raw.get("max_transit_min") or 40)
    if not 10 <= max_transit <= 90:
        raise ValueError("max_transit_min must be 10 to 90")
    lang = "zh" if str(raw.get("lang") or "en") == "zh" else "en"
    return {
        "city": city,
        "days": days,
        "budget_yen": budget,
        "party_size": party,
        "min_google": float(raw.get("min_google") if raw.get("min_google") is not None else 4.0),
        "min_tabelog": float(raw.get("min_tabelog") if raw.get("min_tabelog") is not None else 3.3),
        "max_walk_min": int(raw.get("max_walk_min") or 20),
        "max_transit_min": max_transit,
        "transport": transport,
        "lang": lang,
        "interests": interests,
        "use_llm": bool(raw.get("use_llm")),
        "prompt": str(raw.get("prompt") or "").strip()[:500],
    }


def plan(raw: dict, pois: list[dict] | None = None, propose=None) -> dict:
    req = normalize_request(raw)
    if pois is None:
        pois = load_catalog()["pois"]
    if req.get("use_llm") or req.get("prompt"):
        return plan_llm(req, pois, propose=propose)
    return plan_rules(req, pois)


def check_plan(result: dict, req: dict, pois: list[dict]) -> list[str]:
    by_id = {p["id"]: p for p in pois}
    days = []
    for day in result["days"]:
        days.append([by_id[stop["id"]] for stop in day["stops"]])
    errors = validate_itinerary(days, req, by_id)
    if result["spent_yen"] > req["budget_yen"]:
        errors.append("reported spend exceeds budget")
    for day in result["days"]:
        for stop in day["stops"]:
            closed = hours_ok(by_id[stop["id"]], stop["start_min"], req.get("lang") or "en")
            if closed:
                errors.append(f"{stop['name']}: {closed}")
    return errors
