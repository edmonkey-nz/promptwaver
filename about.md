# PromptWaver

A realtime immersive audio/visual instrument for the Helios laser DAC.

Type a phrase. Claude composes a 3D world and an ambient soundscape to match.
The engine renders both live, coupled through a shared modulation matrix, so
the light and the sound move together rather than merely playing at the same
time.

Created with Claude, with a human in the loop directing the features, the
bugs and the feel.

## What it does

- **Prompt-composed 3D worlds.** Claude authors a scene graph — a small
  library of named shapes, instanced many times, placed and coloured and set
  moving. The engine flies a camera through it and projects to vectors.
- **A soundscape per scene.** A polyphonic pad/pluck/noise synth, composed to
  fit the same prompt. Voices, envelopes, an arpeggiator, delay and EQ.
- **Sound and light coupled.** Modulation routes tie the audio to the visuals
  — the classic one being the level driving camera speed, so the world moves
  with the music.
- **MIDI control.** Hardware knobs onto the camera, the master chain and the
  per-voice mix. Voice bindings follow slot *positions*, not names, so they
  survive a scene change.
- **Two ways to look at it.** A browser preview for composing, and chrome-less
  output windows to drag onto a projector or second screen — with keystone
  correction for an off-axis laser.

## Scene sizes

**small** through **large** change how far apart things are placed.
**massive** is different in kind: it raises the object count as well, composes
the scene as a closed route, and gives the camera a path that walks it. Minutes
of travel rather than a tableau.

## Safety

The laser output is off by default and stays off until explicitly turned on,
independently of whether the engine is running. Stop cuts the beam on the same
tick it is pressed — only the audio fades. Never point a laser at people,
mirrors, or aircraft, and check the rules where you live.

## Credits

Built on the Helios DAC — <https://bitlasers.com/helios-laser-dac/>

Scene direction by the Claude API. Audio is pure numpy and sounddevice; there
is no DAW, no sample library and no game engine underneath any of this.

Licence and third-party notices are in the repository.
