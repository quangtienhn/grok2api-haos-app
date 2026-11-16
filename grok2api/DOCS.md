# Grok2API — Tài liệu Add-on cho Home Assistant OS

Tài liệu này mô tả chính xác những gì add-on làm, dựa trên mã nguồn upstream
`chenyme/grok2api` v3.1.8 và các script đóng gói trong `rootfs/`.
Những phần **chưa được kiểm chứng trên HAOS thật** đều được ghi rõ.

---

## 1. Add-on làm gì

Grok2API là một gateway API: nó nhận request theo chuẩn OpenAI, dùng tài khoản
**Grok Web Chat** (cookie SSO) để gọi grok.com, rồi trả kết quả về theo đúng
định dạng OpenAI (JSON hoặc SSE streaming).

```
Client (HA / app khác)
   │  Authorization: Bearer g2a_xxx_yyy   (client API key)
   ▼
Home Assistant OS  ──►  add-on grok2api  ──►  https://grok.com (WebSocket gateway)
   cổng 8000            /app/grok2api
                        (binary Go, chạy user grok2api uid 10001)
```

Add-on này **không** sửa mã upstream. Nó chỉ:

1. Kéo base image `ghcr.io/chenyme/grok2api:v3.1.8-amd64` / `-arm64`.
2. Cài thêm `bash curl jq openssl` (Alpine apk) để chạy script.
3. Copy `rootfs/usr/local/bin/{haos-entrypoint,haos-bootstrap,healthcheck}.sh`.
4. Đổi entrypoint: script của add-on dựng `config.yaml` rồi mới chạy binary upstream.

Các endpoint quan trọng:

| Endpoint | Ý nghĩa |
| --- | --- |
| `/v1/models`, `/v1/chat/completions` | API tương thích OpenAI (dùng client API key) |
| `/api/admin/v1/auth/login`, `/me/password` | Đăng nhập / đổi mật khẩu quản trị |
| `/api/admin/v1/client-keys` | Tạo / xem / reveal client API key |
| `/api/admin/v1/accounts` | Quản lý tài khoản Grok |
| `/` (Web UI) | Giao diện quản trị (SPA) |
| `/healthz` | Liveness, luôn `200 {"ok":true}` khi process sống |
| `/readyz` | Readiness, **503** khi chưa có model route/tài khoản khả dụng |

Vì sao `watchdog` và `HEALTHCHECK` dùng `/healthz` mà **không** dùng `/readyz`:
một cài đặt mới chưa có tài khoản Grok nào, `/readyz` trả `503` và Supervisor sẽ
khởi động lại container vô hạn. Điều này đã được kiểm chứng bằng test IT02
(`/readyz` = 503 trên instance mới, `/healthz` = 200).

---

## 2. Vì sao KHÔNG dùng Home Assistant Ingress

Bản SPA trong image được build với:

- `index.html` tham chiếu asset **tuyệt đối**: `/assets/…`, `/runtime-config.js`, `/favicon.ico`.
- `public/runtime-config.js` có `apiBaseUrl: ""`, nghĩa là frontend gọi API tại gốc `/api/...`.
- Backend phục vụ frontend tại `/` và đăng ký API tại `/api/admin/v1`, `/v1` — không có
  tiền tố ingress.

Ingress của Supervisor đưa add-on vào đường dẫn `/api/hassio_ingress/<token>/`.
Trình duyệt sẽ tải `/assets/...` từ **Home Assistant Core** thay vì từ add-on → 404,
và các lệnh gọi `/api/admin/v1/...` cũng đi sai chỗ. Vì vậy add-on:

- **không** khai báo `ingress`, `ingress_port`, `panel_icon`, `panel_title`, `panel_admin`;
- dùng `webui: "http://[HOST]:[PORT:8000]/"` để nút **OPEN WEB UI** và sidebar hoạt động;
- dùng `watchdog: "http://[HOST]:[PORT:8000]/healthz"`.

Hệ quả: Web UI và API **lộ ra LAN** trên cổng được map. Xem mục 9 (bảo mật).

---

## 3. Cài đặt

### 3.1. Cài từ thư mục local (khuyến nghị khi chưa publish repository)

