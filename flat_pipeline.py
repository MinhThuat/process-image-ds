#!/usr/bin/env python3
"""
Ảnh trang phục mặc thật -> các MẢNH rập PHẲNG rời (cut-and-sew, in tràn). MỘT script.

2 mode duy nhất (không ghép thành phẩm cuối nữa — xuất từng miếng):
  --pieces : auto nhận VÁY vs ÁO+QUẦN -> mỗi mảnh 1 file.
             * áo/quần: full-bleed chữ nhật (tràn kín khung, nền vải kể cả trắng)
             * váy    : cắt theo silhouette cong thật (nền ngoài trong suốt)
             mặt sau: 1 ảnh -> tự suy từ họa tiết trước; --back (2 ảnh) / --combined
             (1 ảnh có cả 2 mặt) -> dùng lưng THẬT. Tách từng người (song song đa key).
  --art    : 1 bản vẽ CAD phẳng nguyên bộ, mặt trước. Tách từng người.

Pipeline: dola-seed (BytePlus ARK) NHÌN ảnh -> phân loại + tả print từng mảnh ->
gen (primary chatgpt-imagegen/codex, lỗi -> OpenArt Seedream 4.5).

Chạy:
  python3 flat_pipeline.py --pieces --front f.png [--back b.png | --combined] -o out.png
  python3 flat_pipeline.py --art    --front f.png -o out.png
  python3 flat_pipeline.py --selfcheck

Yêu cầu: codex đã đăng nhập + `openart login`; ARK_API_KEY (env hoặc .env cạnh script).
"""
import argparse, base64, io, json, os, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_T0 = time.time()
_LOGF = None  # file handle nếu ghi log ra file

def _open_log(path):
    global _LOGF
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    _LOGF = open(p, "a", encoding="utf-8")
    return p

def log(stage, msg=""):
    """Log 1 dòng có mốc thời gian + tên giai đoạn -> stdout VÀ file (flush ngay)."""
    line = f"[+{time.time() - _T0:6.1f}s][{stage:<9}] {msg}"
    print(line, flush=True)
    if _LOGF:
        _LOGF.write(line + "\n"); _LOGF.flush()

EMIT = None   # nếu set: crop/panel ghi thẳng vào thư mục này (cho UI web poll)

def _workdir(prefix, sub=""):
    """Thư mục làm việc: dùng EMIT (được serve) nếu có, không thì temp."""
    if EMIT:
        d = Path(EMIT) / sub if sub else Path(EMIT)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(tempfile.mkdtemp(prefix=prefix))

import cv2, numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
VSC = Path(os.getenv("VSC_ROOT", "/mnt/6C96C1A096C16AE2/vsc"))
# ưu tiên .env cạnh script (self-contained), không có thì dùng .env gốc của chatgpt-api
_DEF_ENV = (HERE / ".env") if (HERE / ".env").exists() else (VSC / "chatgpt-api" / ".env")
ENV_FILE = Path(os.getenv("ARK_ENV_FILE", _DEF_ENV))
ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
VMODELS = ["dola-seed-2-1-turbo-260628",  # model chính
           "seed-2-0-pro-260328", "seed-2-0-lite-260228", "seed-1-6-250915"]  # fallback
OA_MODEL = "byte-plus-seedream-4-5"

# --- gen prompt: MẢNH VÁY (cắt cong) -> vẽ trên nền xám rồi cutout() theo silhouette ---
FLAT = ("A 2D FLAT VECTOR sewing-pattern diagram, absolutely flat and symmetric, front view, on a "
        "plain flat light grey background. SOLID FLAT COLOR FILL only, NO fabric texture, NO denim "
        "weave, NO topstitching, NO gathers, NO ruffles, NO folds, NO wrinkles, NO shading, NO "
        "gradient, NO drop shadow, NO 3D, completely matte. Clean flat color blocks with crisp edges, "
        "like a technical flat CAD garment panel. No human body, no head, no arms, no legs.")
