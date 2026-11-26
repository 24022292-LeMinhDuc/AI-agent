# Báo Cáo Chi Tiết Chức Năng và Công Cụ Hệ Thống — Virtual Energy Engineer

Tài liệu này tổng hợp kiến trúc, các phân hệ chức năng giao diện, bộ công cụ phân tích (deterministic tools), dịch vụ tích hợp và các tiện ích kiểm chứng của hệ thống **Virtual Energy Engineer**.

---

## 1. Tổng quan kiến trúc & Công nghệ nền tảng

**Virtual Energy Engineer** là ứng dụng kiểm toán và phân tích dữ liệu điện năng lịch sử chạy trên nền tảng Python/Streamlit, kết hợp giữa **tính toán tất định (deterministic)** trên máy người dùng và **trợ lý AI (Gemini)** gọi công cụ qua JSON Schema có kiểm chứng nguồn số liệu.

| Thành phần / Thư viện | Phiên bản (`requirements.txt`) | Vai trò trong hệ thống |
|---|---|---|
| **Streamlit** | `1.67.0` | Giao diện web đa trang, thanh điều hướng, quản lý `session_state` |
| **Pandas & NumPy** | `3.1.1` / `2.5.5` | Xử lý bảng dữ liệu, làm sạch, tổng hợp theo kỳ, hồi quy OLS |
| **Plotly** | `7.1.0` | Vẽ biểu đồ tương tác (phụ tải, nhiệt độ, sản lượng, Pareto SEU, đường cơ sở, thời tiết) |
| **Google GenAI SDK** | `3.4.0` | Kết nối Gemini API (`gemini-3.8-flash` mặc định) cho ánh xạ cột, hỏi đáp và diễn giải báo cáo |
| **JSON Schema** | `4.26.0` | Kiểm tra tính hợp lệ của tham số đầu vào trước khi thực thi công cụ |
| **SQLite (`sqlite3`)** | Chuẩn Python | Lưu lịch sử chạy công cụ (`run_id`) và quyết định `Approve/Reject` tại `runs.db` |
| **python-docx & openpyxl** | `1.2.0` / `3.1.5` | Xuất báo cáo định dạng `.docx` và đọc bảng tính Excel `.xlsx` |
| **PyYAML & tzdata** | `6.0.3` / `2026.6` | Đọc cấu hình giả định `config.yaml` và chuẩn hóa múi giờ (`Asia/Ho_Chi_Minh`) |
| **pytest** | `9.2.0` | Bộ kiểm thử tự động (unit tests & acceptance tests) |

---

## 2. Chi tiết các phân hệ chức năng trên giao diện (`app.py`)

Ứng dụng gồm **6 trang chức năng** chính trên thanh điều hướng bên trái, dùng chung trạng thái dữ liệu đã xác nhận trong phiên làm việc:

### 2.1. Trang Dữ liệu (`_show_data_page`)
- **Nạp dữ liệu đầu vào**:
  - Hỗ trợ tải lên tệp **CSV** (tự nhận diện UTF-8 BOM / UTF-16) và **Excel (`.xlsx`)** (cho phép chọn sheet chứa số đo).
  - Tích hợp sẵn 3 bộ dữ liệu mẫu để chạy thử: `data/sample_a.csv`, `data/sample_b.csv`, `data/sample_c.csv`.
- **Ánh xạ & Chuẩn hóa cột (`mapping.py`)**:
  - Nhận diện 32 cột chuẩn (`CANONICAL_COLUMNS`) thuộc các nhóm: thời gian (`timestamp`), vận hành (`facility_type`, `tariff_period`, `operating_status`, `operating_hours`), biến sản xuất/tòa nhà (`production_t`, `floor_area_m2`, `occupancy_people`, `occupancy_pct`), thời tiết (`temp_c`, `outdoor_humidity_pct`), công tơ điện (`kwh_production`, `kwh_chiller`, `kwh_compressed_air`, `kwh_lighting`, `kwh_other`, `kwh_total`), chất lượng điện/nhiệt (`power_factor`, `peak_demand_kw`, `boiler_fuel_kg`, `steam_t`, `cooling_load_rth`), chi phí & phát thải (`energy_cost_vnd`, `grid_emission_factor_kgco2e_per_kwh`, `scope1_tco2e`, `scope2_tco2e`, `total_emissions_tco2e`).
  - Hỗ trợ gợi ý ánh xạ tự động bằng **từ khóa (`_keyword_mapping`)** hoặc **Gemini AI** (`suggest_mapping`).
  - Cảnh báo sai lệch đơn vị (`detect_unit_issues`, ví dụ Wh vs kWh, W vs kW) và kiểm tra ràng buộc miền giá trị (`validate_mapped_data`).
