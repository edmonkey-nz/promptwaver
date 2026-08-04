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