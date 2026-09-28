# Network Monitoring System (Hệ thống Giám sát Mạng & Thiết bị)

Hệ thống giám sát hiệu năng mạng và tài nguyên máy trạm (Node/Client) phân tán thời gian thực theo mô hình Client-Server, tích hợp lưu trữ dữ liệu vào Cơ sở dữ liệu MySQL.

---

## 🌟 Tính năng Nổi bật

- **Máy chủ Giám sát Đa luồng (Multi-threaded Server)**:
  - Lắng nghe kết nối TCP từ nhiều máy trạm đồng thời.
  - Thu thập và cập nhật liên tục các chỉ số tài nguyên: **CPU**, **RAM**, **Disk**, **Network**.
  - Cơ chế **Heartbeat** tự động phát hiện thiết bị mất kết nối (`ONLINE` -> `OFFLINE` sau 15 giây).
  - Hệ thống cảnh báo tự động khi các chỉ số vượt ngưỡng an toàn (CPU > 80%, RAM > 80%, Disk > 90%).
- **Lưu trữ bền vững trực tiếp trong MySQL**:
  - Tự động kết nối và khởi tạo CSDL `network_monitor` và các bảng `clients`, `history`, `alerts`.
  - Danh sách client, chỉ số mới nhất, lịch sử metrics và cảnh báo được đọc/ghi trực tiếp từ MySQL; khởi động lại server vẫn xem được dữ liệu đã lưu.
  - Server không khởi động nếu không kết nối/ghi được MySQL, tránh báo thành công trong khi dữ liệu chỉ nằm trong RAM.
- **Web Dashboard Thời gian thực (Flask Web)**:
  - Giao diện Dashboard Dark-theme hiện đại, trực quan.
  - Thống kê tổng số Nodes, số thiết bị đang Online, các cảnh báo vượt ngưỡng.
  - Tự động đồng bộ và hiển thị dữ liệu mới nhất mỗi 2.5 giây mà không cần tải lại trang.
- **Server Manager GUI (Giao diện Quản lý Máy chủ Desktop)**:
  - Ứng dụng Tkinter cho phép Bắt đầu/Dừng server với 1 click.
  - Tùy chỉnh linh hoạt cổng TCP Port và HTTP Port.
  - Nút bấm trực tiếp mở Web Dashboard trên trình duyệt.
  - Xem danh sách thiết bị và nhật ký (Server Log) trực tiếp.
- **Client Agent Desktop (GUI) & CLI**:
  - **Client GUI (`client/monitoring_client.py`)**: Giao diện trực quan hiển thị thanh đo tài nguyên thực tế của máy trạm, nút kết nối và ngắt kết nối.
  - **Client CLI (`client/monitoring_client.py --cli`)**: Script dòng lệnh gọn nhẹ chạy ngầm, hỗ trợ tắt an toàn (Graceful Shutdown) gửi lệnh `LOGOUT` khi bấm `Ctrl+C`.

---

## 🏗 Kiến trúc Hệ thống & Cơ chế Hoạt động

### 1. Sơ đồ Kiến trúc Hệ thống
```
                                +-------------------------------------+
                                |     Server Manager GUI (Tkinter)    |
                                |         server.py / server/         |
                                +------------------+------------------+
                                                   | Quản lý tiến trình
                                                   v
+------------------------+     TCP (Cổng 8888)    +-------------------------------------+     HTTP (Cổng 8081)    +------------------------+
|   Client GUI Agent     | =====================> |        Core Monitoring Server       | <====================== |      Web Dashboard     |
| monitoring_client.py   |    REGISTER / SYSTEM   |         server/server.py            |      REST APIs / HTML   |  http://localhost:8081 |
+------------------------+    HEARTBEAT / LOGOUT  |  - TCP Server (Multi-threaded)      |                         +------------------------+
                                                  |  - Flask HTTP Web & REST APIs       |
+------------------------+     TCP (Cổng 8888)    |  - MySQL-backed State & Alert Engine|
|   Client CLI Agent     | =====================> |  - Database Manager (MySQL)         |
| monitoring_client.py   |                        +------------------+------------------+
|        (--cli)         |                                           | Ghi & Truy vấn Dữ liệu
+------------------------+                                           v
                                                  +-------------------------------------+
                                                  |           MySQL Database            |
                                                  |          network_monitor            |
                                                  |   (clients, history, alerts)        |
                                                  +-------------------------------------+
```