1. Bật add-on **Samba share** hoặc **SSH & Web Terminal** trên HAOS.
2. Copy nguyên thư mục `grok2api-haos-app/` vào `/addons/` của HAOS:

   ```
   /addons/grok2api-haos-app/
     repository.yaml
     LICENSE                      (bản sao ở gốc repo — GitHub nhận diện license tại đây)
     grok2api/
       config.yaml  build.yaml  Dockerfile
       rootfs/  translations/  icon.png  logo.png  README.md  DOCS.md
   ```

3. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates** (hoặc restart Supervisor).
4. Mục **Local add-ons** sẽ có **Grok2API** → **Install**.

### 3.2. Cài từ repository GitHub

Trước khi publish, chỉnh **hai** chỗ ở gốc repository:

1. `repository.yaml` → `url` phải là repo của bạn, ví dụ
   `https://github.com/<user>/grok2api-haos-app`. Giá trị mặc định là placeholder `<user>`;
   nếu để nguyên, liên kết trong Add-on Store sẽ trỏ sai.
2. Giữ `LICENSE` ở **gốc repository** (đã có). GitHub chỉ nhận diện giấy phép khi file nằm ngay
   gốc; `grok2api/LICENSE` là bản sao đi kèm add-on, nội dung giống hệt.

Sau đó:

1. Đưa thư mục này lên GitHub (giữ nguyên cấu trúc, `repository.yaml` và `LICENSE` ở gốc).
2. HAOS → Add-on Store → ⋮ → **Repositories** → dán URL repo → **Add**.
3. Chọn **Grok2API** → **Install**.

> Kiểm tra nhanh trước khi push:
> `python -c "import yaml;print(yaml.safe_load(open('repository.yaml'))['url'])"` phải in ra URL của bạn.

### 3.3. Quá trình build

`config.yaml` không có `image:`, nên Supervisor build local theo `build.yaml`:

```yaml
build_from:
  amd64: "ghcr.io/chenyme/grok2api:v3.1.8-amd64"
  aarch64: "ghcr.io/chenyme/grok2api:v3.1.8-arm64"
```

Dockerfile chỉ chạy `apk add` + copy file, **không compile Go** → build nhanh, nhưng
máy HAOS cần Internet để kéo base image (~28 MB nén, ~74 MiB khi giải nén).

> **Chưa kiểm chứng:** bước build và cài đặt thực tế trên HAOS/Supervisor chưa được chạy
> trong quá trình phát triển (không có Docker/WSL2). Đây là hạng mục bắt buộc của lần
> kiểm định tiếp theo.

---

## 4. Cấu hình (tab Configuration)

| Option | Kiểu | Mặc định | Ý nghĩa |
| --- | --- | --- | --- |
| `admin_username` | `str` | `admin` | Tên tài khoản quản trị tạo ở lần chạy đầu |
| `admin_password` | `password` (**bắt buộc**) | rỗng | Mật khẩu quản trị, ≥ 8 ký tự. Không tự sinh |
| `bootstrap_client_key` | `bool?` | `true` | Tự tạo client API key nếu chưa có |
| `client_key_name` | `str?` | `Home Assistant` | Tên key dùng để tìm/tạo |
| `request_timeout` | `int(30,7200)?` | `600` | Timeout tối đa một request (kể cả streaming) |
| `public_base_url` | `str?` | rỗng | Base URL công khai khi chạy sau reverse proxy |

Ví dụ:

```yaml
admin_username: admin
admin_password: "<your-strong-password>"
bootstrap_client_key: true
client_key_name: "Home Assistant"
request_timeout: 600
public_base_url: ""
```

Add-on **không** có option `log_level`, `default_model` hay `api_key`:

- logger upstream hardcode `slog.LevelInfo`, không có biến môi trường để đổi;
- model do upstream quản lý trong DB theo tài khoản, không có option cấu hình;
- client API key **chỉ** được tạo qua API chính thức (mật khẩu/cookie không được đặt trong
  cấu hình add-on).

---

## 5. Dữ liệu lưu trữ

Supervisor tự mount `/data` là vùng persistent (không cần `map:`, và `map` với type `data`
là **sai schema** — add-on không khai báo `map`).

