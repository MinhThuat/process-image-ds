#!/usr/bin/env python3
"""
Ảnh váy mặc thật -> layout cắt-may PHẲNG (all-over-print). MỘT script, KHÔNG cần Claude Code.

Pipeline:
  1. dola-seed (Doubao/Seed vision, BytePlus ARK) NHÌN ảnh -> bbox bodice/skirt + tự mô tả hoạ tiết
  2. crop theo bbox
  3. gen 4 panel phẳng từ crop + shape guide:
       primary = chatgpt-imagegen (backend codex), lỗi -> fallback OpenArt CLI (Seedream 4.5)
  4. cutout (ngưỡng nền) + ghép lên template xám

Fallback: vision có VMODELS (seed-2-0-pro -> lite -> 1-6); gen có chatgpt-imagegen -> OpenArt.

Chạy:
  python3 flat_pipeline.py --front front.png [--back back.png] -o out.png
  # không có --back  -> dùng luôn ảnh front cho mặt sau (mô tả "plain back")
  python3 flat_pipeline.py --selfcheck        # kiểm tra logic scale/clamp toạ độ

Yêu cầu: codex đã đăng nhập (chatgpt-imagegen/dangnhap-codex.sh) + `openart login`;
         ARK_API_KEY (env hoặc chatgpt-api/.env) cho bước vision seed.
"""
import argparse, base64, io, json, mimetypes, os, re, subprocess, sys, tempfile, time
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

EMIT = None   # nếu set: crop/panel/final ghi thẳng vào thư mục này (cho UI web poll)
SCALE = 1.0   # hệ số phóng canvas ghép (1=2400x1900, 2=4800x3800, 3=7200x5700)

def _workdir(prefix, sub=""):
    """Thư mục làm việc: dùng EMIT (được serve) nếu có, không thì temp."""
    if EMIT:
        d = Path(EMIT) / sub if sub else Path(EMIT)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(tempfile.mkdtemp(prefix=prefix))

import cv2, numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
# CLI ngoài (không nằm trong folder này) — override bằng env nếu máy khác đặt chỗ khác
VSC = Path(os.getenv("VSC_ROOT", "/mnt/6C96C1A096C16AE2/vsc"))
# ưu tiên .env cạnh script (self-contained), không có thì dùng .env gốc của chatgpt-api
_DEF_ENV = (HERE / ".env") if (HERE / ".env").exists() else (VSC / "chatgpt-api" / ".env")
ENV_FILE = Path(os.getenv("ARK_ENV_FILE", _DEF_ENV))
ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
VMODELS = ["dola-seed-2-1-turbo-260628",  # model chính
           "seed-2-0-pro-260328", "seed-2-0-lite-260228", "seed-1-6-250915"]  # fallback
OA_MODEL = "byte-plus-seedream-4-5"
BG = (228, 228, 228)

FLAT = ("A 2D FLAT VECTOR sewing-pattern diagram, absolutely flat and symmetric, front view, on a "
        "plain flat light grey background. SOLID FLAT COLOR FILL only, NO fabric texture, NO denim "
        "weave, NO topstitching, NO gathers, NO ruffles, NO folds, NO wrinkles, NO shading, NO "
        "gradient, NO drop shadow, NO 3D, completely matte. Clean flat color blocks with crisp edges, "
        "like a technical flat CAD garment panel. No human body, no head, no arms, no legs.")
SHAPE_BODICE = " Shape: a sleeveless bodice tank panel like the second reference (shape guide)."
SHAPE_FAN = (" Shape: a wide quarter-circle CIRCLE-SKIRT fan panel spread flat like the second "
             "reference (shape guide). Every band follows the curved arc, parallel to the curved hem.")


# ---------- 1. dola-seed vision: bbox + mô tả ----------
def _ark_client():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except Exception:
        pass
    key = os.getenv("ARK_API_KEY")
    if not key:
        sys.exit("Thiếu ARK_API_KEY (env hoặc chatgpt-api/.env).")
    from openai import OpenAI
    return OpenAI(api_key=key, base_url=ARK_BASE)

def _data_uri(p):
    mime = mimetypes.guess_type(p)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(Path(p).read_bytes()).decode()

