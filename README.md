# TOOL V1 RING — Python Web

Ứng dụng Python/Flask quản lý tài khoản Google Flow, hàng đợi prompt, kết quả ảnh và dự án audio/video.

## Cơ chế trình duyệt

Mỗi account dùng một persistent Chromium profile, cổng automation và proxy riêng. Playwright là engine điều khiển duy nhất: nhập prompt, chọn model/tỷ lệ, bấm tạo, theo dõi tiến trình và lấy ảnh kết quả đều đi qua API Playwright. Tool không còn lớp WebSocket tự viết và không có fallback sang engine điều khiển cũ. Nếu Playwright không kết nối được, account báo `PLAYWRIGHT_ERROR` để được xử lý hoặc xoay vòng.

Chromium được mở bên ngoài Playwright để giữ profile đăng nhập lâu dài; Playwright gắn vào browser đang chạy bằng API chính thức `connect_over_cdp`. Đây chỉ là phương thức kết nối do Playwright cung cấp, không phải lớp CDP/WebSocket tự triển khai. Nếu profile chính bị Chromium khóa, tool tạo runtime clone đã loại cache và file khóa rồi chạy trên clone, không sửa phiên đăng nhập gốc.

Khi scheduler chạy nền trên Windows, Chromium được tạo trên desktop riêng `SuperImageFlowWorker` bằng `CreateDesktopW`/`CreateProcessW`. Nút **Mở** đóng worker ẩn và mở lại đúng profile trên desktop chính để người dùng đăng nhập hoặc kiểm tra. Nếu Windows từ chối desktop riêng, DWM Cloak/off-screen là fallback an toàn.

## Tính năng

- Tạo ảnh Flow bằng pool account ACTIVE/dự phòng, mặc định 5 luồng; người dùng tự chọn số luồng và số chạy thực tế chỉ phụ thuộc số account READY.
- Mỗi prompt được gửi theo giãn cách chung tối thiểu 5 giây; account lỗi được xoay xuống hàng dự phòng.
- Chọn trực tiếp 1–4 output trong cài đặt Flow; một prompt chỉ hoàn tất khi đã tải đủ số ảnh thật và lưu `image_files` vào manifest dự án.
- Ảnh kết quả được đối chiếu bằng khóa candidate và SHA-256 với toàn bộ ảnh đã có trong thư mục dự án; ảnh trùng bị trả lại hàng prompt thay vì tính là thành công.
- Chỉ hỗ trợ Google Flow; không chứa đường chạy Grok.
- Chọn model Flow, tỷ lệ ảnh, browser, proxy và mức ưu tiên riêng theo account.
- Mỗi dự án dùng `01_AUDIO`, `02_IMAGE` và `prompts.json` ở thư mục gốc.
- Tách audio bằng FFmpeg và xuất video dự án.

## Chạy ứng dụng

Mở terminal tại thư mục dự án rồi chạy:

```powershell
.\apply.bat
```

Dashboard chạy tại `http://127.0.0.1:5050`. Dữ liệu account tiếp tục được lưu tại `%LOCALAPPDATA%\FlowLinkTest_v09`.

## Cấu trúc

- `app.py`: backend, Playwright controller, account và scheduler.
- `browser_watchdog.py`: đóng đúng browser profile của tool khi backend thoát.
- `templates/index.html`: dashboard.
- `static/`: CSS và JavaScript giao diện.
- `requirements.txt`: Flask và Playwright.
- `apply.bat`: cài phụ thuộc và chạy ứng dụng.
