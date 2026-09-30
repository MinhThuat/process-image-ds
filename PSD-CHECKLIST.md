# QUY TẮC: Nhận case PSD mới → đọc HẾT thông số trước khi làm

> Nguyên tắc số 1: **KHÔNG render một mẫu mới cho tới khi đã liệt kê xong mọi
> layer text + mọi effect + mọi thông số warp/transform của nó.** Bỏ sót 1
> effect = ra sai, mất công sửa vòng lại. Đọc trước, làm sau.

Chạy `python3 inspect_psd.py "<đường dẫn .psd>"`. Nó xuất **2 tầng**:
1. **TOOL-GAP** (in đầu terminal) — chỉ điểm NHANH cái renderer `magnet.py` render
   sai/xấp xỉ/bỏ qua trên layer text hiện: xoay/nghiêng, fill-opacity (chữ rỗng
   ruột), warp lạ, effect chưa hỗ trợ, style bị bỏ (FontCaps/FauxItalic…), opacity/
   blend/mask, text nhiều dòng. **Mỗi dòng ⚠ phải xử hoặc báo user trước khi render.**
2. **Audit đầy đủ** (`<file>.inspect.json` + `<file>.inspect.audit.txt`) — kiểm kê
   MỌI trường psd-tools đọc được, kèm trạng thái (`CHƯA XÁC MINH` / `KHÔNG GIẢI MÃ`
   / `ĐÃ GHI NHẬN` / `ĐANG TẮT` / `LỖI`). Dùng khi TOOL-GAP nghi ngờ hoặc cần soi tay.

> **Nguyên tắc completeness:** TOOL-GAP là bản chép TĨNH khả năng renderer (giữ
> đồng bộ tay với `magnet.py`), audit là toàn bộ dữ liệu thô. "Đủ hay chưa" =
> **(1) không còn dòng TOOL-GAP nào chưa xử lý**, VÀ **(2) render xong so pixel với
> composite gốc phải khớp** (bước này mới lộ loại lỗi TOOL-GAP không biết, vd
> fill-opacity từng bị bỏ sót). Đừng tin mỗi TOOL-GAP; luôn self-check so gốc.
> Lưu ý: audit đánh dấu MỌI thứ `CHƯA XÁC MINH` (chưa chứng minh render giống PS),
> không phải "chưa hỗ trợ" — nên `--strict` gần như luôn exit 2, chỉ để ép đọc tay.

> **Chỉ layer TEXT mới phải soi từng tham số.** Mọi layer khác (art, smartobject,
> adjustment, group, blend, mask...) tool **nướng thẳng từ composite PSD** nên
> giữ nguyên 100% — không cần mổ. Completeness = (1) đủ tham số/effect/warp mỗi
> layer text, (2) màu bake trung thực (color mode / depth).

---

## 0. Cấp document (inspect in đầu ra)
- [ ] **color_mode = RGB (3)**; nếu CMYK/khác → ⚠ màu bake có thể lệch.
- [ ] **depth = 8-bit**; 16/32-bit → ⚠ kiểm màu.
- [ ] **Layer kinds**: nắm có bao nhiêu type (được tái tạo) vs còn lại (bake).

---

## 1. Từng layer text — đọc đủ 6 thông số kiểu chữ (`_style`)
- [ ] **Font** (tên PostScript trong `FontSet`) — folder có đúng file font đó không?
      Khác tên nhưng cùng font thì OK; khác font thật → thiếu → BÁO, không thay bừa.
- [ ] **FontSize × transform** (`_design_size` = FontSize·hypot(tr[2],tr[3])) — cỡ THẬT.
- [ ] **Tracking** (giãn cách chữ) — quên là khoảng cách sai như mẫu NMN.
- [ ] **HorizontalScale** (nén/giãn ngang, vd 0.9 = 90%) — quên là số bị to/nhỏ ngang.
- [ ] **FillColor** (màu chữ gốc).
- [ ] **Justification** (căn trái/giữa/phải).

### 1b. Thông số text tool đang BỎ QUA — TOOL-GAP tự flag nếu ≠ mặc định
`StyleSheetData` có 27 key; tool chỉ áp 6 cái ở §1 (+ StyleRunAlignment). Các key
dưới **tool không dựng** — bật lên là ra sai. TOOL-GAP flag khi lệch mặc định
(vd đã bắt được **NVH 'Monika' FontCaps=2**, **TDT 'Nicole' FauxItalic=True**):

| key | mặc định | bật lên = | mức |
|-----|----------|-----------|-----|
| **FontCaps** | 0 | 1=small-caps, 2=ALL-CAPS → phải viết HOA input | CAO |
| **FauxBold** | false | giả đậm (glyph dày hơn) | vừa |
| **FauxItalic** | false | giả nghiêng | vừa |
| **VerticalScale** | 1.0 | kéo dọc (chỉ đọc cho geometry, KHÔNG áp vào vẽ) | vừa |
| **FontBaseline** | 0 | super/subscript | vừa |
| **Leading / AutoLeading** | auto | giãn dòng (tên ≥2 dòng) | vừa |
| **BaselineShift** | 0 | dời baseline | thấp |
| **Kerning** | 0 | kern tay từng cặp | thấp |
| **Underline / Strikethrough** | false | gạch chân / gạch ngang | thấp |

