- Health: `run edit-engine install-dev` once, then `run edit-engine tests`. Needs ffmpeg/ffprobe.
- To use it in the daily pipeline, in rap-archive's `main.py` change
  `import video_builder` to `import engine_video_builder as video_builder` and set
  `VIDEO_STYLE` to "clips" or "kenburns". "combo"/"visualizer" are NOT supported by the
  engine; it refuses rather than rendering something different. Undo = revert that line.
- Claude vision (`edit_engine/analysis/vision.py`) is optional: no `ANTHROPIC_API_KEY`
  -> it directs the edit from measured motion/exposure/music energy instead. Model can
  be changed with env `EDIT_ENGINE_MODEL`.
- Lives on branch `claude/video-editing-engine-5psfw3`, not merged into master.
