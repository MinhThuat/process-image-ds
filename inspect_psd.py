#!/usr/bin/env python3
"""Xuất báo cáo toàn bộ cấu trúc PSD mà psd-tools đọc được.

Mặc định: in mọi trường đã đọc cùng trạng thái, ghi JSON và .audit.txt.
--summary chỉ rút gọn terminal. --include-binary nhúng cả payload nhị phân.
CHƯA XÁC MINH không đồng nghĩa renderer đã xử lý hoặc chắc chắn bỏ qua.

    python3 inspect_psd.py "<duong_dan.psd>" --output report.json
"""
import argparse
import base64
import hashlib
import json
import logging
import math
import sys
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from math import hypot as _hypot, atan2 as _atan2, degrees as _deg
import attrs
import psd_tools
from psd_tools import PSDImage
from psd_tools.constants import Tag

class PSDEncoder:
    """Keep descriptor types, units, keys and every attrs field, without repr cuts."""

    def __init__(self, include_binary=False):
        self.include_binary = include_binary
        self.issues = []
        self.binary_summaries = []

    def encode(self, value, path="$", active=None):
        if active is None:
            active = set()
        if isinstance(value, Enum):
            return {"_enum": type(value).__name__, "name": value.name,
                    "value": self.encode(value.value, path, active)}
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            self.issues.append({"path": path, "error": "non-finite float", "value": str(value)})
            return {"_float": str(value)}
        if isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            result = {"_type": "bytes", "length": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}
            if self.include_binary or len(data) <= 64:
                result["base64"] = base64.b64encode(data).decode("ascii")
                if all(32 <= b < 127 for b in data):
                    result["ascii"] = data.decode("ascii")
            else:
                result["payload_omitted"] = True
                self.binary_summaries.append({"path": path, "length": len(data)})
            return result
        ident = id(value)
        if ident in active:
            self.issues.append({"path": path, "error": "cyclic reference"})
            return {"_error": "cyclic reference"}
        active.add(ident)
        try:
            if attrs.has(type(value)):
                # Do this BEFORE Mapping: descriptors also have classID/name/unit.
                return {"_type": type(value).__module__ + "." + type(value).__name__,
                        "fields": {f.name: self.encode(getattr(value, f.name),
                                    f"{path}.{f.name}", active)
                                   for f in attrs.fields(type(value))}}
            if isinstance(value, Mapping):
                if all(isinstance(k, str) for k in value):
                    return {k: self.encode(v, f"{path}.{k}", active) for k, v in value.items()}
                # Entries preserve binary keys and avoid string/int key collisions.
                return {"_entries": [
                    {"key": self.encode(k, f"{path}[{i}].key", active),
                     "value": self.encode(v, f"{path}[{i}].value", active)}
                    for i, (k, v) in enumerate(value.items())]}
            if isinstance(value, (list, tuple)):
                return [self.encode(v, f"{path}[{i}]", active) for i, v in enumerate(value)]
            self.issues.append({"path": path, "error": "unhandled Python type",
                                "type": type(value).__module__ + "." + type(value).__name__})
            return {"_unhandled_type": type(value).__name__, "repr": repr(value)}
        except Exception as exc:
            self.issues.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
            return {"_error": f"{type(exc).__name__}: {exc}"}
        finally:
            active.remove(ident)


def transform_details(transform):
    """PSD affine matrix: x'=a*x+c*y+tx; y'=b*x+d*y+ty (y down)."""
    a, b, c, d, tx, ty = map(float, transform)
    sx, sy = _hypot(a, b), _hypot(c, d)
    determinant = a * d - b * c
    dot = a * c + b * d
    return {
        "matrix": [a, b, c, d, tx, ty],
        "rotation_degrees": _deg(_atan2(b, a)) if sx else None,
        "y_axis_rotation_degrees": _deg(_atan2(-c, d)) if sy else None,
        "scale_x": sx, "scale_y_axis_length": sy,
        "signed_scale_y_after_rotation": determinant / sx if sx else None,
        "shear_x_after_rotation": dot / sx if sx else None,
        "skew_degrees": _deg(_atan2(dot, abs(determinant))) if sx and sy else None,
        "translation": [tx, ty], "determinant": determinant,
        "reflected": determinant < 0, "singular": determinant == 0,
        "convention": "Degrees from +x, clockwise in PSD y-down coordinates. "
                      "Rotation is the x-axis orientation; matrix is authoritative with skew/reflection.",
    }


