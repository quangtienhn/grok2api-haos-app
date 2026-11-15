# Grok2API cho Home Assistant OS

Add-on đóng gói [chenyme/grok2api](https://github.com/chenyme/grok2api) v3.1.8 thành một ứng dụng
Home Assistant OS, biến phiên **Grok Web Chat** thành API tương thích OpenAI
(`/v1/models`, `/v1/chat/completions`) kèm Web UI quản trị.

> Đây là lớp **đóng gói** cho Home Assistant OS, không phải bản fork. Binary, Web UI và mã
> provider được lấy nguyên bản từ image upstream `ghcr.io/chenyme/grok2api:v3.1.8-*`.

## Trạng thái

| Hạng mục | Trạng thái |
| --- | --- |
| Kiểm thử tích hợp trên binary thật (Windows amd64) | 37/37 PASS |
| Kiểm thử trên Docker / Supervisor / HAOS thật | **CHƯA CHẠY** (môi trường phát triển không có Docker/WSL2) |
| Chat thành công tới grok.com | **CHƯA KIỂM CHỨNG** (không dùng tài khoản/cookie Grok thật) |
| Sẵn sàng cài lên HAOS | **CHƯA** — cần kiểm định độc lập lại (xem báo cáo) |

## Yêu cầu

- Home Assistant OS trên `amd64` (Intel N150) hoặc `aarch64`.
- Một tài khoản Grok Web (SSO cookie) do bạn tự cung cấp. Add-on không kèm tài khoản nào.
- Mật khẩu quản trị do bạn đặt (bắt buộc, tối thiểu 8 ký tự).

## Cài đặt nhanh

1. Copy thư mục `grok2api-haos-app/` vào thư mục `addons/` của HAOS (qua Samba/SSH add-on),
   hoặc thêm repository `https://github.com/<user>/grok2api-haos-app` trong Add-on Store.
2. Add-on Store → **Grok2API** → **Install**. Lần cài đầu Supervisor build local từ
   `build.yaml` (kéo base image rồi copy script), không cần compile Go.
3. Tab **Configuration**: đặt `admin_password` (≥ 8 ký tự). Giữ `bootstrap_client_key: true`.
4. **Start**, mở Web UI qua nút **OPEN WEB UI** (cổng 8000).
5. Web UI → **Accounts** → import SSO cookie Grok Web của bạn.
6. Web UI → **API keys** → **reveal** để lấy client key, dán vào client OpenAI của bạn.

Chi tiết đầy đủ: xem [`DOCS.md`](DOCS.md).

## Đưa lên GitHub (Custom Repository)

Trước khi publish, chỉnh 2 chỗ ở **thư mục gốc** `grok2api-haos-app/`:

1. `repository.yaml` → đổi `url` thành repo của bạn, ví dụ
   `https://github.com/<user>/grok2api-haos-app` (mặc định là placeholder `<user>`).
2. Giữ `LICENSE` ở **gốc repository** (đã có sẵn). GitHub chỉ nhận diện giấy phép khi file này nằm
   ngay gốc; bản trong `grok2api/` chỉ để đi kèm thư mục add-on và có nội dung giống hệt.

Sau đó: Home Assistant → Add-on Store → ⋮ → **Repositories** → dán URL repo → **Add**.

## Bảo mật — những điểm phải biết

- **Không dùng Home Assistant Ingress.** Bản build SPA dùng đường dẫn tuyệt đối
  (`/assets/...`, `/runtime-config.js`) và gọi API tại gốc `/api/...`; chạy sau ingress
  (`/api/hassio_ingress/<token>/`) sẽ tải sai asset và gọi sai API. Add-on mở trực tiếp cổng
  8000 trên LAN và hiển thị trong sidebar bằng `webui`, không phải ingress.
- **Mật khẩu quản trị là nguồn chuẩn.** Mỗi lần khởi động, add-on đăng nhập bằng mật khẩu
  trong Configuration; nếu mật khẩu đã lưu khác, add-on đổi về giá trị trong Configuration
  (qua API chính thức, có log INFO). Nếu cả hai đều sai, add-on **không** đổi gì và chỉ cảnh báo.
- **Khóa mã hóa trường (`credentialEncryptionKey`) và JWT secret** được sinh một lần và lưu tại
  `/data/config/secrets.env` (quyền `0600`). Sao lưu file này cùng database; **không đổi** khóa
  sau khi đã lưu credential, nếu không dữ liệu cũ không giải mã được.
- **Không có secret nào trong log.** Script không in mật khẩu, cookie, API key hay token.
- **Audit ledger ở chế độ `enforce`**: nếu ledger không ghi được, request bị từ chối thay vì
  chạy tiếp mà không có dấu vết kiểm toán.
- **Phụ thuộc bên thứ ba:** Grok Web provider cần `x-statsig-id`. Mặc định add-on dùng signer
  `https://grok.wodf.de/sign` (dịch vụ công khai của bên thứ ba, không do add-on vận hành).
  Bạn có thể đổi sang chế độ `manual` hoặc signer riêng trong Web UI → Settings.
- Credential trong SQLite được mã hóa **ở mức từng trường** (AES-256-GCM). File `.db` **không**
  được mã hóa toàn bộ; đừng chia sẻ file database.

## Giấy phép

MIT. Xem [`LICENSE`](LICENSE) — bao gồm ghi công upstream (Chenyme) và phạm vi đóng gói.
Bản sao dùng cho GitHub nằm ở gốc repository: `grok2api-haos-app/LICENSE` (nội dung giống hệt).
