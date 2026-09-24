#!/usr/bin/env python3
"""Đọc HẾT thông số / effect / layer của 1 PSD TRƯỚC khi làm case mới.

Nguyên tắc completeness: WHITELIST cái tool xử lý được, còn lại TỰ LA (kể cả
key/effect tương lai chưa biết). Xem PSD-CHECKLIST.md. Dùng lại bộ đọc của
magnet.py -> khớp tool 100%.

Lưu ý kiến trúc tool: chỉ LAYER TEXT mới được tái tạo; mọi layer khác (art,
smartobject, adjustment, group, blend...) được NƯỚNG thẳng từ composite PSD nên
giữ nguyên -> completeness chỉ cần soi kỹ: (1) mọi tham số/effect/warp layer
text, (2) màu bake (color mode).

    python3 inspect_psd.py "<duong_dan.psd>"
"""
import sys
from psd_tools import PSDImage
import magnet as m

_ARC_LIKE = {"warpArc", "warpArcUpper", "warpArcLower"}

# --- StyleSheetData: 3 nhóm, key nào không thuộc 2 nhóm đầu mà lệch NEUTRAL -> flag ---
_HANDLED = {"Font", "FontSize", "FillColor", "Tracking", "HorizontalScale",
            "StyleRunAlignment"}                       # tool áp (justify đọc từ ParagraphRun)
_BENIGN = {"Language", "HindiNumbers", "Kashida", "Tsume", "BaselineDirection",
           "NoBreak", "DLigatures", "Ligatures", "YUnderline", "AutoKerning",
           "AutoLeading", "Leading", "StrokeColor"}    # không đổi raster / mặc định luôn bật
_NEUTRAL = {"BaselineShift": 0.0, "FauxBold": False, "FauxItalic": False,
            "FontCaps": 0, "FontBaseline": 0, "VerticalScale": 1.0,
            "Underline": False, "Strikethrough": False, "Kerning": 0}
_LABEL = {"FontCaps": "chữ HOA/small-caps -> tool KHÔNG tự viết hoa",
          "FauxBold": "giả đậm", "FauxItalic": "giả nghiêng",
          "VerticalScale": "kéo dọc (không áp vào vẽ)", "BaselineShift": "dời baseline",
          "FontBaseline": "super/subscript", "Underline": "gạch chân",
          "Strikethrough": "gạch ngang", "Kerning": "kern tay"}

_HANDLED_FX = {"ColorOverlay", "Stroke", "DropShadow", "GradientOverlay",
               "OuterGlow", "InnerShadow", "BevelEmboss"}


def _flag_style(sd):
    """Mọi key KHÔNG whitelist mà lệch mặc định -> cảnh báo (unknown key luôn báo)."""
    out = []
    for k in sorted(sd.keys()):
        if k in _HANDLED or k in _BENIGN:
            continue
        v = sd[k]
        if k in _NEUTRAL:
            d = _NEUTRAL[k]
            same = (abs(float(v) - float(d)) < 1e-3) if isinstance(d, (int, float)) and not isinstance(d, bool) else (bool(v) == d)
            if same:
                continue
            out.append(f"{k}={v!r} ({_LABEL.get(k, 'bị bỏ')})")
        else:
            out.append(f"{k}={v!r} (KEY LẠ chưa phân loại -> soi thủ công)")
    return out


def _warp_status(style):
    if style == "warpNone":
        return "○ warp=None: nếu pixel cong đủ lớn -> vẫn detect qua fallback (banner LTL)"
    if style == "warpArch":
        return ("⚠ XẤP XỈ: Arch thật chữ ĐỨNG THẲNG (chỉ mép cong). SOI xem có nghiêng sai")
    if style in _ARC_LIKE:
        return "~ gần đúng: parabol + xoay glyph (thay cung tròn)"
    return f"⚠ CHƯA DỰNG: {style} -> tool coi THẲNG, phải báo user"