def inspect_layer(layer, index_path, encoder):
    result = {"index_path": index_path, "name": layer.name, "kind": layer.kind}
    prefix = "layers." + index_path

    def read(name, getter):
        try:
            result[name] = encoder.encode(getter(), prefix + "." + name)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result[name] = {"_error": error}
            encoder.issues.append({"path": prefix + "." + name, "error": error})

    for name in ("layer_id", "visible", "opacity", "blend_mode", "bbox", "clipping"):
        read(name, lambda name=name: getattr(layer, name))
    read("effective_visibility", layer.is_visible)
    read("fill_opacity", lambda: int(layer.tagged_blocks.get_data(Tag.BLEND_FILL_OPACITY, 255)))
    # Include every layer record: masks, blend ranges, flags, all tagged blocks,
    # vector paths, adjustment settings, text descriptors, smart-object metadata.
    read("record", lambda: layer._record)
    if layer.kind == "type":
        for name in ("text", "engine_dict", "resource_dict", "document_resources"):
            read(name, lambda name=name: getattr(layer, name))
        read("transform", lambda: transform_details(layer.transform))
        read("type_tool", lambda: layer._data)
    # API effects may omit non-present/unknown descriptors. All originals above
    # remain in tagged blocks; never rely on this convenience list alone.
    try:
        effects = layer.effects
        result["effects_enabled"] = effects.enabled if effects._data is not None else None
        result["effects_scale"] = effects.scale if effects._data is not None else None
        result["effects"] = []
        for i, effect in enumerate(effects):
            entry = {"type": type(effect).__name__, "properties": {}}
            names = {name for cls in type(effect).__mro__ for name, member in vars(cls).items()
                     if isinstance(member, property) and not name.startswith("_")}
            for name in sorted(names):
                prop_path = f"{prefix}.effects[{i}].{name}"
                try:
                    entry["properties"][name] = encoder.encode(getattr(effect, name), prop_path)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    entry["properties"][name] = {"_error": error}
                    encoder.issues.append({"path": prop_path, "error": error})
            result["effects"].append(entry)
    except Exception as exc:
        encoder.issues.append({"path": prefix + ".effects", "error": str(exc)})
        result["effects_api_error"] = str(exc)
    if layer.is_group():
        result["children"] = [inspect_layer(child, f"{index_path}/{i}", encoder)
                              for i, child in enumerate(layer)]
    return result


