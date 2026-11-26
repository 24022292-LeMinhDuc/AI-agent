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

## Thời tiết theo giờ

Mở **Dashboard → Thời tiết theo giờ**. Nhập vĩ độ, kinh độ của địa điểm rồi chọn **Áp dụng vị trí**. **Dùng vị trí mẫu** lấy thời tiết cho một điểm tại TP. Hồ Chí Minh. Nếu cần một địa điểm mặc định khi mở ứng dụng, điền `weather.location_name`, `weather.latitude` và `weather.longitude` trong `config.yaml` rồi khởi chạy lại.

Dashboard lấy nhiệt độ và độ ẩm hiện tại từ mô hình Open-Meteo, hiển thị các mốc nhiệt độ gần đây và dự báo theo giờ địa phương. Khi Dashboard đang mở, ứng dụng kiểm tra xem đã sang giờ mới chưa và dùng chung một lần lấy thành công cho cùng tọa độ trong mỗi giờ. Nếu API gặp sự cố, bảng có thể hiển thị lần tải gần nhất của địa điểm cùng thời điểm tải. **Ngừng theo dõi** dừng cập nhật trong phiên. [Open-Meteo Weather Forecast API](https://open-meteo.com/en/docs)

Nhiệt độ trong tệp tải lên là nguồn của biểu đồ lịch sử, CDD, EnPI và đường cơ sở. Endpoint công khai của Open-Meteo dùng cho mục đích phi thương mại. [Điều khoản Open-Meteo](https://open-meteo.com/en/terms)

## Cấu trúc thư mục

- `run.ps1`: khởi tạo/cập nhật môi trường và chạy ứng dụng bằng một lệnh.
- `.env.example`: mẫu khai báo API key; bản `.env` thực tế không được đưa vào Git.
- `app.py` — giao diện Streamlit và điều hướng của ứng dụng.
- `agent.py`, `schemas.py`: vòng lặp hỏi đáp, kiểm tra schema và điều phối công cụ.
- `gemini_service.py`: cấu hình model, thời gian chờ và xử lý lỗi Gemini dùng chung.
- `weather_api.py`, `weather_panel.py`: lấy thời tiết theo giờ và hiển thị trong Dashboard.
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

1. Khởi động bằng lệnh ở trên. Mở **Dữ liệu**, chọn loại mẫu **Nhà máy**, **Tòa nhà** hoặc **Kiểm tra chất lượng dữ liệu**, rồi bấm **Dùng dữ liệu mẫu**. Có thể tải CSV hoặc XLSX của cơ sở. Với XLSX, chọn trang tính chứa số đo.
2. Kiểm tra ánh xạ cột, chẳng hạn `Dien_chiller_kWh` → `kwh_chiller`, rồi bấm **Xác nhận mapping**.
3. Mở **Dashboard** để xem điện năng, nhiệt độ và sản lượng. Mục so sánh tháng cho biết chênh lệch tổng, kWh bình quân ngày có số đo và độ phủ từng công tơ.
4. Mở **SEU & EnPI** để xem Pareto và kWh/tấn. Trong mục **Đường cơ sở năng lượng (EnB) và tiết kiệm ước tính**, chọn chiller, biến sản lượng và CDD, kỳ cơ sở 01/11/2025–31/07/2026 và kỳ báo cáo 01/08/2026–31/10/2026 khi dùng `data/sample_a.csv`. Bấm **Tính đường cơ sở** rồi xem chênh lệch, độ phủ và chẩn đoán mô hình.
5. Mở **Cơ hội**, xem các giả định và ghi quyết định. Sang **Báo cáo** để xuất Markdown, DOCX hoặc JSON. Báo cáo dùng kết quả EnB đã tính cho phạm vi hiện tại.
6. Nếu đã cấu hình Gemini, mở **Hỏi đáp** hoặc chuyển một kết quả phân tích từ nút hỏi AI để xem diễn giải kèm nguồn.

## Bộ dữ liệu mẫu mở rộng

| Tệp | Nội dung |
|---|---|
| `data/sample_a.csv` | Nhà máy có sản lượng, phụ tải điện, số liệu lò hơi và tải lạnh |
| `data/sample_b.csv` | Tòa nhà có diện tích sàn, tỷ lệ lấp đầy và tải lạnh |
| `data/sample_c.csv` | Nhà máy với 5 số đo chiller âm và 4 ô điện chiếu sáng thiếu để thử kiểm tra chất lượng |

Mỗi tệp gồm 365 ngày và 31 cột. `Don_vi_lap_day` khai báo `Nguoi` hoặc `Pct` cho cột lấp đầy. Điện năng của mẫu được tổng hợp theo ngày. `Khung_gio` ghi bối cảnh vận hành. Muốn có nhiều khoảng đo trong ngày, dùng ngày và giờ riêng cho mỗi mốc trong `Ngay`.

Điện tổng trong tệp là số đo ở ranh giới tổng. SEU dùng các phụ tải có số đo. Phần **Vận hành, nhiệt và phát thải** trong Dashboard và báo cáo hiển thị số giờ chạy, công suất cực đại, cos φ, nhiên liệu, hơi, chi phí và phát thải đã khai báo. Cường độ điện tòa nhà tính theo kỳ và công cụ chiller tính kW/RT, COP từ các dòng ghép được điện năng với tải lạnh. [Quy đổi tải lạnh của NIST](https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b9)

Giá 2.500 VNĐ/kWh, hệ số điện 0,65 kgCO₂e/kWh và hệ số dầu DO 3,2 kgCO₂e/kg dùng riêng để sinh mẫu. Các cột Scope 1 và Scope 2 là số liệu khai báo trong tệp. Khi lập kiểm kê, dùng dữ liệu nhiên liệu, điện mua ngoài và hệ số có nguồn phù hợp với ranh giới báo cáo. [GHG Protocol Scope 2](https://ghgprotocol.org/scope-2-guidance) · [GHG Protocol về đốt nhiên liệu](https://ghgprotocol.org/sites/default/files/ghgp/Stationary_Combustion_Guidance_final_1.pdf)

Tạo lại ba tệp bằng `.\.venv\Scripts\python.exe data\make_sample.py`.

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
