# FreeCAD の freecadcmd の中で動かす: STEP の各ソリッドを参照モデルの基準値（reference.json）と比べ、干渉を調べる。
# ふつうは `python tools/bench.py check` から呼ぶ。入出力は環境変数 CHECK_STEP、CHECK_REF、CHECK_OUT で受け取る。
#
# 見るもの:
# - 形: 体積が ±0.5% 以内、外形の3辺が ±0.2 mm 以内
# - 位置: 重心と外形（座標）が ±0.2 mm 以内
# - 干渉: ソリッドどうしの重なりが 1 mm³ 以下
# ジョイントは STEP に入らないので、ここでは見ない（joints.json と見比べて、目で確かめる）。
import json
import math
import os

import Part

TOL_VOL = 0.005      # 体積 ±0.5%
TOL_POS = 0.2        # 重心・外形 ±0.2 mm
TOL_INTERF = 1.0     # 重なりの体積 1 mm³ まで許す

step, ref_path, out_path = os.environ["CHECK_STEP"], os.environ["CHECK_REF"], os.environ["CHECK_OUT"]
with open(ref_path, encoding="utf-8-sig") as f:
    ref = json.load(f)["instances"]
res = {"read": False, "n_ref": len(ref)}


def finish():
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    raise SystemExit(0)


try:
    solids = Part.read(step).Solids
except Exception as e:  # 読めない STEP は、不合格として記録する
    res["error"] = "read failed: %s" % type(e).__name__
    finish()

res["read"] = True
meas = []
for s in solids:
    bb = s.optimalBoundingBox(True, False)
    meas.append({"volume": s.Volume, "com": list(s.CenterOfMass),
                 "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]})
res["solid_count"] = len(solids)

# 参照の部品とソリッドを、体積と重心の近い順に組にする
pairs = []
for name, r in ref.items():
    for i, m in enumerate(meas):
        dv = abs(m["volume"] - r["volume"]) / r["volume"]
        dc = math.dist(m["com"], r["center_of_mass"])
        pairs.append((dv * 100 + dc, name, i, dv, dc))
pairs.sort()
used_ref, used_solid, match = set(), set(), {}
for cost, name, i, dv, dc in pairs:
    if name in used_ref or i in used_solid:
        continue
    used_ref.add(name)
    used_solid.add(i)
    r, m = ref[name], meas[i]
    ext_r = sorted(r["bbox"][k + 3] - r["bbox"][k] for k in range(3))
    ext_m = sorted(m["bbox"][k + 3] - m["bbox"][k] for k in range(3))
    d_ext = max(abs(a - b) for a, b in zip(ext_r, ext_m))
    d_box = max(abs(a - b) for a, b in zip(r["bbox"], m["bbox"]))
    match[name] = {"solid": i, "d_volume_rel": round(dv, 5), "d_com_mm": round(dc, 3),
                   "d_extent_mm": round(d_ext, 3), "d_bbox_mm": round(d_box, 3),
                   "shape_ok": dv <= TOL_VOL and d_ext <= TOL_POS,
                   "place_ok": dv <= TOL_VOL and dc <= TOL_POS and d_box <= TOL_POS}
res["match"] = match
res["unmatched_ref"] = [n for n in ref if n not in match]
res["extra_solids"] = len(solids) - len(used_solid)

interf = []
for i in range(len(solids)):
    for j in range(i + 1, len(solids)):
        if solids[i].BoundBox.intersect(solids[j].BoundBox):
            try:
                v = solids[i].common(solids[j]).Volume
            except Exception:
                v = -1
            if v > TOL_INTERF or v < 0:
                interf.append({"a": i, "b": j, "volume": round(v, 3)})
res["interference"] = interf

res["n_shape_ok"] = sum(m["shape_ok"] for m in match.values())
res["n_place_ok"] = sum(m["place_ok"] for m in match.values())
res["shape_ok"] = len(match) == len(ref) and res["n_shape_ok"] == len(ref) and res["extra_solids"] == 0
res["place_ok"] = res["shape_ok"] and res["n_place_ok"] == len(ref)
res["no_interference"] = res["shape_ok"] and not interf
finish()
