/**
 * Lebanon Storm Watch — the background agent.
 *
 * Runs on a GitHub Actions runner (which, unlike a phone in a pocket, is always
 * awake and always online). Every few hours it:
 *   1. pulls the forecast for the places in config.json,
 *   2. scores it with THE SAME code the app uses — the scoring, the storm
 *      grouping and the advice are extracted live out of ../index.html between
 *      the ===SHARED===/===ENGINE===/===ADVISOR=== markers, so the two can
 *      never drift apart,
 *   3. opens a GitHub issue when a storm crosses your alert level, which is
 *      what actually reaches your phone (GitHub emails + pushes it),
 *   4. comments and closes that issue once the storm has passed.
 *
 * No API keys. No server. Node 20+ (built-in fetch).
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const cfg = JSON.parse(fs.readFileSync(path.join(here, "config.json"), "utf8"));
const HTML = fs.readFileSync(path.join(here, "..", "index.html"), "utf8");

/* ---------- borrow the app's own brain ---------- */
function block(name){
  const m = HTML.match(new RegExp("/\\* ===" + name + " START===[\\s\\S]*?\\*/([\\s\\S]*?)/\\* ===" + name + " END=== \\*/"));
  if(!m) throw new Error("marker block " + name + " not found in index.html");
  return m[1];
}
const BRAIN = new Function(
  "const cap = s => s.charAt(0).toUpperCase() + s.slice(1);" +
  "const document = undefined;" +
  block("SHARED") + block("ENGINE") + block("ADVISOR") +
  "; return {wmo, LEVELS, hourThreat, buildEvents, advise, summarise, bestWindow, ts, hh, dayName, dateShort, relTime, r1, compass};"
)();
const { wmo, LEVELS, buildEvents, advise, ts, hh, dayName, relTime, r1 } = BRAIN;

/* ---------- forecast ---------- */
const HOURLY = ["temperature_2m","apparent_temperature","relative_humidity_2m","precipitation_probability",
  "precipitation","rain","showers","snowfall","snow_depth","weather_code","pressure_msl","cloud_cover",
  "visibility","wind_speed_10m","wind_direction_10m","wind_gusts_10m","freezing_level_height","cape"].join(",");

async function forecast(p){
  const q = new URLSearchParams({
    latitude: p.lat, longitude: p.lon, hourly: HOURLY,
    current: "temperature_2m,weather_code,wind_speed_10m,wind_gusts_10m,pressure_msl",
    daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_gusts_10m_max",
    timezone: "Asia/Beirut", forecast_days: "7", wind_speed_unit: "kmh"
  });
  if(p.el != null) q.set("elevation", Math.round(p.el));
  const r = await fetch("https://api.open-meteo.com/v1/forecast?" + q);
  if(!r.ok) throw new Error("Open-Meteo HTTP " + r.status);
  return r.json();
}

function packHours(d, nowSite){
  const H = d.hourly, out = [];
  for(let i = 0; i < H.time.length; i++){
    const t = ts(H.time[i]);
    if(t < nowSite - 3600000) continue;
    out.push({ t, temp:H.temperature_2m[i], feels:H.apparent_temperature?.[i] ?? H.temperature_2m[i],
      rh:H.relative_humidity_2m?.[i] ?? null, pop:H.precipitation_probability?.[i] ?? null,
      precip:H.precipitation[i], rain:(H.rain?.[i] ?? 0) + (H.showers?.[i] ?? 0),
      snow:H.snowfall?.[i] ?? 0, depth:H.snow_depth?.[i] ?? null, code:H.weather_code[i],
      slp:H.pressure_msl?.[i] ?? null, cloud:H.cloud_cover?.[i] ?? null, vis:H.visibility?.[i] ?? null,
      wind:H.wind_speed_10m[i], dir:H.wind_direction_10m[i], gust:H.wind_gusts_10m[i],
      fl:H.freezing_level_height?.[i] ?? null, cape:H.cape?.[i] ?? null });
  }
  return out;
}

/* ---------- GitHub ---------- */
/* A manual run may lower the bar to prove the alerts actually reach a phone.
   Empty env vars (every scheduled run) fall through to config.json. */
const LEVEL   = Number(process.env.ALERT_LEVEL   || cfg.alertLevel   || 3);
const HORIZON = Number(process.env.HORIZON_HOURS || cfg.horizonHours || 48);
const DRILL   = !!(process.env.ALERT_LEVEL || process.env.HORIZON_HOURS);

