# PromptWaver

![version](https://img.shields.io/badge/version-0.81.0-33e0d0)
![status](https://img.shields.io/badge/status-pre--release-orange)
![platform](https://img.shields.io/badge/platform-Ubuntu-informational)
![browser](https://img.shields.io/badge/browser-Chrome%20(WebGL2)-important)
![ai-code](https://img.shields.io/badge/AI%20coded-YES-orange)

> **Pre-release, active development.** Version stays 0.x until things settle;
> scene JSON shape and internal APIs may still change between minor versions.
> See [CHANGELOG.md](CHANGELOG.md) for what's landed.

A realtime **immersive audio/visual instrument**: an ambient light + synth instrument and soundscape explorer. Glowing vector line-art (strokes only, no fills) drawn by the browser's GPU on a projector or screen, with a polyphonic synth providing the sound, it's recomended you use a MIDI controller for a better experience.

Claude acts as an **scene creator**: give it a visual prompt ("water flowing from a blocked sieve", "aurora over a still lake at full moon") and an audio prompt ("scurrying beetles over a wet log with bees buzzing", "dripping water from stalactites in a cave with footsteps and fish") and it composes a scene spec. The local engine then renders at full framerate with no further API calls. Scenes are saved as JSON and can be loaded, tweaked, and re-rendered.

**Note:** You'll need a paid Claude API account to generate new scenes (~5–50¢ NZD each depending on size/model). A $5 credit goes a long way. Offline mode uses a simple fallback director.

Disclaimer: this has been coded almost completely with Claude Code, but with a person planning, orchestrating and UX testing to make this what it is, YMMV. 

![Main UI](/promptwaver-snap.png)

## Why

Theres a lack of integrated ambient AV tools out there, this came from a desire to have a portable and instant solution that doesn't require lugging around synths, midi gear and trying to organise onscreen DAWs and projection software at the same time.

This scene idea was from the frustration of seeing visuals at events that were being repeated multiple times in a performance. So making a stack of scenes to perform against, rather than mixin was the concept. Visually, I enjoy low polygon scene, which could be run without internet access (once generated online) and be projected.

Note: the roots of this reflected on a exploratory game on PS1 called 'Fluid' (1999) where you could build soundscapes while navigating a undersea psychedelic world (as a dolphin!).


## Features

- **AI-composed scenes** — Claude authors both visuals (3D geometry) and audio (synth patches)
- **Live mixer** — tweak camera, soundscape, effects in real time; save settings back to scenes
- **Authored visuals** — 3D worlds from a shape grammar, and flat symmetric 2D patterns (mandalas, lattices)
- **2D pattern scenes** — flat mandalas/kaleidoscopes composed in the frame, with per-shape glow
- **Full-featured synth** — pad/pluck/osc/noise voices, ADSR envelopes, arpeggiator, per-voice LFO, EQ, delay, distortion
- **MIDI control** — learn any control to a hardware knob; slots persist across scene changes
- **Dual output** — two browser output windows (`/output?out=1` and `?out=2`), each with its own keystone/flip
- **Monitor effects** — GPU bloom, trails, mirror, kaleidoscope, and an angular↔smooth line curve
- **3D immersive scenes** — orbit/drift/fly camera modes through composed worlds
- **Scene types** — every scene is 2D (flat, no camera) or 3D (navigable), badged in the library
- **Scene library** — save, load, crossfade, regenerate audio for existing visuals
- **Multi-model support** — choose Haiku (fast/cheap), Sonnet (better), or Opus (best)

## Install

### Browser requirement

The visual output is rendered with **WebGL2**, so both the control page and
the output/projector window need a browser that supports it with hardware
acceleration enabled — in practice **Chrome** (or another Chromium browser).
There is no Canvas2D fallback: on a browser without WebGL2 the canvas stays
black and the page prints the reason instead of drawing.

If the output window is black but the UI works, check `chrome://gpu` for
hardware acceleration, and the browser console for a `[PromptWaver]` message.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # numpy + aiohttp (required)
pip install pyo sounddevice anthropic    # optional: audio, mic reactivity, Claude
```

**pyo build fails with `portaudio.h: No such file or directory`**:
```bash
sudo apt install -y portaudio19-dev libsndfile1-dev libportmidi-dev liblo-dev build-essential
pip install pyo
```

**pyo incompatible with GCC 14**: either use GCC 12, or skip pyo entirely (pure numpy/sounddevice backend works fine):
```bash
sudo apt install gcc-12 g++-12
CC=gcc-12 CXX=g++-12 pip install --no-cache-dir pyo
```

## Run

```bash
# Browser preview, offline (no API calls until you generate)
python run.py

# With Claude API key (set ANTHROPIC_API_KEY env var first)
ANTHROPIC_API_KEY=sk-... python run.py

# Headless mode (no web interface)
python run.py --headless
```

Open <http://localhost:8080>, type a scene prompt, hit **Generate**. Tweak the look/sound, **Save** it, **Start** playback.

## Development

```bash
# VSCode opens directly into the project with F5-ready launch configs
# Python interpreter points at .venv; see .vscode/launch.json
code .
```

`.vscode/settings.json` configures the environment; `.vscode/launch.json` has presets for web-only (no audio hardware needed) and web+diagnostics. `.vscode/extensions.json` recommends Pylance and Ruff.

`pyproject.toml` holds Ruff/Black config (line length 100).

## Documentation

- **[TECHNICAL.md](TECHNICAL.md)** — Architecture, scene format, modulation matrix, audio subsystem, MIDI control details, performance tuning
- **[CHANGELOG.md](CHANGELOG.md)** — Version history and feature additions

## License

MIT — see [LICENSE](LICENSE).
