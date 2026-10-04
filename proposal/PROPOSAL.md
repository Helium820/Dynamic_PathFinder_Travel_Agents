# Japan Day Agent

## 1. Group members

| Name | Student ID |
| --- | --- |
| | |
| | |

Contact member:

## 2. Project objective

Build an agent that plans one to three days in Tokyo, Kyoto, or Osaka. The user sets a budget, a party size, a maximum walk, and minimum Google and Tabelog ratings. The agent returns a timed route that stays inside those limits, plus the places it rejected and why.

## 3. Motivation and challenges

A normal maps app can list highly rated places. It will still produce a day that walks too far, misses a meal, or spends the budget on one expensive dinner. The useful step is the repair: drop the stop that breaks a rule and pick another.

Challenges: Tabelog has no public API, live map calls make a demo and a test set unreproducible, and a walking limit has to be checked in code rather than trusted to the model.

## 4. Potential solutions

The page at `web/index.html` collects the constraints. `server.py` calls `planner.py`. For each day the agent picks a morning anchor, then fills lunch, afternoon stops, and dinner. A stop is kept only when the rating floors, opening hours, walk from the previous stop, walk from the morning anchor, and the remaining budget all pass. A restaurant over ¥8,000 a person is skipped so one splurge cannot eat the day. If a later day cannot be built, the agent removes the priciest stop from the day before and tries again.

The place list is a frozen cache in `data/pois.json`: neighborhood coordinates, a sample Google rating, a sample Tabelog rating for restaurants, and a sample price. The demo does not scrape Tabelog or call Google. An optional model path can propose place ids; the same checker rejects an illegal draft and falls back to the rules agent.

The interface is a phone-layout web page with an OpenStreetMap view. That is the mobile app for the demo. A separate Flutter client would not change the agent.

## 5. Experimental dataset and evaluation plan

`eval.py` runs 36 scenarios across the three cities: comfortable budgets, a 12-minute walk, a budget too small for a meal, a Tabelog floor of 3.7, a Google floor of 4.6, temple and theme-park filters, and a three-day trip. A plan fails if it breaks the budget, the walk, a rating floor, or opening hours. The same file compares a one-shot baseline that picks the highest Google rating and ignores distance. The agent should have zero walk breaks. The baseline should have many.

Known limits: the cache is small and the ratings are samples, a new neighborhood needs new rows, and the route is a slot filler rather than a full transit timetable. Live Tabelog or Google refresh is a later tool behind the same checker.
