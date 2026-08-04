future enchancements
1) DONE - add a 'global' section above the 3rd column (above 'audio diagnostics'). include a 'master audio' slider, and move the VU meter to this section, making these both vertical outputs and UI. include a 'Disable Audio' and and a 'Disable visuals' buttons (these both have a 2 second fade out). include the 'scene transitions' section in this section too - but remove the support text for this.
2)DONE-  add a new section 'Scene settings' under the new 'global' section in column 3. Add the 'Save Soundscape settings' button here, add a 'Save Camera settings' and a 'Save all scene settings' and a 'Save as' button which allows provides a modal popup and to specify a new name and saves the current scene config into it. add a then remove the 'Save soundscape' button from the primary soundscape section. remove everything under the 'generate scene' button in the scene section in the last comlumn in the last row.
3)DONE -  in the header area, change the indicator approach to this "Connections - [indicator light] Engine - [indicator light] Claude API" - so that the backend engine and API are simply shown.
4) add a oscilator mode (waveform options - sine etc, with effect) that can map to 2 options, a) the XXXXX a dropdown of objects/shapes inthe scene's json file - so that they could be scaled/morphed inde
5) add a 'hue' override in the global section. this overrides the color of the output's scene

6) MIDI control surface (DONE — see README "MIDI control")

Port the MIDI layer from the sibling laser-laser-laser project (SettingsStore.cc_map /
cc_mode / MidiInput). Camera and master-audio params are easy — they're already stable
string keys (camera.speed, master, eq.low, delay.mix). The instruments are the hard part
because voices are addressed BY NAME (voice.deep_bass.level) and the names are Claude's
invention, different in every generated scene.

  a) Map SLOTS, not names. The MIDI layer binds voice#0.level, voice#1.pan etc. and
     resolves the slot index against the current soundscape's voice list at the moment
     the CC arrives. Nothing in dsp.py or _apply_scape_param changes — the indirection
     lives entirely in midi.py.

  b) Make slots mean something by telling the director to emit voices in a consistent
     priority order (foundation/bass first, then pads, then leads/detail). Then CC 20 is
     always "the low end" on every scene ever generated, and muscle memory holds.

  c) Two-level storage, deliberately NOT one:
       settings.json  -> the persistent slot-based layout ("voice#0.level": 20). This is
                         the controller. Constant across every scene.
       scene JSON     -> optional name-based midi_overrides ("voice.shimmer_lead.pan": 47),
                         which win while that scene is loaded.
     Resolution per incoming CC: scene override -> global slot map -> unmapped.
     Storing the whole map per-scene was considered and rejected: it would change the
     controller layout under you on every scene load, which is the exact problem this
     is meant to solve.

  d) "Pin MIDI map to scene" button (Global section, under Disable Audio/Visuals) —
     freezes the currently-resolved slot->name assignment into the loaded scene as
     explicit overrides. Optional, for a scene dialled in ahead of a set; not something
     you have to press after every generate.

  e) Per-control learn icon (the sibling's approach): a small dim/bound/pulsing note
     button. Only two places need touching because every slider goes through one of
     them — knob() (all soundscape controls) and slider() (camera + monitor filters).

  f) Default encoder mode = catch (soft takeover) for voice params. Loading a scene
     replaces every level at once; with absolute mode the first knob you touch snaps
     the whole mix.

  g) PARAM_RANGES lives server-side, not shipped up from the browser, so MIDI keeps
     working with no UI open.

  Needs mido + python-rtmidi (neither currently in requirements.txt).

7) 3D render scaling (DONE)

Measured against the 45fps budget (22.2ms/frame): shipped scenes run 2.4-22.0ms p95,
median ~40% of budget — but ants.json is at 99% and already dropping frames. Scaling
node counts on the current code: 3x = 18.9ms (no margin left), 10x = 39.8ms (~25fps).

Cause: World.render3d transforms the ENTIRE world every frame before the camera can
cull any of it — 1580 Path3D objects at 10x — and the camera then keeps max_strokes
(130) of them. Profiling: render3d 41% of frame time, _project_lookat 57%, and ~92% of
the transformed geometry is discarded. Cost scales with how big the world IS, not with
how much can actually be drawn.