### 2. Kế hoạch & Cơ chế Hoạt động Chi tiết

Hệ thống vận hành theo quy trình phân tán gồm 4 luồng chính:

#### A. Luồng Đăng ký & Gửi Chỉ số qua TCP (Client Agent -> Server)
1. **Khởi tạo & Kết nối**: Khi chạy `monitoring_client.py`, máy trạm gửi gói tin `REGISTER|<name>|<ip>|<port>` tới TCP Port `8888` của Server.
2. **Lắng nghe đa luồng**: Server (`server/server.py`) tiếp nhận kết nối TCP bằng một Thread độc lập cho mỗi Client (`tcp_client_session`), phản hồi `OK|REGISTERED` sau khi lưu client vào bảng `clients` trong MySQL.
3. **Gửi metrics định kỳ**: Mỗi `interval` giây (mặc định 3 giây), Client thu thập các thông số hệ thống bằng `psutil` và gửi thông điệp `SYSTEM|<name>|CPU=...|RAM=...|DISK=...|NETWORK=...`.
4. **Heartbeat & Phát hiện ngắt kết nối**: Định kỳ Client gửi gói `HEARTBEAT|<name>`. Server lưu heartbeat vào MySQL. Sau 15 giây nếu không nhận được dữ liệu từ client, trạng thái sẽ chuyển từ `ONLINE` sang `OFFLINE` trong MySQL.

#### B. Luồng Xử lý Cảnh báo & Lưu trữ CSDL (Alert Engine & Database Engine)
1. **Kiểm tra ngưỡng (Threshold Evaluation)**: Khi nhận dữ liệu từ `SYSTEM`, hệ thống so sánh các chỉ số với ngưỡng an toàn:
   - **CPU** > 80%
   - **RAM** > 80%
   - **Disk** > 90%
2. **Ghi nhận Cảnh báo**: Nếu chỉ số vượt ngưỡng, Server lưu bản ghi cảnh báo trong bảng `alerts` của MySQL.
3. **Bắt buộc lưu MySQL**:
   - Các API Dashboard đọc trực tiếp client, lịch sử và cảnh báo từ MySQL; cache RAM chỉ giữ trạng thái heartbeat tạm thời để xác định client đang hoạt động.
   - Nếu không thể kết nối hoặc ghi vào MySQL, server từ chối khởi động hoặc từ chối bản tin; không tuyên bố lưu thành công bằng RAM.
   - Khi khởi động lại, client cũ vẫn hiện trong Dashboard ở trạng thái `OFFLINE`; history và alerts đã lưu vẫn có thể xem lại.

#### C. Luồng Dashboard & REST APIs (Server -> Web UI)
1. **REST APIs (Flask)**: Server mở HTTP Server (cổng `8081`) cung cấp các REST API cho Dashboard client.
2. **Web Dashboard thời gian thực**: Giao diện HTML/CSS/JS gửi yêu cầu AJAX tới `/api/clients` và `/api/alerts` mỗi 2.5 giây để cập nhật biểu đồ và bảng trạng thái máy trạm mà không cần tải lại trang.

---

## 🗄️ Cấu hình Cơ sở Dữ liệu MySQL

### 1. Kết nối tới MySQL Server đang quản lý bằng MySQL Workbench
MySQL Workbench là ứng dụng quản trị; chương trình kết nối tới **MySQL Server** mà Workbench đang kết nối, không kết nối tới chính ứng dụng Workbench. Lấy `Hostname`, `Port`, `Username` và database từ connection trong Workbench.