- [ ] **Text nhiều dòng** (`\r`/`\n` trong text) → tool xử lý 1 dòng → ⚠ kiểm.
- Không tính (metadata/mặc định luôn bật, không đổi raster): Ligatures, Kashida,
  YUnderline, Language, HindiNumbers, Tsume, BaselineDirection, NoBreak, StrokeColor.

### 1c. Fill opacity (tagged block `BLEND_FILL_OPACITY`, KHÁC layer Opacity) — ĐÃ DỰNG
- [ ] **Fill opacity < 255** → ruột chữ mờ/rỗng, layer style (viền/bóng) VẪN hiện.
      Tool honor đúng (mẫu **MLT312 StarJedi** fill=0 = rỗng ruột chỉ còn viền trắng).
      TOOL-GAP flag. **Lưu ý:** chữ rỗng ruột → đổi màu ruột KHÔNG có tác dụng (đúng
      bản chất, không phải bug); muốn đổi màu phải nhắm vào viền — hỏi user.

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
| warpNone (không warp) | **pixel-fallback guard 2-MÉP**: chỉ nhận cong khi CẢ mép trên+dưới cùng cong & đủ lớn; xoay/nghiêng thì bỏ | ✅ cong NƯỚNG SẴN vào pixel (LTL 'The Saenz Family', warpNone nhưng pixel cong) vẫn bắt; ⚠ descender/swash chỉ lệch 1 mép KHÔNG còn ăn nhầm (VPC 'Tony' font Athelas). ĐỪNG hard-return 0 cho warpNone |
| warp THẬT + bend=0 | vẽ thẳng | ✅ đúng (deterministic) |
| **warpArch** | `_draw_arch_field` — chữ ĐỨNG THẲNG, chỉ mép trên/dưới cong (khi `_supports_arch`: Hrzn, không perspective, |bend|<100) | ✅ đúng khi đủ điều kiện; ngoài điều kiện → rơi về xấp xỉ Arc → soi mắt |
| warpArc / warpArcUpper / warpArcLower | parabol + xoay glyph theo tiếp tuyến | ~ gần đúng (parabol thay cung tròn) |
| warpBulge / warpShellUpper / warpShellLower | — | ⚠ CHƯA DỰNG, coi thẳng → báo |
| warpFlag / warpWave / warpFish / warpRise | — | ⚠ CHƯA DỰNG → báo |
| warpFisheye / warpInflate / warpSqueeze / warpTwist | — | ⚠ CHƯA DỰNG → báo |
| warpCustom (mesh tự vẽ) | — | ⚠ CHƯA DỰNG → báo |

- [ ] orientation phải là **Hrzn**; **Vrtc** (cong dọc) → ⚠ chưa dựng → báo.
- [ ] perspective / perspective_other ≠ 0 → ⚠ chưa dựng → báo.
- [ ] **Arch vs Arc**: xác nhận đúng loại. Arc = chữ nghiêng theo cung;
      Arch = chữ đứng thẳng. Nhầm loại = chữ nghiêng/thẳng sai.

### 3b. Transform XOAY / NGHIÊNG (b,c ≠ 0 trong ma trận) — ĐÃ DỰNG
- [ ] Layer text xoay/nghiêng (vd tay áo LNG 'Shania' 90°, TDT 'Nicole' 3°) →
      `_draw_rotated_field`: vẽ ngang rồi xoay cả cụm, **NEO theo đầu chữ** (tên dài
      mọc ra xa, không đè art như Stitch). TOOL-GAP flag để soi lại vị trí.
- [ ] Pixel-fallback arc BỎ QUA khi layer xoay (parabol theo cột x cho chữ dọc = ẢO).

## 4. Thuộc tính LAYER TEXT (chỉ layer text; art bake nên bỏ qua)
- [ ] **opacity** < 255 → tool vẽ đục → ⚠ báo.
- [ ] **blend_mode** ≠ NORMAL (Multiply/Screen/Overlay…) → tool vẽ NORMAL → ⚠ báo.
- [ ] **layer mask** (`has_mask`) → tool bỏ → ⚠ báo.
- [ ] Layer text **ẩn** (mắt off) → đừng biến thành field.

## 5. Layer nền + thứ tự (`_bg_layer`, `_bake_bases`)
- [ ] Có layer **background 1 màu đơn full-canvas** không? → cho đổi màu nền.
- [ ] Có **art/pattern nằm TRÊN** text (đè lên) không? → giữ z-order (base_above).
- [ ] **PSD nhiều GROUP biến thể** (mẫu để nhiều version, đa số ẩn — vd TDT 6 nhân
      vật): `_bake_bases` chỉ bake layer VỐN hiện, KHÔNG bật layer/group ẩn (nếu không
      artwork biến thể ẩn lọt vào base thành khối chữ nhật). Đã fix; kiểm base sạch.

---

## Sau khi render mẫu mới — self-check bắt buộc
- [ ] **TOOL-GAP không còn dòng nào chưa xử lý** (đã dựng hoặc đã báo user).
- [ ] **So render vs composite PSD (bắt buộc, quan trọng nhất)**: khớp width, cap-height,
      màu, viền, effect, **ruột (fill-opacity)**, vị trí. Đo lệch pixel — loại lỗi
      TOOL-GAP không biết (vd fill-opacity) CHỈ lộ ở bước này.
- [ ] Render lại đúng tên gốc + 1 tên ngắn + 1 tên dài → kiểm neo/căn/auto-shrink.
- [ ] Không làm vỡ case cũ (NMN, MLT, NVH, VTY, LTL, LNG, TDT…) — render thử vài mẫu.