# Vision chỉ cần bbox (toạ độ 0-1000) + mô tả hoạ tiết -> gửi ảnh THU NHỎ cho nhanh,
# không đổi kết quả (bbox chuẩn hoá). Ảnh gốc to gửi full-res chậm ~2 phút/lần.
VISION_MAXSIDE = int(os.getenv("FLAT_VISION_MAXSIDE", "1152"))
# số panel gen song song. codex gen song song ĐƯỢC khi token còn hạn; chỉ kẹt khi
# token hết hạn và nhiều tiến trình cùng refresh (đua xoay refresh_token -> hỏng auth).
# -> _gen_assemble chạy panel ĐẦU một mình để hâm nóng/refresh token rồi mới bung song song.
GEN_WORKERS = int(os.getenv("FLAT_GEN_WORKERS", "4"))

def _vision_uri(img, maxside=VISION_MAXSIDE):
    im = Image.open(img)
    if max(im.size) > maxside:
        r = maxside / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

VISION_PROMPT = (
    "This is a photo of a person wearing a skater/skirt dress. Return ONLY JSON: "
    '{"bodice":{"box":[x0,y0,x1,y1],"desc":"..."},"skirt":{"box":[x0,y0,x1,y1],"desc":"..."}}. '
    "box = bounding box of that dress part, integers normalized 0-1000 (x from left, y from top). "
    "bodice = top/torso panel (neckline to waist, exclude head/arms/background); "
    "skirt = flared part below the waist (exclude legs/background). "
    "desc = a concise flat-technical description of the PRINTED GRAPHIC on that part "
    "(colors as words, bands top-to-bottom, motifs and their positions) so it can be redrawn as a "
    "flat vector panel. No prose, just the spec.")

def _vision(client, img, prompt, need=()):
    """Gọi vision với fallback qua VMODELS, log từng lần thử. `need` = key bắt buộc trong JSON."""
    last = None
    for m in VMODELS:
        try:
            log("vision", f"thử model {m} ...")
            r = client.chat.completions.create(model=m, messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _vision_uri(img)}}]}])
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

def seed_vision(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, VISION_PROMPT, need=("bodice", "skirt"))
    return js, (w, h), m

PEOPLE_PROMPT = (
    "This photo shows several people standing in a row, each wearing a dress. Return ONLY JSON "
    '{"people":[[x0,y0,x1,y1],...]} ordered LEFT TO RIGHT, one TIGHT bounding box per person '
    "covering that person's full dress (shoulders to hem), integers normalized 0-1000 (x from left, "
    "y from top). Exclude neighboring people from each box as much as possible.")

def seed_people(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, PEOPLE_PROMPT, need=("people",))
    return js["people"], (w, h), m

TWOVIEW_PROMPT = (
    "This ONE photo shows the SAME dress from TWO angles side by side: a FRONT view (chest graphic "
    "faces the camera) and a BACK view (rear of the dress). Return ONLY JSON: "
    '{"front":{"bodice":{"box":[..],"desc":".."},"skirt":{"box":[..],"desc":".."}},'
    '"back":{"bodice":{"box":[..],"desc":".."},"skirt":{"box":[..],"desc":".."}}}. '
    "box = integers normalized 0-1000 (x from left, y from top) of that part on that view. "
    "Correctly decide which figure is FRONT vs BACK. desc = concise flat-technical spec of the printed "
    "graphic on that part (colors, bands, motifs, positions) to redraw as a flat vector panel.")

def seed_two_views(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, TWOVIEW_PROMPT, need=("front", "back"))
    return js, (w, h), m

def crop_norm(img, box, wh, out):
    w, h = wh
    x0, y0, x1, y1 = (box[0]*w//1000, box[1]*h//1000, box[2]*w//1000, box[3]*h//1000)
    x0, x1 = sorted((max(0, x0), min(w, x1)))
    y0, y1 = sorted((max(0, y0), min(h, y1)))
    Image.open(img).crop((x0, y0, x1, y1)).save(out)
    return (x0, y0, x1, y1)


# ---------- 3. gen: chatgpt-imagegen (primary) -> OpenArt Seedream 4.5 (fallback) ----------
# ưu tiên binary bundled trong ./bin, không có thì dùng trên PATH
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
    """File là script python (shebang) -> chạy qua interpreter (bắt buộc trên Windows,
    vì Windows không exec trực tiếp script không có .exe)."""
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
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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


# ---------- 4. cutout ngưỡng + ghép ----------
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

def _fit(img, bw, bh):
    r = min(bw / img.width, bh / img.height)
    return img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)