| Đường dẫn | Nội dung | Quyền |
| --- | --- | --- |
| `/data/options.json` | Option Supervisor ghi (chứa mật khẩu admin) | Supervisor tạo |
| `/data/config/config.yaml` | Config sinh ra cho binary upstream, chứa secret | `0600` root |
| `/data/config/secrets.env` | `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEY` | `0600` |
| `/data/config/admin.applied` | Mật khẩu admin đã đồng bộ (JSON) | `0600` |
| `/data/app/backend.db` | SQLite: tài khoản, key, audit, settings | `0700` dir |
| `/data/app/media` | Ảnh/video tạm | `0700` |

Đổi `CREDENTIAL_ENCRYPTION_KEY` sau khi đã import tài khoản sẽ làm credential cũ không
giải mã được. Muốn "reset" hoàn toàn: xoá cả `/data/app` và `/data/config/secrets.env`.

---

## 6. Luồng khởi động

`haos-entrypoint.sh` (chạy bằng root, PID 1):

1. Đọc `/data/options.json` bằng `jq`.
2. Kiểm tra `admin_password` ≥ 8 ký tự. Sai → log lỗi rõ ràng và dừng add-on
   (không sinh mật khẩu ngẫu nhiên, vì phải ghi ra log).
3. Lần chạy đầu: sinh `JWT_SECRET` (32 byte hex) và `CREDENTIAL_ENCRYPTION_KEY`
   (32 byte base64) → `/data/config/secrets.env` (`0600`).
4. Render `/data/config/config.yaml` (`0600`) từ template nhúng trong script.
5. `su-exec grok2api:grok2api` chạy `/app/grok2api --config /data/config/config.yaml
   --listen 0.0.0.0:8000`.
6. Chạy `haos-bootstrap.sh` (cùng user `grok2api`) sau khi `/healthz` trả 200.
7. Forward `TERM/INT/QUIT` cho process con và trả về đúng exit code.

`haos-bootstrap.sh` (chỉ gọi API chính thức trên `127.0.0.1:8000`):

1. Chờ `/healthz` (tối đa 240 s).
2. **Đồng bộ mật khẩu**: đăng nhập bằng `admin_password` trong Configuration.
   - Thành công → ghi `/data/config/admin.applied`.
   - Thất bại → thử mật khẩu đã áp dụng trước đó; nếu được thì
     `PUT /api/admin/v1/me/password` để đổi sang mật khẩu trong Configuration.
   - Cả hai thất bại → **cảnh báo, không đổi gì** (không khoá tài khoản, không reset ngầm).
3. **Client API key**: tìm key theo `client_key_name`; nếu chưa có thì
   `POST /api/admin/v1/client-keys`. Secret **không** được in ra log; lấy trong Web UI.
4. Mọi lỗi đều chỉ ghi log — bootstrap **không** làm chết container.

Nguyên tắc: **option `admin_password` là nguồn chuẩn**. Nếu bạn đổi mật khẩu trong Web UI,
lần khởi động sau add-on sẽ đưa mật khẩu về giá trị trong Configuration. Muốn đổi mật khẩu,
hãy đổi ở tab Configuration rồi restart.

---

## 7. Lấy client API key

1. Web UI → **API keys**. Nếu `bootstrap_client_key: true`, key tên `Home Assistant` đã tồn tại.
2. Bấm **reveal** để xem secret (dạng `g2a_<prefix>_<secret>`).
3. Client gửi kèm:

   ```
   Authorization: Bearer g2a_<prefix>_<secret>
   ```

   hoặc header `X-API-Key: g2a_...`.

Key sai/thiếu → `401 {"error":{"code":"invalid_api_key", ...}}` (đã kiểm chứng IT09).

> Lưu ý: API key không phải mật khẩu Grok. Nó chỉ mở cổng vào gateway nội bộ.
> Không đặt key vào file cấu hình add-on và không chia sẻ công khai.

---

## 8. Dùng từ Home Assistant

Sau khi đã import ít nhất một tài khoản Grok Web, `/v1/models` mới có dữ liệu
(trước đó danh sách rỗng và chat trả `404 model_not_found` — đã kiểm chứng IT11).

### 8.1. Gọi bằng `rest_command`

```yaml
rest_command:
  grok_chat:
    url: "http://<HA_IP>:8000/v1/chat/completions"
    method: POST
    headers:
      Authorization: !secret grok2api_key
      Content-Type: application/json
    payload: >-
      {"model": "<model-id-trong-/v1/models>",
       "messages": [{"role": "user", "content": "{{ message }}"}],
       "stream": false}
```