def main(path):
    psd = PSDImage.open(path)
    print(f"# {path}   canvas {psd.width}x{psd.height}  color_mode={psd.color_mode} depth={psd.depth}")
    if int(psd.color_mode) != 3:
        print("  ⚠ COLOR MODE KHÔNG PHẢI RGB -> màu bake có thể lệch, kiểm kỹ")
    if psd.depth != 8:
        print(f"  ⚠ depth={psd.depth} (không phải 8-bit) -> kiểm màu bake")
    bg_name, bg_color = m._bg_layer(psd)
    print(f"Background solid layer: "
          f"{f'{bg_name!r} màu {bg_color}' if bg_name else '(không có)'}")
    kinds = {}
    for l in psd.descendants():
        kinds[l.kind] = kinds.get(l.kind, 0) + 1
    print(f"Layer kinds: {kinds}  (chỉ 'type' được tái tạo; còn lại bake nguyên)")
    print("=" * 70)

    for l in psd.descendants():
        if l.kind != "type":
            continue
        font, size, color, just, track, hscale = m._style(l)
        fx = m._effects(l)
        clip = m._clip_names(l)
        geo = m._text_geometry(l)
        try:
            text = l.text.replace("\r", " ").replace("\n", " ")
        except Exception:
            text = "?"
        print(f"\n▶ TYPE LAYER: {l.name!r}   text={text!r}")
        print(f"  font={font!r}  design_size={m._design_size(l):.1f}px  "
              f"FillColor={color}  justify={just}")
        print(f"  Tracking={track}  HorizontalScale={hscale}")
        # layer-level: chỉ quan trọng vì đây LÀ layer text (tool tự vẽ, không bake)
        if l.opacity != 255 or str(l.blend_mode) != "BlendMode.NORMAL" or l.has_mask():
            print(f"  ⚠ LAYER TEXT: opacity={l.opacity} blend={l.blend_mode} "
                  f"mask={l.has_mask()} -> tool vẽ normal/đục, sẽ sai")
        if "\r" in (l.text or "") or "\n" in (l.text or ""):
            print("  ⚠ TEXT NHIỀU DÒNG -> tool xử lý 1 dòng, kiểm leading/xuống dòng")
        for r in _flag_style(sd := l.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]):
            print(f"  ⚠ THÔNG SỐ BỊ BỎ (báo user!): {r}")
        # effects: đối chiếu TRỰC TIẾP mọi effect layer có với whitelist
        try:
            raw = [type(e).__name__ for e in (l.effects or []) if getattr(e, "enabled", True)]
        except Exception:
            raw = []
        drawn = [k for k in ("fill", "strokes", "shadow", "inner_shadow",
                             "gradient", "glow", "bevel") if fx.get(k)]
        print(f"  Effects trên layer: {raw or '(none)'}")
        print(f"  Effects dựng được: {drawn or '(none)'}")
        if fx.get("strokes"):
            print(f"    strokes(w,color) = {[(s['w'], s['color']) for s in fx['strokes']]}")
        for name in raw:
            if name not in _HANDLED_FX:
                print(f"  ⚠ EFFECT CHƯA HỖ TRỢ (báo user!): {name}")
        if clip:
            print(f"  Clipping mask (pattern trong chữ): {clip}")
        if geo:
            w = geo["warp"]
            print(f"  Warp: style={w['style']} bend={w['bend']} orient={w['orientation']} "
                  f"-> arc_px={m._detect_arc(l):.0f}")
            print(f"    -> {_warp_status(w['style'])}")
            if w["orientation"] != "Hrzn":
                print(f"  ⚠ warp orientation={w['orientation']} (không phải Hrzn) -> chưa dựng")
            if w.get("perspective") or w.get("perspective_other"):
                print("  ⚠ warp có perspective -> chưa dựng, báo")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("dùng: python3 inspect_psd.py <file.psd>")
    main(sys.argv[1])