Server tự động kết nối tới MySQL khi dịch vụ server khởi động; không cần nhập hay bấm kiểm tra trong giao diện. Server Manager kế thừa cấu hình môi trường Windows và truyền cấu hình đó cho tiến trình server.

Nếu muốn dùng database đã có trong Workbench, đặt `MYSQL_DB` bằng tên database đó và `MYSQL_CREATE_DATABASE=false`. Server kết nối thẳng tới database này, sau đó tạo các bảng ứng dụng còn thiếu. Tài khoản cần quyền truy cập database và quyền tạo bảng; không cần quyền `CREATE DATABASE`. Nếu database chưa có, dùng `MYSQL_CREATE_DATABASE=true` để ứng dụng tự tạo (mặc định).

Có thể tạo database trước trong tab SQL của Workbench:
```sql
CREATE DATABASE network_monitor
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

### 2. Cấu hình tự động trên Windows
Ứng dụng tự đọc cấu hình từ file `.env` ở thư mục gốc dự án (cùng cấp với `Main.py`), nên có thể khởi chạy bằng Server Manager mà không cần thiết lập lại ở mỗi lần chạy. Sao chép `.env.example` thành `.env` rồi sửa bằng đúng Hostname, Port, Username và mật khẩu của kết nối MySQL Server trong Workbench:

```dotenv
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=mat-khau-MySQL-thuc-te
MYSQL_DB=network_monitor
MYSQL_CREATE_DATABASE=false
```

`MYSQL_PASSWORD` phải là mật khẩu tài khoản MySQL thực tế; không để nguyên giá trị mẫu. Lỗi `1045 Access denied ... (using password: NO)` nghĩa là mật khẩu không được cung cấp, thường do chưa tạo `.env` hoặc ứng dụng được mở từ môi trường chưa có biến cấu hình. Đóng rồi chạy lại `Main.py` sau khi sửa `.env`. `MYSQL_CREATE_DATABASE=false` chỉ kết nối database có sẵn; nếu cần ứng dụng tự tạo database, đổi giá trị thành `true` (tài khoản phải có quyền `CREATE DATABASE`).

Các biến môi trường Windows `MYSQL_*`, nếu được thiết lập, sẽ được ưu tiên hơn giá trị trong `.env`. File `.env` đã được loại khỏi Git; không commit hoặc chia sẻ file này. MySQL Workbench chỉ là ứng dụng quản trị—cần nhập đúng thông tin kết nối tới MySQL Server mà Workbench đang sử dụng.

### 3. Khóa quản trị để ngắt client
Đặt `MONITOR_ADMIN_TOKEN` trước khi khởi động server. Dashboard sẽ yêu cầu nhập khóa khi bạn bấm **Ngắt**; không lưu khóa trong mã nguồn hoặc chia sẻ khóa cho client.

**Windows (PowerShell):**
```powershell
$env:MONITOR_ADMIN_TOKEN="replace-with-a-long-random-token"
```

**Linux / macOS:**
```bash
export MONITOR_ADMIN_TOKEN="replace-with-a-long-random-token"
```

Nếu chưa cấu hình khóa, server vẫn khởi động nhưng thao tác ngắt client bị vô hiệu hóa. API quản trị chỉ nên sử dụng trên mạng tin cậy; HTTP mặc định không mã hóa khóa.

### 4. Cấu trúc Các Bảng Dữ liệu (Schema)
Khi `MYSQL_CREATE_DATABASE` bật, hệ thống tự tạo database `network_monitor` nếu chưa có. Dù database được tạo tự động hay có sẵn từ Workbench, server sẽ tạo các bảng ứng dụng còn thiếu:
- **`clients`**: Lưu danh sách máy trạm (`client_key`, `name`, `ip`, `cpu`, `ram`, `disk`, `network`, `status`, `last_seen`, `registered_at`).
- **`history`**: Lưu lịch sử biến động chỉ số tài nguyên (`client_key`, `cpu`, `ram`, `disk`, `network`, `timestamp`).
- **`alerts`**: Lưu nhật ký các cảnh báo vi phạm ngưỡng (`client_key`, `client_name`, `metric`, `value`, `limit_val`, `timestamp`).

---

## 🚀 Hướng dẫn Cài đặt & Sử dụng

### 1. Cài đặt Môi trường
Yêu cầu Python 3.8 trở lên. Cài đặt các thư viện cần thiết:
```bash
pip install -r requirements.txt
```

---

### 2. Khởi động Máy chủ (Server)

Bạn có thể chạy Server bằng 1 trong 2 cách:

#### Cách 1: Sử dụng Giao diện Quản lý (Khuyên dùng)
```bash
python Main.py
```
- Nhập cổng TCP (mặc định: `8888`) và cổng HTTP (mặc định: `8081`). *(Lưu ý: Mặc định chọn 8081 để tránh xung đột cổng 8080 thường bị các dịch vụ như Lenovo Vantage chiếm dụng).*
- Bấm **Start Server**.
- Bấm **Open Web Dashboard** để mở giao diện web trên trình duyệt (`http://localhost:8081`).

