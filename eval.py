"""Constraint checks for the Japan day agent. One file, no test framework."""

from __future__ import annotations

from planner import check_plan, load_catalog, normalize_request, plan, walk_minutes

POIS = load_catalog()["pois"]


def scenarios() -> list[dict]:
    rows = []
    for city in ("tokyo", "kyoto", "osaka"):
        rows.append(_case(f"{city}-two-days", city, 2, 80000, 2, 20, 4.0, 3.3, [], 6))
        rows.append(_case(f"{city}-one-day", city, 1, 25000, 1, 20, 4.0, 3.3, [], 3))
        rows.append(_case(f"{city}-short-walk", city, 1, 30000, 2, 12, 4.0, 3.3, [], 3))
        rows.append(_case(f"{city}-tight-budget", city, 1, 8000, 2, 20, 4.0, 3.3, [], 3))
        rows.append(_case(f"{city}-impossible-budget", city, 1, 2000, 2, 20, 4.0, 3.3, [], 0))
        rows.append(_case(f"{city}-strict-tabelog", city, 1, 50000, 2, 20, 4.0, 3.7, [], 0))
        rows.append(_case(f"{city}-strict-google", city, 1, 50000, 2, 20, 4.6, 3.3, [], 0))
        rows.append(_case(f"{city}-temples", city, 1, 30000, 2, 20, 4.0, 3.3, ["temple"], 3))
    rows.append(_case("osaka-theme-park", "osaka", 1, 80000, 2, 15, 4.0, 3.3, ["theme-park"], 3))
    rows.append(_case("osaka-theme-park-broke", "osaka", 1, 15000, 2, 15, 4.0, 3.3, ["theme-park"], 0))
    rows.append(_case("tokyo-three-days", "tokyo", 3, 90000, 2, 18, 4.0, 3.3, [], 8))
    rows.append(_case("kyoto-party-of-four", "kyoto", 2, 100000, 4, 20, 4.0, 3.3, [], 6))
    rows.append(_case("tokyo-views", "tokyo", 1, 40000, 2, 20, 4.0, 3.3, ["viewpoint"], 3))
    rows.append(_case("tokyo-pop", "tokyo", 1, 30000, 2, 20, 4.0, 3.3, ["pop"], 3))
    rows.append(_case("osaka-markets", "osaka", 1, 20000, 2, 20, 4.0, 3.3, ["market"], 2))
    rows.append(_case("osaka-three-days", "osaka", 3, 120000, 2, 20, 4.0, 3.3, [], 8))
    rows.append(_case("tokyo-wider-walk", "tokyo", 2, 60000, 2, 25, 4.0, 3.3, [], 6))
    rows.append(_case("kyoto-tighter", "kyoto", 2, 40000, 1, 15, 4.0, 3.3, [], 6))
    rows.append(_case("osaka-higher-floors", "osaka", 2, 40000, 1, 18, 4.2, 3.4, [], 6))
    rows.append(_case("tokyo-museums", "tokyo", 1, 40000, 2, 20, 4.0, 3.3, ["museum"], 2))
    return rows


def _case(name, city, days, budget, party, walk, google, tabelog, interests, min_stops) -> dict:
    return {
        "name": name,
        "min_stops": min_stops,
        "request": {
            "city": city,
            "days": days,
            "budget_yen": budget,
            "party_size": party,
            "max_walk_min": walk,
            "min_google": google,
            "min_tabelog": tabelog,
            "interests": interests,
        },
    }


def naive_plan(req: dict) -> dict:
    """One-shot baseline: highest Google rating, ignore walking distance."""
    pool = [
        poi
        for poi in POIS
        if poi["city"] == req["city"] and (poi["kind"] == "restaurant" or not req["interests"] or set(poi["tags"]) & set(req["interests"]))
    ]
    pool.sort(key=lambda poi: poi.get("google_rating") or 0, reverse=True)
    days = []
    spent = 0
    used = set()
    for number in range(req["days"]):
        stops = []
        for poi in pool:
            if poi["id"] in used or len(stops) == 4:
                continue
            cost = poi["price_yen"] * req["party_size"]
            if spent + cost > req["budget_yen"]:
                continue
            stops.append({"id": poi["id"], "name": poi["name"], "start_min": 9 * 60 + 30, "cost_yen": cost})
            used.add(poi["id"])
            spent += cost
        if stops:
            days.append({"day": number + 1, "stops": stops, "spent_yen": sum(s["cost_yen"] for s in stops)})
    return {"days": days, "spent_yen": spent, "budget_yen": req["budget_yen"]}


def _walk_violations(result: dict, req: dict) -> int:
    by_id = {poi["id"]: poi for poi in POIS}
    count = 0
    for day in result["days"]:
        prev = None
        for stop in day["stops"]:
            if prev is not None and walk_minutes(prev, by_id[stop["id"]]) > req["max_walk_min"]:
                count += 1
            prev = by_id[stop["id"]]
    return count