- **Làm sạch & Ghi nhận chất lượng (`tools.clean`)**:
  - Tự động xử lý dòng trùng lặp, loại bỏ nhóm bản ghi xung đột tại cùng thời điểm (`resolve_measurement_rows`), nội suy hoặc thống kê giá trị thiếu/âm/ngoài biên.

### 2.2. Trang Dashboard (`_show_dashboard`)
- **Phân tích tiêu thụ theo thời gian (`tools.dashboard_analysis`)**:
  - Tổng hợp theo **Ngày**, **Tuần** (bắt đầu từ thứ Hai) hoặc **Tháng** trong khoảng ngày lọc.
  - Tính tổng `kWh`, nhiệt độ trung bình từ tệp, sản lượng, chi phí ước tính theo đơn giá cấu hình và độ phủ dữ liệu.
  - Hiển thị biểu đồ đa trục Plotly (`_build_dashboard_figure`).
- **So sánh hai kỳ (`tools.compare_periods`)**:
  - Đối chiếu hai tháng bất kỳ theo tổng `kWh`, bình quân `kWh/ngày có số đo`, nhiệt độ, sản lượng và độ phủ từng công tơ.
- **Thời tiết theo giờ (`weather_panel.py` & `weather_api.py`)**:
  - Tra cứu dữ liệu nhiệt độ và độ ẩm theo giờ trực tiếp từ **Open-Meteo Weather Forecast API** theo tọa độ cấu hình trong `config.yaml` hoặc nhập trên giao diện (có cơ chế cache theo giờ).

### 2.3. Trang SEU & EnPI (`_show_seu_enpi`, `_show_energy_baseline`)
- **Xác định đối tượng sử dụng năng lượng đáng kể — SEU (`tools.seu_pareto`)**:
  - Xếp hạng các công tơ thành phần theo sản lượng điện tiêu thụ, vẽ biểu đồ Pareto (`_build_seu_figure`) và đánh dấu các hạng mục vượt ngưỡng tích lũy (mặc định `seu_threshold: 0.8` trong `config.yaml`).
- **Chỉ số hiệu suất năng lượng — EnPI (`tools.enpi_regression`)**:
  - Tính cường độ năng lượng trực tiếp (`kWh/tấn` trên các bản ghi ghép cặp hợp lệ).
  - Chạy hồi quy tuyến tính theo biến sản lượng (`production_t`), độ ngày làm mát (`cdd`, tính từ `temp_c` và `cdd_base_temp_c` trong `config.yaml`) hoặc kết hợp cả hai.
- **Đường cơ sở năng lượng chuẩn hóa — EnB (`energy_baseline.py`)**:
  - Khớp mô hình bình phương tối thiểu (**OLS**) theo ngày trên kỳ cơ sở (`baseline_start` – `baseline_end`) từ số đo gốc sau chuẩn hóa đơn vị.
  - Hỗ trợ 7 biến điều chỉnh (`BASELINE_DRIVERS`): `production_t`, `cdd`, `operating_hours`, `outdoor_humidity_pct`, `floor_area_m2`, `occupancy_people`, `occupancy_pct`.
  - Dự báo điện năng tham chiếu cho kỳ báo cáo (`reporting_start` – `reporting_end`), tính chênh lệch (`tham chiếu - thực đo`), phần trăm tiết kiệm và độ phủ ngày hợp lệ.
  - Tự động loại ngày có biến ngoài khoảng quan sát kỳ cơ sở (tránh ngoại suy) và trả về các chỉ số kiểm định mô hình: $R^2$, $R^2$ hiệu chỉnh, $\text{CV(RMSE)}$ (%) và $\text{NMBE}$ (%).

