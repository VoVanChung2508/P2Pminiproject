# Network Monitoring System (Hệ thống Giám sát Mạng & Thiết bị)

Hệ thống giám sát hiệu năng mạng và tài nguyên máy trạm (Node/Client) phân tán thời gian thực theo mô hình Client-Server. 

---

## 🌟 Tính năng Nổi bật

- **Máy chủ Giám sát Đa luồng (Multi-threaded Server)**:
  - Lắng nghe kết nối TCP từ nhiều máy trạm đồng thời.
  - Thu thập và cập nhật liên tục các chỉ số tài nguyên: **CPU**, **RAM**, **Disk**, **Network**.
  - Cơ chế **Heartbeat** tự động phát hiện thiết bị mất kết nối (`ONLINE` -> `OFFLINE` sau 15 giây).
  - Hệ thống cảnh báo tự động khi các chỉ số vượt ngưỡng an toàn (CPU > 80%, RAM > 80%, Disk > 90%).
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

## 🏗 Kiến trúc Hệ thống

```
                               +-------------------------------------+
                               |     Server Manager GUI (Tkinter)    |
                               |         server.py / server/         |
                               +------------------+------------------+
                                                  | Quản lý tiến trình
                                                  v
+------------------------+     TCP (Cổng 8888)    +-------------------------------------+     HTTP (Cổng 8081)    +------------------------+
|   Client GUI Agent     | =====================> |        Core Monitoring Server       | <====================== |      Web Dashboard     |
| monitoring_client.py   |    REGISTER / SYSTEM   |               Main.py               |      REST APIs / HTML   |  http://localhost:8081 |
+------------------------+    HEARTBEAT / LOGOUT  |  - TCP Server (Multi-threaded)      |                         +------------------------+
                                                  |  - Flask HTTP Web & REST APIs       |
+------------------------+     TCP (Cổng 8888)    |  - State Manager & Alert Engine     |
|   Client CLI Agent     | =====================> |                                     |
| monitoring_client.py   |                        +-------------------------------------+
|        (--cli)         |
+------------------------+
```

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
python server.py
# hoặc:
python server/server.py
```
- Nhập cổng TCP (mặc định: `8888`) và cổng HTTP (mặc định: `8081`). *(Lưu ý: Mặc định chọn 8081 để tránh xung đột cổng 8080 thường bị các dịch vụ như Lenovo Vantage / AgentService chiếm dụng).*
- Bấm **Start Server**.
- Bấm **Open Web Dashboard** để mở giao diện web trên trình duyệt (`http://localhost:8081`).

#### Cách 2: Chạy trực tiếp từ dòng lệnh
```bash
python Main.py
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
| **REGISTER** | `REGISTER\|<client_name>\|<ip>\|<port>` | `OK\|REGISTERED` | Đăng ký thiết bị mới vào danh sách giám sát |
| **SYSTEM** | `SYSTEM\|<client_name>\|CPU=<cpu>\|RAM=<ram>\|DISK=<disk>\|NETWORK=<net>` | `OK\|SYSTEM` | Cập nhật thông số tài nguyên thời gian thực |
| **HEARTBEAT**| `HEARTBEAT\|<client_name>` | `OK\|HEARTBEAT` | Duy trì trạng thái sống (tránh bị timeout) |
| **LOGOUT** | `LOGOUT\|<client_name>` | `OK\|LOGOUT` | Ngắt kết nối, chuyển trạng thái sang `OFFLINE` |

---

## 🌐 Danh sách REST APIs

| Endpoint | Method | Mô tả |
| :--- | :--- | :--- |
| `/api/health` | GET | Kiểm tra trạng thái máy chủ và thông tin cổng đang lắng nghe |
| `/api/clients` | GET | Danh sách toàn bộ các client, trạng thái Online/Offline và các thông số mới nhất |
| `/api/clients/<name>/history` | GET | Lịch sử các mẫu đo gần nhất của một client cụ thể (lên tới 120 mẫu) |
| `/api/alerts` | GET | Danh sách các cảnh báo vượt ngưỡng tài nguyên gần nhất |

---

## 📁 Cấu trúc Thư mục

```
P2Pminiproject/
├── Main.py                     # Core Server: TCP Listener + Flask Dashboard + REST APIs
├── server.py                   # Điểm khởi chạy chính của Server Manager GUI (Root Launcher)
├── monitoring_client.py        # Điểm khởi chạy chính của Client Agent (Root Launcher)
├── requirements.txt            # Danh sách thư viện phụ thuộc (Flask, psutil)
├── README.md                   # Tài liệu hướng dẫn hệ thống
├── client/
│   ├── monitoring_client.py    # Client Agent tích hợp: Core Engine + GUI (Tkinter) + CLI
│   ├── http_client.py          # Module gọi REST API của Server
│   ├── tcp_client.py           # Module gửi nhận gói tin TCP Protocol
│   └── protocol.py             # Tiện ích giao thức client
├── server/
│   ├── server.py               # Module chạy Server Manager GUI
│   ├── server_gui.py           # Giao diện ServerManagerGUI (Tkinter)
│   ├── server_main.py          # Module quản lý server mở rộng
│   ├── client_handler.py       # Handler xử lý kết nối máy trạm
│   └── peer_info.py            # Cấu trúc thông tin thiết bị
└── shared/
    └── protocol.py             # Các tiện ích mã hóa/giải mã thông điệp chung
```
