"""The GPU path's twin test, half 1: Python's own frames as ground truth.

web/static/scene-eval.js re-implements the Python renderer in the browser
(motion, the max_strokes budget, projection, clipping, depth colour, gains).
This writes, for every library scene plus targeted variants, the packed
geometry the browser gets, the snapshots the engine would publish, and the
frame Python itself draws for each — then twin_test.js runs scene-eval.js on
the same inputs and compares. Run both after ANY change to scene-eval.js or
to the Python code it mirrors (see that file's header):

    .venv/bin/python tools/gpu_twin/twin_gen.py /tmp/pw_twin 20
    node tools/gpu_twin/twin_test.js /tmp/pw_twin promptwaver/web/static/scene-eval.js

Writes only under OUTDIR; reads scenes/ but never saves anything.
"""
import colorsys, copy, glob, json, os, sys, tempfile

sys.path.insert(0, os.getcwd())
import numpy as np  # noqa: E402
from promptwaver.scenes import Scene, SceneSpec, SceneManager  # noqa: E402
from promptwaver.modulation import ModMatrix  # noqa: E402
from promptwaver.gpu_pack import pack_test_pattern  # noqa: E402
from promptwaver.geometry import test_pattern_frame  # noqa: E402

OUT = sys.argv[1]
NF = int(sys.argv[2]) if len(sys.argv) > 2 else 20
os.makedirs(OUT, exist_ok=True)
DT = 1 / 24


def frame_json(fr):
    return [{"p": np.asarray(p.points, np.float64).round(7).tolist(),
             "c": [float(c) for c in p.color], "g": float(getattr(p, "glow", 0.0))} for p in fr]


def scene_snap(sc, gain):
    return {"id": sc.gpu_id, "a": 1.0 if gain is None else float(gain),
            "cam": sc.camera.gpu_state() if sc.camera is not None else None,
            "layers": [u for _, _, u in sc._tick_state], "sm": sc._shape_scales}


def load(name, mutate=None):
    d = json.load(open(f"scenes/{name}.json"))
    if mutate:
        mutate(d)
    sc = Scene(SceneSpec.from_dict(d))
    if sc.camera is not None:
        sc.camera.aspect = 16 / 9
    return sc


def post(fr, gain, hue):
    if gain < 1.0:
        for p in fr:
            p.color = tuple(c * gain for c in p.color)
    if hue is not None:
        for p in fr:
            _, s, v = colorsys.rgb_to_hsv(*p.color)
            p.color = colorsys.hsv_to_rgb(hue, s, v)
    return fr


cases = []


def run_single(case, sc, plane=False, gain=1.0, hue=None, nf=NF):
    blobs = {sc.gpu_id: sc.gpu_blob()}
    m = ModMatrix(); t = 0.0; frames = []
    for i in range(nf):
        t += DT
        sc.tick(t, DT, m, disable_plane=plane)
        snap = {"scenes": [scene_snap(sc, None)], "gain": gain, "hue": hue,
                "test": False, "plane": plane}
        fr = post(sc.draw(), gain, hue)
        frames.append({"snap": snap, "expect": frame_json(fr)})
    write(case, blobs, frames)


def write(case, blobs, frames):
    d = os.path.join(OUT, case.replace(" ", "_"))
    os.makedirs(d, exist_ok=True)
    for gid, b in blobs.items():
        open(os.path.join(d, f"geom_{gid}.bin"), "wb").write(b)
    json.dump(frames, open(os.path.join(d, "frames.json"), "w"))
    cases.append(case)


names = sorted(os.path.basename(f)[:-5] for f in glob.glob("scenes/*.json"))
for name in names:
    run_single(name, load(name))

# variants
run_single("V_plane_eniac", load("eniac"), plane=True)
run_single("V_plane_train", load("train station"), plane=True)
run_single("V_hue_gain_hotlava", load("hot lava"), gain=0.6, hue=0.3)
run_single("V_hue_gain_mandala", load("Mandala"), gain=0.45, hue=0.8)


def depth_hue(d):
    d["camera"]["depth"] = {"mode": "both", "near_color": [0.2, 1.0, 0.4],
                            "far_color": [0.1, 0.1, 0.6], "ttl_quantize": False}
run_single("V_depthhue_rocks", load("rocks", depth_hue))


def depth_ttl(d):
    d["camera"]["depth"] = {"mode": "hue", "near_color": [0.7, 0.9, 0.2],
                            "far_color": [0.1, 0.4, 0.6], "ttl_quantize": True}
run_single("V_depthttl_ants", load("ants", depth_ttl))

sc = load("Mandala")
for k, v in (("scale", 1.7), ("rotate", 0.13), ("spread", 1.6), ("glow", 0.3), ("max_strokes", 50)):
    assert sc.set_layer_param(0, k, v), k
run_single("V_p2d_params_mandala", sc)
sc = load("crop circles")
for k, v in (("scale", 0.6), ("rotate", 0.71), ("spread", 0.4)):
    sc.set_layer_param(0, k, v)
run_single("V_p2d_params_crop", sc)

sc = load("eniac")
shapes = list(json.load(open("scenes/eniac.json"))["layers"][0]["params"]["defs"].keys())
sc._shape_mod_scales = lambda synth, _s=shapes: {_s[0]: 1.6, _s[2]: 0.5}
run_single("V_shapemod_eniac", sc)


def fly(d):
    d["camera"]["mode"] = "fly"
run_single("V_fly_hotlava", load("hot lava", fly))
run_single("V_fly_rocks", load("rocks", fly))

sc = load("eniac")
sc.camera.max_strokes = 5000
run_single("V_whole_eniac", sc, nf=6)

# crossfade through SceneManager
for a, b, nm in (("Mandala", "eniac", "V_xfade_mandala_eniac"), ("rocks", "hot lava", "V_xfade_rocks_lava")):
    smgr = SceneManager(tempfile.mkdtemp())
    smgr.set_scene(SceneSpec.from_dict(json.load(open(f"scenes/{a}.json"))))
    m = ModMatrix(); t = 0.0; frames = []; blobs = {}
    for i in range(36):
        if i == 4:
            smgr.set_scene(SceneSpec.from_dict(json.load(open(f"scenes/{b}.json"))), crossfade=1.0)
        t += DT
        smgr.tick(t, DT, m)
        for s_, _ in smgr.live:
            if s_.camera is not None:
                s_.camera.aspect = 1.0
            blobs[s_.gpu_id] = s_.gpu_blob()
        snap = {"scenes": [scene_snap(s_, g) for s_, g in smgr.live], "gain": 1.0, "hue": None,
                "test": False, "plane": False}
        frames.append({"snap": snap, "expect": frame_json(smgr.draw())})
    write(nm, blobs, frames)

# test pattern
write("V_testpattern", {0: pack_test_pattern()},
      [{"snap": {"scenes": [], "gain": 1.0, "hue": None, "test": True, "plane": False},
        "expect": frame_json(test_pattern_frame())}])

json.dump(cases, open(os.path.join(OUT, "cases.json"), "w"))
print("cases:", len(cases))