### 2.4. Trang Cơ hội (`_show_opportunities`)
- **Đánh giá phương án tiết kiệm (`tools.list_opportunities`)**:
  - Đọc danh sách cơ hội mẫu từ `config.yaml` (ví dụ: tối ưu chiller, khí nén, chiếu sáng) cùng tỷ lệ tiết kiệm giả định (`saving_pct`) và chi phí đầu tư (`capex_vnd`).
  - Quy đổi sản lượng điện năm từ bình quân ngày có số đo × 365, ước tính `kWh` tiết kiệm/năm, tiền tiết kiệm/năm (VNĐ), phát thải CO₂ giảm (nếu có hệ số), **ROI (%)** và **thời gian hoàn vốn (năm)**.
- **Lưu vết quyết định (`db.save_decision`)**:
  - Kỹ sư có thể bấm **Approve** hoặc **Reject** từng cơ hội; trạng thái được lưu vào SQLite (`runs.db`) theo `scope_id` của bộ dữ liệu.

### 2.5. Trang Báo cáo (`_show_report_export`, `report.py`)
- **Tổng hợp hồ sơ (`build_report_data`)**:
  - Gom metadata tệp nguồn, tóm tắt chất lượng dữ liệu, kết quả Dashboard, SEU, EnPI, Đường cơ sở EnB (nếu đã tính trong phiên), danh sách cơ hội kèm quyết định đã lưu, các giả định từ `config.yaml` và toàn bộ `run_id` liên quan.
- **Diễn giải kết quả (`generate_interpretation`)**:
  - Tạo phần nhận xét tổng quan (dùng Gemini nếu có API key, hoặc tự động chuyển sang mẫu diễn giải tất định khi chạy offline).
- **Xuất đa định dạng**:
  - **Markdown (`.md`)** (`render_markdown`)
  - **Microsoft Word (`.docx`)** (`render_docx`)
  - **JSON (`.json`)** (`render_report_json`)

### 2.6. Trang Hỏi đáp (`_show_chat`, `agent.py`)
- **Trợ lý kỹ sư năng lượng ảo (`agent.ask`)**:
  - Cho phép người dùng hỏi tự do hoặc chuyển trực tiếp kết quả từ các trang phân tích sang để AI diễn giải (`_handoff_to_chat`).
  - Thực hiện vòng lặp **function calling** với Gemini (tối đa `MAX_TOOL_ROUNDS = 6`, `MAX_TOOL_CALLS = 16`, giữ tối đa `MAX_HISTORY_MESSAGES = 12` tin nhắn gần nhất).
  - Bắt buộc trích dẫn nguồn số liệu bằng cú pháp `[run_id: <id>]` và hiển thị bằng chứng nguồn (`_show_answer_evidence`) ngay dưới câu trả lời.

---

## 3. Danh mục 8 công cụ phân tích lõi (AI & Backend Tools)

Mọi công cụ trong `schemas.py` và `agent.py` đều được kiểm tra chặt chẽ đầu vào bằng **JSON Schema** (`additionalProperties: False`) trước khi thực thi và đều được ghi log vào SQLite (`db.log_run`) để cấp mã `run_id`:

| Tên công cụ (`name`) | Hàm thực thi phía sau | Tham số đầu vào bắt buộc | Chức năng & Kết quả trả về |
|---|---|---|---|
| `dataset_summary` | `tools.dataset_summary` | *(không có)* | Trả về thống kê tổng quan, khoảng ngày, danh sách công tơ, các chỉ số kiểm toán khai báo (EUI, hiệu suất chiller, phát thải) mà không gửi toàn bộ bảng dữ liệu thô ra ngoài. |
| `dashboard_data` | `tools.dashboard_analysis` | `level` (`day`/`week`/`month`), `start_date`, `end_date` | Tổng hợp kWh theo công tơ, sản lượng, nhiệt độ, chi phí và độ phủ; trả chuỗi dữ liệu cho UI và phần `summary` cho Gemini. |
| `data_quality` | `tools.clean` | *(không có)* | Làm sạch dữ liệu nguồn (`measured_df`), thống kê dòng trùng, xung đột thời điểm, giá trị âm/thiếu và lưu bản sạch vào `clean_df`. |
| `seu_analysis` | `tools.seu_pareto` | `threshold` (`0` – `1`) | Tính tỷ trọng và tích lũy Pareto cho các công tơ thành phần, xác định danh sách SEU vượt ngưỡng. |
| `enpi` | `tools.enpi_regression` | `total_col` (`kwh_*`), `drivers` (`production_t`, `cdd`) | Tính `kWh/tấn` trực tiếp và hồi quy tuyến tính EnPI theo các biến điều chỉnh đã chọn. |
| `energy_baseline` | `energy_baseline.normalized_baseline` | `baseline_start`, `baseline_end`, `reporting_start`, `reporting_end`, `energy_col`, `drivers` | Khớp mô hình OLS kỳ cơ sở, chuẩn hóa dự báo sang kỳ báo cáo, tính chênh lệch điện năng và bộ chỉ số `R²`, `Adj R²`, `CV(RMSE)`, `NMBE`. |
| `compare_periods` | `tools.compare_periods` | `month_a`, `month_b` (`YYYY-MM`) | So sánh tổng `kWh`, bình quân ngày có số đo, nhiệt độ, sản lượng và độ phủ công tơ giữa 2 tháng. |
| `list_opportunities` | `tools.list_opportunities` | *(không có)* | Tính toán tiết kiệm điện, tiền, CAPEX, ROI và thời gian hoàn vốn cho các cơ hội cấu hình trong `config.yaml`. |

