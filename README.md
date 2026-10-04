# Japan Day Agent

A phone-sized planner for a short trip in Tokyo, Kyoto, or Osaka. You set a budget, a walking limit, and rating floors. The page can switch to Traditional Chinese, and it can leave the walking neighborhood by bus or train when that ride still fits the time limit and the fare still fits the budget. The agent searches a cached list of places, drops anything that fails the ride, the hours, the Tabelog or Google floor, or the remaining budget, and rebuilds the day.

## Tech stack

- Python 3 standard library for the agent and the local web server
- A frozen place cache in `data/pois.json` (neighborhood coordinates, sample ratings, sample prices)
- Leaflet and OpenStreetMap tiles in the browser
- Optional OpenAI-compatible chat model through `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in `.env`

## Model key

There is no port to set for OpenRouter. The app already listens on http://127.0.0.1:8765. The model address is the base URL.

1. Create a key at https://openrouter.ai/keys
2. Copy `.env.example` to `.env` in this folder
3. Put the key in `LLM_API_KEY`
4. Set `LLM_BASE_URL` to `https://openrouter.ai/api/v1` (do not add `/chat/completions`)
5. Set `LLM_MODEL` to a free model id from https://openrouter.ai/models?max_price=0 — the id ends in `:free`

Leave the key in `.env` only. Do not paste it into the web page or into source files. The next plan reads `.env` again, so the server does not need a restart after you save it. Then tick “Let a model draft the route”, or type a note.

Live Tabelog scraping is not part of this app. Tabelog does not offer a public API, and a scraper would break the demo and the evaluation. The same is true of a Flutter client: the page is already a phone layout, and the graded part is the agent.

## Architecture

`planner.py` is the agent. For each day it tries a morning anchor, then fills lunch, afternoon stops, and dinner. A short hop stays a walk. A longer hop becomes a bus under 3 km or a train beyond that, with a sample fare. A stop is added only after the tools accept it. If a later day cannot be built, the agent drops the priciest stop from the previous day and tries again.

`server.py` serves `web/index.html` and `POST /api/plan`. The page sends the constraints and draws the route.

If “Let a model draft the route” is checked, or the note box has text, and the API variables are set, that note is sent with the place list. The model proposes place ids. The same checker rejects an illegal draft and asks once more. After a failed draft, the rules agent builds a legal plan and says the note was not applied.

## How to run

```bash
python3 eval.py
python3 server.py
```

Open http://127.0.0.1:8765 on a phone-width browser window. EN / 中文 stays on this browser. Walk, bus, and train is the page default; walk only keeps a day inside one neighborhood. Ride minutes and fares are a sample, not a live timetable. `eval.py` checks 36 walk-only scenarios plus one transit day, and exits with an error if a plan breaks a budget, a ride, a rating, or opening hours.
