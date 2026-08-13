# PromptWaver

![version](https://img.shields.io/badge/version-0.72.0-33e0d0)
![status](https://img.shields.io/badge/status-pre--release-orange)
![platform](https://img.shields.io/badge/platform-Ubuntu-informational)
![ai-code](https://img.shields.io/badge/AI%20coded-YES-orange)

> **Pre-release, active development.** Version stays 0.x until things settle;
> scene JSON shape and internal APIs may still change between minor versions.
> See [CHANGELOG.md](CHANGELOG.md) for what's landed.

A realtime **immersive audio/visual instrument** ambient scene and soundscape explorer. Procedural vector visuals stream to a laser (ILDA) or second monitor, and a polyphonic synth provides the sound, it's recomended you use a MIDI controller for a better experience.

Claude acts as an **scene creator**: give it a visual prompt ("water flowing from a blocked sieve", "aurora over a still lake at full moon") and an audio prompt ("scurrying beetles over a wet log", "dripping water from stalactites in a cave") and it composes a scene spec. The local engine then renders at full framerate with no further API calls. Scenes are saved as JSON and can be loaded, tweaked, and re-rendered.

**Note:** You'll need a paid Claude API account to generate new scenes (~5–40¢ NZD each depending on size/model). A $5 credit goes a long way. Offline mode uses a simple fallback director.

Disclaimer: this has been coded almost completely with Claude Code, but with a person planning, orchestrating and UX testing to make this what it is, YMMV. 

![Main UI](/promptwaver-snap.png)

## Why

Theres a lack of integrated ambient AV tools out there, this came from a desire to have a portable and instant solution that doesn't require lugging around synths, midi gear and trying to organise onscreen DAWs and projection software at the same time.

This scene idea with the frustration of seeing visuals at events that was repeated came to the idea of a immersive low polygon scene, which could be run without internet access (once generated online).

Note: the roots of this reflected on a exploratory game on PS1 called 'Fluid' (1999) where you could build soundscapes while navigating a undersea psychedelic world (as a dolphin!).


## Features

- **AI-composed scenes** — Claude authors both visuals (3D geometry) and audio (synth patches)
- **Live mixer** — tweak camera, soundscape, effects in real time; save settings back to scenes
- **Procedural visuals** — flow fields, attractors, ripples, hand-authored geometry via shape grammar
- **2D pattern scenes** — flat mandalas/kaleidoscopes composed in the frame, with per-shape glow
- **Full-featured synth** — pad/pluck/osc/noise voices, ADSR envelopes, arpeggiator, per-voice LFO, EQ, delay, distortion
- **MIDI control** — learn any control to a hardware knob; slots persist across scene changes
- **Dual output** — laser + data projector simultaneously with independent keystone/flip per screen
- **Monitor effects** — glow, trails, kaleidoscope (display-only, laser unaffected)
- **3D immersive scenes** — orbit/drift/fly camera modes through composed worlds
- **Scene types** — every scene is 2D (flat, no camera) or 3D (navigable), badged in the library
- **Scene library** — save, load, crossfade, regenerate audio for existing visuals
- **Multi-model support** — choose Haiku (fast/cheap), Sonnet (better), or Opus (best)

## Install

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

### Helios DAC (optional, for laser hardware)

```bash
sudo apt install -y libusb-1.0-0-dev build-essential git
git clone https://github.com/Grix/helios_dac.git
cd helios_dac/sdk/cpp
g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDacAPI.cpp HeliosDac.cpp
g++ -shared -o libHeliosDacAPI.so *.o -lusb-1.0
```

Install system-wide:
```bash
sudo cp libHeliosDacAPI.so /usr/local/lib/ && sudo ldconfig
```

udev permission:
```bash
sudo tee /etc/udev/rules.d/60-heliosdac.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="1209", ATTR{idProduct}=="e500", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Run

```bash
# Browser preview, offline (no API calls until you generate)
python run.py

# With Claude API key (set ANTHROPIC_API_KEY env var first)
ANTHROPIC_API_KEY=sk-... python run.py

# With laser hardware
python run.py --laser --pps 11000 --max-step 0.03 --invert-x

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

`.vscode/settings.json` configures the environment; `.vscode/launch.json` has presets for web-only (no audio/laser needed), web+laser, and web+diagnostics. `.vscode/extensions.json` recommends Pylance and Ruff.

`pyproject.toml` holds Ruff/Black config (line length 100).

## Documentation

- **[TECHNICAL.md](TECHNICAL.md)** — Architecture, scene format, modulation matrix, audio subsystem, MIDI control details, performance tuning
- **[CHANGELOG.md](CHANGELOG.md)** — Version history and feature additions

## License

MIT — see [LICENSE](LICENSE).