---

## 4. Dịch vụ nền tảng, Lưu trữ & Tiện ích đi kèm

### 4.1. Dịch vụ Gemini (`gemini_service.py`)
- Quản lý cấu hình API từ biến môi trường / `.env`: `GEMINI_API_KEY`, `GEMINI_MODEL` (mặc định `gemini-3.8-flash`), `GEMINI_FALLBACK_MODELS` (mặc định `gemini-3.6-flash`, `gemini-3.5-flash-lite`).
- Tích hợp timeout (`20,000 ms`), retry lỗi tạm thời (`500`, `503`, `504` tối đa 2 lần) và cơ chế `GeminiSession` giữ nguyên model đã phản hồi thành công trong suốt các vòng gọi công cụ của một lượt hỏi đáp.

### 4.2. Cơ sở dữ liệu SQLite (`db.py` -> `runs.db`)
- **Lưu lịch sử chạy (`log_run`, `get_runs`, `get_run`)**: Lưu tên công cụ, tham số JSON, kết quả JSON, thời gian và `scope_id` theo bộ dữ liệu.
- **Lưu quyết định (`save_decision`, `list_decisions`, `latest_decisions`)**: Lưu lịch sử duyệt/từ chối các cơ hội tiết kiệm năng lượng để đưa vào báo cáo.

### 4.3. Dịch vụ thời tiết (`weather_api.py`, `weather_panel.py`)
- Kết nối trực tiếp API công khai `https://api.open-meteo.com/v1/forecast` (giới hạn kích thước phản hồi 128 KB, xử lý lỗi mạng/HTTP rõ ràng).
- Cho phép xem nhiệt độ, độ ẩm hiện tại và chuỗi dự báo theo giờ tại địa điểm của cơ sở.

### 4.4. Tiện ích kiểm chứng, đo hiệu năng & kiểm thử (`scripts/`, `tests/`)
- **`scripts/verify_numbers.py`**: Công cụ CLI và hàm `verify_results()` dùng để đối chiếu tự động các con số xuất hiện trong câu trả lời AI với kết quả lưu trong `runs.db` theo `[run_id: ...]`. Trả về mã thoát khác 0 nếu thiếu nguồn hoặc phát hiện số liệu không khớp.
- **`scripts/benchmark_app.py`**: Chạy giả lập giao diện bằng Streamlit `AppTest` với dữ liệu mẫu để đo thời gian phản hồi và số lần gọi hàm tính toán.
- **Bộ kiểm thử `tests/`**: Gồm 15 tệp test (`test_acceptance.py`, `test_agent_ask.py`, `test_app_data_flow.py`, `test_app_performance_cache.py`, `test_baseline_integration.py`, `test_db_agent.py`, `test_energy_baseline.py`, `test_mapping.py`, `test_report.py`, `test_sample_data.py`, `test_tools_analysis.py`, `test_tools_clean_seu.py`, `test_verify_numbers.py`, `test_weather_api.py`, `test_weather_panel.py`).
- **`run.ps1`**: Script PowerShell tự động kiểm tra Python 3.12+, khởi tạo `.venv`, đồng bộ `requirements.txt` theo mã băm SHA256 và khởi chạy Streamlit tại `http://127.0.0.1:8501`.