def _place(cv, img, box):
    x0, y0, x1, y1 = box; im = _fit(img, x1 - x0, y1 - y0)
    cv.alpha_composite(im, (x0 + (x1 - x0 - im.width) // 2, y0 + (y1 - y0 - im.height) // 2))

def assemble(panels, out, scale=None):
    s = scale or SCALE or 1.0
    def S(v): return int(round(v * s))
    def box(b): return tuple(S(x) for x in b)
    W, H = S(2400), S(1900)
    cv = Image.new("RGBA", (W, H), BG + (255,)); d = ImageDraw.Draw(cv)
    for i in range(2):
        y = 55 + i * 70
        d.rectangle(box([70, y, 2400 - 70, y + 46]), fill=(255, 255, 255, 255),
                    outline=(150, 150, 150, 255), width=max(1, S(2)))
    _place(cv, cutout(panels["skirt_front"]), box((40, 200, 1200, 900)))
    _place(cv, cutout(panels["skirt_back"]),  box((40, 960, 1200, 1660)))
    _place(cv, cutout(panels["bodice_front"]), box((1280, 300, 2340, 830)))
    _place(cv, cutout(panels["bodice_back"]),  box((1280, 980, 2340, 1510)))
    cv.convert("RGB").save(out)
    return out


# ---------- orchestration ----------
def _gen_assemble(work, bcf, bcb, scf, scb, dbf, dbb, dsf, dsb, out):
    """4 crop + 4 mô tả -> gen 4 panel -> ghép."""
    jobs = [
        ("bodice_front", FLAT + SHAPE_BODICE + " " + dbf, [bcf, ASSETS/"shape_bodice.png"]),
        ("bodice_back",  FLAT + SHAPE_BODICE + " " + dbb, [bcb, ASSETS/"shape_bodice.png"]),
        ("skirt_front",  FLAT + SHAPE_FAN    + " " + dsf, [scf, ASSETS/"shape_fan.png"]),
        ("skirt_back",   FLAT + SHAPE_FAN    + " " + dsb, [scb, ASSETS/"shape_fan.png"]),
    ]
    def _one(item):
        i, (name, prompt, refs) = item
        dst = work / f"panel_{name}.png"
        log("gen", f"panel {i}/4: {name} (bắt đầu)")
        gen_panel(prompt, refs, dst)
        log("gen", f"panel {i}/4: {name} (xong)")
        return name, dst
    items = list(enumerate(jobs, 1))
    panels = {}
    if GEN_WORKERS > 1 and len(items) > 1:
        # panel đầu chạy MỘT MÌNH -> nếu token codex hết hạn thì chỉ 1 tiến trình
        # refresh (tránh đua xoay refresh_token). Xong rồi mới bung phần còn lại.
        name, dst = _one(items[0]); panels[name] = dst
        with ThreadPoolExecutor(max_workers=min(GEN_WORKERS, len(items) - 1)) as ex:
            for name, dst in ex.map(_one, items[1:]):
                panels[name] = dst
    else:
        for it in items:
            name, dst = _one(it); panels[name] = dst
    log("assemble", "cắt nền (ngưỡng) + ghép template ...")
    assemble(panels, out)
    log("done", str(out))
    return out


def run(front, back, out, emit_sub=""):
    log("start", f"1 váy | front={Path(front).name}" + (f" back={Path(back).name}" if back else " (tự suy mặt sau)"))
    client = _ark_client()
    work = _workdir("flat_", emit_sub)
    log("setup", f"workdir {work}")

    def process(img, side):
        log("crop", f"{side}: nhìn ảnh ...")
        js, wh, m = seed_vision(client, img)
        log("crop", f"{side}: bodice {js['bodice']['box']} skirt {js['skirt']['box']}")
        bc = work / f"crop_bodice_{side}.png"; sc = work / f"crop_skirt_{side}.png"
        crop_norm(img, js["bodice"]["box"], wh, bc)
        crop_norm(img, js["skirt"]["box"],  wh, sc)
        return js, bc, sc

    if back:  # nhìn 2 ảnh song song cho nhanh
        with ThreadPoolExecutor(max_workers=2) as ex:
            ff = ex.submit(process, front, "front"); fb = ex.submit(process, back, "back")
            jf, bcf, scf = ff.result(); jb, bcb, scb = fb.result()
        bdesc_b, sdesc_b = jb["bodice"]["desc"], jb["skirt"]["desc"]
    else:  # không có ảnh sau -> tái dùng crop trước, mô tả "plain back"
        jf, bcf, scf = process(front, "front")
        bcb, scb = bcf, scf
        bdesc_b = jf["bodice"]["desc"] + " This is the BACK: keep the same yoke/trim but plain body, remove any front-only buttons/buckle/emblem."
        sdesc_b = jf["skirt"]["desc"] + " Identical to the front."

    return _gen_assemble(work, bcf, bcb, scf, scb,
                         jf["bodice"]["desc"], bdesc_b, jf["skirt"]["desc"], sdesc_b, out)


def run_twoviews(img, out):
    """1 ảnh chứa CẢ view trước + sau của cùng 1 váy -> dùng lưng THẬT."""
    client = _ark_client()
    log("start", f"2-view (trước+sau trong 1 ảnh) | {Path(img).name}")
    log("crop", "nhìn ảnh, tách 2 view ...")
    js, wh, m = seed_two_views(client, img)
    fb, bb = js["front"], js["back"]
    log("crop", f"front bodice {fb['bodice']['box']} | back bodice {bb['bodice']['box']}")
    work = _workdir("fb_")
    bcf = work / "bodice_front.png"; scf = work / "skirt_front.png"
    bcb = work / "bodice_back.png";  scb = work / "skirt_back.png"
    crop_norm(img, fb["bodice"]["box"], wh, bcf); crop_norm(img, fb["skirt"]["box"], wh, scf)
    crop_norm(img, bb["bodice"]["box"], wh, bcb); crop_norm(img, bb["skirt"]["box"], wh, scb)
    return _gen_assemble(work, bcf, bcb, scf, scb,
                         fb["bodice"]["desc"], bb["bodice"]["desc"],
                         fb["skirt"]["desc"], bb["skirt"]["desc"], out)


def run_multi(img, out):
    """Ảnh nhóm nhiều người: detect từng người -> chạy full pipeline (1 ảnh) -> out_1..N."""
    log("start", f"NHÓM nhiều người | {Path(img).name}")
    client = _ark_client()
    log("detect", "nhìn ảnh, tách từng người ...")
    boxes, wh, m = seed_people(client, img)
    log("detect", f"phát hiện {len(boxes)} người")
    work = _workdir("multi_")
    outs = []
    for i, box in enumerate(boxes, 1):
        person = work / f"person_{i}.png"
        crop_norm(img, box, wh, person)
        dst = out.with_name(f"{out.stem}_{i}{out.suffix}")
        log("person", f"=== {i}/{len(boxes)} -> {dst.name} ===")
        try:
            run(str(person), None, dst, emit_sub=f"person_{i}")
            outs.append(dst)
        except Exception as e:
            log("person", f"  người {i} lỗi, bỏ qua: {str(e)[:140]}")
    log("done", f"multi {len(outs)}/{len(boxes)}: " + ", ".join(o.name for o in outs))
    return outs


def _selfcheck():
    # crop_norm scale/clamp: 0-1000 -> pixel, clamp trong khung, tự sửa thứ tự
    im = Image.new("RGB", (1000, 500), "white")
    p = Path(tempfile.mktemp(suffix=".png")); im.save(p)
    assert crop_norm(p, [100, 200, 900, 400], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 100, 900, 200)
    assert crop_norm(p, [900, 0, 100, 999], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 0, 900, 499)  # sort x, scale y
    print("selfcheck OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--front"); ap.add_argument("--back")
    ap.add_argument("--multi", action="store_true",
                    help="ảnh nhóm nhiều người: tự tách từng người -> out_1..N")
    ap.add_argument("--twoviews", action="store_true",
                    help="1 ảnh chứa cả view trước+sau của cùng 1 váy: dùng lưng thật")
    ap.add_argument("-o", "--out", default="flat_out.png")
    ap.add_argument("--log", help="file ghi log (mặc định: <out>.log cạnh output)")
    ap.add_argument("--emit", help="ghi crop/panel/final vào thư mục này (cho UI web poll)")
    ap.add_argument("--scale", type=float, default=1.0, help="phóng cỡ ghép (1/2/3)")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
    elif a.front:
        if a.emit:
            globals()["EMIT"] = a.emit
        globals()["SCALE"] = max(0.5, min(4.0, a.scale))
        out = Path(a.out)
        logpath = _open_log(a.log or out.with_suffix(".log"))
        log("start", f"log -> {logpath}")
        if a.multi:
            run_multi(a.front, out)
        elif a.twoviews:
            run_twoviews(a.front, out)
        else:
            run(a.front, a.back, out)
    else:
        ap.error("cần --front (và tùy chọn --back/--multi/--twoviews), hoặc --selfcheck")
