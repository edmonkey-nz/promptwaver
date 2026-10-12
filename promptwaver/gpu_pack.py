"""Scene geometry for the browser renderer, packed once per scene.

The browser evaluates every frame itself (web/static/scene-eval.js): this
module sends it the static half — each layer's geometry from its generator's
`gpu_pack` — and the engine's per-tick snapshot (`Engine._snapshot`) sends the
moving half. Nothing here runs per frame.

Wire format, one websocket BINARY message per scene:

    uint32 LE   header length H
    H bytes     JSON header, space-padded to a multiple of 4
    ...         the arrays, each starting on a 4-byte boundary

    header = {"type": "geometry", "id": <scene gpu_id>, "is3d": bool,
              "layers": [{"kind": "world"|"pattern2d"|"empty", ...meta,
                          "arrays": {name: [byte_offset, n_values, "f4"|"i4", cols]}}]}

`byte_offset` counts from the start of the message, so the browser can view
each array in place (`new Float32Array(buf, off, n)`) without copying.
"""

from __future__ import annotations

import json
import struct

import numpy as np

#: The test pattern's geometry id. Scene ids start at 1 (scenes._GPU_IDS).
TEST_PATTERN_ID = 0

_DTYPES = {np.dtype(np.float32): "f4", np.dtype(np.int32): "i4"}


def _pack(gid: int, is3d: bool, layers: list[dict]) -> bytes:
    chunks: list[bytes] = []
    head_layers = []
    for layer in layers:
        meta = {k: v for k, v in layer.items() if k != "arrays"}
        meta["arrays"] = {}
        for name, arr in layer.get("arrays", {}).items():
            a = np.ascontiguousarray(arr)
            if a.dtype not in _DTYPES:
                a = a.astype(np.float32 if a.dtype.kind == "f" else np.int32)
            cols = int(a.shape[1]) if a.ndim == 2 else 1
            meta["arrays"][name] = [None, int(a.size), _DTYPES[a.dtype], cols]
            chunks.append(a.tobytes())
        head_layers.append(meta)

    # Offsets depend on the header's own length, which depends on the offsets'
    # digits — so size the header with placeholders first, then fill in.
    def header_bytes(offsets):
        k = 0
        for meta in head_layers:
            for name in meta["arrays"]:
                meta["arrays"][name][0] = offsets[k]
                k += 1
        raw = json.dumps({"type": "geometry", "id": gid, "is3d": bool(is3d),
                          "layers": head_layers}, separators=(",", ":")).encode()
        return raw + b" " * (-len(raw) % 4)

    n = len(chunks)
    head = header_bytes([10 ** 12] * n)          # widest possible offsets
    base = 4 + len(head)
    offsets, pos = [], base
    for c in chunks:
        offsets.append(pos)
        pos += len(c)                            # every array is 4-byte items
    head2 = header_bytes(offsets)
    head2 = head2 + b" " * (len(head) - len(head2))   # keep the size we budgeted
    return struct.pack("<I", len(head2)) + head2 + b"".join(chunks)


def pack_scene(scene) -> bytes:
    layers = []
    for g, base_params in scene._gens:
        p = dict(g.defaults)
        p.update(base_params)
        layers.append(g.gpu_pack(p))
    return _pack(scene.gpu_id, scene.is_3d, layers)


def pack_test_pattern() -> bytes:
    from .geometry import test_pattern_frame
    from .generators.pattern2d import pack_frame_2d
    return _pack(TEST_PATTERN_ID, False, [pack_frame_2d(test_pattern_frame())])