#### Cách 2: Chạy trực tiếp từ dòng lệnh
```bash
python -m server.server
```
- Mở trình duyệt và truy cập: `http://localhost:8081`

---

### 3. Khởi động Máy trạm (Client Agent)

#### Cách 1: Sử dụng Client GUI (Giao diện đồ họa)
```bash
python client/monitoring_client.py
# hoặc từ thư mục gốc:
python monitoring_client.py
```
1. Nhập **Tên Client** (ví dụ: `PC-VanPhong-01`).
2. Nhập **Server Host** (mặc định: `127.0.0.1` nếu cùng máy, hoặc IP máy chủ LAN).
3. Nhập **TCP Port** (`8888`) và **HTTP Port** (`8081`).
4. Bấm **Bắt đầu Giám sát** để kết nối và tự động gửi số liệu định kỳ.

#### Cách 2: Sử dụng Client CLI (Chạy ngầm dòng lệnh)
```bash
python client/monitoring_client.py TenMayTram --cli --host 127.0.0.1 --port 8888 --interval 3
# hoặc từ thư mục gốc:
python monitoring_client.py TenMayTram --cli --host 127.0.0.1 --port 8888 --interval 3
```
- Nhấn `Ctrl+C` bất cứ lúc nào để dừng agent an toàn (tự động gửi `LOGOUT`).

---

## 📡 Đặc tả Giao thức TCP (TCP Protocol Specification)

Giao tiếp qua TCP sử dụng chuỗi ký tự UTF-8 phân tách bằng dấu gạch đứng `|` và kết thúc bằng ký tự xuống dòng `\n`.

| Lệnh | Định dạng gửi từ Client | Phản hồi từ Server | Ý nghĩa |
| :--- | :--- | :--- | :--- |
| **REGISTER** | `REGISTER\|<client_name>\|<ip>\|<port>` hoặc thêm `\|PROCESS_LIST_V1` | `OK\|REGISTERED` | Đăng ký thiết bị; client mới quảng bá khả năng nhận yêu cầu danh sách tiến trình |
| **SYSTEM** | `SYSTEM\|<client_name>\|CPU=<cpu>\|RAM=<ram>\|DISK=<disk>\|NETWORK=<net>` | `OK\|SYSTEM` | Cập nhật thông số tài nguyên thời gian thực |
| **HEARTBEAT**| `HEARTBEAT\|<client_name>` | `OK\|HEARTBEAT` hoặc `COMMAND\|GET_PROCESS_LIST\|<request_id>\|<limit>` | Duy trì trạng thái sống; nếu có yêu cầu đang chờ, server gửi lệnh cố định trên kết nối này |
| **LOGOUT** | `LOGOUT\|<client_name>` | `OK\|LOGOUT` | Ngắt kết nối, chuyển trạng thái sang `OFFLINE` |
| **PROCESS_LIST** | `PROCESS_LIST\|<client_name>\|<request_id>\|<JSON>` | `OK\|PROCESS_LIST` | Client trả tối đa 20 tiến trình với `pid`, `name`, `username`, `cpu_percent`, `memory_percent`, `status` |
| **PROCESS_LIST_ERROR** | `PROCESS_LIST_ERROR\|<client_name>\|<request_id>\|UNAVAILABLE` | `OK\|PROCESS_LIST` | Client báo không thể thu thập thông tin tiến trình |