const TOKEN = process.env.GITHUB_TOKEN;
const REPO  = process.env.GITHUB_REPOSITORY;
const OWNER = (cfg.notifyUser || (REPO || "/").split("/")[0] || "").trim();
const LABEL = "storm-watch";
const DRY   = !TOKEN || !REPO || process.env.DRY_RUN === "1";

async function gh(method, url, body){
  const r = await fetch(url.startsWith("http") ? url : "https://api.github.com/repos/" + REPO + url, {
    method,
    headers: { Authorization: "Bearer " + TOKEN, Accept: "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined
  });
  if(!r.ok) throw new Error(method + " " + url + " → " + r.status + " " + (await r.text()).slice(0,300));
  return r.json();
}
const openIssues = () => DRY ? [] : gh("GET", "/issues?state=open&labels=" + LABEL + "&per_page=100");

/* a stable id for one storm, so the same storm is never announced twice */
const sig = (place, e) => place.n.replace(/\s+/g,"_") + "|" + new Date(e.start).toISOString().slice(0,13) + "|" + e.kind;
const md  = html => String(html).replace(/<b>/g,"**").replace(/<\/b>/g,"**").replace(/<[^>]+>/g,"");

function issueBody(place, elevation, e, a, now, d){
  const L = LEVELS[e.peak];
  const lines = [];
  lines.push("## " + e.icon + " " + e.kind.toUpperCase() + " — " + L.n.toUpperCase());
  lines.push("");
  if(OWNER){ lines.push("@" + OWNER); lines.push(""); }
  lines.push("**" + place.n + "**, " + Math.round(elevation) + " m · `" + place.lat + ", " + place.lon + "`");
  lines.push("");
  lines.push("| | |");
  lines.push("|---|---|");
  lines.push("| Starts | " + dayName(e.start) + " " + hh(e.start) + " Beirut (**" + relTime(e.start, now) + "**) |");
  lines.push("| Lasts | " + e.durH + " hours, worst at " + hh(e.peakHour.t) + " |");
  if(e.rain >= 0.5) lines.push("| Rain | " + e.rain + " mm |");
  if(e.snow >= 0.3) lines.push("| Snow | **" + e.snow + " cm** at your height |");
  if(e.minFL < 99999) lines.push("| Snow line | " + Math.round(e.minFL) + " m |");
  lines.push("| Peak gust | " + e.maxGust + " km/h |");
  lines.push("| Coldest | " + e.minTemp + " °C |");
  if(e.tagList.length) lines.push("| Hazards | " + e.tagList.join(" · ") + " |");
  lines.push("");
  lines.push("### What to do");
  a.acts.filter(x => x.p >= 1).forEach(x => lines.push("- " + x.icon + " " + md(x.text) + (x.when ? "  _(" + x.when + ")_" : "")));
  const clear = a.acts.find(x => x.p === 0 && x.icon === "✅");
  if(clear){ lines.push(""); lines.push("> " + md(clear.text)); }
  lines.push("");
  lines.push("### Next 7 days");
  const D = d.daily;
  lines.push("| Day | Min/Max | Rain | Snow | Gust |");
  lines.push("|---|---|---|---|---|");
  for(let i = 0; i < D.time.length; i++){
    lines.push("| " + D.time[i].slice(5) + " | " + Math.round(D.temperature_2m_min[i]) + "/" +
      Math.round(D.temperature_2m_max[i]) + "°C | " + r1(D.precipitation_sum[i]) + " mm | " +
      r1(D.snowfall_sum[i]) + " cm | " + Math.round(D.wind_gusts_10m_max[i]) + " km/h |");
  }
  lines.push("");
  if(DRILL){
    lines.push("> ⚙️ **This is a test.** The run was started by hand with the alert level lowered to " +
      LEVEL + " and a " + HORIZON + " h horizon, to check that alerts reach your phone. " +
      "The weather in it is real; it is simply below the level you normally want to hear about. " +
      "Scheduled runs are unaffected.");
    lines.push("");
  }
  lines.push("<sub>Open-Meteo forecast, scored by Lebanon Storm Watch. Not an official warning — " +
             "for danger to life follow Civil Defence (125).</sub>");
  lines.push("");
  lines.push("<!-- storm-sig: " + sig(place, e) + " -->");
  return lines.join("\n");
}

/* Optional: ntfy.sh push straight to the phone. Set "ntfyTopic" in config.json
   and subscribe to the same topic in the ntfy app. No account, no key. The
   topic name is the only secret, so make it long and unguessable. */
async function ntfy(place, e, title){
  const topic = (cfg.ntfyTopic || "").trim();
  if(!topic) return;
  const body = [
    e.kind.toUpperCase() + " — " + place.n,
    "Starts " + dayName(e.start) + " " + hh(e.start) + " (" + relTime(e.start, Date.now()) + "), " + e.durH + " h",
    (e.rain >= 0.5 ? e.rain + " mm rain. " : "") + (e.snow >= 0.3 ? e.snow + " cm snow. " : "") +
      "Gusts " + e.maxGust + " km/h.",
    e.tagList.length ? e.tagList.join(" · ") : ""
  ].filter(Boolean).join("\n");
  try{
    const r = await fetch("https://ntfy.sh/" + encodeURIComponent(topic), {
      method: "POST",
      headers: {
        "Title": title.replace(/[^\x20-\x7E]/g, "").trim() || "Storm alert",
        "Priority": e.peak >= 4 ? "urgent" : "high",
        "Tags": e.peak >= 4 ? "rotating_light" : "warning",
        "Click": "https://enochwalks.github.io/lebanese-rap-archive/weather/"
      },
      body
    });
    console.log("  ntfy: " + (r.ok ? "sent" : "failed " + r.status));
  }catch(err){ console.log("  ntfy failed: " + err.message); }
}

/* ---------- main ---------- */
async function run(){
  const existing = await openIssues();
  const seen = new Set(existing.map(i => (i.body || "").match(/storm-sig: (.+?) -->/)?.[1]).filter(Boolean));
  const alive = new Set();
  let opened = 0;

  for(const place of cfg.places){
    const d = await forecast(place);
    const nowSite = Date.now() + (d.utc_offset_seconds || 0) * 1000;
    const elevation = d.elevation != null ? d.elevation : place.el;
    const hours = packHours(d, nowSite);
    const { scored, events } = buildEvents(hours, elevation);
    const a = advise(scored, events, elevation, place, nowSite);

    const worth = events.filter(e => {
      if(e.end <= nowSite) return false;
      const inH = (e.start - nowSite) / 3600000;
      if(e.peak >= 4 && !DRILL) return inH <= (cfg.severeHorizonHours ?? 96);
      return e.peak >= LEVEL && inH <= HORIZON;
    });

    console.log("· " + place.n + " @" + Math.round(elevation) + "m — " + events.length +
      " event(s), " + worth.length + " worth an alert. " + a.say.replace(/<[^>]+>/g, ""));

    for(const e of worth){
      const id = sig(place, e);
      alive.add(id);
      if(seen.has(id)){ console.log("  already announced: " + id); continue; }
      const title = (DRILL ? "[test] " : "") + e.icon + " " + LEVELS[e.peak].n + " " + e.kind +
                    " — " + place.n + ", " + dayName(e.start) + " " + hh(e.start);
      const body = issueBody(place, elevation, e, a, nowSite, d);
      if(DRY){ console.log("\n--- WOULD OPEN ISSUE ---\n" + title + "\n" + body + "\n"); }
      else {
        const issue = { title, body, labels: [LABEL] };
        if(OWNER) issue.assignees = [OWNER];
        try{ await gh("POST", "/issues", issue); }
        catch(err){                                  // assignee rejected? still send the alert
          if(!OWNER) throw err;
          console.log("  (could not assign to " + OWNER + ": " + err.message.slice(0,80) + ")");
          delete issue.assignees;
          await gh("POST", "/issues", issue);
        }
        await ntfy(place, e, title);
      }
      opened++;
    }
  }

  /* close what has blown over */
  for(const i of existing){
    const id = (i.body || "").match(/storm-sig: (.+?) -->/)?.[1];
    if(!id || alive.has(id)) continue;
    const startISO = id.split("|")[1];
    if(Date.parse(startISO + ":00:00Z") > Date.now() - 6*3600000) continue;  // still in play
    if(!DRY){
      await gh("POST", "/issues/" + i.number + "/comments", { body: "✅ This storm has passed. Closing." });
      await gh("PATCH", "/issues/" + i.number, { state: "closed", state_reason: "completed" });
    }
    console.log("closed #" + i.number);
  }
  console.log(DRY ? "\n(dry run — no issues were created)"
                  : "\ndone: " + opened + " new alert(s)" + (DRILL ? " [test run: level " + LEVEL + ", " + HORIZON + " h]" : ""));
}

run().catch(e => { console.error("AGENT FAILED:", e.message); process.exit(1); });
