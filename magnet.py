#!/usr/bin/env python3
"""Magnet — thay tên hàng loạt trên mẫu magnet (PSD có layer text sống).

Luồng:
  1. analyze(psd)      -> tự dò các layer text (font/màu/vị trí/size) + preview.
  2. save_template()   -> ẩn các field đã chọn, xuất base.png (đã trống tên),
                          copy font vào registry, ghi template.json.
  3. render_rows()     -> mỗi dòng đơn: vẽ tên mới lên base.png đúng chỗ đã nhớ.

Chỉ xử lý FIELD dạng TEXT (tên/tàu/năm là chữ). Tuỳ chọn dạng ẩn/hiện pixel
(vd chọn tên tàu Disney bằng layer ảnh) chưa hỗ trợ ở v1.
"""
import io, json, math, os, re, shutil, zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from psd_tools import PSDImage

FONT_EXTS = {".ttf", ".otf", ".ttc"}
JUSTIFY = {0: "left", 1: "right", 2: "center"}


# ---------- font resolve ----------
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _font_index(folder: Path):
    """{normalized_name -> path} cho mọi file font trong folder (đệ quy)."""
    idx = {}
    files = [p for p in folder.rglob("*") if p.suffix.lower() in FONT_EXTS]
    for p in files:
        keys = {_norm(p.stem)}
        try:
            fam, style = ImageFont.truetype(str(p), 10).getname()
            keys |= {_norm(fam), _norm(f"{fam}{style}")}
        except Exception:
            pass
        for k in keys:
            if k:
                idx.setdefault(k, p)
    return idx, files


def _resolve_font(psd_name, folder: Path):
    """PostScript/family name (từ PSD) -> file font trong folder. None nếu không chắc."""
    idx, files = _font_index(folder)
    want = _norm(psd_name)
    if not want:
        return files[0] if len(files) == 1 else None
    if want in idx:
        return idx[want]
    best, score = None, 0
    for k, p in idx.items():
        s = 0
        if k == want:
            s = 3
        elif k.startswith(want) or want.startswith(k):
            s = 2
        elif k in want or want in k:
            s = 1
        if s > score:
            best, score = p, s
    if best:
        return best
    return None   # tên không khớp font nào -> báo thiếu (KHÔNG thay đại font khác: hình chữ sẽ sai)


