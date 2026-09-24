# QUY TẮC: Nhận case PSD mới → đọc HẾT thông số trước khi làm

> Nguyên tắc số 1: **KHÔNG render một mẫu mới cho tới khi đã liệt kê xong mọi
> layer text + mọi effect + mọi thông số warp/transform của nó.** Bỏ sót 1
> effect = ra sai, mất công sửa vòng lại. Đọc trước, làm sau.

Chạy `python inspect_psd.py "<đường dẫn .psd>"` để in ra bảng dưới đây trước khi
đụng code. Chỉ khi bảng đó khớp với những gì tool hỗ trợ mới bắt tay render;
gặp ô ⚠ (chưa hỗ trợ / lệch) thì báo user trước, không tự bịa.

---

## 1. Từng layer text — đọc đủ 6 thông số kiểu chữ (`_style`)
- [ ] **Font** (tên PostScript trong `FontSet`) — folder có đúng file font đó không?
      Khác tên nhưng cùng font thì OK; khác font thật → thiếu → BÁO, không thay bừa.
- [ ] **FontSize × transform** (`_design_size` = FontSize·hypot(tr[2],tr[3])) — cỡ THẬT.
- [ ] **Tracking** (giãn cách chữ) — quên là khoảng cách sai như mẫu NMN.
- [ ] **HorizontalScale** (nén/giãn ngang, vd 0.9 = 90%) — quên là số bị to/nhỏ ngang.
- [ ] **FillColor** (màu chữ gốc).
- [ ] **Justification** (căn trái/giữa/phải).

### 1b. Thông số text tool đang BỎ QUA — soi, nếu ≠ mặc định thì BÁO user
`StyleSheetData` có 27 key; tool chỉ áp 6 cái trên. Các key dưới **tool không
dựng** — mẫu nào bật là ra sai, phải cảnh báo (inspect_psd.py tự flag):

| key | mặc định | bật lên = | mức |
|-----|----------|-----------|-----|
| **FontCaps** | 0 | 1=small-caps, 2=ALL-CAPS → phải viết HOA input | CAO |
| **FauxBold** | false | giả đậm (glyph dày hơn) | vừa |
| **FauxItalic** | false | giả nghiêng | vừa |
| **VerticalScale** | 1.0 | kéo dọc (chỉ đọc cho geometry, KHÔNG áp vào vẽ) | vừa |
| **Leading / AutoLeading** | auto | giãn dòng (tên ≥2 dòng) | vừa |
| **BaselineShift** | 0 | dời baseline | thấp |
| **Kerning / AutoKerning** | auto | kern tay từng cặp | thấp |
| **Underline / Strikethrough** | false | gạch chân / gạch ngang | thấp |
| **StrokeColor** | — | viền trong Character panel (khác Layer Style Stroke) | thấp |
| **Ligatures / DLigatures** | — | chữ ghép | thấp |

## 2. Từng layer text — duyệt HẾT 10 loại Layer Style (`_effects`)
Photoshop có đúng 10 nhóm effect. Tick từng dòng cho MỖI layer — có/không.
Loại ✅ phải lấy đủ (đừng bỏ layer nào); loại ⚠ nếu bật thì **BÁO user**
(tool gom vào `_unsupported`, đừng render lặng lẽ như không có):

| Layer Style (PS) | class psd_tools | trạng thái |
|------------------|-----------------|------------|
| Color Overlay | ColorOverlay | ✅ đè màu chữ (`fill`) |
| Stroke | Stroke | ✅ GOM TẤT CẢ viền (đồng tâm), đọc **Position** (Outside=Size / Center=Size·0.5 / Inside≈0). Sót 1 viền = mất viền như VTY |
| Drop Shadow | DropShadow | ✅ angle+distance+size+opacity |
| Inner Shadow | InnerShadow | ✅ |
| Gradient Overlay | GradientOverlay | ✅ dải màu trong chữ |
| Outer Glow | OuterGlow | ✅ quầng ngoài |
| Bevel & Emboss | BevelEmboss | ✅ ánh kim (hl/sh color+opacity, angle, altitude) |
| Pattern Overlay | PatternOverlay | ⚠ CHƯA DỰNG → báo |
| Inner Glow | InnerGlow | ⚠ CHƯA DỰNG → báo |
| Satin | Satin | ⚠ CHƯA DỰNG → báo |

