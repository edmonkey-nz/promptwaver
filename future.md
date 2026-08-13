# Backlog

## Done in 0.72.0

1) ~~when useign the 'regenerate audio' make it choose the current scene in the dropdown when first opened, and make sure the prompt field is empty.~~
2) ~~fix the status of the 'regenerating audio' throbber like the 'generating scene'~~
3) ~~when audio has been regenerated, close the modal and refrsh the scene so the new audio plays~~
4) ~~change the 'shape modulation' collasped pane title to '3D Scene modulation' and make it collasped if an 2d scene is open.~~
5) ~~change the 'modulation' pane title to '2D Scene modulation' and make it collasped if an 3d scene is open.~~
6) ~~update the new 2d paramter silders to allow midi mapping.~~

## Open

- **The 2D director prompt still lets polar motifs dominate.** Generated
  patterns read spidery rather than the crisp straight-line geometry of the
  reference images. Prompt tuning, not a code problem.
- **`max_strokes` is authored too low.** Claude picked 50 for `jupiter`, which
  expands to 133 — so 62% of the pattern never draws. The budget line in the
  2D prompt needs work.
- **Per-voice `waveform` / `arp` as modulation sources.** Deliberately not
  added: waveform is a discrete choice, so mapping it to a continuous param
  produces meaningless jumps. Arp rhythm is already covered by `voice.<name>`
  level. An honest version of this is a step sequencer, which is its own
  feature.
- **The 2D/3D panel split assumes the matrix is 2D-only.** It isn't — it also
  drives `camera.speed` and the other camera destinations on 3D scenes, and is
  now collapsed by default there. Revisit if that gets annoying.
- **`apply_audio_to_scene` doesn't update `spec.audio_prompt`**, so a scene's
  recorded prompt still shows the original after a regenerate.
- **The three procedural generators are still not AI-selectable.** Each
  director prompt names exactly one generator, so `flow_field`, `attractor`
  and `ripples` can be driven by hand but never chosen by Claude.
- **CHANGELOG has no entries for 0.31–0.70.** Not worth reconstructing.

a) check noise - doesnt fade out on muting (jupiter - space whsiper)
b) main eq's are too strong, make sliders less effecting.