DONE. Bound the transform by the stroke budget instead of by world size. Node centres
are cheap (one vector each), so sort nodes by nearest-point distance, transform
near-to-far, and stop at ~3x max_strokes, with far-plane and view-cone rejects on top.
Measured (p95, interleaved A/B in one process, identical output in every case):

    1x  ( 35 nodes)   11.0ms -> 11.0ms   break-even
    3x  (105 nodes)   17.8ms -> 15.4ms   1.16x
    10x (350 nodes)   37.9ms -> 21.3ms   1.78x
    20x (700 nodes)   60.0ms -> 24.1ms   2.49x

Render cost goes roughly flat in world size, so 10-20x worlds are comfortable.

Two things that had to be right for output to stay identical (both were wrong first
time and caught by diffing every shipped scene frame-by-frame against the old path):
  - the far cull must measure depth ALONG THE VIEW AXIS, matching what the camera
    culls on. Euclidean distance is always the larger off-axis, so culling on it
    drops geometry the camera would have kept.
  - the view cone has to contain the frustum, which is a RECTANGLE — so the
    half-angle goes to the corner, not the middle of an edge. Testing against fov/2
    cuts the corners off the frame.

This also self-tunes for the dual-output rig (see 8): the bound is derived from
max_strokes, so raising detail for the projector automatically buys more geometry
through the transform, and dropping it for the laser automatically stops paying for it.

Further levers if ever needed:
  - 45% of nodes are motion:none, and bob/drift/pulse only change offset or uniform
    scale — only spin (6.5% of nodes) actually rotates geometry. World-space points
    could be cached and offset-added rather than re-multiplied every frame.
  - The per-Path transform loop could batch into one numpy op per node.

Camera tracking ("slow drive with random variations") is essentially free — Camera.update
is O(1) per frame and independent of scene size. Drift mode is currently three
fixed-ratio sinusoids (scene3d.py). Sum 2-3 incommensurate sinusoids per axis with a
per-scene seed, and let the target wander too, not just the position. Keep the _drift_t
integration exactly as-is — the comment above it explains why it matters once audio is
modulating speed.

Note: max_strokes is a hard laser constraint, not a software one. A 10x world doesn't
put 10x more on screen; it gives 10x more world to move through.

8) Output detail profiles (laser vs data projector)

The same rig gets used two ways, and the two want very different detail settings —
currently changed by hand on every switch:

    laser (Helios)     low camera.far ("draw depth"), low max_strokes
    HD data projector  much higher on both; it can take far more detail

These are per-output-device settings, not per-scene ones (like keystone), so they
shouldn't live in the scene file where they'd be overwritten on every scene load.
Proposal: a named output profile in settings.json holding far/max_strokes multipliers
(or absolute overrides) applied on top of whatever the scene specifies, with a
profile switch in the UI — so a scene authored on the projector still reads correctly
on the laser without editing the scene.

9) DONE - Add 'about' modal - needs to open and just render a 'about.md' file. provide a 'about' button in header region.
   about.md added at the project root; served by a /about route (read per request, so
   editing it shows up on the next open without a restart). "?" button beside the gear
   in the header. Rendered by a small markdown subset in index.html — headings, lists,
   rules, inline code/bold/italic/links — which escapes the source BEFORE adding any
   markup, since the result goes through innerHTML. Hard-wrapped lines join into one
   paragraph (or one bullet: a wrapped list item was breaking out of its <li> and
   continuing as a paragraph underneath, caught by looking at the rendered page).

10) DONE - slider values hidden - provide a checkbox in settings modal to hide all slider
    values (on/hidden by default)
    Settings > Display > "hide slider values", default on, kept in localStorage rather
    than settings.json — it is a per-browser cosmetic preference, not something that
    changes what the hardware does. Uses visibility (not display) so nothing reflows as
    it toggles, and the value reappears on hover/focus of its control, because hiding it
    outright makes fine adjustment guesswork.

10b) DONE - deeper bass / warmer synths (was: is this a prompt slider, lo-fi <> hi-fi?)
   Diagnosed before building: it was NOT a prompt problem. There is no filter anywhere
   in the synth, and `tone` was read only by pad/noise — on osc and pluck, the voices
   used for bass and leads, writing it did nothing. So no prompt could have fixed it.
   A resonant lowpass is ruled out by the module's founding constraint (no per-sample
   IIR, pure numpy can't vectorise one), so warmth is done additively instead:
   bandlimited wavetables built from a truncated harmonic series with a cutoff-shaped
   rolloff, cached and read back by phase. Aliasing 2.79% -> 0.00%; cost 2.2% of the
   audio callback budget. `tone` now works on every voice type, and the existing
   cold<>warm slider drives it — so no fourth slider was needed. See README "Tone".

11) build a audio EQ to paramater effector (eg bass levels affect speed or FoV)