def _llm_rejects_then_falls_back() -> None:
    def propose(req, pool, errors):
        if errors:
            return None
        return [["tyo-sensoji", "tyo-shibuya"]]

    result = plan(
        {
            "city": "tokyo",
            "days": 1,
            "budget_yen": 20000,
            "party_size": 1,
            "max_walk_min": 20,
            "min_google": 4.0,
            "min_tabelog": 3.3,
            "use_llm": True,
        },
        POIS,
        propose=propose,
    )
    assert result["mode"] == "rules_after_rejected_draft", result["mode"]
    assert result["repairs"] >= 1
    req = normalize_request({"city": "tokyo", "days": 1, "budget_yen": 20000, "party_size": 1, "use_llm": True})
    assert check_plan(result, req, POIS) == []


def _transit_reaches_another_area() -> None:
    base = {
        "city": "tokyo",
        "days": 1,
        "budget_yen": 40000,
        "party_size": 2,
        "max_walk_min": 12,
        "max_transit_min": 40,
        "min_google": 4.0,
        "min_tabelog": 3.3,
    }
    walking = plan({**base, "transport": "walk"}, POIS)
    riding = plan({**base, "transport": "transit", "lang": "zh"}, POIS)
    modes = {stop.get("mode") for day in riding["days"] for stop in day["stops"]}
    areas = {stop["area"] for day in riding["days"] for stop in day["stops"]}
    walk_areas = {stop["area"] for day in walking["days"] for stop in day["stops"]}
    if not ({"bus", "train"} & modes) or len(areas) <= len(walk_areas):
        raise SystemExit(f"transit did not add a ride: modes {modes} areas {areas} vs {walk_areas}")
    req = normalize_request({**base, "transport": "transit"})
    errors = check_plan(riding, req, POIS)
    if errors or riding["spent_yen"] > base["budget_yen"]:
        raise SystemExit(f"transit plan illegal: {errors}")


def _prompt_reaches_the_model() -> None:
    seen = {}

    def propose(req, pool, errors):
        seen["prompt"] = req.get("prompt")
        return None

    result = plan(
        {"city": "osaka", "days": 1, "budget_yen": 40000, "party_size": 2, "prompt": "skip USJ, more dessert"},
        POIS,
        propose=propose,
    )
    if seen.get("prompt") != "skip USJ, more dessert":
        raise SystemExit(f"prompt did not reach the model: {seen}")
    if not any("not sent" in text or "未送到" in text for text in result["warnings"]):
        raise SystemExit(f"missing unused-prompt warning: {result['warnings']}")
    ids = [stop["id"] for day in result["days"] for stop in day["stops"]]
    if not ids:
        raise SystemExit("prompt fallback returned no stops")


def _note_explains_the_change() -> None:
    def propose(req, pool, errors):
        if not (req.get("baseline_brief") or "").strip():
            raise SystemExit("baseline was not shown to the model")
        return {
            "days": [["tyo-sensoji", "tyo-daikokuya", "tyo-kappabashi", "tyo-sometaro"]],
            "why": "Stay in Asakusa and walk the whole neighborhood.",
        }

    result = plan(
        {
            "city": "tokyo",
            "days": 1,
            "budget_yen": 40000,
            "party_size": 2,
            "max_walk_min": 12,
            "transport": "walk",
            "prompt": "想深度遊",
            "lang": "zh",
            "use_llm": True,
        },
        POIS,
        propose=propose,
    )
    thought = result.get("thought") or {}
    if result["mode"] != "llm" or "Asakusa" not in thought.get("why", "") and "淺草" not in thought.get("after", ""):
        raise SystemExit(f"thought missing: {thought}")
    if not thought.get("before") or not thought.get("after"):
        raise SystemExit(f"before/after missing: {thought}")


def main() -> None:
    failures = []
    agent_walk_breaks = 0
    baseline_walk_breaks = 0
    for case in scenarios():
        req = normalize_request(case["request"])
        result = plan(req, POIS)
        errors = check_plan(result, req, POIS)
        stops = sum(len(day["stops"]) for day in result["days"])
        if errors or stops < case["min_stops"]:
            failures.append(f"{case['name']}: stops {stops} expected >= {case['min_stops']} errors {errors}")
        ids = [stop["id"] for day in result["days"] for stop in day["stops"]]
        if "tyo-shibuya-trap" in ids:
            failures.append(case["name"] + ": kept the low-Tabelog cafe")
        agent_walk_breaks += _walk_violations(result, req)
        baseline_walk_breaks += _walk_violations(naive_plan(req), req)
    _llm_rejects_then_falls_back()
    _transit_reaches_another_area()
    _prompt_reaches_the_model()
    _note_explains_the_change()
    print(f"scenarios {len(scenarios())}")
    print(f"agent walk breaks {agent_walk_breaks}")
    print(f"baseline walk breaks {baseline_walk_breaks}")
    if failures:
        print("FAILED")
        print("\n".join(failures))
        raise SystemExit(1)
    if baseline_walk_breaks <= agent_walk_breaks:
        print("baseline did not walk farther than the agent")
        raise SystemExit(1)
    print("ok")


if __name__ == "__main__":
    main()
