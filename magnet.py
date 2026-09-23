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
                out["stroke"] = sw
                out["stroke_color"] = list(c)
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
    if unsupported:
        out["_unsupported"] = unsupported
    return out


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


def _detect_arc(layer):
    """Độ cong (sagitta px, dương = cong lên/cười) của tên đặt trên cung. 0 = thẳng.

    PSD không lưu type-on-path, nên tự dò: khớp parabol vào 'đáy mực' theo cột.
    """
    try:
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
        cal_box = [box[0], box[1], box[2], box[3] - int(abs(arc))]   # bỏ phần cong khi tính cỡ chữ
        size_px, top_frac = _calibrate(ff, txt, cal_box) if ff else (max(1, cal_box[3] - cal_box[1]), 0.8)
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
        })
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


# ---------- build base (ẩn field + nền) ----------
def _build_base(psd_path, layer_names, bg_name=None):
    psd = PSDImage.open(str(psd_path))
    want = set(layer_names)
    for l in psd.descendants():
        if (l.kind == "type" and l.name in want) or (bg_name and l.name == bg_name):
            l.visible = False                       # nền ẩn -> base trong suốt chỗ nền, tô lại lúc render
    return psd.composite(force=True).convert("RGBA")


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
    base = _build_base(psd_path, [f["layer"] for f in fields], bg_name)
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
        saved.append({**f, "font_file": fn})
    tpl = {"slug": slug, "canvas": list(PSDImage.open(str(psd_path)).size),
           "dpi": dpi, "base": "base.png", "fields": saved}
    if bg_name:
        tpl["background"] = {"layer": bg_name, "color": bg_color}   # tô lại lúc render, mặc định = màu gốc
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


def _hsqueeze(layer, cx, s):
    """Nén ngang cả lớp theo hệ số s quanh trục x=cx (giữ nguyên chiều cao)."""
    W, H = layer.size
    sc = layer.resize((max(1, int(round(W * s))), H), Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(sc, (int(round(cx - cx * s)), 0))         # điểm cx giữ nguyên vị trí
    return out


def _draw_field(img, f, tdir, value, override=None):
    """Bọc quanh _draw_field_raw: nếu PSD nén ngang (HorizontalScale != 1) thì vẽ ở box nới
    rộng 1/hs rồi nén lại đúng hs -> chữ giữ đúng chiều cao thiết kế, không bị auto-shrink cả 2 chiều."""
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
    sw = int(fx.get("stroke") or 0)
    scol = _rgba(fx.get("stroke_color") or (0, 0, 0))
    just = f.get("justify", "center")
    ax = {"left": x0, "right": x1, "center": (x0 + x1) / 2}[just]
    anchor = {"left": "l", "right": "r", "center": "m"}[just] + "s"   # +baseline
    baseline = y0 + f["top_frac"] * size
    grad, bev, glow, insh = fx.get("gradient"), fx.get("bevel"), fx.get("glow"), fx.get("inner_shadow")
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

    # ---- fast-path: fill đặc, không effect fill đặc biệt -> vẽ 1 lệnh (như cũ, đã kiểm) ----
    if not (grad or bev or insh):
        _draw_tracked(ImageDraw.Draw(img), ax, baseline, value, font, fill, anchor[0],
                      track_px, sw, scol)
        return

    # ---- fill nâng cao (gradient / bevel): cần alpha mask của chữ ----
    amask = Image.new("L", img.size, 0)
    _draw_tracked(ImageDraw.Draw(amask), ax, baseline, value, font, 255, anchor[0], track_px)
    bbox = amask.getbbox()
    # 3. stroke (viền) vẽ TRƯỚC fill để nằm dưới
    if sw:
        st = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _draw_tracked(ImageDraw.Draw(st), ax, baseline, value, font, (0, 0, 0, 0),
                      anchor[0], track_px, sw, scol)
        img.alpha_composite(st)
    # 4. fill: bevel > gradient > solid
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


def _draw_arc(img, f, font, value, size, override=None):
    """Vẽ từng chữ dọc theo parabol: đỉnh ở giữa box, hai mép thấp hơn |arc| px.
    arc>0 = cong lên (cười); mỗi glyph xoay tiếp tuyến với cung."""
    x0, y0, x1, y1 = f["box"]
    W = max(1, x1 - x0); xc = (x0 + x1) / 2
    arc = float(f["arc"])
    color = override or tuple(f["color"])
    just = f.get("justify", "center")
    track_px = float(f.get("track") or 0) / 1000.0 * size    # giãn cách chữ (PS tracking)
    _, tot = _layout(font, value, track_px)
    startx = {"left": x0, "right": x1 - tot, "center": xc - tot / 2}[just]
    Yv = y0 + f["top_frac"] * size                  # baseline tại đỉnh cung
    T = int(size * 3) + 8; half = T / 2
    cur = startx
    for ch in value:
        cw = font.getlength(ch) + track_px
        t = (cur - xc) / (W / 2)
        py = Yv + arc * t * t                       # baseline y tại chữ này
        slope = arc * 2 * (cur - xc) / ((W / 2) ** 2)
        ang = math.degrees(math.atan(slope))
        tile = Image.new("RGBA", (T, T), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((half, half), ch, font=font, fill=color, anchor="ls")
        tile = tile.rotate(-ang, resample=Image.BICUBIC, center=(half, half))
        img.alpha_composite(tile, (int(round(cur - half)), int(round(py - half))))
        cur += cw


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
    return img


def render_rows(slug, rows, data_dir, out_dir):
    """rows: list[dict] (mỗi dict 1 đơn, có key field + 'order'). Trả list path PNG."""
    tpl = load_template(slug, data_dir)
    if not tpl:
        raise ValueError(f"template không tồn tại: {slug}")
    tdir = _reg(data_dir) / slug
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dpi = tpl.get("dpi", 72)
    paths = []
    for i, row in enumerate(rows, 1):
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(row.get("order") or "")).strip() or "don"
        img = render_one(tpl, tdir, row)
        p = out_dir / f"{i:03d}_{safe}.png"             # prefix index -> không đè nhau
        img.save(p, dpi=(dpi, dpi))                     # giữ DPI gốc của PSD (vd 300)
        img.convert("RGB").save(p.with_suffix(".jpg"),  # xuất kèm JPG (nền trắng)
                                quality=95, dpi=(dpi, dpi))
        paths.append(p)
    return paths


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