SHAPE_BODICE = " Shape: a sleeveless bodice tank panel like the second reference (shape guide)."
SHAPE_FAN = (" Shape: a wide quarter-circle CIRCLE-SKIRT fan panel spread flat like the second "
             "reference (shape guide). Every band follows the curved arc, parallel to the curved hem.")

# --- gen prompt: MẢNH ÁO/QUẦN (full-bleed) -> mỗi mảnh in tràn kín khung, không viền ---
# Neo {base} = màu nền vải (parse từ desc): panel THƯA chi tiết thì model hay tô đen/void
# vùng trơn -> ép "toàn khung là màu nền, vùng trơn giữ nguyên màu nền, cấm void/glow".
PANEL_BLEED = (
    "A full-bleed textile PRINT for the {label} — the flat printed artwork of ONE panel only, NOT the "
    "whole garment. The ENTIRE frame is this panel's fabric: its BASE colour is {base}, which FILLS the "
    "whole image edge to edge; the listed graphics sit ON TOP of that base and every other area stays "
    "{base}. Large plain areas MUST remain {base} — do NOT darken them, do NOT add any black void, glow, "
    "vignette, gradient or spotlight anywhere. Zoom in so the print FILLS 100% of the frame and BLEEDS "
    "OFF all four edges: NO margin, NO border, NO letterboxing, NO garment silhouette/outline, NO hood, "
    "NO collar, NO cuffs, NO person, NO hanger, NO mockup. Flat solid colours only, crisp shapes, NO 3D, "
    "NO shading, NO shadow, NO fabric texture, completely matte. The print of this panel: {desc}")

def _base_colour(desc):
    """Màu nền vải để neo prompt — vision luôn mở đầu desc bằng 'base <màu>; ...'."""
    m = re.match(r"\s*base\s+([^;.,]+)", desc or "", re.I)
    return m.group(1).strip() if m else "the panel's own solid fabric colour"

# --- gen prompt: ART PHẲNG (1 bản vẽ nguyên bộ, mặt trước) ---
OUTFIT_FLAT = (
    "A 2D FLAT technical fashion-flat drawing of a COMPLETE outfit laid flat, FRONT view, on a plain "
    "light grey background. Draw the TOP garment and the BOTTOM garment as flat, symmetric garment "
    "silhouettes, the top placed above the bottom, both fully visible. SOLID FLAT COLOR FILL only, "
    "crisp clean edges like a technical CAD flat: NO fabric texture, NO 3D, NO shading, NO gradient, "
    "NO shadow, NO folds/wrinkles. NO person, no head, no hands, no feet, no hanger, no mockup. "
    "Redraw EVERY visible front detail faithfully in the right place and colour. "
    "TOP garment: {top}. BOTTOM garment: {bottom}.")


