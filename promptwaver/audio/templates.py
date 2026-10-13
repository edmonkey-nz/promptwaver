"""Starting voices for "+ instrument": one playable voice of any type, in the
key of the scene it is being added to, with no API call.

A voice needs very little from the scene it joins. Its swell phase, envelope,
`voice.<name>` modulation source and MIDI slot all set themselves up when it
is added (dsp.Soundscape.add_voice). The one real relationship is the KEY:
anything that plays its own pitches has to agree with the voices already
there. That is derived here from the scene's own voices:

  1. a voice that plays a scale (pluck/bell/harp/fm, or tone grains): its
     root pitch class and its scale, as authored;
  2. else the lowest held voice's root, with a scale built from the pads'
     chord tones — a pentatonic of the chord's quality (major if it has a
     major third, minor if a minor one, the third-less "suspended" one if
     neither, which can't clash with either);
  3. else A minor pentatonic.

`granular` on its default source needs none of it: it is made of the other
voices, so it is in key by construction.
"""

from __future__ import annotations

ADDABLE_TYPES = ("pad", "osc", "sub", "pluck", "bell", "harp", "fm", "granular", "noise")

_SCALED = ("pluck", "bell", "harp", "fm")
_HELD = ("pad", "osc", "sub")

_PENTA = {"major": [0, 2, 4, 7, 9], "minor": [0, 3, 5, 7, 10], "sus": [0, 2, 5, 7, 10]}

#: Lowest MIDI note each type's root is placed at or above. Mirrors the
#: registers the director is told to use, so an added voice sits where a
#: generated one would.
_REGISTER = {"sub": 24, "pad": 36, "osc": 48, "harp": 55, "fm": 57, "pluck": 67,
             "bell": 72, "granular": 72}

#: Starting points, chosen to sound like the type's most characteristic use
#: rather than its defaults. Levels are deliberately modest: the voice joins
#: a mix that was already balanced without it.
_TEMPLATES = {
    "pad": {"waveform": "saw", "tone": 0.35, "detune": 0.012, "level": 0.28,
            "env": {"attack": 6.0, "decay": 2.0, "sustain": 0.85, "release": 6.0}},
    "osc": {"waveform": "saw", "unison": 3, "detune": 0.012, "tone": 0.4, "level": 0.22,
            "env": {"attack": 3.0, "decay": 1.5, "sustain": 0.8, "release": 4.0}},
    "sub": {"waveform": "sine", "tone": 0.2, "level": 0.32, "reverb": 0.1,
            "env": {"attack": 4.0, "decay": 2.0, "sustain": 0.9, "release": 5.0}},
    "pluck": {"waveform": "triangle", "tone": 0.5, "rate": 0.5, "decay": 1.4,
              "drift": 0.25, "level": 0.2},
    "bell": {"character": "celesta", "tone": 0.6, "rate": 0.25, "decay": 2.5,
             "drift": 0.2, "level": 0.2},
    # No `gap`, though a gapped harp is the idiomatic setting: a gap aligns
    # the first roll to a (beat + gap) grid, so a just-added harp could sit
    # silent for ~14s and read as "add did nothing". Turn gap up from here.
    "harp": {"tone": 0.6, "rate": 0.3, "decay": 8.0, "damp": 0.7, "roll": 6,
             "roll_spread": 0.07, "drift": 0.2, "level": 0.24},
    "fm": {"ratio": 1.0, "index": 2.5, "index_decay": 0.4, "decay": 2.0, "rate": 0.5,
           "drift": 0.2, "level": 0.22},
    "granular": {"source": "scene", "pitch": 12, "density": 20, "grain": 0.15,
                 "jitter": 0.2, "scatter": 1.5, "level": 0.3},
    "noise": {"tone": 0.4, "level": 0.1},
}


def scene_key(voices: list[dict]) -> tuple[int, list[int]]:
    """(root pitch class 0-11, scale as semitones from the root)."""
    for v in voices:
        t = v.get("type")
        if t in _SCALED or (t == "granular" and v.get("source") == "tone"):
            try:
                scale = sorted({int(x) % 12 for x in (v.get("scale") or [])}) or _PENTA["minor"]
                return int(v.get("note", 69)) % 12, scale
            except (TypeError, ValueError):
                pass
    held = [v for v in voices if v.get("type") in _HELD and isinstance(v.get("note"), (int, float))]
    if held:
        low = min(held, key=lambda v: v["note"])
        tones = set()
        for v in held:
            off = int(v["note"]) - int(low["note"])
            for c in v.get("chord") or [0]:
                try:
                    tones.add((off + int(c)) % 12)
                except (TypeError, ValueError):
                    pass
        quality = "major" if 4 in tones else "minor" if 3 in tones else "sus"
        return int(low["note"]) % 12, _PENTA[quality]
    return 9, _PENTA["minor"]


def _chord(voices: list[dict], vtype: str) -> list[int]:
    """A held voice's chord: the scene's own pad/osc chord if it has one, so
    a second pad doubles the harmony rather than inventing another."""
    if vtype == "sub":
        return [0]
    for v in voices:
        if v.get("type") in ("pad", "osc") and v.get("chord"):
            return [int(c) for c in v["chord"]][:6]
    return [0, 7, 12] if vtype == "pad" else [0, 7]


def new_voice(vtype: str, voices: list[dict]) -> dict:
    """A ready-to-play voice of `vtype` for a scene whose voices are `voices`.
    Raises ValueError for a type that can't be added."""
    if vtype not in ADDABLE_TYPES:
        raise ValueError(f"unknown instrument type {vtype!r}")
    v = {"name": vtype, "type": vtype}
    v.update({k: (dict(x) if isinstance(x, dict) else x) for k, x in _TEMPLATES[vtype].items()})
    if vtype in _REGISTER:
        pc, scale = scene_key(voices)
        base = _REGISTER[vtype]
        v["note"] = base + (pc - base) % 12
        if vtype in _HELD:
            v["chord"] = _chord(voices, vtype)
        else:
            v["scale"] = scale
    # Spread across the stereo field instead of stacking in the centre.
    v["pan"] = (0.25, -0.25, 0.4, -0.4, 0.1, -0.1)[len(voices) % 6]
    if vtype == "sub":
        v["pan"] = 0.0          # the low end stays centred
    return v