### 8.2. Dùng integration kiểu OpenAI

Nếu bạn dùng một integration tự viết hỗ trợ “OpenAI compatible”:

- Base URL: `http://<HA_IP>:8000/v1`
- API key: client key `g2a_...`
- Model: lấy đúng ID từ `GET /v1/models`

Grok2API không phải OpenAI; một số tham số riêng của OpenAI
(`response_format`, `logprobs`, tools đặc thù…) có thể không được hỗ trợ đầy đủ.
Hãy kiểm tra với model của bạn.

---

## 9. Bảo mật

| Hạng mục | Trạng thái trong add-on |
| --- | --- |
| Chạy non-root | Binary chạy user `grok2api` (uid/gid 10001, có sẵn trong image) |
| Mật khẩu admin | Bắt buộc trong option, không log, hash trong DB, ≥ 8 ký tự |
| Brute-force | Rate limit đăng nhập của upstream: 12 lần/phút/user, 30 lần/phút/IP |
| API key | Chỉ tạo qua admin API; lưu dạng hash + secret mã hóa; reveal cần đăng nhập |
| Credential Grok | Mã hóa AES-256-GCM **từng trường** trong SQLite |
| Toàn bộ file DB | **Không** mã hóa (SQLite thuần, không SQLCipher) → bảo vệ bằng quyền file |
| Audit | `ledgerMode: enforce` — ledger lỗi thì request bị từ chối |
| Log | Không in mật khẩu, cookie, API key, token (đã quét tự động IT16) |
| TLS | Không có; add-on phục vụ HTTP trong LAN |

Khuyến nghị:

- **Không** forward cổng 8000 ra Internet. Nếu cần truy cập ngoài, dùng reverse proxy có
  TLS (Nginx Proxy Manager, Caddy…) và đặt `public_base_url` tương ứng.
- Giới hạn firewall LAN cho cổng 8000 nếu có thể.
- Sao lưu `/data` (snapshot HAOS) sẽ chứa `secrets.env` + database → bảo vệ bản backup.

### 9.1. Phụ thuộc bên thứ ba: Statsig signer

Grok Web yêu cầu header `x-statsig-id` hợp lệ. Upstream mặc định:

- mode `url`, signer `https://grok.wodf.de/sign` (dịch vụ công khai **không** do add-on
  vận hành), timeout 12 s, cache 1 giờ, fallback giá trị cũ khi signer lỗi.
- `StatsigMode`, `StatsigSignerURL`, `StatsigManualValue` **không** đọc từ `config.yaml`
  (tag `yaml:"-"`); chỉ đổi được qua Admin UI → Settings
  (`PUT /api/admin/v1/settings`).

Nếu signer chết, chat có thể lỗi dù tài khoản còn hạn. Cách xử lý:

1. Web UI → **Settings** → mục Statsig.
2. Đổi `statsigMode` sang `manual` (dán giá trị `x-statsig-id` bạn tự lấy), hoặc giữ `url`
   nhưng đặt signer riêng do bạn kiểm soát.
3. Lưu; không cần restart.

Add-on **không** che giấu phụ thuộc này và không tắt nó (nếu tắt, Grok Web provider
không hoạt động).

---

## 10. Sao lưu và phục hồi

- **Backup**: Settings → System → Backups → tạo snapshot. Snapshot HAOS bao gồm `/data`
  của add-on (database, secrets, media).
- **Restore**: phục hồi snapshot trên cùng máy hoặc máy khác (cùng kiến trúc). Vì
  `secrets.env` nằm trong `/data`, credential vẫn giải mã được.
- **Sao lưu thủ công**: copy toàn bộ `/data` của add-on (Samba/SSH) — tương đương
  `backend.db` + `config/secrets.env` + `app/media`.
- **Không** copy riêng `backend.db` mà bỏ `secrets.env` (hoặc ngược lại): credential sẽ
  không đọc được.

---

## 11. Đổi / quên mật khẩu

**Đổi mật khẩu (bình thường):**

1. Tab **Configuration** → sửa `admin_password` → **Save**.
2. **Restart** add-on. Log sẽ ghi `Admin password rotated to the add-on option value`.
3. Đăng nhập lại Web UI bằng mật khẩu mới.