# ---------- đọc style layer text ----------
def _style(layer):
    """(font_name, size_pt, color_rgba, justify, tracking) từ engine_dict; mặc định nếu thiếu.
    tracking = giãn cách chữ theo PS (đơn vị 1/1000 em) — Pillow không tự áp nên phải đọc ra."""
    font_name, size_pt, color, just, track, hscale = "", None, (0, 0, 0, 255), "center", 0.0, 1.0
    try:
        ed = layer.engine_dict
        sd = ed["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
        fonts = layer.resource_dict["FontSet"]
        font_name = str(fonts[sd.get("Font", 0)]["Name"])
        size_pt = float(sd.get("FontSize", 0)) or None
        track = float(sd.get("Tracking", 0) or 0)
        hscale = float(sd.get("HorizontalScale", 1) or 1)   # nén/giãn ngang (PS), 0.9 = 90%
        v = sd.get("FillColor", {}).get("Values")     # [A,R,G,B] 0..1
        if v and len(v) == 4:
            color = (round(v[1] * 255), round(v[2] * 255), round(v[3] * 255), round(v[0] * 255))
    except Exception:
        pass
    try:
        j = layer.engine_dict["ParagraphRun"]["RunArray"][0]["ParagraphSheet"]["Properties"]["Justification"]
        just = JUSTIFY.get(int(j), "center")
    except Exception:
        pass
    return font_name, size_pt, color, just, track, hscale


def _calibrate(font_file: Path, text, box):
    """size_px sao cho chữ GỐC vừa chiều cao box; top_frac = (đỉnh ink -> baseline)/size.

    Đo bằng anchor 'ls' (gốc = baseline) để khớp với cách _draw_field vẽ (anchor '*s').
    getbbox anchor 'ls' trả (l, t, r, b) với y so với baseline: t âm (trên baseline).
    """
    x0, y0, x1, y1 = box
    box_h = max(1, y1 - y0)
    ref = text or "Ag"
    g = 200
    try:
        f = ImageFont.truetype(str(font_file), g)
        l, t, r, b = f.getbbox(ref, anchor="ls")
        h = max(1, b - t)
        size = max(4, round(g * box_h / h))
        f2 = ImageFont.truetype(str(font_file), size)
        top = -f2.getbbox(ref, anchor="ls")[1]        # đỉnh ink phía trên baseline (dương)
        return size, top / size
    except Exception:
        return box_h, 0.8


def _rgba(c):
    """list/tuple màu -> (r,g,b,a) 4 phần tử."""
    c = tuple(int(x) for x in c)
    return c if len(c) == 4 else c + (255,)


def _hex(s):
    """'#e01919' / 'e01919' / '#e11' -> (r,g,b,255); None nếu rỗng/không hợp lệ."""
    if not s:
        return None
    s = str(s).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
    except ValueError:
        return None


def _color(d):
    """{Rd,Grn,Bl} trong descriptor effect (thang 0..255) -> (r,g,b)."""
    if not d:
        return None
    v = [float(d.get(b"Rd  ", 0)), float(d.get(b"Grn ", 0)), float(d.get(b"Bl  ", 0))]
    return tuple(max(0, min(255, int(round(x)))) for x in v)


def _effects(layer):
    """Effect trên layer tên: fill (ColorOverlay), stroke, drop shadow. {} nếu không có.

    Vẽ lại bằng Pillow (stroke_width, shadow offset+blur). Bevel/gradient/glow chưa hỗ trợ:
    những loại đó gom vào key '_unsupported' để UI cảnh báo (thay vì lặng lẽ bỏ qua).
    """
    out = {}
    unsupported = []
    try:
        effs = list(layer.effects or [])
    except Exception:
        return out
    for e in effs:
        if not getattr(e, "enabled", True):
            continue
        n = type(e).__name__
        if n == "ColorOverlay":
            c = _color(e.color)
            if c:
                out["fill"] = list(c)                      # đè màu chữ
        elif n == "Stroke" and float(e.size) > 0:
            c = _color(e.color)
            # PS Stroke Size = px lan ra theo Position; Pillow stroke_width vẽ ra NGOÀI đúng số px đó:
            #   Outside -> =Size | Center -> =Size/2 (chỉ nửa lan ngoài) | Inside -> ~0 (lan vào trong).
            mul = {b"OutF": 1.0, b"CtrF": 0.5, b"InsF": 0.0}.get(e.position, 1.0)
            sw = round(float(e.size) * mul)
            if c and sw > 0:
                out.setdefault("strokes", []).append({"w": sw, "color": list(c)})   # gom NHIỀU viền (đồng tâm)
        elif n == "DropShadow":
            c = _color(e.color)
            if c:
                ang = math.radians(float(e.angle)); dist = float(e.distance)
                out["shadow"] = {"dx": round(-dist * math.cos(ang)), "dy": round(dist * math.sin(ang)),
                                 "color": list(c), "blur": round(float(e.size)),
                                 "opacity": round(float(e.opacity) * 255 / 100)}
        elif n == "GradientOverlay":
            g = _grad(e)
            if g:
                out["gradient"] = g                        # tô dải màu vào chữ
        elif n == "OuterGlow":
            c = _color(e.color)
            screen = bytes(getattr(e, "blend_mode", b"")) == b"Scrn"
            # glow Screen + màu tối = vô hình trong PS -> bỏ (tránh vẽ quầng đen sai)
            if c and float(e.size) > 0 and not (screen and max(c) < 30):
                out["glow"] = {"color": list(c), "size": round(float(e.size)),
                               "opacity": round(float(e.opacity) * 255 / 100)}
        elif n == "InnerShadow":
            c = _color(e.color)
            if c:
                ang = math.radians(float(e.angle)); dist = float(e.distance)
                out["inner_shadow"] = {"dx": round(-dist * math.cos(ang)), "dy": round(dist * math.sin(ang)),
                                       "color": list(c), "blur": round(float(e.size)),
                                       "opacity": round(float(e.opacity) * 255 / 100)}
        elif n == "BevelEmboss":
            out["bevel"] = {"hl": list(_color(e.highlight_color) or (255, 255, 255)),
                            "sh": list(_color(e.shadow_color) or (0, 0, 0)),
                            "hl_op": round(float(e.highlight_opacity)), "sh_op": round(float(e.shadow_opacity)),
                            "angle": float(e.angle), "alt": float(e.altitude)}
        else:
            unsupported.append(n)                          # Satin / Pattern / InnerGlow (chưa dựng)
    if out.get("strokes"):                                 # viền lớn nhất -> shadow silhouette + tương thích code cũ
        big = max(out["strokes"], key=lambda s: s["w"])
        out["stroke"], out["stroke_color"] = big["w"], big["color"]
    if unsupported:
        out["_unsupported"] = unsupported
    return out


def _strokes_of(fx):
    """[(w, rgba)] các viền, sắp LỚN -> NHỎ (để vẽ đồng tâm). Fallback template cũ (1 viền)."""
    ss = fx.get("strokes")
    if ss:
        return sorted([(int(s["w"]), _rgba(s["color"])) for s in ss if s.get("w", 0) > 0], key=lambda x: -x[0])
    w = int(fx.get("stroke") or 0)
    return [(w, _rgba(fx.get("stroke_color") or (0, 0, 0)))] if w > 0 else []


def _grad(e):
    """Gradient overlay -> {stops:[[loc0-1,[r,g,b]]], gtype:'rad'/'lin', angle, reversed}."""
    try:
        stops = []
        for s in e.gradient.get(b"Clrs"):
            loc = float(s.get(b"Lctn")) / 4096.0
            c = s.get(b"Clr ")
            rgb = [int(round(float(c.get(k, 0)))) for k in (b"Rd  ", b"Grn ", b"Bl  ")]
            stops.append([round(loc, 4), rgb])
        stops.sort()
        if len(stops) < 2:
            return None
        return {"stops": stops, "gtype": "rad" if b"Rdl" in bytes(e.type) else "lin",
                "angle": float(e.angle), "reversed": bool(e.reversed)}
    except Exception:
        return None


def _text_geometry(layer):
    """Retain PSD warp and pre-warp coordinates instead of reducing every warp to an arc."""
    wp = getattr(layer, "warp", None) or {}
    def enum(key, default):
        return getattr(wp.get(key), "enum", default).decode("ascii", "replace")
    sd = layer.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
    def rect(key):
        d = layer._data.text_data.get(key)
        return [float(d[k]) for k in (b"Left", b"Top ", b"Rght", b"Btom")] if d else None
    return {
        "warp": {"style": enum(b"warpStyle", b"warpNone"),
                 "bend": float(wp.get(b"warpValue", 0)),
                 "perspective": float(wp.get(b"warpPerspective", 0)),
                 "perspective_other": float(wp.get(b"warpPerspectiveOther", 0)),
                 "orientation": enum(b"warpRotate", b"Hrzn")},
        "transform": [float(v) for v in layer.transform],
        "bounds": rect(b"bounds"), "ink_bounds": rect(b"boundingBox"),
        "font_size": float(sd.get("FontSize", 0)),
        "vertical_scale": float(sd.get("VerticalScale", 1)),
        "baseline_shift": float(sd.get("BaselineShift", 0)),
        "reference_box": list(layer.bbox),
    }


def _supports_arch(g):
    if not g:
        return False
    w = g["warp"]
    a, b, c, d, _, _ = g["transform"]
    bounds = g.get("bounds")
    return (w["style"] == "warpArch" and w["orientation"] == "Hrzn"
            and not w["perspective"] and not w["perspective_other"]
            and abs(b) < 1e-6 and abs(c) < 1e-6 and a > 0 and d > 0
            and g["vertical_scale"] > 0 and bounds is not None
            and bounds[2] > bounds[0] and abs(w["bend"]) < 100)


def _arch_sagitta(g):
    width = (g["bounds"][2] - g["bounds"][0]) * g["transform"][0]
    return width / 2 * math.tan(g["warp"]["bend"] / 100 * math.pi / 4)


def _detect_arc(layer):
    """Độ cong (sagitta px, dương = cong lên/cười) của tên đặt trên cung. 0 = thẳng.

    ƯU TIÊN đọc warp data trong PSD (ổn định MỌI máy/version psd_tools). Chỉ khi PSD không
    có warp mới dò từ pixel (composite warp không đồng nhất giữa các version -> lệch máy).
    """
    try:                                            # 1. warp data (nguồn chuẩn, deterministic)
        wp = getattr(layer, "warp", None)
        if wp:
            enum = getattr(wp.get(b"warpStyle"), "enum", b"") or b""
            bend = float(wp.get(b"warpValue", 0) or 0)
            x0, _y0, x1, _y1 = layer.bbox
            width = max(1, x1 - x0)
            if enum and enum != b"warpNone" and not bend:
                return 0.0                          # warp THẬT nhưng bend=0 = thẳng (deterministic)
            # warpNone: KHÔNG return sớm -> chữ cong có thể nướng sẵn/uốn tay vào pixel
            # (mẫu LTL banner) -> thả xuống pixel-fallback (có guard sag>=25 né descender giả).
            if enum == b"warpArch":
                geometry = _text_geometry(layer)
                if _supports_arch(geometry):
                    return _arch_sagitta(geometry)
            if enum in (b"warpArch", b"warpArc", b"warpArcUpper", b"warpArcLower"):
                # bend% -> sagitta parabol px (dùng cùng cỡ chữ thiết kế thật -> khớp mẫu; bend 32,
                # width 2576 -> ~340, xấp xỉ box_h - cap_height).
                return float(round(0.412 * bend / 100.0 * width)) if abs(bend) >= 1 else 0.0
            if enum not in (b"warpNone", b""):
                return 0.0                          # warp kiểu khác (wave/flag...) -> coi thẳng, né parabol sai
    except Exception:
        pass
    try:                                            # 2. fallback: dò từ pixel (PSD không có warp)
        a = np.asarray(layer.composite(force=True))
        if a.ndim != 3 or a.shape[2] < 4:
            return 0.0
        m = a[..., 3] > 30
        cols = np.where(m.any(0))[0]
        if len(cols) < 20:
            return 0.0
        yb = np.array([np.where(m[:, cx])[0].max() for cx in cols], float)
        A, B, C = np.polyfit(cols.astype(float), yb, 2)
        w = float(cols.max() - cols.min())
        sag = A * w * w / 4.0                          # baseline(mép) - baseline(đỉnh)
        rows = np.where(m.any(1))[0]
        h = float(rows.max() - rows.min()) if len(rows) else 1.0
        # chỉ coi là cong khi độ cong đủ LỚN so với chiều cao chữ (né descender/swash
        # của font script tạo parabol giả).
        return float(round(sag)) if abs(sag) >= max(25, 0.25 * h) else 0.0
    except Exception:
        return 0.0


def _dpi(psd):
    """DPI thật của PSD (mặc định 72). Lưu dạng fixed-point 16.16."""
    try:
        return round(psd.image_resources.get(1005).data.horizontal / 65536)
    except Exception:
        return 72


def _design_size(layer):
    """Cỡ chữ THẬT theo thiết kế (px canvas) = FontSize(engine) * scale của transform layer.
    Ổn định, không phụ thuộc arc/box; 0 nếu thiếu."""
    try:
        sd = layer.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
        fs = float(sd.get("FontSize", 0))
        tr = layer.transform
        return fs * math.hypot(tr[2], tr[3])
    except Exception:
        return 0.0


def _type_layers(psd):
    """Các layer text ĐANG HIỆN (bỏ layer ẩn/biến thể không dùng)."""
    out = []
    for l in psd.descendants():
        if l.kind == "type" and l.is_visible() and l.bbox != (0, 0, 0, 0):
            out.append(l)
    return out


# ---------- analyze ----------
def analyze(psd_path):
    """Dò field + preview. Trả dict để UI hiện & sửa; CHƯA lưu gì."""
    psd_path = Path(psd_path)
    psd = PSDImage.open(psd_path)
    folder = psd_path.parent
    fields = []
    used = set()
    for l in _type_layers(psd):
        font_name, _size_pt, color, just, track, hscale = _style(l)
        ff = _resolve_font(font_name, folder)
        box = list(l.bbox)
        txt = str(l.text)
        arc = _detect_arc(l)                          # tên cong (banner) -> sagitta px
        ds = _design_size(l) if (arc and ff) else 0   # chữ cong: lấy cỡ THẬT từ warp transform (không trừ arc)
        if ds >= 4:
            size_px = int(round(ds))
            top = -ImageFont.truetype(str(ff), size_px).getbbox(txt or "Ag", anchor="ls")[1]
            top_frac = top / size_px
        else:
            cal_box = [box[0], box[1], box[2], box[3] - int(abs(arc))]   # (không có warp) bỏ phần cong khi tính cỡ
            size_px, top_frac = _calibrate(ff, txt, cal_box) if ff else (max(1, cal_box[3] - cal_box[1]), 0.8)
        geometry = None
        try:
            geometry = _text_geometry(l)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        if _supports_arch(geometry):
            tr = geometry["transform"]
            size_px = max(4, round(_design_size(l) * geometry["vertical_scale"]))
            hscale *= tr[0] / tr[3] / geometry["vertical_scale"]
            baseline = tr[5] - geometry["baseline_shift"] * tr[3]
            top_frac = (baseline - arc - box[1]) / size_px
            geometry["justify"] = just
        key = re.sub(r"[^a-z0-9]+", "_", str(l.name or "field").lower()).strip("_") or "field"
        while key in used:
            key += "_2"
        used.add(key)
        fields.append({
            "key": key, "layer": str(l.name), "text": txt,
            "font_name": font_name,
            "font_file": ff.name if ff else "",
            "color": list(color), "justify": just,
            "box": box, "size_px": size_px, "top_frac": round(top_frac, 4),
            "arc": arc, "track": track, "hscale": round(hscale, 4), "effects": _effects(l),
            "clip_layers": _clip_names(l),          # pattern mask vào chữ (nếu có)
            "pattern": bool(_clip_names(l)),         # cờ cho UI + save; save đổi thành tên file texture
            "text_geometry": geometry,
        })
        if geometry and geometry["warp"]["style"] != "warpNone" and not _supports_arch(geometry):
            fields[-1]["warp_warning"] = "Warp chỉ dựng gần đúng: " + geometry["warp"]["style"]
    preview = psd.composite()
    bg_name, bg_color = _bg_layer(psd)                    # nền màu đơn đổi được (nếu có)
    bg = {"layer": bg_name, "color": bg_color} if bg_name else None
    return {"canvas": list(psd.size), "fields": fields, "preview": preview, "background": bg}


# ---------- phát hiện nền màu đơn ----------
def _is_solid(layer):
    """(r,g,b) nếu layer phủ đều 1 màu (solid fill hoặc pixel tô đều); None nếu là artwork."""
    im = layer.composite(force=True)
    if im is None:
        return None
    rgba = np.asarray(im.convert("RGBA")).reshape(-1, 4)
    op = rgba[rgba[:, 3] > 200][:, :3]
    if len(op) < 100 or op.std(0).max() >= 6:       # nhiều màu -> không phải nền
        return None
    return [int(x) for x in op.mean(0).round()]


def _bg_layer(psd):
    """Layer nền dưới cùng, phủ hết canvas, 1 màu -> (name, [r,g,b]); (None,None) nếu không có.
    Bắt cả 2 dạng user hay xuất: Solid Color fill layer VÀ pixel full-canvas tô đều."""
    W, H = psd.size
    for l in psd.descendants():                     # descendants() đi từ dưới lên -> lấy nền đáy
        if l.is_visible() and tuple(l.bbox) == (0, 0, W, H):
            c = _is_solid(l)
            if c:
                return str(l.name), c
    return None, None


# ---------- pattern "mask vào text" (clipping mask trong PSD) ----------
def _clip_names(layer):
    """Tên các layer đang clip (mask) vào layer này -> đó là pattern/texture phủ trong chữ."""
    return [str(c.name) for c in getattr(layer, "clip_layers", []) or []]


def _inpaint(patch):
    """patch RGBA (pattern∩chữ, có lỗ giữa nét) -> texture RGB KÍN bằng lan màu (watercolor mượt)."""
    rgb = patch[..., :3]; known = patch[..., 3] > 76      # ~0.3*255
    if known.sum() < 10:
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    imgf = Image.fromarray(np.where(known[..., None], rgb, 0).astype(np.uint8))
    wf = Image.fromarray((known * 255).astype(np.uint8))
    for _ in range(40):                                   # lặp: blur rồi giữ pixel gốc -> lấp dần lỗ
        ib = np.asarray(imgf.filter(ImageFilter.GaussianBlur(6))).astype(np.float32)
        wb = np.asarray(wf.filter(ImageFilter.GaussianBlur(6))).astype(np.float32) / 255.0
        filled = ib / np.maximum(wb[..., None], 1e-3)
        filled[known] = rgb[known]
        imgf = Image.fromarray(np.clip(filled, 0, 255).astype(np.uint8))
        wf = Image.fromarray((np.clip(wb + 0.5, 0, 1) * 255).astype(np.uint8))
    return imgf


def _extract_pattern(psd_path, text_name, clip_names, bbox):
    """Composite chỉ text+clip -> pattern∩chữ, cắt theo bbox chữ, lấp lỗ -> texture RGB để tô tên mới."""
    psd = PSDImage.open(str(psd_path))
    keep = {text_name, *clip_names}
    for l in psd.descendants():
        l.visible = l.name in keep
    arr = np.asarray(psd.composite(force=True).convert("RGBA"))
    x0, y0, x1, y1 = [int(v) for v in bbox]
    return _inpaint(arr[y0:y1, x0:x1].astype(np.float32))


# ---------- build base (ẩn field + nền + layer clip pattern) ----------
def _build_base(psd_path, layer_names, bg_name=None, extra_hide=None):
    psd = PSDImage.open(str(psd_path))
    want = set(layer_names)
    extra = set(extra_hide or [])
    for l in psd.descendants():
        if (l.kind == "type" and l.name in want) or (bg_name and l.name == bg_name) or (l.name in extra):
            l.visible = False                       # ẩn -> base trong suốt chỗ đó, vẽ/tô lại lúc render
    return psd.composite(force=True).convert("RGBA")


def _bake_bases(psd_path, exclude_names, bg_name=None):
    """Tách base theo z-order: (dưới-chữ, trên-chữ). Layer NẰM TRÊN chữ (art đè lên) tách ra
    base_above để dán LẠI sau khi vẽ chữ -> giữ đúng thứ tự (chữ không che art). above=None nếu không có."""
    excl = set(exclude_names)
    layers = list(PSDImage.open(str(psd_path)).descendants())
    cutoff = max([i for i, l in enumerate(layers) if l.name in excl], default=len(layers) - 1)
    has_above = any(i > cutoff for i in range(len(layers)))
    keep = lambda l: l.name not in excl and not (bg_name and l.name == bg_name)
    def bake(pred):
        psd = PSDImage.open(str(psd_path)); ls = list(psd.descendants())
        for i, l in enumerate(ls):
            l.visible = pred(i, l)
        return psd.composite(force=True).convert("RGBA")
    above = None
    if has_above:
        try:
            a = bake(lambda i, l: i > cutoff)
            above = a if a.getbbox() else None
        except Exception:
            above = None                            # composite lỗi -> không tách, gộp vào below (không mất art)
    if above is not None:
        below = bake(lambda i, l: i <= cutoff and keep(l))   # dưới-chữ (art trên đã tách ra above)
    else:
        below = bake(lambda i, l: keep(l))                   # như cũ: gộp tất cả trừ chữ/nền
    return below, above


def _preserve_static_pixels(psd_path, below, above, exclude_names, bg_name=None):
    """Use Photoshop's merged preview outside the editable layers' original footprints.

    Re-compositing an untouched layer can change its effects in psd-tools. Keep
    those pixels verbatim. An editable solid background requires the transparent
    baked bases instead, so that case deliberately keeps the existing path.
    """
    if bg_name:
        return below, above
    psd = PSDImage.open(str(psd_path))
    preview = psd.topil()
    if preview is None:
        return below, above
    preview = preview.convert("RGBA")
    affected = Image.new("L", psd.size)
    draw = ImageDraw.Draw(affected)
    names = set(exclude_names)
    for layer in psd.descendants():
        if layer.name not in names:
            continue
        effects = _effects(layer)
        margin = max([4] + [w for w, _ in _strokes_of(effects)])
        margin += max([0] + [abs(e.get("dx", 0)) + abs(e.get("dy", 0))
                             + 4 * e.get("blur", e.get("size", 0))
                             for k in ("shadow", "glow", "inner_shadow") if (e := effects.get(k))])
        x0, y0, x1, y1 = layer.bbox
        draw.rectangle((x0-margin, y0-margin, x1+margin, y1+margin), fill=255)
    below = Image.composite(below, preview, affected)
    if above is not None:
        above.putalpha(ImageChops.multiply(above.getchannel("A"), affected))
        if not above.getbbox():
            above = None
    return below, above


# ---------- save template ----------
def _reg(data_dir):
    d = Path(data_dir) / "magnet_templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_template(psd_path, slug, fields, data_dir):
    """Ghi registry: base.png + fonts + template.json. fields: list đã user xác nhận."""
    psd_path = Path(psd_path)
    slug = _slug(slug)
    tdir = _reg(data_dir) / slug
    (tdir / "fonts").mkdir(parents=True, exist_ok=True)
    dpi = _dpi(PSDImage.open(str(psd_path)))
    bg_name, bg_color = _bg_layer(PSDImage.open(str(psd_path)))   # nền màu đơn (nếu có)
    clip_hide = [c for f in fields for c in f.get("clip_layers", [])]   # layer pattern -> ẩn khỏi base
    exclude = [f["layer"] for f in fields] + clip_hide
    base, base_above = _bake_bases(psd_path, exclude, bg_name)           # tách z-order: dưới/trên chữ
    base, base_above = _preserve_static_pixels(psd_path, base, base_above, exclude, bg_name)
    base.save(tdir / "base.png", dpi=(dpi, dpi))
    src_folder = psd_path.parent
    saved = []
    for f in fields:
        fn = f.get("font_file") or ""
        src = src_folder / fn if fn else None
        if not (src and src.is_file()):
            hit = _resolve_font(f.get("font_name", ""), src_folder)
            src, fn = hit, (hit.name if hit else "")
        if src and src.is_file():
            shutil.copy2(src, tdir / "fonts" / src.name)
            fn = src.name
        sf = {**f, "font_file": fn}
        if f.get("pattern") and f.get("clip_layers"):   # trích texture pattern -> lưu PNG, tô lúc render
            tex = _extract_pattern(psd_path, f["layer"], f["clip_layers"], f["box"])
            pfn = f"pattern_{_slug(f['key'])}.png"
            tex.save(tdir / pfn)
            sf["pattern"] = pfn
        saved.append(sf)
    tpl = {"slug": slug, "canvas": list(PSDImage.open(str(psd_path)).size),
           "dpi": dpi, "base": "base.png", "fields": saved}
    if bg_name:
        tpl["background"] = {"layer": bg_name, "color": bg_color}   # tô lại lúc render, mặc định = màu gốc
    if base_above is not None:
        base_above.save(tdir / "base_above.png", dpi=(dpi, dpi))
        tpl["base_above"] = "base_above.png"                        # art nằm trên chữ -> dán sau khi vẽ chữ
    (tdir / "template.json").write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    return tpl


def _slug(s):
    # Giữ tiếng Việt + dấu cách; chỉ bỏ ký tự cấm trong tên folder (Windows/Linux).
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", str(s)).strip().strip(".") or "template"


def rename_template(old, new, data_dir):
    reg = _reg(data_dir)
    src = reg / old
    new = _slug(new)
    dst = reg / new
    if not (src / "template.json").is_file():
        raise ValueError("template không tồn tại")
    if new == old:
        return json.loads((src / "template.json").read_text(encoding="utf-8"))
    if dst.exists():
        raise ValueError("tên mới đã tồn tại")
    src.rename(dst)
    tpl = json.loads((dst / "template.json").read_text(encoding="utf-8"))
    tpl["slug"] = new
    (dst / "template.json").write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    return tpl


def delete_template(slug, data_dir):
    d = _reg(data_dir) / slug
    if not (d / "template.json").is_file():
        raise ValueError("template không tồn tại")
    shutil.rmtree(d)


def load_template(slug, data_dir):
    p = _reg(data_dir) / slug / "template.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_templates(data_dir):
    reg = _reg(data_dir)
    out = []
    for d in sorted(reg.iterdir()) if reg.exists() else []:
        j = d / "template.json"
        if j.is_file():
            t = json.loads(j.read_text(encoding="utf-8"))
            out.append({"slug": t["slug"], "fields": [f["key"] for f in t["fields"]],
                        "bg": (t.get("background") or {}).get("color")})    # None nếu không có nền đổi được
    return out


def csv_header(tpl):
    return "order," + ",".join(f["key"] for f in tpl["fields"]) + "\n"


# ---------- render ----------
def _layout(font, value, track_px):
    """[(x_offset, ch)] + tổng bề ngang, có cộng tracking giữa các chữ."""
    xs, x = [], 0.0
    for ch in value:
        xs.append((x, ch)); x += font.getlength(ch) + track_px
    return xs, max(0.0, x - track_px)               # bỏ tracking thừa sau ký tự cuối


def _draw_tracked(draw, ax, baseline, value, font, fill, ha, track_px, sw=0, scol=None):
    """Vẽ chuỗi có tracking (giãn cách chữ). track_px=0 -> vẽ 1 lệnh y như cũ (giữ kerning)."""
    if not track_px:
        draw.text((ax, baseline), value, font=font, fill=fill, anchor=ha + "s",
                  stroke_width=sw, stroke_fill=scol)
        return
    xs, total = _layout(font, value, track_px)
    start = {"l": ax, "m": ax - total / 2, "r": ax - total}[ha]
    for xo, ch in xs:
        draw.text((start + xo, baseline), ch, font=font, fill=fill, anchor="ls",
                  stroke_width=sw, stroke_fill=scol)


def _draw_text_multi(img, ax, baseline, value, font, ha, track_px, fill, strokes):
    """Vẽ chữ với NHIỀU viền đồng tâm (lớn->nhỏ, viền nhỏ đè lên) rồi fill trên cùng, lên img."""
    d = ImageDraw.Draw(img)
    for w, col in strokes:                              # viền lớn nhất trước (ngoài cùng)
        _draw_tracked(d, ax, baseline, value, font, (0, 0, 0, 0), ha, track_px, w, col)
    _draw_tracked(d, ax, baseline, value, font, fill, ha, track_px)   # fill đè lên trên


def _hsqueeze(layer, cx, s):
    """Nén ngang cả lớp theo hệ số s quanh trục x=cx (giữ nguyên chiều cao)."""
    W, H = layer.size
    sc = layer.resize((max(1, int(round(W * s))), H), Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(sc, (int(round(cx - cx * s)), 0))         # điểm cx giữ nguyên vị trí
    return out


def _arch_geometry(f):
    """Canvas-space arch, adjusted for the editable box and sagitta (also used by the UI)."""
    g = f["text_geometry"]
    a, _, _, _, tx, _ = g["transform"]
    old = g["reference_box"]
    box = f["box"]
    ratio = max(1.0, box[2] - box[0]) / max(1.0, old[2] - old[0])
    width = (g["bounds"][2] - g["bounds"][0]) * a * ratio
    center = tx + (g["bounds"][0] + g["bounds"][2]) * a / 2
    center = box[0] + (center - old[0]) * ratio
    anchor = box[0] + (tx - old[0]) * ratio
    if f.get("justify", "center") != g.get("justify", "center"):
        anchor = {"left": box[0], "center": center, "right": box[2]}[f["justify"]]
    # Arch above 180 degrees folds back onto itself and is not invertible.
    sag = float(f.get("arc") or 0)
    sag = math.copysign(min(abs(sag), width / 2 * .999999), sag)
    return width, center, anchor, sag


def _paint_arch_mask(mask, f, tdir, override=None):
    """Apply effects AFTER warping: outline thickness and shadow direction stay in canvas space."""
    import cv2
    fx = f.get("effects") or {}
    oc = _hex(override)
    fill = oc or _rgba(fx.get("fill") or f["color"])
    strokes = _strokes_of(fx)
    out = Image.new("RGBA", mask.size)

    def stamp(alpha, color, opacity=255):
        color = _rgba(color)
        strength = color[3] * opacity / (255 * 255)
        if strength < 1:
            alpha = alpha.point(lambda v: round(v * strength))
        layer = Image.new("RGBA", mask.size, color[:3] + (0,))
        layer.putalpha(alpha)
        out.alpha_composite(layer)

    def dilate(radius):
        radius = max(0, int(round(radius)))
        if not radius:
            return mask
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
        return Image.fromarray(cv2.dilate(np.asarray(mask), kernel))

    def shift(alpha, dx, dy):
        shifted = Image.new("L", mask.size)
        shifted.paste(alpha, (int(round(dx)), int(round(dy))))
        return shifted

    glow = fx.get("glow")
    if glow:
        stamp(mask.filter(ImageFilter.GaussianBlur(max(1, glow["size"]))),
              glow["color"], glow.get("opacity", 255))
    shadow = fx.get("shadow")
    if shadow:
        silhouette = dilate(strokes[0][0] if strokes else 0)
        silhouette = shift(silhouette, shadow["dx"], shadow["dy"])
        if shadow.get("blur"):
            silhouette = silhouette.filter(ImageFilter.GaussianBlur(shadow["blur"]))
        stamp(silhouette, shadow["color"], shadow.get("opacity", 255))
    for radius, color in strokes:
        stamp(dilate(radius), color)

    bbox = mask.getbbox()
    pattern = f.get("pattern")
    texture = tdir / pattern if isinstance(pattern, str) else None
    if not oc and texture and texture.is_file():
        patch = Image.open(texture).convert("RGB").resize((bbox[2] - bbox[0], bbox[3] - bbox[1]))
        layer = Image.new("RGBA", mask.size)
        layer.paste(patch, bbox[:2]); layer.putalpha(mask)
        out.alpha_composite(layer)
    elif not oc and (fx.get("bevel") or fx.get("gradient")):
        rgb = (_bevel_fill(mask, fx["bevel"]) if fx.get("bevel")
               else _grad_fill(mask.size, bbox, fx["gradient"]))
        layer = Image.fromarray(rgb).convert("RGBA"); layer.putalpha(mask)
        out.alpha_composite(layer)
    else:
        stamp(mask, fill)
    inner = fx.get("inner_shadow")
    if inner and not oc:
        shifted = shift(mask, inner["dx"], inner["dy"])
        if inner.get("blur"):
            shifted = shifted.filter(ImageFilter.GaussianBlur(inner["blur"]))
        stamp(ImageChops.subtract(mask, shifted), inner["color"], inner.get("opacity", 255))
    return out


def _draw_arch_field(img, f, tdir, value, override=None):
    """Photoshop horizontal Arch: circular x remap and y displacement, not rotated glyphs.

    The saved bounds define the warp domain. New text keeps this domain and shrinks
    only when it exceeds its width. Old templates without geometry use the legacy renderer.
    """
    import cv2
    if not value:
        return
    width, center, anchor, sag = _arch_geometry(f)
    hs = max(.01, float(f.get("hscale") or 1))
    tracking = float(f.get("track") or 0) / 1000
    size = max(4, int(round(f["size_px"])))
    font_path = str(tdir / "fonts" / f["font_file"])
    def measure(font, text):
        return _layout(font, text, tracking * font.size)[1] if tracking else font.getlength(text)
    font = ImageFont.truetype(font_path, size)
    # Keep the original design size despite small font-rasterizer rounding differences.
    old = f["text_geometry"]["reference_box"]
    box_ratio = max(1, f["box"][2] - f["box"][0]) / max(1, old[2] - old[0])
    limit = max(width, measure(font, str(f.get("text") or "")) * hs * box_ratio)
    advance = measure(font, value)
    if advance * hs > limit:
        size = max(4, int(size * limit / (advance * hs)))
        font = ImageFont.truetype(font_path, size)
        advance = measure(font, value)
    track_px = tracking * size
    xs, _ = _layout(font, value, track_px)
    if track_px:
        boxes = [(x + b[0], b[1], x + b[2], b[3])
                 for x, ch in xs for b in [font.getbbox(ch, anchor="ls")]]
        ink = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    else:
        ink = font.getbbox(value, anchor="ls")
    left, top = math.floor(ink[0]) - 2, math.floor(ink[1]) - 2
    right, bottom = math.ceil(ink[2]) + 2, math.ceil(ink[3]) + 2
    plain = Image.new("L", (max(1, right - left), max(1, bottom - top)))
    _draw_tracked(ImageDraw.Draw(plain), -left, -top, value, font, 255, "l", track_px)
    start = anchor - {"left": 0, "center": advance * hs / 2, "right": advance * hs}[f.get("justify", "center")]
    baseline = f["box"][1] + f["top_frac"] * size + sag

    fx = f.get("effects") or {}
    margin = max([4] + [w for w, _ in _strokes_of(fx)])
    margin += max([0] + [abs(e.get("dx", 0)) + abs(e.get("dy", 0))
                         + 4 * e.get("blur", e.get("size", 0))
                         for k in ("shadow", "glow", "inner_shadow") if (e := fx.get(k))])
    x0 = max(0, math.floor(min(center - width / 2, start + left * hs) - margin))
    x1 = min(img.width, math.ceil(max(center + width / 2, start + right * hs) + margin))
    y0 = max(0, math.floor(baseline + top - abs(sag) - margin))
    y1 = min(img.height, math.ceil(baseline + bottom + abs(sag) + margin))
    if x1 <= x0 or y1 <= y0:
        return
    xx, yy = np.meshgrid(np.arange(x0, x1, dtype=np.float32), np.arange(y0, y1, dtype=np.float32))
    if abs(sag) > 1e-5:
        theta = 2 * math.atan(2 * abs(sag) / width)
        radius = width / (2 * math.sin(theta))
        nx = (xx - center) / radius
        unwarped_x = center + width / 2 * np.arcsin(np.clip(nx, -1, 1)) / theta
        rise = math.copysign(1, sag) * (np.sqrt(np.maximum(0, radius * radius - (xx - center) ** 2))
                                      - radius * math.cos(theta))
    else:
        unwarped_x, rise = xx, 0
    map_x = ((unwarped_x - start) / hs - left).astype(np.float32)
    map_y = (yy + rise - baseline - top).astype(np.float32)
    warped = cv2.remap(np.asarray(plain), map_x, map_y, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    if abs(sag) > 1e-5:
        warped[np.abs(nx) >= 1] = 0
    mask = Image.fromarray(warped)
    if mask.getbbox():
        img.alpha_composite(_paint_arch_mask(mask, f, tdir, override), (x0, y0))


def _draw_field(img, f, tdir, value, override=None):
    """Bọc quanh _draw_field_raw: nếu PSD nén ngang (HorizontalScale != 1) thì vẽ ở box nới
    rộng 1/hs rồi nén lại đúng hs -> chữ giữ đúng chiều cao thiết kế, không bị auto-shrink cả 2 chiều."""
    if _supports_arch(f.get("text_geometry")):
        _draw_arch_field(img, f, tdir, value, override)
        return
    hs = float(f.get("hscale") or 1.0)
    if abs(hs - 1.0) < 1e-3:
        _draw_field_raw(img, f, tdir, value, override)
        return
    x0, y0, x1, y1 = f["box"]
    cx = (x0 + x1) / 2
    half = (x1 - x0) / (2 * hs)
    f2 = {**f, "box": [cx - half, y0, cx + half, y1]}   # nới box ngang 1/hs để bù phần nén
    work = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _draw_field_raw(work, f2, tdir, value, override)
    img.alpha_composite(_hsqueeze(work, cx, hs))


def _draw_field_raw(img, f, tdir, value, override=None):
    x0, y0, x1, y1 = f["box"]
    box_w = max(1, x1 - x0)
    fpath = tdir / "fonts" / f["font_file"]
    track = float(f.get("track") or 0)              # 1/1000 em (PS tracking)
    size = int(f["size_px"])
    font = ImageFont.truetype(str(fpath), size)
    tp = track / 1000.0 * size
    _, w = _layout(font, value, tp)                 # bề rộng chữ MỚI ở cỡ thiết kế
    # trust-size: giới hạn = max(box, bề rộng chữ GỐC đo cùng font). Đo chữ gốc cùng font để khử
    # độ rộng dư khi font kèm khác bản gốc -> chữ cùng độ dài giữ đúng cỡ + khe; chỉ shrink khi
    # chữ mới rộng hơn cả hai. Font đúng: w_orig<=box -> limit=box (y hệt cũ).
    orig = str(f.get("text") or "")
    limit = box_w
    if orig:
        _, w_orig = _layout(font, orig, tp)
        limit = max(box_w, w_orig)
    if w > limit:                                   # auto-shrink cho vừa (kể cả tracking)
        size = max(4, int(size * limit / w))
        font = ImageFont.truetype(str(fpath), size)
    track_px = track / 1000.0 * size
    oc = _hex(override)                             # mã màu người dùng nhập (rỗng -> giữ màu PSD)
    if f.get("arc"):                                # tên đặt trên cung (banner)
        _draw_arc(img, f, font, value, size, oc)
        return
    fx = f.get("effects") or {}
    fill = oc or _rgba(fx.get("fill") or f["color"])
    strokes = _strokes_of(fx)                       # [(w,rgba)] nhiều viền đồng tâm
    sw = strokes[0][0] if strokes else 0            # viền lớn nhất (cho shadow silhouette)
    scol = strokes[0][1] if strokes else _rgba((0, 0, 0))
    just = f.get("justify", "center")
    ax = {"left": x0, "right": x1, "center": (x0 + x1) / 2}[just]
    anchor = {"left": "l", "right": "r", "center": "m"}[just] + "s"   # +baseline
    baseline = y0 + f["top_frac"] * size
    grad, bev, glow, insh = fx.get("gradient"), fx.get("bevel"), fx.get("glow"), fx.get("inner_shadow")
    pat_img = None                                  # texture "pattern mask vào text" (clip mask trong PSD)
    if f.get("pattern") and not oc:
        pp = tdir / f["pattern"]
        if pp.is_file():
            pat_img = Image.open(pp).convert("RGBA")
    if oc:                                          # ép màu chữ đặc theo mã nhập; giữ viền/bóng/glow
        grad = bev = insh = None

    # ---- 1. outer glow (xa nhất, sau chữ) ----
    if glow:
        gc = _rgba(glow["color"])
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sil = Image.new("L", img.size, 0)
        _draw_tracked(ImageDraw.Draw(sil), ax, baseline, value, font, 255, anchor[0], track_px)
        col = Image.new("RGBA", img.size, gc[:3] + (0,)); col.putalpha(sil)
        lay = col.filter(ImageFilter.GaussianBlur(max(1, glow["size"])))
        op = glow.get("opacity", 255)
        if op < 255:
            lay.putalpha(lay.split()[3].point(lambda p: p * op // 255))
        img.alpha_composite(lay)

    # ---- 2. drop shadow ----
    sh = fx.get("shadow")
    if sh:
        sc = _rgba(sh["color"])
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _draw_tracked(ImageDraw.Draw(lay), ax + sh["dx"], baseline + sh["dy"], value, font,
                      sc, anchor[0], track_px, sw, sc)
        if sh.get("blur"):
            lay = lay.filter(ImageFilter.GaussianBlur(sh["blur"]))
        op = sh.get("opacity", 255)
        if op < 255:
            lay.putalpha(lay.split()[3].point(lambda p: p * op // 255))
        img.alpha_composite(lay)

    # ---- fast-path: fill đặc, không effect fill đặc biệt -> vẽ viền (nhiều lớp) + fill ----
    if not (grad or bev or insh or pat_img):
        _draw_text_multi(img, ax, baseline, value, font, anchor[0], track_px, fill, strokes)
        return

    # ---- fill nâng cao (gradient / bevel): cần alpha mask của chữ ----
    amask = Image.new("L", img.size, 0)
    _draw_tracked(ImageDraw.Draw(amask), ax, baseline, value, font, 255, anchor[0], track_px)
    bbox = amask.getbbox()
    # 3. stroke (nhiều viền đồng tâm) vẽ TRƯỚC fill để nằm dưới
    if strokes:
        st = Image.new("RGBA", img.size, (0, 0, 0, 0))
        std = ImageDraw.Draw(st)
        for wv, col in strokes:
            _draw_tracked(std, ax, baseline, value, font, (0, 0, 0, 0), anchor[0], track_px, wv, col)
        img.alpha_composite(st)
    # 4. fill: pattern > bevel > gradient > solid
    if pat_img is not None and bbox:               # trải texture lên bbox chữ mới, clip theo alpha chữ
        pat = pat_img.resize((bbox[2] - bbox[0], bbox[3] - bbox[1]))
        fi = Image.new("RGBA", img.size, (0, 0, 0, 0))
        fi.paste(pat, (bbox[0], bbox[1]))
        fi.putalpha(amask)
        img.alpha_composite(fi)
    else:
        if bev:
            rgb = _bevel_fill(amask, bev)
        elif grad:
            rgb = _grad_fill(img.size, bbox, grad)
        else:
            rgb = None
        if rgb is not None:
            fi = Image.fromarray(rgb, "RGB").convert("RGBA"); fi.putalpha(amask)
            img.alpha_composite(fi)
    # 5. inner shadow (bóng tối bên trong chữ)
    if insh:
        ic = _rgba(insh["color"])
        shp = Image.new("L", img.size, 0)
        _draw_tracked(ImageDraw.Draw(shp), ax + insh["dx"], baseline + insh["dy"], value, font,
                      255, anchor[0], track_px)
        if insh.get("blur"):
            shp = shp.filter(ImageFilter.GaussianBlur(insh["blur"]))
        # tối = phần trong chữ KHÔNG được silhouette-lệch phủ
        dark = ImageChops.subtract(amask, shp)
        op = insh.get("opacity", 255)
        lay = Image.new("RGBA", img.size, ic[:3] + (0,))
        lay.putalpha(dark.point(lambda p: p * op // 255))
        img.alpha_composite(lay)


def _grad_fill(size_wh, bbox, grad):
    """Ảnh RGB gradient trải trên bbox chữ (dọc theo angle / radial). Ngoài bbox = màu mép."""
    W, H = size_wh
    stops = grad["stops"]
    if grad.get("reversed"):
        stops = [[1 - l, c] for l, c in stops][::-1]
    locs = np.array([l for l, _ in stops]); cols = np.array([c for _, c in stops], float)
    bx0, by0, bx1, by1 = bbox; bw, bh = max(1, bx1 - bx0), max(1, by1 - by0)
    yy, xx = np.mgrid[0:H, 0:W].astype(float)
    if grad["gtype"] == "rad":
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        t = np.clip(np.sqrt(((xx - cx) / (bw / 2)) ** 2 + ((yy - cy) / (bh / 2)) ** 2), 0, 1)
    else:
        a = math.radians(grad["angle"])
        u = np.cos(a) * ((xx - bx0) / bw - .5) - np.sin(a) * ((yy - by0) / bh - .5)
        t = np.clip(u + .5, 0, 1)
    r = np.interp(t, locs, cols[:, 0]); g = np.interp(t, locs, cols[:, 1]); b = np.interp(t, locs, cols[:, 2])
    return np.stack([r, g, b], -1).astype(np.uint8)


def _bevel_fill(amask, bev):
    """Silver/emboss: nền kim loại (trung điểm hl/sh) + highlight mép sáng + shadow mép tối theo góc sáng."""
    a = np.asarray(amask).astype(np.float32) / 255.0
    H, W = a.shape
    # nền kim loại = trung điểm highlight/shadow của chính bevel (bạc/vàng tuỳ màu bevel).
    # Bevel trong PS đè lên fill, nên nền trung tính này cho ánh kim đúng hơn là dùng gradient bên dưới.
    metal = (np.array(bev["hl"], np.float32) + np.array(bev["sh"], np.float32)) / 2
    base = np.ones((H, W, 3), np.float32) * metal
    gy, gx = np.gradient(a)
    ang = math.radians(bev["angle"]); alt = math.radians(bev["alt"])
    lx, ly, lz = math.cos(alt) * math.cos(ang), math.cos(alt) * math.sin(ang), math.sin(alt)
    k = 0.05
    nz = np.sqrt(gx * gx + gy * gy + k * k)
    dot = (-gx * lx + gy * ly + k * lz) / np.maximum(nz, 1e-6)      # -1..1 (dương=hướng sáng)
    hl = np.array(bev["hl"], np.float32); sh = np.array(bev["sh"], np.float32)
    pos = np.clip(dot, 0, 1)[..., None] * (bev["hl_op"] / 100)
    neg = np.clip(-dot, 0, 1)[..., None] * (bev["sh_op"] / 100)
    out = base + pos * (hl - base) + neg * (sh - base)
    return np.clip(out, 0, 255).astype(np.uint8)


def _stamp_arc(layer, f, font, value, size, fill, strokes=(), ox=0, oy=0):
    """Dán từng chữ theo cung parabol lên layer, mỗi tile vẽ NHIỀU viền đồng tâm (lớn->nhỏ) + fill,
    xoay tiếp tuyến, lệch (ox,oy). Dùng cho pass shadow/glow (1 viền) và pass chữ (nhiều viền)."""
    x0, y0, x1, y1 = f["box"]
    W = max(1, x1 - x0); xc = (x0 + x1) / 2
    arc = float(f["arc"])
    just = f.get("justify", "center")
    track_px = float(f.get("track") or 0) / 1000.0 * size    # giãn cách chữ (PS tracking)
    _, tot = _layout(font, value, track_px)
    startx = {"left": x0, "right": x1 - tot, "center": xc - tot / 2}[just]
    Yv = y0 + f["top_frac"] * size                  # baseline tại đỉnh cung
    maxsw = max([w for w, _ in strokes], default=0)
    T = int(size * 3) + 8 + 2 * maxsw; half = T / 2
    cur = startx
    for ch in value:
        cw = font.getlength(ch) + track_px
        t = (cur - xc) / (W / 2)
        py = Yv + arc * t * t                       # baseline y tại chữ này
        slope = arc * 2 * (cur - xc) / ((W / 2) ** 2)
        ang = math.degrees(math.atan(slope))
        tile = Image.new("RGBA", (T, T), (0, 0, 0, 0)); td = ImageDraw.Draw(tile)
        for wv, col in strokes:                     # viền ngoài (lớn) trước, viền trong đè lên
            td.text((half, half), ch, font=font, fill=(0, 0, 0, 0), anchor="ls", stroke_width=wv, stroke_fill=col)
        td.text((half, half), ch, font=font, fill=fill, anchor="ls")     # fill trên cùng
        tile = tile.rotate(-ang, resample=Image.BICUBIC, center=(half, half))
        layer.alpha_composite(tile, (int(round(cur - half + ox)), int(round(py - half + oy))))
        cur += cw


def _draw_arc(img, f, font, value, size, override=None):
    """Chữ cong (banner) + effect: drop shadow, glow, NHIỀU viền. Fill = màu đặc.
    arc>0 = cong lên; mỗi glyph xoay tiếp tuyến với cung."""
    fx = f.get("effects") or {}
    fill = _rgba(override or f["color"])
    strokes = _strokes_of(fx)                        # [(w,rgba)] lớn->nhỏ
    big, bigcol = (strokes[0] if strokes else (0, _rgba((0, 0, 0))))
    # 1. outer glow (silhouette = chữ + viền lớn nhất)
    glow = fx.get("glow")
    if glow:
        gc = _rgba(glow["color"])
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _stamp_arc(lay, f, font, value, size, gc, [(big, gc)] if big else ())
        lay = lay.filter(ImageFilter.GaussianBlur(max(1, glow["size"])))
        op = glow.get("opacity", 255)
        if op < 255:
            lay.putalpha(lay.split()[3].point(lambda p: p * op // 255))
        img.alpha_composite(lay)
    # 2. drop shadow (silhouette gồm cả viền lớn nhất, lệch + blur)
    sh = fx.get("shadow")
    if sh:
        sc = _rgba(sh["color"])
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _stamp_arc(lay, f, font, value, size, sc, [(big, sc)] if big else (), sh["dx"], sh["dy"])
        if sh.get("blur"):
            lay = lay.filter(ImageFilter.GaussianBlur(sh["blur"]))
        op = sh.get("opacity", 255)
        if op < 255:
            lay.putalpha(lay.split()[3].point(lambda p: p * op // 255))
        img.alpha_composite(lay)
    # 3. chữ + nhiều viền đồng tâm (curve theo cung)
    main = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _stamp_arc(main, f, font, value, size, fill, strokes)
    img.alpha_composite(main)


def render_one(tpl, tdir, values):
    """values: {key: text, __c_<key>: hex, __bg: hex}. Trả ảnh RGBA đã vẽ tên mới."""
    base = Image.open(tdir / tpl["base"]).convert("RGBA")
    bg = tpl.get("background")
    if bg:                                          # tô nền (mã user nhập, rỗng -> màu gốc) rồi dán art lên
        oc = _hex(values.get("__bg")) or _rgba(bg["color"])
        img = Image.new("RGBA", base.size, tuple(oc[:3]) + (255,))
        img.alpha_composite(base)
    else:
        img = base                                  # template cũ: nền đã bake trong base.png
    for f in tpl["fields"]:
        v = values.get(f["key"], "")
        if v != "":
            _draw_field(img, f, tdir, str(v), values.get("__c_" + f["key"]))
    if tpl.get("base_above"):                        # art nằm TRÊN chữ -> dán đè lại, giữ đúng z-order
        img.alpha_composite(Image.open(tdir / tpl["base_above"]).convert("RGBA"))
    return img


def render_rows(slug, rows, data_dir, out_dir, fmts=("png", "jpg")):
    """rows: list[dict] (mỗi dict 1 đơn, có key field + 'order'). fmts: định dạng xuất ('png'/'jpg').
    Chỉ lưu định dạng được chọn. Trả list path ĐẠI DIỆN mỗi đơn (để preview)."""
    tpl = load_template(slug, data_dir)
    if not tpl:
        raise ValueError(f"template không tồn tại: {slug}")
    tdir = _reg(data_dir) / slug
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dpi = tpl.get("dpi", 72)
    fmts = [f for f in ("png", "jpg") if f in fmts] or ["png"]   # ít nhất 1 định dạng
    outs = []
    for i, row in enumerate(rows, 1):
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(row.get("order") or "")).strip() or "don"
        img = render_one(tpl, tdir, row)
        stem = f"{i:03d}_{safe}"                          # prefix index -> không đè nhau
        made = []
        if "png" in fmts:
            pp = out_dir / f"{stem}.png"; img.save(pp, dpi=(dpi, dpi)); made.append(pp)
        if "jpg" in fmts:
            jp = out_dir / f"{stem}.jpg"                 # q100 + subsampling=0: gần lossless, hết mờ mép chữ
            img.convert("RGB").save(jp, quality=100, subsampling=0, dpi=(dpi, dpi)); made.append(jp)
        outs.append(made[0])                             # ưu tiên PNG cho preview nếu có
    return outs


def zip_paths(paths, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, Path(p).name)
    return zip_path


# ---------- self-check ----------
def _selfcheck_effects():
    """Kiểm cơ chế render effect (không cần PSD): gradient biến thiên, bevel sáng-tối 2 phía."""
    m = Image.new("L", (300, 300), 0)
    ImageDraw.Draw(m).ellipse((60, 60, 240, 240), fill=255)   # glyph tròn giả
    bbox = m.getbbox()
    # gradient dọc: đỉnh khác đáy
    g = _grad_fill((300, 300), bbox, {"stops": [[0.0, [20, 20, 20]], [1.0, [230, 230, 230]]],
                                      "gtype": "lin", "angle": 90.0, "reversed": False})
    assert abs(int(g[bbox[1] + 5, 150, 0]) - int(g[bbox[3] - 5, 150, 0])) > 100, "gradient không biến thiên dọc"
    # bevel: highlight (mép hướng sáng) sáng hơn shadow (mép đối diện)
    b = _bevel_fill(m, {"hl": [255, 255, 255], "sh": [0, 0, 0], "hl_op": 100, "sh_op": 100,
                        "angle": 90.0, "alt": 30.0})
    lum = b[..., 0][np.asarray(m) > 128]                      # độ sáng trong glyph
    assert lum.max() - lum.min() > 60, f"bevel không tạo tương phản ({lum.min()}..{lum.max()})"
    # hex override: 3 dạng hợp lệ + loại rỗng/sai
    assert _hex("#e01919") == (224, 25, 25, 255) and _hex("e01919") == (224, 25, 25, 255)
    assert _hex("#f00") == (255, 0, 0, 255)
    assert _hex("") is None and _hex(None) is None and _hex("xyz") is None and _hex("#12") is None
    # tracking: giãn cách -> tổng bề ngang rộng hơn; track=0 giữ nguyên
    fnt = ImageFont.load_default()
    _, w0 = _layout(fnt, "ABC", 0); _, wT = _layout(fnt, "ABC", 10)
    assert wT - w0 == 20, f"tracking phải cộng 10px x2 khoảng, được {wT-w0}"
    print(f"OK self-check effect: gradient biến thiên; bevel sáng-tối {lum.min()}..{lum.max()}; hex parse OK")


def _demo():
    """Học 1 PSD mẫu rồi render tên mới — kiểm tra pipeline không vỡ."""
    import tempfile
    _selfcheck_effects()
    sample = os.getenv("MAGNET_SAMPLE")
    if not sample or not Path(sample).is_file():
        print("skip demo (đặt MAGNET_SAMPLE=<đường dẫn .psd> để chạy)")
        return
    tmp = Path(tempfile.mkdtemp())
    a = analyze(sample)
    assert a["fields"], "phải dò ra ít nhất 1 field"
    for f in a["fields"]:
        assert f["size_px"] > 0 and len(f["color"]) == 4
    tpl = save_template(sample, "demo", a["fields"], tmp)
    assert (tmp / "magnet_templates" / "demo" / "base.png").is_file()
    row = {"order": "T1", **{f["key"]: "TEST" for f in tpl["fields"]}}
    paths = render_rows("demo", [row], tmp, tmp / "out")
    assert paths and paths[0].stat().st_size > 0
    print(f"OK demo: {len(a['fields'])} field -> {paths[0]} ({paths[0].stat().st_size}B)")


if __name__ == "__main__":
    _demo()
