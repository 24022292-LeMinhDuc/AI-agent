# Virtual Energy Engineer

Ứng dụng chạy trên máy để phân tích dữ liệu điện lịch sử. Nhập số đo, kiểm tra chất lượng, theo dõi tiêu thụ, đánh giá hiệu suất và chênh lệch so với đường cơ sở, rồi xuất báo cáo có nguồn tính toán.

**Dữ liệu mẫu và các giả định trong cấu hình phục vụ chạy thử.** Để đánh giá một cơ sở thực tế, dùng số đo và giả định đã xác minh. Hệ số phát thải mặc định là `null`.

## Cài đặt và chạy bằng một lệnh

Cần Python 3.12 trở lên và PowerShell. `run.ps1` tạo `.venv` khi cần, cài các phiên bản thư viện trong `requirements.txt` và khởi chạy Streamlit tại `pi`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

`ExecutionPolicy Bypass` chỉ áp dụng cho tiến trình này, không thay đổi policy toàn máy. Nếu policy hiện tại cho phép chạy script thì có thể dùng ngắn hơn: `.\run.ps1`.

Để bật Gemini, tạo API key trong Google AI Studio và lưu vào `.env`.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Điền `GEMINI_API_KEY=...`, lưu tệp rồi chạy lại `.\run.ps1`. `.env` bị loại khỏi Git. Khi chạy offline, nhập liệu, mapping từ khóa, tính toán, duyệt cơ hội và xuất báo cáo mẫu đều hoạt động. Hỏi đáp Gemini cần API key.

Tích hợp `google-genai` dùng `gemini-3.8-flash` mặc định. Có thể đặt `GEMINI_MODEL` và danh sách `GEMINI_FALLBACK_MODELS` phân cách bằng dấu phẩy trong `.env`. Khi model đầu tiên gặp lỗi tạm thời hoặc không khả dụng, ứng dụng thử các model dự phòng. Một lượt hỏi đáp giữ model đã phản hồi để tiếp tục các bước gọi công cụ. [Tài liệu model Gemini](https://aistudio.google.com/docs/models/gemini-3.8-flash)

Thanh bên dẫn đến **Dữ liệu**, **Dashboard**, **SEU & EnPI**, **Cơ hội**, **Báo cáo** và **Hỏi đáp**. Dữ liệu và bộ lọc được giữ khi chuyển trang. Thao tác **Xóa dữ liệu** kết thúc phạm vi phân tích hiện tại. Xem [phân tích chức năng, công cụ và đề xuất phát triển](docs/ui-backend-map.md).

## Cấu trúc thư mục

- `run.ps1`: khởi tạo/cập nhật môi trường và chạy ứng dụng bằng một lệnh.
- `.env.example`: mẫu khai báo API key; bản `.env` thực tế không được đưa vào Git.
- `app.py` — giao diện Streamlit và điều hướng của ứng dụng.
- `agent.py`, `schemas.py`: vòng lặp hỏi đáp, kiểm tra schema và điều phối công cụ.
- `gemini_service.py`: cấu hình model, thời gian chờ và xử lý lỗi Gemini dùng chung.
- `mapping.py`: đề xuất mapping, cảnh báo đơn vị và chuẩn hóa cột.
- `tools.py`: các phép tính tất định cho làm sạch, SEU, EnPI, so sánh và cơ hội.
- `energy_baseline.py` — khớp đường cơ sở theo ngày, chuẩn hóa theo điều kiện kỳ báo cáo và tính chỉ số độ phù hợp.
- `db.py`: lưu lịch sử chạy công cụ và quyết định Approve/Reject trong SQLite.
- `report.py`: tổng hợp hồ sơ và xuất Markdown, DOCX hoặc JSON.
- `config.yaml`: các giả định cấu hình; cần kiểm chứng trước khi áp dụng thực tế.
- `data/`: ba bộ CSV mẫu và `make_sample.py` để tạo dữ liệu giả lập.
- `tests/`: unit tests và acceptance tests.
- `scripts/verify_numbers.py`: đối chiếu số trong câu trả lời với kết quả công cụ theo `run_id`.
- `docs/`: tài liệu dự án.
- `runs.db`: cơ sở dữ liệu SQLite được tạo khi ghi lần chạy hoặc quyết định đầu tiên.

## Demo trong 5 phút

1. Khởi động bằng lệnh ở trên. Mở **Dữ liệu**, chọn **Dùng dữ liệu mẫu** hoặc tải một tệp CSV hay XLSX. Với XLSX, chọn trang tính chứa số đo.
2. Kiểm tra ánh xạ cột, chẳng hạn `Điện chiller (kWh)` → `kwh_chiller`, rồi bấm **Xác nhận mapping**.
3. Mở **Dashboard** để xem điện năng, nhiệt độ và sản lượng. Mục so sánh tháng cho biết chênh lệch tổng, kWh bình quân ngày có số đo và độ phủ từng công tơ.
4. Mở **SEU & EnPI** để xem Pareto và kWh/tấn. Trong mục **Đường cơ sở năng lượng (EnB) và tiết kiệm ước tính**, chọn chiller, biến sản lượng và CDD, kỳ cơ sở 01/11/2025–31/07/2026 và kỳ báo cáo 01/08/2026–31/10/2026 khi dùng `data/sample_a.csv`. Bấm **Tính đường cơ sở** rồi xem chênh lệch, độ phủ và chẩn đoán mô hình.
5. Mở **Cơ hội**, xem các giả định và ghi quyết định. Sang **Báo cáo** để xuất Markdown, DOCX hoặc JSON. Báo cáo dùng kết quả EnB đã tính cho phạm vi hiện tại.
6. Nếu đã cấu hình Gemini, mở **Hỏi đáp** hoặc chuyển một kết quả phân tích từ nút hỏi AI để xem diễn giải kèm nguồn.

## Kiểm chứng câu trả lời

Có thể truyền trực tiếp câu trả lời có trích dẫn `[run_id: 12]` hoặc đọc từ tệp.

```powershell
python scripts/verify_numbers.py "Tiết kiệm 1,000 kWh [run_id: 12]"
python scripts/verify_numbers.py --file answer.txt
```

Mặc định công cụ đọc `runs.db` ở thư mục dự án. Dùng `--db path\to\runs.db` để chỉ định cơ sở dữ liệu khác. Kết quả `run_id` được dùng để đối chiếu sự xuất hiện của giá trị. Việc xác nhận ý nghĩa, đơn vị và kết luận vẫn cần đọc kết quả nguồn. Lệnh trả mã thoát khác 0 khi thiếu nguồn hoặc có số chưa khớp.

## Chạy kiểm thử

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Chạy benchmark để đo thời gian các bước giao diện và số lần gọi phép tính với CSV mẫu.

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_app.py
```

Benchmark dùng AppTest và chế độ mapping dự phòng, không gọi Gemini thật; thời gian bao gồm chi phí của Streamlit Test Runner.