> Đổi mật khẩu trong Web UI cũng được, nhưng lần restart sau add-on sẽ đưa về giá trị
> trong Configuration (nguồn chuẩn). Hãy đổi ở Configuration để tránh nhầm lẫn.

**Quên mật khẩu (không đăng nhập được):**

1. Đặt `admin_password` = mật khẩu bạn **còn nhớ** (nếu có) → restart.
2. Nếu không còn mật khẩu nào: dừng add-on, xoá `/data/app/backend.db` (và tuỳ chọn
   `/data/config/admin.applied`), sau đó start lại. Database được tạo mới, tài khoản admin
   được tạo từ option.
   - Mất: tài khoản Grok đã import, client API key, audit cũ.
   - Giữ: `secrets.env` (nếu không xoá) và media.
3. Nếu xoá cả `secrets.env`, mọi credential cũ (nếu còn) sẽ không giải mã được.

Add-on **không** có API “quên mật khẩu” vì upstream không có; mọi thay đổi mật khẩu đều
qua `PUT /api/admin/v1/me/password` (cần đăng nhập).

---

## 12. Xử lý sự cố

| Hiện tượng | Nguyên nhân thường gặp | Cách xử lý |
| --- | --- | --- |
| Add-on dừng ngay, log `admin_password is missing or shorter than 8 characters` | Chưa đặt mật khẩu | Đặt `admin_password` ≥ 8 ký tự trong Configuration |
| Log `Could not sign in with either the option password or the previously applied password` | Mật khẩu trong DB khác cả hai nguồn | Đặt `admin_password` đúng mật khẩu hiện tại hoặc làm theo mục 11 |
| Log `Could not create client API key ... (HTTP 409)` | Key cùng tên đã tồn tại | Bình thường; reveal key sẵn có trong Web UI |
| Add-on chạy nhưng sidebar báo không kết nối | Cổng 8000 bị đổi/chiếm | Configuration → Network → xem port mapping; mở `http://<ip>:8000/` |
| `401 invalid_api_key` khi gọi `/v1` | Sai/thiếu `Authorization: Bearer g2a_...` | Reveal lại key trong Web UI |
| `404 model_not_found` | Chưa import tài khoản hoặc model ID sai | Import tài khoản; lấy ID từ `GET /v1/models` |
| Chat lỗi dù đã có tài khoản | Statsig signer chết, cookie SSO hết hạn, hoặc Grok chặn Cloudflare | Xem log; đổi signer/`manual`; import lại cookie |
| `/readyz` trả 503 | Chưa có route/tài khoản khả dụng | Bình thường với cài đặt mới; watchdog dùng `/healthz` |
| Supervisor báo unhealthy | `/healthz` không trả 200 | Xem log container; kiểm tra process/binary |

Xem log: **Settings → Add-ons → Grok2API → Log**. Log là JSON của upstream và dòng
`TIMESTAMP [LEVEL] message` của script add-on.

---

## 13. Giới hạn đã biết (và chưa kiểm chứng)

- **Chưa chạy trên Docker/Supervisor/HAOS thật** trong quá trình phát triển. Mọi kết quả
  kiểm thử hiện có là trên **binary Go thật** chạy trên Windows amd64 + script bash thật.
- **Chưa kiểm chứng chat thành công tới grok.com** (không dùng cookie/tài khoản Grok thật).
  Luồng chat dùng WebSocket gateway `wss://grok.com/ws/mgw/` với TLS thật; chưa có bằng
  chứng end-to-end trong môi trường kiểm thử.
- **Chưa benchmark trên Intel N150.** Số đo hiện tại: khởi động ~0,53 s, RAM idle ~71 MB
  working set / ~95 MB private (Windows, không phải HAOS).
- Không có TLS: cần reverse proxy nếu muốn HTTPS.
- Không hỗ trợ nhiều instance chia sẻ một database (add-on chạy 1 replica, `runtimeStore: memory`).
- `/data` phải nằm trên ổ SSD; SQLite + audit ledger ghi liên tục.

---

## 14. Gỡ cài đặt

1. Dừng add-on.
2. **Uninstall** (tuỳ chọn: xoá dữ liệu — HA sẽ hỏi hoặc bạn xoá `/data` thủ công).
3. Xoá thư mục `/addons/grok2api-haos-app` (nếu cài local) hoặc repository đã thêm.