class ParseMessages(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append({"level": record.levelname, "message": record.getMessage()})


def _key_label(value):
    """Readable labels only; entry index keeps even identical labels distinct."""
    if isinstance(value, dict):
        if "_enum" in value:
            return value["name"]
        if value.get("_type") == "bytes":
            return value.get("ascii", "bytes:" + value["sha256"])
        fields = value.get("fields", {})
        if set(fields) == {"value"}:
            return _key_label(fields["value"])
    return str(value)


def _encoded_scalar(value):
    while isinstance(value, dict) and set(value.get("fields", {})) == {"value"}:
        value = value["fields"]["value"]
    return value


def audit_tree(value, path, disabled=False):
    """Visit every encoded field. Unknown means unverified, never supported.

    This inventories parsed data, not Photoshop rendering capabilities. Bytes
    remain opaque even when base64 is included. No binary body is printed.
    """
    status = "ĐANG TẮT" if disabled else "CHƯA XÁC MINH"

    def row(data, state=status, at=path):
        return {"path": at, "value": data, "status": state,
                "disabled_context": disabled}

    if isinstance(value, dict):
        if value.get("_type") == "bytes":
            yield row({k: v for k, v in value.items() if k != "base64"}, "KHÔNG GIẢI MÃ")
            return
        if "_error" in value or "_unhandled_type" in value or "_float" in value:
            yield row(value, "LỖI")
            return
        if "_enum" in value:
            yield row(value)
            return
        if "fields" in value and "_type" in value:
            fields = value["fields"]
            entries = fields.get("_items", {}).get("_entries", []) if isinstance(fields.get("_items"), dict) else []
            switches = {_key_label(e["key"]): _encoded_scalar(e["value"]) for e in entries}
            inactive = disabled or any(switches.get(k) is False for k in ("enab", "present", "masterFXSwitch"))
            yield row(value["_type"], "ĐÃ GHI NHẬN", path + "/@type")
            for name, item in fields.items():
                yield from audit_tree(item, path if name == "_items" else path + "/" + name, inactive)
            if not fields:
                yield row({})
            return
        if "_entries" in value:
            if not value["_entries"]:
                yield row({})
            for i, entry in enumerate(value["_entries"]):
                # JSON escaping prevents line breaks/slashes in names hiding a field.
                label = json.dumps(_key_label(entry["key"]), ensure_ascii=False)
                entry_path = f"{path}/[{i}:{label}]"
                yield row(entry["key"], "ĐÃ GHI NHẬN", entry_path + "/@key")
                yield from audit_tree(entry["value"], entry_path, disabled)
            return
        if not value:
            yield row({})
        for key, item in value.items():
            yield from audit_tree(item, path + "/" + key, disabled)
    elif isinstance(value, list):
        if not value:
            yield row([])
        for i, item in enumerate(value):
            yield from audit_tree(item, f"{path}[{i}]", disabled)
    else:
        yield row(value)


def make_audit(report):
    rows = list(audit_tree(report["document"], "document"))

    def layers(items):
        for layer in items:
            prefix = "layer[" + layer["index_path"] + "] " + json.dumps(layer["name"], ensure_ascii=False)
            for key, value in layer.items():
                if key != "children":
                    rows.extend(audit_tree(value, prefix + "/" + key))
            layers(layer.get("children", []))

    layers(report["layers"])
    # Canonical raw tree includes document resources and records not exposed by
    # the high-level layer API. Duplication is intentional and labelled.
    rows.extend(audit_tree(report["raw_psd"], "raw_psd"))
    for key in ("issues", "parser_messages"):
        for i, issue in enumerate(report[key]):
            rows.append({"path": f"{key}[{i}]", "value": issue,
                         "status": "LỖI", "disabled_context": False})
    return rows


def audit_line(row):
    return (f"[{row['status']}] {row['path']} = "
            + json.dumps(row["value"], ensure_ascii=False, allow_nan=False))


# ---- TOOL-GAP: chỉ điểm nhanh cái renderer magnet.py hiện KHÔNG/CHƯA dựng đúng ----
# GIỮ ĐỒNG BỘ với magnet.py khi renderer thêm khả năng. Đây là bản chép tĩnh, cố ý
# KHÔNG import magnet để script đứng độc lập; audit đầy đủ vẫn ở .audit.txt.
_FX_OK = {"ColorOverlay", "Stroke", "DropShadow", "GradientOverlay",
          "OuterGlow", "InnerShadow", "BevelEmboss"}
_WARP_OK = {"warpNone", "warpArc", "warpArcUpper", "warpArcLower", "warpArch"}
_STYLE_NEUTRAL = {"FontCaps": 0, "FauxBold": False, "FauxItalic": False,
                  "FontBaseline": 0, "BaselineShift": 0.0, "VerticalScale": 1.0,
                  "Underline": False, "Strikethrough": False, "Kerning": 0}


def collect_tool_gaps(psd):
    """Duyệt layer TEXT hiện, trả list cảnh báo cái tool render sai/xấp xỉ/bỏ qua."""
    gaps = []
    for l in psd.descendants():
        if l.kind != "type" or not l.is_visible():
            continue
        nm = repr(l.name)
        try:
            a, b, c, d = (float(v) for v in l.transform[:4])
            if abs(b) > 1e-3 * max(1.0, abs(a)) or abs(c) > 1e-3 * max(1.0, abs(d)):
                gaps.append(f"{nm}: XOAY/NGHIÊNG {_deg(_atan2(b, a)):.1f}° (đã có engine xoay — soi lại vị trí/hướng)")
        except Exception:
            pass
        try:
            fo = int(l.tagged_blocks.get_data(Tag.BLEND_FILL_OPACITY, 255))
            if fo != 255:
                gaps.append(f"{nm}: FILL OPACITY={fo} — ruột {'RỖNG' if fo == 0 else 'MỜ'}, đổi màu ruột KHÔNG tác dụng")
        except Exception:
            pass
        wp = getattr(l, "warp", None) or {}
        st = getattr(wp.get(b"warpStyle"), "enum", b"warpNone")
        st = st.decode("ascii", "replace") if isinstance(st, (bytes, bytearray)) else str(st)
        if st not in _WARP_OK:
            gaps.append(f"{nm}: WARP '{st}' CHƯA DỰNG (tool coi THẲNG)")
        elif st == "warpArch":
            gaps.append(f"{nm}: WARP Arch (xấp xỉ Arc) — soi chữ có đứng thẳng không")
        try:
            for e in (l.effects or []):
                en = type(e).__name__
                if getattr(e, "enabled", True) and en not in _FX_OK:
                    gaps.append(f"{nm}: EFFECT {en} CHƯA HỖ TRỢ")
        except Exception:
            pass
        try:                                            # pattern/texture clip vào chữ (layer riêng clip)
            clip = [c.name for c in (getattr(l, "clip_layers", None) or [])]
            if clip:
                gaps.append(f"{nm}: PATTERN clip vào chữ {clip} (tool DỰNG được — trích texture; tên MỚI là XẤP XỈ, soi lại)")
        except Exception:
            pass
        try:
            sd = l.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
            for k, neu in _STYLE_NEUTRAL.items():
                v = sd.get(k, neu)
                same = (abs(float(v) - float(neu)) < 1e-3) if isinstance(neu, (int, float)) and not isinstance(neu, bool) else (bool(v) == neu)
                if not same:
                    gaps.append(f"{nm}: {k}={v!r} (tool BỎ QUA)")
        except Exception:
            pass
        if l.opacity != 255 or str(l.blend_mode) != "BlendMode.NORMAL" or l.has_mask():
            gaps.append(f"{nm}: opacity={l.opacity} blend={l.blend_mode} mask={l.has_mask()} (tool vẽ normal/đục)")
        if "\r" in (l.text or "") or "\n" in (l.text or ""):
            gaps.append(f"{nm}: TEXT NHIỀU DÒNG (tool xử 1 dòng)")
    return gaps


def build_report(path, include_binary=False):
    encoder = PSDEncoder(include_binary)
    capture = ParseMessages()
    logger = logging.getLogger("psd_tools")
    logger.addHandler(capture)
    try:
        psd = PSDImage.open(path)
        with Path(path).open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        report = {
            "schema_version": 2, "source": str(Path(path).resolve()),
            "source_sha256": digest, "psd_tools_version": psd_tools.__version__,
            "coverage": {
                "scope": "All records parsed by psd-tools, including document resources, "
                         "all layers and tagged blocks, disabled effects, text style and paragraph runs.",
                "include_binary": include_binary,
                "limitations": [
                    "Not a guarantee of all Photoshop semantics or matching rendering.",
                    "Opaque bytes are not decoded; embedded smart objects are not recursively opened. "
                    "External linked assets are not fetched.",
                    "Rasterized text/pixel layers do not retain an editable text transform. "
                    "Smart-object/vector transforms remain in their native descriptors.",
                    "Parser may skip unsupported structures/padding. Keep the original PSD; "
                    "this JSON is not a round-trip backup.",
                ],
            },
            "document": {"width": psd.width, "height": psd.height, "depth": psd.depth,
                         "color_mode": encoder.encode(psd.color_mode)},
            "layers": [inspect_layer(layer, str(i), encoder) for i, layer in enumerate(psd)],
            "raw_psd": encoder.encode(psd._record, "raw_psd"),
            "issues": encoder.issues, "parser_messages": capture.messages,
            "binary_payloads_summarized": encoder.binary_summaries,
        }
        report["audit"] = make_audit(report)
        report["tool_gaps"] = collect_tool_gaps(psd)
        return report
    finally:
        logger.removeHandler(capture)


def print_layers(layers, indent=""):
    for layer in layers:
        print(f"{indent}[{layer['index_path']}] {layer['kind']} {layer['name']!r} "
              f"visible={layer.get('visible')} opacity={layer.get('opacity')}")
        transform = layer.get("transform", {})
        if "matrix" in transform:
            print(f"{indent}  transform={transform['matrix']} "
                  f"rotation={transform['rotation_degrees']}° "
                  f"skew={transform['skew_degrees']}° reflected={transform['reflected']}")
        fo = layer.get("fill_opacity")
        print(f"{indent}  Fill opacity={fo}/255" + (" ⚠ RUỘT CHỮ TRONG SUỐT" if fo == 0 else ""))
        for effect in layer.get("effects", []):
            print(f"{indent}  effect={effect['type']} "
                  f"enabled={effect['properties'].get('enabled')} "
                  f"master_enabled={layer.get('effects_enabled')}")
        print_layers(layer.get("children", []), indent + "  ")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", "-o", type=Path, help="Đường dẫn JSON; mặc định <file>.inspect.json")
    parser.add_argument("--include-binary", action="store_true", help="Nhúng mọi byte payload dạng base64; file có thể rất lớn")
    parser.add_argument("--magnet-check", action="store_true", help="Tương thích lệnh cũ; kiểm kê đầy đủ đã bật mặc định")
    parser.add_argument("--summary", action="store_true", help="Chỉ in tóm tắt; bảng đầy đủ vẫn ở JSON và TXT")
    parser.add_argument("--strict", action="store_true", help="Exit 2 nếu còn trường chưa xác minh hoặc chưa giải mã")
    args = parser.parse_args(argv)
    output = args.output or args.path.with_suffix(".inspect.json")
    if output.resolve() == args.path.resolve() or (output.exists() and output.samefile(args.path)):
        parser.error("Không được ghi đè PSD nguồn bằng JSON")
    audit_output = output.with_suffix(".audit.txt")
    if audit_output.resolve() == args.path.resolve() or (audit_output.exists() and audit_output.samefile(args.path)):
        parser.error("Không được ghi báo cáo TXT đè lên nguồn")
    if audit_output.resolve() == output.resolve() or (audit_output.exists() and output.exists() and audit_output.samefile(output)):
        parser.error("Đường dẫn JSON và TXT phải khác nhau")
    report = build_report(args.path, args.include_binary)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"PSD: {args.path} | {report['document']}")
    gaps = report.get("tool_gaps", [])
    print("=== TOOL-GAP (renderer magnet.py) ===")
    if gaps:
        for g in gaps:
            print(f"  ⚠ {g}")
    else:
        print("  ✓ không phát hiện gap trên layer text hiện (vẫn nên soi bảng audit đầy đủ)")
    print("-" * 60)
    print_layers(report["layers"])
    print(f"JSON đầy đủ cấu trúc đã đọc: {output}")
    print("⚠ Báo cáo không chứng minh render giống Photoshop; dữ liệu chưa giải mã giữ ở raw_psd.")
    if report["binary_payloads_summarized"]:
        print(f"⚠ {len(report['binary_payloads_summarized'])} payload nhị phân chỉ ghi size/hash; "
              "dùng --include-binary để nhúng byte gốc đã đọc.")
    for issue in report["issues"]:
        print(f"⚠ {issue['path']}: {issue['error']}")
    for message in report["parser_messages"]:
        print(f"⚠ psd-tools: {message['message']}")
    with audit_output.open("w", encoding="utf-8") as stream:
        for row in report["audit"]:
            line = audit_line(row)
            stream.write(line + "\n")
            if not args.summary:
                print(line)
    counts = {}
    for row in report["audit"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"Kiểm kê {len(report['audit'])} mục: {counts}")
    print(f"Bảng thông số và trạng thái: {audit_output}")
    unresolved = any(r["status"] in {"CHƯA XÁC MINH", "KHÔNG GIẢI MÃ", "LỖI"} for r in report["audit"])
    return 2 if report["issues"] or report["parser_messages"] or (args.strict and unresolved) else 0


if __name__ == "__main__":
    sys.exit(main())
