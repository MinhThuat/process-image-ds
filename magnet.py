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
from PIL import Image, ImageDraw, ImageFilter, ImageFont
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
    return files[0] if len(files) == 1 else None   # 1 font duy nhất -> chắc là nó


# ---------- đọc style layer text ----------
def _style(layer):
    """(font_name, size_pt, color_rgba, justify) từ engine_dict; giá trị mặc định nếu thiếu."""
    font_name, size_pt, color, just = "", None, (0, 0, 0, 255), "center"
    try:
        ed = layer.engine_dict
        sd = ed["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
        fonts = layer.resource_dict["FontSet"]
        font_name = str(fonts[sd.get("Font", 0)]["Name"])
        size_pt = float(sd.get("FontSize", 0)) or None
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
    return font_name, size_pt, color, just


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


def _color(d):
    """{Rd,Grn,Bl} trong descriptor effect (thang 0..255) -> (r,g,b)."""
    if not d:
        return None
    v = [float(d.get(b"Rd  ", 0)), float(d.get(b"Grn ", 0)), float(d.get(b"Bl  ", 0))]
    return tuple(max(0, min(255, int(round(x)))) for x in v)


def _effects(layer):
    """Effect trên layer tên: fill (ColorOverlay), stroke, drop shadow. {} nếu không có.

    Vẽ lại bằng Pillow (stroke_width, shadow offset+blur). Bevel/gradient/glow chưa hỗ trợ.
    """
    out = {}
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
            if c:
                # PS "outside" ~ gấp đôi bề rộng Pillow (Pillow vẽ stroke giữa nét)
                mul = 2 if e.position == b"OutF" else 1
                out["stroke"] = round(float(e.size) * mul)
                out["stroke_color"] = list(c)
        elif n == "DropShadow":
            c = _color(e.color)
            if c:
                ang = math.radians(float(e.angle)); dist = float(e.distance)
                out["shadow"] = {"dx": round(-dist * math.cos(ang)), "dy": round(dist * math.sin(ang)),
                                 "color": list(c), "blur": round(float(e.size)),
                                 "opacity": round(float(e.opacity) * 255 / 100)}
    return out


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
        font_name, _size_pt, color, just = _style(l)
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
            "arc": arc, "effects": _effects(l),
        })
    preview = psd.composite()
    return {"canvas": list(psd.size), "fields": fields, "preview": preview}


# ---------- build base (ẩn field) ----------
def _build_base(psd_path, layer_names):
    psd = PSDImage.open(str(psd_path))
    want = set(layer_names)
    for l in psd.descendants():
        if l.kind == "type" and l.name in want:
            l.visible = False
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
    base = _build_base(psd_path, [f["layer"] for f in fields])
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
            out.append({"slug": t["slug"], "fields": [f["key"] for f in t["fields"]]})
    return out


def csv_header(tpl):
    return "order," + ",".join(f["key"] for f in tpl["fields"]) + "\n"


# ---------- render ----------
def _draw_field(img, f, tdir, value):
    x0, y0, x1, y1 = f["box"]
    box_w = max(1, x1 - x0)
    fpath = tdir / "fonts" / f["font_file"]
    size = int(f["size_px"])
    font = ImageFont.truetype(str(fpath), size)
    w = font.getlength(value)
    if w > box_w:                                   # auto-shrink cho vừa bề ngang
        size = max(4, int(size * box_w / w))
        font = ImageFont.truetype(str(fpath), size)
    if f.get("arc"):                                # tên đặt trên cung (banner)
        _draw_arc(img, f, font, value, size)
        return
    fx = f.get("effects") or {}
    fill = _rgba(fx.get("fill") or f["color"])
    sw = int(fx.get("stroke") or 0)
    scol = _rgba(fx.get("stroke_color") or (0, 0, 0))
    just = f.get("justify", "center")
    ax = {"left": x0, "right": x1, "center": (x0 + x1) / 2}[just]
    anchor = {"left": "l", "right": "r", "center": "m"}[just] + "s"   # +baseline
    baseline = y0 + f["top_frac"] * size
    sh = fx.get("shadow")
    if sh:                                          # đổ bóng: vẽ bản silhouette lệch + mờ, ghép phía sau
        sc = _rgba(sh["color"])
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).text((ax + sh["dx"], baseline + sh["dy"]), value, font=font,
                                 fill=sc, anchor=anchor, stroke_width=sw, stroke_fill=sc)
        if sh.get("blur"):
            lay = lay.filter(ImageFilter.GaussianBlur(sh["blur"]))
        op = sh.get("opacity", 255)
        if op < 255:
            lay.putalpha(lay.split()[3].point(lambda p: p * op // 255))
        img.alpha_composite(lay)
    draw = ImageDraw.Draw(img)
    draw.text((ax, baseline), value, font=font, fill=fill, anchor=anchor,
              stroke_width=sw, stroke_fill=scol)


def _draw_arc(img, f, font, value, size):
    """Vẽ từng chữ dọc theo parabol: đỉnh ở giữa box, hai mép thấp hơn |arc| px.
    arc>0 = cong lên (cười); mỗi glyph xoay tiếp tuyến với cung."""
    x0, y0, x1, y1 = f["box"]
    W = max(1, x1 - x0); xc = (x0 + x1) / 2
    arc = float(f["arc"])
    color = tuple(f["color"])
    just = f.get("justify", "center")
    tot = font.getlength(value)
    startx = {"left": x0, "right": x1 - tot, "center": xc - tot / 2}[just]
    Yv = y0 + f["top_frac"] * size                  # baseline tại đỉnh cung
    T = int(size * 3) + 8; half = T / 2
    cur = startx
    for ch in value:
        cw = font.getlength(ch)
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
    """values: {key: text}. Trả ảnh RGBA đã vẽ tên mới."""
    img = Image.open(tdir / tpl["base"]).convert("RGBA")
    for f in tpl["fields"]:
        v = values.get(f["key"], "")
        if v != "":
            _draw_field(img, f, tdir, str(v))
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
def _demo():
    """Học 1 PSD mẫu rồi render tên mới — kiểm tra pipeline không vỡ."""
    import tempfile
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
