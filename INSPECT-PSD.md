# Xuất thông số PSD

Chạy từ thư mục gốc dự án:

```bash
python3 studio/inspect_psd.py "mau.psd"
python3 studio/inspect_psd.py "mau.psd" --include-binary -o "mau.full.json"
```

Lệnh đầu in mọi trường cùng trạng thái, ghi `mau.inspect.json` và
`mau.inspect.audit.txt`. Dùng `--summary` nếu chỉ muốn tóm tắt trên terminal;
hai file vẫn có bảng đầy đủ. Lệnh thứ hai nhúng cả payload
nhị phân mà thư viện đọc được dưới dạng base64; dung lượng có thể rất lớn.
Script không chỉnh sửa PSD nguồn. Đường dẫn JSON/TXT đã tồn tại sẽ được ghi đè.

## Trạng thái kiểm kê

- `CHƯA XÁC MINH`: mặc định cho mọi giá trị chưa được xác minh với renderer.
  Không đồng nghĩa đã hỗ trợ, cũng không khẳng định chắc chắn dựng sai.
- `ĐANG TẮT`: descriptor có `enab=False`, `present=False` hoặc nằm dưới
  `masterFXSwitch=False`; các tham số vẫn được liệt kê. Đây là trạng thái
  descriptor, không suy diễn visibility của layer thành tắt mọi effect.
- `KHÔNG GIẢI MÃ`: byte gốc chưa được diễn giải, kể cả khi đã nhúng base64.
  Có size/hash và đường dẫn; không in payload base64 dài ra terminal/TXT.
  Trạng thái này cũng có thể xuất hiện ở mã định danh nhị phân ngắn.
- `LỖI`: trường đọc lỗi, kiểu không được serializer hỗ trợ hoặc cảnh báo parser.
- `ĐÃ GHI NHẬN`: key và tên kiểu cấu trúc; **không phải xác nhận render đúng**.

`audit` trong JSON và TXT duyệt cả cây layer lẫn `raw_psd`, nên có dữ liệu lặp.
Mỗi dòng ghi đường dẫn, giá trị, trạng thái. Mọi run, trường lạ, effect tắt đều
được duyệt; không còn whitelist bỏ qua style hay công nhận cả loại effect.
Chưa có trường nào được tự gắn nhãn renderer đã xử lý đúng.

`--strict` trả exit code 2 nếu còn trường chưa xác minh/chưa giải mã hoặc lỗi.
Không dùng cờ này: exit 0 chỉ nói quá trình xuất thành công, không chứng minh
khả năng render; lỗi đọc/parser vẫn trả exit 2.

## Nội dung JSON

- `document`: kích thước, color mode, bit depth.
- `layers`: cây layer, kể cả layer ẩn, group, pixel, text, smart object,
  adjustment; có thứ tự, ID, visibility, opacity, blend mode và bounding box.
- `layers[].record`: mọi trường record và tagged block, gồm mask, clipping,
  blend ranges, vector path và các descriptor mà thư viện đọc được.
- Với text: `engine_dict`, `resource_dict`, `document_resources`, `type_tool`
  giữ toàn bộ style run, paragraph run, bảng font, dữ liệu warp và text gốc.
  Cấu trúc `fields._items` chứa dữ liệu của đối tượng psd-tools.
- `transform`: cả 6 thành phần affine, góc trục X/Y, scale, shear/skew,
  translation, determinant và reflection. Góc dương theo chiều kim đồng hồ
  trong hệ tọa độ PSD có Y hướng xuống. Khi có skew/lật, dùng ma trận gốc;
  một góc đơn không diễn tả đủ biến đổi.
- `effects`: từng effect gồm các thuộc tính API, cả effect tắt, kèm trạng thái
  bật/tắt chung. Descriptor gốc luôn nằm trong tagged blocks, kể cả effect
  lạ hoặc effect không được API liệt kê.
- `raw_psd`: toàn bộ record cấp file do psd-tools parse, bao gồm image
  resources, color data, record layer, channel data và composite image data.
- `issues`, `parser_messages`: trường đọc lỗi, kiểu chưa hỗ trợ, cảnh báo parser.
  Script in cảnh báo và trả exit code 2 nếu có mục trong hai danh sách này.
- `binary_payloads_summarized`: các payload chỉ ghi kích thước và SHA-256.
  Dùng `--include-binary` để nhúng payload. Không có cờ này, byte ngắn đến
  64 byte vẫn được giữ đầy đủ để đọc key/ID dễ hơn.

Descriptor giữ tên kiểu, `classID`, đơn vị và key gốc. Mapping có key nhị phân
dùng `_entries` với cặp `key`/`value`, tránh đổi key gây trùng/mất dữ liệu.
Một số dữ liệu xuất hiện cả ở mục thuận tiện và ở `raw_psd`.

## Giới hạn

Đây là bản xuất **mọi cấu trúc thư viện đọc được**, không phải cam kết giải mã
100% định dạng riêng của Photoshop. Dữ liệu opaque chỉ được giữ dưới dạng
byte; smart object nhúng chưa được mở đệ quy, file liên kết ngoài không được
tải. Thư viện có thể bỏ qua dữ liệu chưa hỗ trợ hoặc padding. JSON không thay
thế bản PSD gốc và không dùng làm bản sao có thể khôi phục chính xác file.

Text đã raster hóa không còn thông số text/góc xoay gốc để khôi phục chắc chắn.
Transform của smart object/vector giữ trong descriptor tương ứng, chưa được
quy đổi thành một góc chung. Có đủ dữ liệu không đồng nghĩa renderer magnet
dựng đúng mọi hiệu ứng.

`--magnet-check` được giữ để tương thích lệnh cũ; kiểm kê đầy đủ đã bật mặc định.
Bộ kiểm tra whitelist cũ đã bị bỏ để tránh bỏ sót và kết luận lỗi thời về renderer.

Kiểm tra hồi quy:

```bash
python3 -m unittest discover -s studio -p test_inspect_psd.py
```
