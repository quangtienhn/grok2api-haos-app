# Changelog — Grok2API cho Home Assistant OS

## [3.1.8] — 2026-11-15

Bản đóng gói đầu tiên, được sửa lại sau báo cáo Anti Audit độc lập.

### Fixed

- **P0-1 — `map: [type: data]` không hợp lệ**: đã xoá hoàn toàn khối `map`. `/data` do
  Supervisor tự mount, không cần khai báo; `data` cũng không phải type hợp lệ của `map`.
- **P0-2 — Ingress không dùng được**: bỏ `ingress`, `ingress_port`, `panel_icon`,
  `panel_title`, `panel_admin`. Bản SPA dùng asset tuyệt đối (`/assets/...`) và gọi API ở
  gốc `/api/...`, nên không chạy được dưới `/api/hassio_ingress/<token>/`.
  Thay bằng `webui` + `watchdog` trỏ thẳng cổng 8000.
- **P0-3 — Client API key không thể tạo qua option**: thêm `haos-bootstrap.sh` tạo key qua
  API chính thức (`POST /api/admin/v1/client-keys`), idempotent theo tên, không in secret
  ra log; người dùng reveal trong Web UI.
- **P1-2 — Mật khẩu quản trị**: `admin_password` là option **bắt buộc** (≥ 8 ký tự), không
  sinh mật khẩu ngẫu nhiên (vì phải ghi log). Có đồng bộ mật khẩu khi khởi động qua
  `PUT /api/admin/v1/me/password`; nếu không xác thực được thì chỉ cảnh báo, không reset ngầm.
  Sửa lỗi `curl` gửi POST thay vì PUT khi đổi mật khẩu (test IT23c phát hiện).
- **P2-3 — Audit ledger bị nới lỏng**: `ledgerMode` trả về `enforce` (mặc định của upstream).
- **Watchdog**: dùng `/healthz` thay vì `/readyz`. `/readyz` trả 503 trên cài đặt mới
  (chưa có tài khoản) và sẽ gây restart loop.

### Removed

- Option không có tác dụng: `log_level` (logger upstream hardcode `slog.LevelInfo`),
  `default_model`, `api_key`.
- `mock-server/mock_server.py` và `tests/test_suite.py` — mock Python không kiểm thử gì
  liên quan tới binary thật; thay bằng `tests/integration/run_integration.py` chạy binary Go thật.

### Added

- `rootfs/usr/local/bin/haos-entrypoint.sh`: đọc `options.json`, kiểm tra mật khẩu, sinh
  secret persistent (`0600`), render `config.yaml` (`0600`), chạy non-root, forward signal.
- `rootfs/usr/local/bin/haos-bootstrap.sh`: đồng bộ mật khẩu + tạo client API key.
- `rootfs/usr/local/bin/healthcheck.sh`: `HEALTHCHECK` dùng `/healthz`.
- `tests/integration/run_integration.py`: 37 test trên binary Go thật (manifest, API, static
  SPA, mã hóa at-rest, restart, log hygiene, bootstrap E2E, entrypoint simulation).
- `tests/integration/measure_resources.py`: đo thời gian khởi động / RAM / CPU.
- `LICENSE` (MIT + ghi công upstream) ở **gốc repository** để GitHub nhận diện giấy phép, và bản
  sao trong `grok2api/` đi kèm add-on; icon/logo mới; tài liệu `README.md`/`DOCS.md`;
  `translations/{en,vi}.yaml` theo đúng option hiện có.

### Packaging polish

- `LICENSE` được sao ra gốc `grok2api-haos-app/` (giống hệt bản trong `grok2api/`) để GitHub
  hiển thị đúng license khi dùng làm Custom Repository.
- `haos-bootstrap.sh`: `trap cleanup EXIT INT TERM` thay cho `EXIT` — thư mục tạm `mktemp -d`
  vẫn được dọn khi container bị dừng lúc đang `wait_for_health`.
- `repository.yaml`: `url` chuyển về dạng `https://github.com/<user>/grok2api-haos-app` và ghi
  chú rõ phải thay `<user>` trước khi publish; không còn trỏ nhầm về repo upstream.
- Thêm test IT30 khoá 3 điểm trên (LICENSE gốc, URL repository, signal trap).

### Known limitations

- Chưa chạy trên Docker/Supervisor/HAOS thật (môi trường phát triển không có Docker/WSL2).
- Chưa kiểm chứng chat thành công tới grok.com (không dùng credential thật).
- Chưa benchmark trên Intel N150.
