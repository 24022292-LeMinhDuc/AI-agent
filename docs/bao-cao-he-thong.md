# Kiến trúc và công cụ hệ thống

Virtual Energy Engineer dùng Streamlit cho giao diện, pandas và NumPy cho phép tính, Plotly cho biểu đồ, SQLite cho lịch sử chạy và quyết định. Các phiên bản thư viện nằm trong [requirements.txt](../requirements.txt). [Bản đồ chức năng và cách đọc kết quả](ui-backend-map.md)

## Luồng xử lý

1. `app.py` nhận CSV hoặc XLSX. `mapping.py` chuẩn hóa tên cột, đơn vị và miền giá trị. Phiên giữ bản đã ánh xạ và bản sạch để các công cụ chọn nguồn dữ liệu phù hợp.
2. `agent.run_tool` kiểm tra tham số theo `schemas.py` trước khi gọi `tools.py` hoặc `energy_baseline.py`. Lần thực thi được lưu trong `runs.db` cùng tham số, kết quả, thời điểm, phạm vi và `run_id`. Lỗi kiểm tra tham số được trả về trước khi ghi lần chạy.
3. `report.py` tổng hợp phạm vi tệp, chất lượng, SEU, EnPI, nhiệt và phát thải khai báo, cùng cơ hội đã duyệt hoặc từ chối. Nếu người dùng đã tính EnB trong phiên, báo cáo đính kèm kết quả đó. Hồ sơ xuất ở định dạng Markdown, Word và JSON.
4. `gemini_service.py` dùng chung cấu hình, thời gian chờ và model dự phòng. `agent.ask` giữ model đã phản hồi trong suốt lượt hỏi đáp, giới hạn số vòng gọi công cụ và đối chiếu số có trích dẫn với kết quả trong phiên.
5. `weather_api.py` lấy dữ liệu mô hình Open-Meteo. `weather_panel.py` hiển thị nhiệt độ và độ ẩm theo giờ và dùng bộ nhớ đệm theo tọa độ. Các phép tính lịch sử dùng nhiệt độ trong tệp nguồn.

## Danh mục công cụ

| Công cụ | Tham số chính | Kết quả |
|---|---|---|
| `dataset_summary` | — | Khoảng ngày, độ phủ, điện tổng, đối chiếu công tơ và chỉ số vận hành đã khai báo |
| `dashboard_data` | `level`, `start_date`, `end_date` | Chuỗi tổng hợp theo kỳ và KPI. Gemini nhận phần tóm tắt. |
| `data_quality` | — | Lưu bản sạch trong phiên và trả số giá trị loại, nội suy và còn thiếu theo cột điện |
| `seu_analysis` | `threshold` | Pareto và danh sách SEU từ các công tơ phụ tải |
| `enpi` | `total_col`, `drivers` | kWh/tấn từ các dòng ghép cặp và hồi quy theo sản lượng, CDD hoặc cả hai |
| `energy_baseline` | Ngày đầu và cuối hai kỳ, `energy_col`, `drivers`, `model_type`, `adjustments` | Đường cơ sở OLS hoặc Change-Point (3PC, 3PH, 4P, 5P, Auto) theo ngày, điện tham chiếu, tiết kiệm sau NRA, CUSUM, FSU 90%/95% và kiểm định mô hình |
| `energy_balance` | `fuel_type` (tùy chọn) | Cân bằng GJ/TOE đầu vào-đầu ra, liên kết Sankey, SEU theo GJ, hiệu suất lò hơi, kW/RT & COP chiller và CO₂ do đốt nhiên liệu |
| `tariff_power_quality` | — | Chi phí điện TOU, kịch bản dịch tải, công suất đỉnh, hệ số phụ tải, cos φ, tiền phản kháng và dung lượng bù sơ bộ |
| `compare_periods` | `month_a`, `month_b` | Chênh lệch giữa hai tháng, bình quân ngày có số đo và độ phủ từng công tơ |
| `list_opportunities` | — | Ước tính điện, tiền và phát thải tiết kiệm, ROI (%) từ tiền tiết kiệm năm và hoàn vốn (tháng) |

Tên cột chuẩn nằm trong `mapping.CANONICAL_COLUMNS`. Các biến EnB nằm trong `energy_baseline.BASELINE_DRIVERS`. Các trường hợp chất lượng dữ liệu, tính toán, giao diện và tích hợp được kiểm tra trong `tests/`.

## Tiện ích

- `run.ps1` chuẩn bị môi trường Python và khởi chạy ứng dụng.
- CLI `scripts/verify_numbers.py` và trợ lý trong `agent.py` dùng chung `verification.verify_results`. Trợ lý đọc bằng chứng theo `run_id` và phạm vi phiên, yêu cầu Gemini viết lại tối đa hai lần khi còn sai lệch, rồi chuyển kết quả kiểm chứng cho giao diện.
- `scripts/benchmark_app.py` đo thời gian và số lần chạy phép tính qua AppTest.

Xem lệnh chạy ứng dụng, tạo lại dữ liệu mẫu và kiểm thử trong [README](../README.md).