12) build a instrument effector to a scenes shape (eg a 'base_drone' output (saw,adsr) affect a scale/rotate/positon of a shape ) - would need a UI element of dropdowns of  instrument and scenes shapes..

13) DONE - add a 'output ratio' in the settings, eg '1:1, 16:9, 16:10, 4:3' so users can
    set the render viewport ratio.
    Settings > Output, offering 1:1 / 4:3 / 16:10 / 16:9 / 21:9. Stored in settings.json
    as a rig property (like keystone) so it survives scene loads — _install_spec
    re-applies it to each freshly built camera. Widening it REVEALS more at the sides:
    fov stays the vertical angle and the horizontal one grows, so proportions hold.
    Applied to all three outputs so they agree — preview canvas reshapes, output windows
    letterbox into the window, and the DAC letterboxes into the galvos' square field
    (they scan a square whatever the ratio, so 1:1 is a laser's native shape).
    Found on the way: Camera.aspect existed but was hardcoded to 1.0 and
    _clip_and_project MULTIPLIED by it — backwards, would have shown less world on a
    wider screen. Harmless while it was always 1.0, which is why nobody noticed.
    _cone_cos in world.py had to flip to match, or the frustum cull would clip corners.

14) DONE(ish) - update the licence file - remove ref to laserflow too.
    There was no laserflow reference in LICENSE to remove — it read "Copyright (c) 2026
    Eddie" and nothing else. The actual laserflow reference is the GIT AUTHOR:
        user.name  = LaserFlow Dev
        user.email = dev@laserflow.local
    That stamps every commit, including all of this one's. Left alone deliberately —
    changing someone's git identity isn't a call to make for them. To change it:
        git config user.name "..."; git config user.email "..."
    (past commits keep the old author unless history is rewritten).
    LICENSE itself now names the project, and gained a safety notice making clear the
    AS-IS terms cover the beam as much as the code. Added THIRD_PARTY_LICENSES.md,
    which about.md already claimed existed — numpy/aiohttp required, sounddevice+
    PortAudio/anthropic/mido/python-rtmidi+RtMidi optional, Helios DAC SDK for hardware,
    Playwright dev-only, and the Claude API as a service rather than a bundled component.

15) DONE - build a LFO that can be applied per instrument and routed to an instrument's
    parameters: options: 'amount', 'osc type', 'speed' etc.
    Per-voice "lfo": {"on","dest","shape","rate","depth"} in the soundscape spec, so it
    travels with the scene and touches only its own voice (distinct from the global
    modulation matrix, which routes at engine level). Targets cover the three asked for
    — 'amount' = level, 'osc type' = waveform, 'speed' = rate — plus pan, tone, detune,
    sub. Shapes sine/triangle/saw/square/random (sample & hold, hashed from the cycle
    number so playback is reproducible).

    Rate range is 0-0.5Hz (dsp.LFO_MAX_RATE), clamped in the DSP as well as the UI and
    MIDI table so a hand-edited scene can't sit outside what the controls express; the
    useful range is 0.02-0.15Hz. Rate 0 freezes the LFO at its phase offset.

    The design is forced by the block size, not chosen: one audio block is 190-370ms depending on the
    configured blocksize (8192-16384), so anything evaluated once per block cannot
    represent an LFO faster than roughly 0.3-0.6Hz. So level and pan are applied as PER-SAMPLE arrays (measured
    tracking 0.05/0.15/0.3/0.5Hz exactly), while tone/detune/sub/waveform/rate select
    a wavetable or note schedule before the block renders and can only step between
    blocks. The UI says so under the controls; the director is told to keep those slow.

    Phase comes from the absolute sample clock — modulation is identical at any block
    size (0.99+ correlation between 2048 and 16384) and can't jump on a live edit.
    A level LFO is unipolar/downward-only so switching it on never exceeds the authored
    level. Cost with every voice modulated: 3.6% of the audio callback budget.

    On "sparingly": the prompt alone wasn't enough. Told at most two, the model obeyed
    on a neutral brief (2/4) but went to 4/5 on one leaning hard on movement. Tightening
    the wording got it to 3, so there is also a hard ceiling in code (_limit_lfos):
    never on the foundation voice, at most 3 total, later voices trimmed first, and it
    logs each trim rather than doing it silently.

16) add a 'show scene title' button to fade in the scenes title to the output monitor in bottom left corner for 10 secs 

17) write the settings used to make the scene into the json file, (image prompt, audio prompt, settings)