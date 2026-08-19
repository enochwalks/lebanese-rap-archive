"""
inspect.py -- render a project as a standalone HTML report.

An automated edit you cannot see is an automated edit you cannot judge. This
writes a single self-contained page showing the timeline from above: where
every shot sits, which source it came from, which beat it was cut on, and the
reason the program gave for each decision.

The page reads the *raw project JSON*, not a pre-digested summary, so the same
file also accepts any other project dropped onto it -- compare two seeds, or
open last week's render, without regenerating anything.

    from edit_engine.inspect import write_report
    write_report(project, sequence, "output/track.html")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .model import Project, Sequence

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg:#f6f7f9; --panel:#ffffff; --ink:#14171c; --muted:#5d6672; --line:#dfe3e9;
  --accent:#c8102e; --accent-soft:#c8102e22; --good:#12805c; --warn:#9a6700;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#0e1116; --panel:#161b22; --ink:#e7ebf0; --muted:#93a0b0; --line:#262d36;
    --accent:#ff5c73; --accent-soft:#ff5c7322; --good:#3fb984; --warn:#d8a531;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  --bg:#0e1116; --panel:#161b22; --ink:#e7ebf0; --muted:#93a0b0; --line:#262d36;
  --accent:#ff5c73; --accent-soft:#ff5c7322; --good:#3fb984; --warn:#d8a531;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3);
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
.wrap { max-width:1240px; margin:0 auto; padding:28px 20px 80px; }
h1 { font-size:1.5rem; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:1.05rem; margin:32px 0 12px; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:.9rem; margin:0 0 22px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
        box-shadow:var(--shadow); padding:16px 18px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
.stat .n { font-size:1.5rem; font-weight:650; letter-spacing:-.02em; }
.stat .l { color:var(--muted); font-size:.78rem; text-transform:uppercase;
           letter-spacing:.06em; margin-top:2px; }
.scroll { overflow-x:auto; }
.lane { position:relative; height:46px; margin:6px 0; border-radius:8px;
        background:color-mix(in srgb, var(--ink) 5%, transparent); min-width:640px; }
.lane-label { color:var(--muted); font-size:.75rem; text-transform:uppercase;
              letter-spacing:.07em; margin-top:10px; }
.clip { position:absolute; top:4px; bottom:4px; border-radius:5px; overflow:hidden;
        cursor:pointer; border:1px solid rgba(0,0,0,.18); font-size:.7rem;
        padding:3px 5px; color:#0d0f12; white-space:nowrap; }
.clip:hover, .clip.on { outline:2px solid var(--ink); outline-offset:1px; z-index:3; }
.clip.audio { background:#8a8f98 !important; color:#fff; }
.ruler { position:relative; height:22px; min-width:640px; border-bottom:1px solid var(--line); }
.tick { position:absolute; top:0; bottom:0; width:1px; background:var(--line); }
.tick span { position:absolute; top:2px; left:3px; font-size:.65rem; color:var(--muted); }
.beats { position:relative; height:12px; min-width:640px; margin-top:2px; }
.beat-tick { position:absolute; top:3px; width:1px; height:6px;
             background:color-mix(in srgb, var(--ink) 28%, transparent); }
.beat-tick.used { height:12px; top:0; width:2px; background:var(--accent); }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
th { text-align:left; font-weight:600; color:var(--muted); font-size:.72rem;
     text-transform:uppercase; letter-spacing:.06em; padding:8px 10px;
     border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel); }
td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
tr.on td { background:var(--accent-soft); }
tr:hover td { background:color-mix(in srgb, var(--ink) 4%, transparent); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem; }
.swatch { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:6px; }
.why { color:var(--muted); font-size:.8rem; }
.pill { display:inline-block; padding:1px 7px; border-radius:20px; font-size:.72rem;
        border:1px solid var(--line); }
.pill.hit { color:var(--good); border-color:color-mix(in srgb,var(--good) 40%,transparent); }
.pill.off { color:var(--warn); border-color:color-mix(in srgb,var(--warn) 40%,transparent); }
.warn { border-left:3px solid var(--warn); padding-left:12px; margin:8px 0;
        color:var(--warn); font-size:.85rem; }
.drop { border:1.5px dashed var(--line); border-radius:12px; padding:14px;
        text-align:center; color:var(--muted); font-size:.85rem; margin-top:28px; }
.drop.hot { border-color:var(--accent); color:var(--accent); }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin:10px 0 0; font-size:.8rem;
          color:var(--muted); }
.dir { border-left:3px solid var(--accent); padding:2px 0 2px 14px; margin:18px 0 0; }
.dir .name { font-size:1.15rem; font-weight:650; letter-spacing:-.01em; }
.dir .why { color:var(--muted); font-size:.9rem; margin-top:3px; }
.dir .params { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.tagpill { font-size:.72rem; padding:2px 9px; border-radius:20px; border:1px solid var(--line);
           color:var(--muted); }
.bar { display:inline-block; height:6px; border-radius:3px; background:var(--accent);
       vertical-align:middle; min-width:2px; }
.byline { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
          color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">Edit report</h1>
  <p class="sub" id="subtitle"></p>
  <div class="card stats" id="stats"></div>

  <div id="direction"></div>

  <h2>Timeline</h2>
  <div class="card">
    <div class="scroll">
      <div class="ruler" id="ruler"></div>
      <div class="beats" id="beats"></div>
      <div id="lanes"></div>
    </div>
    <div class="legend" id="legend"></div>
  </div>

  <div id="sources"></div>
  <div id="warnings"></div>

  <h2>Shot list &mdash; and why</h2>
  <div class="card scroll">
    <table>
      <thead><tr>
        <th>#</th><th>In</th><th>Dur</th><th>Source</th><th>Reads from</th>
        <th>Energy</th><th>Cut</th><th>Move</th><th>Reasoning</th>
      </tr></thead>
      <tbody id="shots"></tbody>
    </table>
  </div>

  <div class="drop" id="drop">Drop another <b>project.json</b> here to inspect it</div>
</div>
<script>
const EMBEDDED = __DATA__;
const PALETTE = ["#e8c468","#7fb3d5","#88c9a1","#e39aa8","#b79ae3","#e0a878",
                 "#8fd0c8","#d4d48a","#c9a2c9","#9fc0e8"];

function frac(t){ // {"value":"1001/30","rate":"30"} -> seconds
  if(!t) return 0;
  const p = s => { const [a,b] = String(s).split("/"); return b ? +a/+b : +a; };
  return p(t.value) / p(t.rate);
}
function tc(sec, fps){
  const f = Math.round(sec*fps), ff = f % Math.round(fps);
  const s = Math.floor(f/Math.round(fps));
  const pad = n => String(n).padStart(2,"0");
  return `${pad(Math.floor(s/3600))}:${pad(Math.floor(s/60)%60)}:${pad(s%60)}:${pad(ff)}`;
}
function esc(s){ return String(s??"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function render(project){
  const seq = project.sequences[0];
  const fps = (()=>{ const [a,b]=String(seq.rate).split("/"); return b? +a/+b : +a; })();
  const media = {}; (project.registry?.media||[]).forEach(m => media[m.media_id]=m);
  const analysis = seq.metadata?.analysis || {};

  const vTracks = seq.video_tracks||[], aTracks = seq.audio_tracks||[];
  const shots = (vTracks[0]?.clips)||[];
  const all = [...vTracks,...aTracks].flatMap(t=>t.clips||[]);
  const total = Math.max(...all.map(c=>frac(c.start)+frac(c.source_range.duration)/Math.abs(evalSpeed(c)) ),0)||1;

  function evalSpeed(c){ const [a,b]=String(c.speed||"1").split("/"); return b? +a/+b : +a; }
  function dur(c){ return frac(c.source_range.duration)/Math.abs(evalSpeed(c)); }

  const colorOf = {}; let ci=0;
  Object.keys(media).forEach(id => colorOf[id] = PALETTE[ci++ % PALETTE.length]);

  document.getElementById("title").textContent = project.name || "Edit report";
  document.getElementById("subtitle").textContent =
    `${seq.name} · ${seq.width}×${seq.height} · ${fps.toFixed(3).replace(/0+$/,"").replace(/\\.$/,"")} fps · ` +
    `${total.toFixed(2)}s · built by ${analysis.program||"unknown"}` +
    (analysis.seed!=null ? ` · seed ${analysis.seed}` : "");

  const lengths = shots.map(dur).sort((a,b)=>a-b);
  const median = lengths.length ? lengths[Math.floor(lengths.length/2)] : 0;
  const hasBeats = (analysis.beat_count||0) > 0;
  const onBeat = shots.filter(c=>c.metadata?.decision?.cut_on_beat).length;
  const stats = [
    [shots.length, "shots"],
    [median ? median.toFixed(2)+"s" : "—", "median shot"],
    [hasBeats && shots.length ? Math.round(onBeat/shots.length*100)+"%" : "—", "cuts on beat"],
    [analysis.beat_count ?? "—", "beats found"],
    [analysis.tempo_bpm ? analysis.tempo_bpm+" bpm" : "—", "tempo"],
    [Object.keys(media).length, "sources"],
  ];
  document.getElementById("stats").innerHTML = stats.map(([n,l])=>
    `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`).join("");

  // ruler
  const step = total>120?30: total>60?10: total>20?5:2;
  let ruler="";
  for(let t=0;t<=total;t+=step)
    ruler += `<div class="tick" style="left:${t/total*100}%"><span>${t}s</span></div>`;
  document.getElementById("ruler").innerHTML = ruler;

  // beat strip: every detected beat, with the ones actually cut on highlighted
  const cutAt = new Set(shots.map(c=>Math.round(frac(c.start)*1000)));
  document.getElementById("beats").innerHTML = (analysis.beats||[]).map(b=>{
    const used = cutAt.has(Math.round(b*1000));
    return `<div class="beat-tick ${used?"used":""}" style="left:${b/total*100}%"></div>`;
  }).join("");

  // lanes
  let lanes="";
  [...vTracks,...aTracks].forEach(track=>{
    if(!(track.clips||[]).length) return;
    lanes += `<div class="lane-label">${esc(track.name)}</div><div class="lane">`;
    track.clips.forEach((c,i)=>{
      const L = frac(c.start)/total*100, W = dur(c)/total*100;
      const isAudio = track.kind === "A";
      const idx = c.metadata?.decision?.shot;
      lanes += `<div class="clip ${isAudio?"audio":""}" data-clip="${esc(c.clip_id)}"`+
        ` style="left:${L}%;width:${Math.max(W,0.25)}%;background:${colorOf[c.media_id]||"#999"}"`+
        ` title="${esc(c.name)}">${idx?idx:""}</div>`;
    });
    lanes += `</div>`;
  });
  document.getElementById("lanes").innerHTML = lanes;

  const audioIds = new Set(aTracks.flatMap(t=>(t.clips||[]).map(c=>c.media_id)));
  document.getElementById("legend").innerHTML = Object.values(media).map(m=>
    `<span><i class="swatch" style="background:${
      audioIds.has(m.media_id) && !vTracks.some(t=>(t.clips||[]).some(c=>c.media_id===m.media_id))
      ? "#8a8f98" : colorOf[m.media_id]}"></i>${esc(m.name)}</span>`).join("");

  // what the director decided, and why
  const dir = analysis.direction;
  document.getElementById("direction").innerHTML = !dir ? "" : `
    <h2>Direction</h2>
    <div class="card">
      <div class="dir">
        <div class="byline">${dir.backend === "claude"
          ? "decided by Claude from the footage and the song"
          : "decided from measurements (no content vision)"}</div>
        <div class="name">${esc(dir.style_name)}</div>
        <div class="why">${esc(dir.rationale||"")}</div>
        <div class="params">
          <span class="tagpill">shot length ×${(+dir.shot_length_bias||1).toFixed(2)}</span>
          <span class="tagpill">in-points: ${esc(dir.prefer_windows||"")}</span>
          <span class="tagpill">ordering: ${esc(dir.ordering||"")}</span>
          <span class="tagpill">moves: ${(dir.preferred_moves||[]).map(esc).join(", ")}</span>
        </div>
      </div>
    </div>`;

  // what it saw in each source
  const srcs = analysis.sources || [];
  document.getElementById("sources").innerHTML = !srcs.length ? "" : `
    <h2>What it saw in the footage</h2>
    <div class="card scroll"><table>
      <thead><tr><th>Source</th><th>Subject</th><th>Type</th><th>Mood</th>
        <th>Pace</th><th>Motion</th><th>Notes</th></tr></thead>
      <tbody>${srcs.map(sd=>{
        const base = (sd.path||"").split(/[\\/]/).pop();
        const rank = +sd.motion_rank;
        return `<tr>
          <td>${esc(base)}</td>
          <td>${esc(sd.subject||"")}</td>
          <td>${esc(sd.scene_type||"")}</td>
          <td>${esc(sd.mood||"")}</td>
          <td>${esc(sd.pace||"")}</td>
          <td>${isFinite(rank)
              ? `<span class="bar" style="width:${Math.round(rank*60)+2}px"></span>
                 <span class="mono"> ${rank.toFixed(2)}</span>` : "—"}</td>
          <td class="why">${esc(sd.edit_notes||"")}</td>
        </tr>`; }).join("")}
      </tbody></table></div>`;

  // warnings
  const notes = [];
  if(!hasBeats) notes.push(
    "No beat data for this song, so cuts fell back to a fixed interval "+
    "&mdash; the edit is on a metronome, not on the music. Install librosa and re-run.");
  const offline = Object.values(media).filter(m=>!m.info);
  if(offline.length) notes.push(
    `${offline.length} source(s) offline: ${offline.map(m=>esc(m.name)).join(", ")}`);
  document.getElementById("warnings").innerHTML =
    notes.map(n=>`<div class="warn">${n}</div>`).join("");

  // shot table
  document.getElementById("shots").innerHTML = shots.map((c,i)=>{
    const d = c.metadata?.decision || {};
    const fx = (c.effects||[])[0];
    const src = media[c.media_id];
    const beat = !hasBeats ? `<span class="pill">no beat data</span>`
      : d.cut_on_beat ? `<span class="pill hit">on beat</span>`
      : `<span class="pill off">off beat</span>`;
    return `<tr data-clip="${esc(c.clip_id)}">
      <td class="mono">${d.shot ?? i+1}</td>
      <td class="mono">${tc(frac(c.start),fps)}</td>
      <td class="mono">${dur(c).toFixed(2)}s</td>
      <td><i class="swatch" style="background:${colorOf[c.media_id]}"></i>${esc(src?src.name:c.media_id)}</td>
      <td class="mono">${frac(c.source_range.start_time).toFixed(2)}s</td>
      <td>${d.music_energy==null ? "—" :
          `<span class="bar" style="width:${Math.round(d.music_energy*40)+2}px"></span>
           <span class="mono"> ${(+d.music_energy).toFixed(2)}</span>`}</td>
      <td>${beat}</td>
      <td>${fx?esc(fx.kind):"—"}</td>
      <td class="why">${esc(d.effect_reason||"")}${d.source_reason?"<br>"+esc(d.source_reason):""}${
          d.source_in_reason?"<br>"+esc(d.source_in_reason):""}</td>
    </tr>`;
  }).join("");

  // link table rows and timeline blocks both ways
  const sync = id => {
    document.querySelectorAll("[data-clip]").forEach(el=>
      el.classList.toggle("on", el.dataset.clip===id));
  };
  document.querySelectorAll("[data-clip]").forEach(el=>{
    el.addEventListener("mouseenter", ()=>sync(el.dataset.clip));
    el.addEventListener("click", ()=>{
      sync(el.dataset.clip);
      document.querySelector(`tr[data-clip="${el.dataset.clip}"]`)
        ?.scrollIntoView({block:"center", behavior:"smooth"});
    });
  });
}

render(EMBEDDED);

const drop = document.getElementById("drop");
["dragenter","dragover"].forEach(e=>drop.addEventListener(e,ev=>{
  ev.preventDefault(); drop.classList.add("hot"); }));
["dragleave","drop"].forEach(e=>drop.addEventListener(e,ev=>{
  ev.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", ev=>{
  const file = ev.dataTransfer.files[0]; if(!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try { render(JSON.parse(reader.result)); }
    catch(err){ drop.textContent = "Could not read that file: "+err.message; }
  };
  reader.readAsText(file);
});
</script>
</body>
</html>
"""


def build_report_html(project: Project, sequence: Optional[Sequence] = None,
                      title: Optional[str] = None) -> str:
    """Return a standalone HTML page describing the project."""
    data: Dict[str, Any] = project.to_dict()
    if sequence is not None:
        # Put the sequence of interest first; the page reads sequences[0].
        chosen = sequence.sequence_id
        data["sequences"].sort(key=lambda s: s["sequence_id"] != chosen)
    page_title = title or project.name or "Edit report"
    return (_PAGE
            .replace("__TITLE__", page_title.replace("<", "&lt;"))
            .replace("__DATA__", json.dumps(data)))


def write_report(project: Project, sequence: Optional[Sequence] = None,
                 path: str | Path = "edit-report.html",
                 title: Optional[str] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report_html(project, sequence, title), encoding="utf-8")
    return path
