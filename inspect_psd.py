#!/usr/bin/env python3
"""Đọc HẾT thông số mỗi layer text trong 1 PSD trước khi làm case mới.
Xem PSD-CHECKLIST.md. Dùng lại chính bộ đọc của magnet.py -> khớp tool 100%.

    python inspect_psd.py "<duong_dan.psd>"
"""
import sys
from psd_tools import PSDImage
import magnet as m

# Trạng thái từng warpStyle so với cái tool THẬT SỰ dựng được (xem PSD-CHECKLIST.md)
_ARC_LIKE = {"warpArc", "warpArcUpper", "warpArcLower"}


def _warp_status(style):
    if style == "warpNone":
        return "✅ thẳng"
    if style == "warpArch":
        return ("⚠ XẤP XỈ: tool xoay glyph như Arc, nhưng Arch thật chữ ĐỨNG THẲNG "
                "(chỉ mép cong). SOI MẮT xem chữ có nghiêng sai không")
    if style in _ARC_LIKE:
        return "~ gần đúng: parabol + xoay glyph (thay cho cung tròn)"
    return f"⚠ CHƯA DỰNG: {style} -> tool coi THẲNG, phải báo user"


def main(path):
    psd = PSDImage.open(path)
    print(f"# {path}   canvas {psd.width}x{psd.height}")
    bg_name, bg_color = m._bg_layer(psd)
    print(f"Background solid layer: "
          f"{f'{bg_name!r} màu {bg_color}' if bg_name else '(không có)'}")
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
        print(f"\n▶ LAYER: {l.name!r}   text={text!r}")
        print(f"  font={font!r}  design_size={m._design_size(l):.1f}px  "
              f"FillColor={color}  justify={just}")
        print(f"  Tracking={track}  HorizontalScale={hscale}")
        # effect nào có
        keys = [k for k in ("fill", "strokes", "shadow", "inner_shadow",
                            "gradient", "glow", "bevel") if fx.get(k)]
        print(f"  Effects dựng được: {keys or '(none)'}")
        if fx.get("strokes"):
            print(f"    strokes(w,color) = "
                  f"{[(s['w'], s['color']) for s in fx['strokes']]}")
        if clip:
            print(f"  Clipping mask (pattern trong chữ): {clip}")
        if geo:
            w = geo["warp"]
            print(f"  Warp: style={w['style']} bend={w['bend']} "
                  f"orient={w['orientation']} -> arc_px={m._detect_arc(l):.0f}")
            print(f"    -> {_warp_status(w['style'])}")
        if fx.get("_unsupported"):
            print(f"  ⚠ EFFECT CHƯA HỖ TRỢ (báo user!): {fx['_unsupported']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("dùng: python inspect_psd.py <file.psd>")
    main(sys.argv[1])
