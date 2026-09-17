# Flat Studio — ảnh váy → rập phẳng (web app, Windows)

Ảnh váy mặc thật → layout cắt-may **phẳng**. Web app khởi động bằng `.bat`, không cần quyền admin.

## Cài (1 lần)
```
setup.bat
```
Tự tải **Python + Node + Git** portable vào `%USERPROFILE%\dsds_studio\tools`, cài `aiohttp/pillow/opencv/…` + `codex` (npm), rồi lần lượt mở **đăng nhập OpenArt** (chờ bạn xong) → **đăng nhập ChatGPT/codex**.

Cần chuẩn bị thêm:
- `.env` cạnh file này có dòng `ARK_API_KEY=ark-...` (cho dola-seed nhìn ảnh).
- `bin\chatgpt-imagegen.exe` và `bin\openart.exe` (bản Windows) — đặt vào thư mục `bin\`.

## Chạy
```
start.bat            REM mở http://127.0.0.1:8770
start.bat 9000       REM đổi cổng
```

## Giao diện
- **Bàn cắt (trên):** hiện real-time — mảnh crop → 4 panel gen → thành phẩm ghép.
- **Bảng điều khiển (dưới):** chọn chế độ (1 ảnh / 2 ảnh / 2-view / nhóm), thả ảnh, bấm **Tạo rập**, xem **Nhật ký**.
- **Badge codex hết hạn:** hiện nút *Đăng nhập lại* (chạy `codex login`, hiện URL/mã) khi phát hiện codex lỗi auth.

## Pipeline (lõi)
`dola-seed` (ARK vision) nhìn+crop → `chatgpt-imagegen` (codex) gen, lỗi → **fallback OpenArt Seedream 4.5** → cắt nền + ghép. Xem `flat_pipeline.py`.

## Tự cập nhật
`autoupdate.sh` chạy nền: mỗi 60s `git fetch` + `reset --hard` về remote. Cần cấu hình 1 lần:
```
git init && git add . && git commit -m init
git remote add origin <URL-REPO>
```
Có code mới → app hiện nút **Restart**. Dữ liệu (ảnh) nằm ở `%USERPROFILE%\dsds_studio\` (ngoài repo) nên reset an toàn.

## Dữ liệu (NGOÀI folder studio)
Mặc định nằm cạnh studio (cùng folder cha): `../flat_studio_data/`
- Ảnh upload: `flat_studio_data/uploads/`
- Kết quả mỗi lần chạy: `flat_studio_data/runs/<timestamp>/` (crop_*, panel_*, final.png, run.log)

Đổi chỗ khác: đặt biến môi trường `STUDIO_DATA=D:\duong\dan\folder` trước khi chạy (hoặc thêm vào start.bat).
Để ngoài repo nên autoupdate `git reset --hard` không đụng tới.

## Mở thư mục nhanh
Bấm vào bất kỳ ảnh nào trên bàn cắt (mảnh crop, panel, hay thành phẩm) → mở thẳng thư mục chứa ảnh đó bằng File Explorer.