- [ ] **Clipping mask** (`clip_layers`) → pattern/texture phủ trong chữ (mẫu NVH).
      Khác Pattern Overlay: đây là layer riêng clip vào chữ, tool DỰNG được ✅.
- [ ] Kiểm tra `enabled`: effect tắt (con mắt off) thì bỏ, đừng vẽ.

## 3. Warp / chữ cong (`_text_geometry`, `_detect_arc`, `_stamp_arc`)
Đọc `warp`: **style** + **bend** (warpValue) + **orientation** + perspective.
LẤY TỪ warp data, KHÔNG dò pixel (lệch máy). Đối chiếu style với bảng —
mỗi dòng phải tick, gặp cột "báo" thì **báo user, không render lặng lẽ**:

| warpStyle | tool làm gì hiện tại | trạng thái |
|-----------|----------------------|------------|
| warpNone (không warp) | nếu pixel cong đủ lớn (sag≥25) → detect qua **pixel-fallback** rồi vẽ cong; không thì thẳng | ✅ — chữ cong NƯỚNG SẴN/uốn tay vào pixel (banner LTL) vẫn bắt được. ĐỪNG hard-return 0 cho warpNone |
| warp THẬT + bend=0 | vẽ thẳng | ✅ đúng (deterministic) |
| **warpArch** | **xoay glyph theo cung như Arc** | ⚠ XẤP XỈ — Arch thật chữ ĐỨNG THẲNG, chỉ mép trên/dưới cong. Phải soi mắt xem chữ có bị nghiêng sai không |
| warpArc / warpArcUpper / warpArcLower | parabol + xoay glyph theo tiếp tuyến | ~ gần đúng (parabol thay cung tròn) |
| warpBulge / warpShellUpper / warpShellLower | — | ⚠ CHƯA DỰNG, coi thẳng → báo |
| warpFlag / warpWave / warpFish / warpRise | — | ⚠ CHƯA DỰNG → báo |
| warpFisheye / warpInflate / warpSqueeze / warpTwist | — | ⚠ CHƯA DỰNG → báo |
| warpCustom (mesh tự vẽ) | — | ⚠ CHƯA DỰNG → báo |

- [ ] orientation phải là **Hrzn**; **Vrtc** (cong dọc) → ⚠ chưa dựng → báo.
- [ ] perspective / perspective_other ≠ 0 → ⚠ chưa dựng → báo.
- [ ] **Arch vs Arc**: xác nhận đúng loại. Arc = chữ nghiêng theo cung;
      Arch = chữ đứng thẳng. Nhầm loại = chữ nghiêng/thẳng sai.

## 4. Thuộc tính layer (mọi layer, cả text lẫn art)
- [ ] **opacity** < 255 → layer mờ; tool có tính không? Nếu không → báo.
- [ ] **blend_mode** ≠ NORMAL (Multiply/Screen/Overlay…) → tool vẽ như NORMAL → ⚠ báo.
- [ ] **layer mask** (`has_mask`) → vùng ẩn một phần; tool bỏ qua → ⚠ báo.
- [ ] Layer **ẩn** (mắt off) → đừng dựng nhầm.

## 5. Layer nền + thứ tự (`_bg_layer`, `_bake_bases`)
- [ ] Có layer **background 1 màu đơn full-canvas** không? → cho đổi màu nền.
- [ ] Có **art/pattern nằm TRÊN** text (đè lên) không? → giữ z-order (base_above).

---

## Sau khi render mẫu mới — self-check bắt buộc
- [ ] So render vs composite PSD: **width, cap-height, màu, viền, effect** đều khớp.
- [ ] Chạy regression `/tmp/rall.py` (hoặc bộ test hiện có) → phải PASS hết,
      không được làm vỡ case cũ (NMN, MLT, NVH, VTY…).
- [ ] Không còn `_unsupported` nào bị bỏ qua âm thầm.