---

Yêu cầu tiến trình chỉ được gửi tới client đã đăng ký và quảng bá `PROCESS_LIST_V1`; lệnh được giao ở heartbeat kế tiếp và hết hạn sau 30 giây. Server chỉ chấp nhận `GET_PROCESS_LIST`, không thực thi shell hay lệnh tùy ý. Request và kết quả được giữ trong bộ nhớ máy chủ, không lưu vào MySQL. Các endpoint yêu cầu tiến trình/kết quả dùng header `X-Admin-Token`; danh tính client hiện tại chỉ dựa trên tên đăng ký, chưa có xác thực mật mã cho client.

## 🌐 Danh sách REST APIs

| Endpoint | Method | Mô tả |
| :--- | :--- | :--- |
| `/api/health` | GET | Kiểm tra trạng thái máy chủ và thông tin cổng đang lắng nghe |
| `/api/clients` | GET | Danh sách toàn bộ các client, trạng thái Online/Offline và các thông số mới nhất |
| `/api/clients/<name>/history` | GET | Lịch sử các mẫu đo gần nhất của một client cụ thể (lên tới 120 mẫu) |
| `/api/alerts` | GET | Danh sách các cảnh báo vượt ngưỡng tài nguyên gần nhất |
| `/api/clients/<name>/disconnect` | POST | Ngắt một client; yêu cầu header `X-Admin-Token: <MONITOR_ADMIN_TOKEN>` |
| `/api/clients/<name>/process-list` | POST | Yêu cầu client gửi danh sách tối đa 20 tiến trình; cần admin token |
| `/api/clients/<name>/process-list` | GET | Xem trạng thái/kết quả request tiến trình gần nhất; cần admin token |

Ngắt từ server là ngắt logic: client được lưu trạng thái `OFFLINE` trong MySQL, các lần gửi heartbeat/chỉ số tiếp theo bị từ chối và agent GUI/CLI sẽ dừng. Để kết nối lại, người dùng phải khởi động giám sát lại để gửi lệnh `REGISTER`. `/api/health` trả về `storage` là `mysql` hoặc `unavailable`, cùng trạng thái bật/tắt tính năng ngắt quản trị.

---

## 📁 Cấu trúc Thư mục

```
P2Pminiproject/
├── Main.py                     # Launcher mở Server Manager GUI
├── monitoring_client.py        # Điểm khởi chạy chính của Client Agent (Root Launcher)
├── requirements.txt            # Danh sách thư viện phụ thuộc (Flask, psutil, mysql-connector-python)
├── README.md                   # Tài liệu hướng dẫn hệ thống & Kiến trúc CSDL
├── common/
│   ├── database.py             # Module quản lý kết nối và lưu trữ bền vững trong MySQL
│   └── ...
├── client/
│   ├── monitoring_client.py    # Client Agent tích hợp: Core Engine + GUI (Tkinter) + CLI
│   ├── http_client.py          # Module gọi REST API của Server
│   ├── tcp_client.py           # Module gửi nhận gói tin TCP Protocol
│   └── protocol.py             # Tiện ích giao thức client
├── server/
│   ├── server.py               # Core service: TCP Listener + Flask Dashboard + REST APIs
│   ├── server_gui.py           # Giao diện ServerManagerGUI (Tkinter)
│   ├── server_main.py          # Module quản lý server mở rộng
│   ├── client_handler.py       # Handler xử lý kết nối máy trạm
│   └── peer_info.py            # Cấu trúc thông tin thiết bị
└── shared/
    └── protocol.py             # Các tiện ích mã hóa/giải mã thông điệp chung
```
