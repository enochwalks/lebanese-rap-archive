# Lebanon Storm Watch · مرصد العواصف

A single-file weather app built for the Lebanese mountains: it tells you **what storm is
coming, when it arrives, how bad it will be, and whether it falls as rain, sleet or snow
at your own altitude**.

## Run it

* **On your phone / laptop:** open `index.html` in any browser. Nothing to install, no API key,
  no account. On iPhone use Share → *Add to Home Screen* and it behaves like an installed app.
* **From anywhere (recommended):** turn on GitHub Pages for this repo
  (Settings → Pages → Branch: `main`, folder `/root`), then visit
  `https://<your-user>.github.io/<repo>/weather/`.

## What it does

| Panel | What it gives you |
|---|---|
| Storm outlook | The next storm, a countdown to it, its peak severity and how long it lasts |
| Right now | Temperature, wind + gusts, humidity, cloud, and the 3-hour pressure trend (falling fast = a system is closing in) |
| Mountain conditions | Snow line vs. **your** elevation, rain/sleet/snow call, new snow in 24 h and 72 h, snow already on the ground, coldest hour, strongest gust, and how many icy-road hours are coming |
| Next 48 hours | Hour-by-hour strip, colour-coded by threat, with a gust bar under each hour |
| Incoming storms | Every storm event in the next 10 days, grouped and ranked, with rain/snow totals and hazard tags |
| 10-day outlook | Daily summary |
| Live rain radar | Animated radar, past 2 h plus a 30-minute nowcast, centred on you |
| Across Lebanon | The worst day of the coming week for ten regions, coast to Hermon |

Locations cover the mountain villages (Bcharre, Cedars, Hasroun, Faraya, Laqlouq, Sannine,
Barouk, Rachaya, Qammoua, Jezzine…), plus GPS ("My spot") and a search box for any village.
Passing your village's real elevation makes the model downscale to your ridge instead of
averaging the whole valley — that is what gets the snow line right.

## Storm levels

`Calm → Watch → Advisory → Warning → Severe → Extreme`, scored per hour from wind gusts,
rain rate, thunderstorm and hail codes, snowfall rate, freezing rain, the freezing-level
height relative to your altitude, visibility and CAPE (storm energy). Adjacent threat hours
are merged into one named event (thunderstorm, snowstorm, blizzard, ice storm, windstorm,
rainstorm, fog).

## Data

* Forecast — [Open-Meteo](https://open-meteo.com) (ICON / ECMWF / GFS blend), free, no key.
* Radar — [RainViewer](https://rainviewer.com). Basemap — OpenStreetMap / CARTO.

These levels are computed by this app from raw model output. They are **not** official
warnings. For life-threatening weather follow Lebanon's Civil Defence (125) and the
Beirut Meteorological Service.
