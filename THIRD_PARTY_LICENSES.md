# Third-party components

PromptWaver itself is MIT licensed (see [LICENSE](LICENSE)). It does not
vendor or redistribute any of the following — they are installed separately
by `pip` and remain under their own terms. This list is here so you know what
you are pulling in and under what conditions.

## Required

| Component | Licence | Used for |
|---|---|---|
| [NumPy](https://numpy.org) | BSD-3-Clause | all geometry and audio DSP |
| [aiohttp](https://docs.aiohttp.org) | Apache-2.0 | the web control surface and its websocket |

## Optional

Each unlocks a feature; without it the app runs with that feature disabled
rather than failing.

| Component | Licence | Used for |
|---|---|---|
| [sounddevice](https://python-sounddevice.readthedocs.io) | MIT | audio output and mic input |
| [PortAudio](https://www.portaudio.com) | MIT | the system audio layer sounddevice binds to |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT | the Claude scene director |
| [mido](https://mido.readthedocs.io) | MIT | MIDI message parsing |
| [python-rtmidi](https://github.com/SpotlightKid/python-rtmidi) | MIT | MIDI port I/O |
| [RtMidi](https://github.com/thestk/rtmidi) | MIT (with an attribution request) | the C++ library python-rtmidi wraps |

## Hardware

| Component | Licence | Used for |
|---|---|---|
| [Helios DAC SDK](https://bitlasers.com/helios-laser-dac/) | MIT | `libHeliosDacAPI.so`, the laser DAC interface |

`libHeliosDacAPI.so` is **not** included in this repository — see the README's
install section for building or obtaining it. It is a product of Gitle Mikkelsen
/ Bitlasers.

## Development only

Not required to run PromptWaver, and not a dependency of any shipped code
path.

| Component | Licence | Used for |
|---|---|---|
| [Playwright](https://playwright.dev/python/) | Apache-2.0 | driving a real browser in UI tests |

## Services

The scene director calls the [Claude API](https://www.anthropic.com), which is
a paid service under Anthropic's own terms, not a bundled component. It is
entirely optional: with no API key the app falls back to a local keyword
mapping and never touches the network.

---

Licence identifiers above are recorded as of the last update to this file.
Upstream projects can relicense, so check the version you actually install if
that matters for your use.