# ---------- 1. dola-seed vision ----------
def _ark_keys():
    """Nhiều key -> vision chạy song song thật (ARK serialize theo key). Nạp từ
    ARK_API_KEYS='k1,k2,...' và/hoặc ARK_API_KEY, ARK_API_KEY2, ARK_API_KEY3..."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except Exception:
        pass
    keys = []
    for k in (os.getenv("ARK_API_KEYS") or "").split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    for name in ("ARK_API_KEY", "ARK_API_KEY2", "ARK_API_KEY3", "ARK_API_KEY4"):
        v = os.getenv(name)
        if v and v not in keys:
            keys.append(v)
    return keys

def _ark_clients():
    keys = _ark_keys()
    if not keys:
        sys.exit("Thiếu ARK_API_KEY (env hoặc chatgpt-api/.env).")
    from openai import OpenAI
    return [OpenAI(api_key=k, base_url=ARK_BASE) for k in keys]

def _ark_client():
    return _ark_clients()[0]

# Vision chỉ cần bbox/mô tả -> gửi ảnh THU NHỎ cho nhanh (không đổi kết quả chuẩn hoá).
VISION_MAXSIDE = int(os.getenv("FLAT_VISION_MAXSIDE", "1152"))

def _vision_uri(img, maxside=VISION_MAXSIDE):
    im = Image.open(img)
    if max(im.size) > maxside:
        r = maxside / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def _vision(client, img, prompt, need=()):
    """Gọi vision với fallback qua VMODELS, log từng lần thử. `need` = key bắt buộc trong JSON.
    `img` là 1 đường dẫn HOẶC list nhiều đường dẫn (gộp vào 1 request — ARK serialize
    theo key nên 1 request nhiều ảnh nhanh hơn nhiều request rời)."""
    imgs = img if isinstance(img, (list, tuple)) else [img]
    content = [{"type": "text", "text": prompt}]
    for i in imgs:
        content.append({"type": "image_url", "image_url": {"url": _vision_uri(i)}})
    last = None
    for m in VMODELS:
        try:
            log("vision", f"thử model {m} ...")
            r = client.chat.completions.create(model=m,
                messages=[{"role": "user", "content": content}])
            js = json.loads(re.search(r"\{.*\}", r.choices[0].message.content, re.S).group())
            for k in need:
                if k not in js:
                    raise ValueError(f"thiếu key '{k}'")
            log("vision", f"OK ({m})")
            return js, m
        except Exception as e:
            last = f"{m}: {str(e)[:90]}"
            log("vision", f"lỗi {m}: {str(e)[:70]}")
    sys.exit(f"Vision lỗi hết model. Cuối: {last}")

# --- vision: tách người (mode nhiều người) ---
PEOPLE_PROMPT = (
    "This photo shows several people standing in a row, each wearing an outfit. Return ONLY JSON "
    '{"people":[[x0,y0,x1,y1],...]} ordered LEFT TO RIGHT, one TIGHT bounding box per person '
    "covering that person's full outfit (shoulders to hem), integers normalized 0-1000 (x from left, "
    "y from top). Exclude neighboring people from each box as much as possible.")

def seed_people(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, PEOPLE_PROMPT, need=("people",))
    return js["people"], (w, h), m

# --- vision: PIECES = phân loại đồ + tả print từng mảnh (trước + sau) ---
_PIECES_SCHEMA = (
    "Classify the outfit, then describe each cut-and-sew PANEL as a concise flat-technical PRINT spec "
    "(base colour + every graphic/motif and its position; colours as words or hex; no prose). "
    "Return ONLY JSON. If it is a DRESS (one-piece, torso + skirt): "
    '{"type":"dress","bodice_front":"..","skirt_front":"..","bodice_back":"..","skirt_back":".."}. '
    "If it is a TOP + BOTTOM (a separate upper garment + pants/shorts): "
    '{"type":"top_bottom","top_front":"..","sleeve":"..","pants_front":"..","top_back":"..","pants_back":".."}. '
    "top_front/top_back = the TORSO/BODY panel of the upper garment only (EXCLUDE sleeves, hood, collar). "
    "sleeve = ONE sleeve's print (base colour, cuff, any stripe/badge). "
    "pants_front/pants_back = the pants legs panel.")
_PIECES_INTRO = {
    "infer": ("This photo shows the FRONT of a person's outfit. " + _PIECES_SCHEMA +
              " Only the front is visible: INFER each *_back panel from the front — keep the same base "
              "colour, yokes and trims, but a plainer body; drop front-only graphics/badges/zips unless "
              "they clearly wrap around to the back."),
    "two": ("You are given TWO photos of the SAME outfit: the FIRST image is the FRONT, the SECOND is "
            "the BACK. " + _PIECES_SCHEMA +
            " Describe *_front panels from the first image and *_back panels from the second (real back)."),
    "combined": ("This ONE photo shows the SAME outfit from TWO angles side by side: a FRONT view and a "
                 "BACK view. Decide which is which. " + _PIECES_SCHEMA +
                 " Describe *_front panels from the front view and *_back panels from the back view (real back)."),
}

def seed_pieces(client, imgs, source):
    """imgs: 1 path (infer/combined) hoặc [front, back] (two). Trả JSON {type, panel descs}."""
    js, _ = _vision(client, imgs, _PIECES_INTRO[source], need=("type",))
    return js

# mảnh áo+quần: (slug file, label gửi model, key JSON, mặt) — full-bleed
_TB_SPECS = [
    ("ao_truoc",   "front torso/body panel of the top (no sleeves, no hood)", "top_front",   "front"),
    ("ao_sau",     "back torso/body panel of the top (no sleeves, no hood)",  "top_back",    "back"),
    ("tay_ao",     "one sleeve",                                              "sleeve",      "front"),
    ("quan_truoc", "front of the pants",                                      "pants_front", "front"),
    ("quan_sau",   "back of the pants",                                       "pants_back",  "back"),
]
# mảnh váy: (slug file, shape prompt, ảnh shape-guide, key JSON, mặt) — cắt cong
_DRESS_SPECS = [
    ("than_truoc", SHAPE_BODICE, "shape_bodice.png", "bodice_front", "front"),
    ("than_sau",   SHAPE_BODICE, "shape_bodice.png", "bodice_back",  "back"),
    ("ta_truoc",   SHAPE_FAN,    "shape_fan.png",    "skirt_front",  "front"),
    ("ta_sau",     SHAPE_FAN,    "shape_fan.png",    "skirt_back",   "back"),
]

# --- vision: ART = tả cả bộ (áo trên + quần dưới) ---
OUTFIT_PROMPT = (
    "This is a photo of a person wearing a full outfit. Describe the ENTIRE outfit as a precise "
    'flat-technical spec so it can be redrawn as a fashion-flat. Return ONLY JSON: '
    '{"top":{"desc":"..."},"bottom":{"desc":"..."}}. '
    "top.desc = the upper garment (or the top half of a dress): type, base fabric colour, and EVERY "
    "visible FRONT detail with colours and positions — chest graphics/panels, buttons, badges, pockets, "
    "zipper, hood, collar, sleeve prints, cuffs. "
    "bottom.desc = the lower garment (or skirt): type, base colour, and every visible front detail — "
    "knee patches, side stripes, pockets, waistband, hems. "
    "Colours as words or hex. Be specific and positional; no prose outside the spec.")

def seed_outfit(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, OUTFIT_PROMPT, need=("top", "bottom"))
    return js, (w, h), m

def crop_norm(img, box, wh, out):
    w, h = wh
    x0, y0, x1, y1 = (box[0]*w//1000, box[1]*h//1000, box[2]*w//1000, box[3]*h//1000)
    x0, x1 = sorted((max(0, x0), min(w, x1)))
    y0, y1 = sorted((max(0, y0), min(h, y1)))
    Image.open(img).crop((x0, y0, x1, y1)).save(out)
    return (x0, y0, x1, y1)


# ---------- 2. gen: chatgpt-imagegen (primary) -> OpenArt Seedream 4.5 (fallback) ----------
def _bin(name):
    cands = [name + ".exe", name] if sys.platform == "win32" else [name]
    for c in cands:
        b = HERE / "bin" / c
        if b.exists():
            return str(b)
    return name
CHATGPT_BIN = _bin("chatgpt-imagegen")
OPENART_BIN = _bin("openart")

def _launcher(path):
    """File là script python (shebang) -> chạy qua interpreter (bắt buộc trên Windows)."""
    try:
        with open(path, "rb") as f:
            first = f.readline(200)
        if first.startswith(b"#!") and b"python" in first:
            return [sys.executable]
    except Exception:
        pass
    return []

def _win_native_ok(path):
    """True nếu file chạy được trên nền tảng hiện tại. Trên Windows, binary ELF (Linux)
    không chạy được -> False để báo lỗi rõ thay vì WinError 193 khó hiểu."""
    if sys.platform != "win32":
        return True
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic != b"\x7fELF"          # ELF = binary Linux, không phải PE Windows
    except Exception:
        return True

def _run(cmd, timeout):
    # Ép child in UTF-8 + đọc UTF-8 -> tránh UnicodeError khi đường dẫn có dấu tiếng Việt.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=env, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"rc={p.returncode}")

def _chatgpt_gen(prompt, refs, out, timeout=420):
    cmd = _launcher(CHATGPT_BIN) + [CHATGPT_BIN, prompt, "-o", str(out), "--size", "auto",
           "--backend", "codex", "--quiet"]
    for r in refs:
        cmd += ["-i", str(r)]
    _run(cmd, timeout)
    if not Path(out).exists():
        raise RuntimeError("chatgpt-imagegen không tạo file")
    return out

def _openart_gen(prompt, refs, out, timeout=300):
    if not _win_native_ok(OPENART_BIN):
        raise RuntimeError("OpenArt chỉ có bản Linux, không chạy được trên Windows "
                           "(chỉ dùng chatgpt-imagegen làm gen chính)")
    cmd = _launcher(OPENART_BIN) + [OPENART_BIN, "generate", "image", prompt, "--model", OA_MODEL,
           "-o", str(out), "--yes", "--compact"]
    for r in refs:
        cmd += ["--image", str(r)]
    _run(cmd, timeout)
    if not Path(out).exists():
        raise RuntimeError("openart không tạo file")
    return out

def gen_panel(prompt, refs, out):
    """Primary chatgpt-imagegen (codex), lỗi -> fallback OpenArt Seedream 4.5."""
    try:
        log("gen", "  → thử chatgpt-imagegen (codex) ...")
        r = _chatgpt_gen(prompt, refs, out)
        log("gen", "  ✓ xong bằng chatgpt-imagegen")
        return r
    except Exception as e:
        log("gen", "  ✗ chatgpt-imagegen lỗi -> fallback OpenArt. Lỗi đầy đủ:")
        log("gen", "  " + str(e)[:1500].replace("\n", "\n  "))
    r = _openart_gen(prompt, refs, out)
    log("gen", "  ✓ xong bằng OpenArt Seedream 4.5")
    return r


# ---------- 3. cutout theo ngưỡng nền (cho mảnh váy: cắt cong, nền trong suốt) ----------
def cutout(path, T=12):
    rgb = cv2.imread(str(path))[:, :, ::-1].copy()
    im = rgb.astype(np.int16); h, w = im.shape[:2]
    corners = np.concatenate([im[:20, :20].reshape(-1, 3), im[:20, -20:].reshape(-1, 3),
                              im[-20:, :20].reshape(-1, 3), im[-20:, -20:].reshape(-1, 3)])
    bg = np.median(corners, 0)
    fg = (np.abs(im - bg).max(2) > T).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n > 1:
        fg = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    fg = ndimage.binary_fill_holes(fg).astype(np.uint8)
    al = cv2.GaussianBlur((fg * 255).astype(np.uint8), (3, 3), 0)
    rgba = np.dstack([rgb, al]); ys, xs = np.where(fg > 0)
    return Image.fromarray(rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1])


# ---------- 4. workers từng người ----------
def run_pieces_one(client, img, out, emit_sub="", back=None, combined=False):
    """1 người/1 trang phục -> mỗi mảnh 1 file.
      áo+quần: full-bleed chữ nhật. váy: cắt cong (cutout), nền trong suốt.
      mặt sau: back (2 ảnh) / combined (1 ảnh 2 mặt) -> lưng thật; else tự suy từ trước.
      tay phải = lật ngang tay trái."""
    work = _workdir("pieces_", emit_sub)
    if back:
        source, vimgs = "two", [img, back]
    elif combined:
        source, vimgs = "combined", img
    else:
        source, vimgs = "infer", img
    log("crop", "nhìn ảnh: phân loại đồ + tả print từng mảnh ...")
    js = seed_pieces(client, vimgs, source)
    typ = (js.get("type") or "top_bottom").strip()
    ref_front = work / "crop_front.png"; Image.open(img).save(ref_front)
    if back:
        ref_back = work / "crop_back.png"; Image.open(back).save(ref_back)
    else:
        ref_back = ref_front   # combined: ảnh chứa cả 2 mặt; infer: dùng ảnh trước làm style-ref
    refs = {"front": ref_front, "back": ref_back}
    log("crop", f"loại: {typ}")
    outs = []
    if typ == "dress":
        for slug, shape, guide, key, side in _DRESS_SPECS:
            desc = js.get(key) or ""
            if not desc:
                continue
            dst = out.with_name(f"{out.stem}_{slug}{out.suffix}")
            tmp = work / f"gen_{slug}.png"
            log("gen", f"gen mảnh váy {slug} (cắt cong) ...")
            gen_panel(FLAT + shape + " " + desc, [refs[side], ASSETS / guide], tmp)
            cutout(tmp).save(dst)               # cắt theo silhouette, nền trong suốt
            outs.append(dst)
    else:  # top_bottom (mặc định)
        for slug, label, key, side in _TB_SPECS:
            desc = js.get(key) or ""
            if not desc:
                continue
            dst = out.with_name(f"{out.stem}_{slug}{out.suffix}")
            log("gen", f"gen mảnh {slug} (in tràn) ...")
            gen_panel(PANEL_BLEED.format(label=label, base=_base_colour(desc), desc=desc),
                      [refs[side]], dst)
            outs.append(dst)
            if slug == "tay_ao":                # tay phải = lật ngang tay trái
                dst2 = out.with_name(f"{out.stem}_tay_ao_2{out.suffix}")
                Image.open(dst).transpose(Image.FLIP_LEFT_RIGHT).save(dst2)
                outs.append(dst2)
                log("gen", "mảnh tay_ao_2 = lật ngang tay_ao")
    log("done", f"{len(outs)} mảnh: " + ", ".join(o.name for o in outs))
    return outs[0] if outs else out

def run_art_one(client, img, out, emit_sub="", **_):
    """1 người: vision tả cả bộ -> gen 1 bản vẽ phẳng nguyên bộ, mặt trước."""
    work = _workdir("art_", emit_sub)
    log("crop", "nhìn ảnh: tả chi tiết cả bộ ...")
    js, _, _ = seed_outfit(client, img)
    top = js["top"].get("desc", "") if isinstance(js["top"], dict) else str(js["top"])
    bot = js["bottom"].get("desc", "") if isinstance(js["bottom"], dict) else str(js["bottom"])
    log("crop", f"trên: {top[:60]}… | dưới: {bot[:60]}…")
    ref = work / "crop_outfit.png"
    Image.open(img).save(ref)
    log("gen", "gen bản vẽ phẳng nguyên bộ ...")
    gen_panel(OUTFIT_FLAT.format(top=top, bottom=bot), [ref], out)
    log("done", str(out))
    return out


# ---------- 5. orchestration: tách từng người (song song đa key) ----------
def _run_perperson(img, out, worker, label, wprefix, **wkw):
    """Tách từng người -> mỗi người chạy `worker` -> out_1..N (1 người: out).
    Nhiều người + nhiều key -> chạy song song."""
    log("start", f"{label} | {Path(img).name}")
    clients = _ark_clients()
    log("detect", "nhìn ảnh, tách từng người ...")
    boxes, wh, _ = seed_people(clients[0], img)
    log("detect", f"phát hiện {len(boxes)} người")
    work = _workdir(wprefix)
    single = len(boxes) == 1

    def one(i, box):
        person = work / f"person_{i}.png"
        crop_norm(img, box, wh, person)
        dst = out if single else out.with_name(f"{out.stem}_{i}{out.suffix}")
        log("person", f"=== {i}/{len(boxes)} -> {dst.name} ===")
        try:
            return worker(clients[(i - 1) % len(clients)], str(person), dst,
                          emit_sub=f"person_{i}", **wkw)
        except Exception as e:
            log("person", f"  người {i} lỗi, bỏ qua: {str(e)[:140]}")
            return None

    items = list(enumerate(boxes, 1))
    if len(items) > 1 and len(clients) > 1:   # nhiều người + nhiều key -> song song
        with ThreadPoolExecutor(max_workers=min(len(clients), len(items))) as ex:
            outs = [d for d in ex.map(lambda t: one(*t), items) if d]
    else:
        outs = [d for d in (one(i, b) for i, b in items) if d]
    log("done", f"{label} {len(outs)}/{len(boxes)}: " + ", ".join(o.name for o in outs))
    return outs

def run_pieces(front, out, back=None, combined=False):
    """Tách mảnh. 1 ảnh (tự suy sau) -> tách từng người. --back/--combined -> 1 trang phục, lưng thật."""
    if back or combined:
        client = _ark_client()
        tag = "+sau thật (2 ảnh)" if back else "2-view (1 ảnh)"
        log("start", f"PIECES tách mảnh | {Path(front).name} | {tag}")
        run_pieces_one(client, front, Path(out), back=back, combined=combined)
        return [Path(out)]
    return _run_perperson(front, Path(out), run_pieces_one, "PIECES tách mảnh", "piecesset_")

def run_art(front, out):
    """Art phẳng: 1 bản vẽ nguyên bộ mặt trước. Tách từng người."""
    return _run_perperson(front, Path(out), run_art_one, "ART phẳng nguyên bộ", "artset_")


def _selfcheck():
    # crop_norm scale/clamp: 0-1000 -> pixel, clamp trong khung, tự sửa thứ tự
    im = Image.new("RGB", (1000, 500), "white")
    p = Path(tempfile.mktemp(suffix=".png")); im.save(p)
    assert crop_norm(p, [100, 200, 900, 400], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 100, 900, 200)
    assert crop_norm(p, [900, 0, 100, 999], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 0, 900, 499)  # sort x, scale y
    # 3 nguồn suy mặt sau đều có prompt riêng, đều ép JSON có "type" + đủ mảnh 2 mặt
    for src in ("infer", "two", "combined"):
        pr = _PIECES_INTRO[src]
        assert '"type"' in pr and "top_back" in pr and "bodice_back" in pr, src
    # mỗi loại đồ có đủ mảnh 2 mặt
    assert {s[0] for s in _TB_SPECS} == {"ao_truoc", "ao_sau", "tay_ao", "quan_truoc", "quan_sau"}
    assert {s[0] for s in _DRESS_SPECS} == {"than_truoc", "than_sau", "ta_truoc", "ta_sau"}
    # neo màu nền: parse 'base <màu>' để ép panel thưa không tô đen/void
    assert _base_colour("base white; yoke yellow") == "white"
    assert _base_colour("base mustard yellow #E6A817; x") == "mustard yellow #E6A817"
    assert _base_colour("no base word") == "the panel's own solid fabric colour"
    print("selfcheck OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--front")
    ap.add_argument("--back", help="ảnh mặt sau riêng (pieces: dùng lưng thật)")
    ap.add_argument("--combined", action="store_true",
                    help="pieces: ảnh --front chứa CẢ mặt trước + sau (2-view)")
    ap.add_argument("--pieces", action="store_true",
                    help="tách từng mảnh (auto váy/áo-quần), full-bleed, tách người, tự suy mặt sau")
    ap.add_argument("--art", action="store_true",
                    help="1 bản vẽ phẳng nguyên bộ mặt trước; tách từng người")
    ap.add_argument("-o", "--out", default="flat_out.png")
    ap.add_argument("--log", help="file ghi log (mặc định: <out>.log cạnh output)")
    ap.add_argument("--emit", help="ghi crop/mảnh vào thư mục này (cho UI web poll)")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
    elif a.front:
        if a.emit:
            globals()["EMIT"] = a.emit
        out = Path(a.out)
        logpath = _open_log(a.log or out.with_suffix(".log"))
        log("start", f"log -> {logpath}")
        if a.art:
            run_art(a.front, out)
        else:                                   # mặc định: pieces
            run_pieces(a.front, out, back=a.back, combined=a.combined)
    else:
        ap.error("cần --front (+ --pieces hoặc --art), hoặc --selfcheck")
