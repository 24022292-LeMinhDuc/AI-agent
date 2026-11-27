# Kiểm chứng khoảng trống của Virtual Energy Engineer

Kiểm tra ngày 26/11/2026 trên `main` tại `9eda2ef`. Hướng dẫn áp dụng nằm ở `docs/AGENTS.md:4–23`. Đối chiếu mã ứng dụng, kiểm thử, cấu hình và thư viện cài trong môi trường dự án. SQLite đang dùng được mở ở chế độ chỉ đọc. Các phép kiểm tra bổ sung dùng dữ liệu giả lập hoặc chỉ đọc metadata.

**Cách đọc bảng.** Đã có nghĩa là khả năng được hỏi đã hiện diện. Một phần nghĩa là có cơ chế nhưng thiếu phần kiểm soát hoặc hồ sơ cần thiết. Chưa có nghĩa là chưa thấy cơ chế tương ứng trong phạm vi repo đã kiểm tra. Rủi ro đánh giá hậu quả khi dùng tính năng cho kiểm toán thực tế. Công sức ước lượng tương đối cho việc thực hiện và kiểm thử. S là thay đổi cục bộ, M là thay đổi vài mô-đun, L là thay đổi kiến trúc hoặc phương pháp.

## Kết quả kiểm thử gốc

Môi trường Windows, Python 3.13.15, pytest 9.2.0. Chạy qua `pytest.main` bằng `.venv\Scripts\python.exe -B -X utf8`. Tắt cache pytest và đặt `GEMINI_API_KEY` thành chuỗi rỗng trong tiến trình đo để dùng các ca mô phỏng. pytest-cov 7.0.0, coverage 7.10.7 và Ruff 0.14.0 được cài riêng vào thư mục tạm, cùng nơi ghi dữ liệu coverage.

| Lệnh hoặc đối số thực thi | Kết quả |
|---|---|
| `pytest -q -p no:cacheprovider --tb=short` | `126 passed in 23.72s`, mã thoát 0 |
| `pytest -q -p no:cacheprovider --tb=short --cov=. --cov-report=term` | `126 passed, 4 warnings in 50.28s`, mã thoát 0. Báo cáo mặc định `4267` câu lệnh, `284` chưa chạy, `93%`. |
| Đọc lại cùng tệp đo coverage với danh sách `git ls-files "*.py"` | Toàn bộ module `4331` câu lệnh, `348` chưa chạy, `92%`. Mã ứng dụng và script, bỏ `tests/`, `2750` câu lệnh, `346` chưa chạy, `87%`. |
| `ruff check --no-cache .` | Mã thoát 1. Ba lỗi `E402` tại `scripts/benchmark_app.py:20–22` do import sau khi đổi `sys.path` ở dòng 18. |
| Type-check | Repo và môi trường dự án chưa cấu hình hoặc cài mypy/Pyright. Không có lệnh kiểm tra kiểu của dự án để chạy. |

Coverage tính theo câu lệnh. Báo cáo mặc định bỏ qua `scripts/benchmark_app.py`. Khi liệt kê tệp Python được Git theo dõi, script này hiện `64/64` câu lệnh chưa chạy. Dưới đây là bảng nguyên số đo đọc từ cùng lần chạy `--cov`.

```text
Name                                  Stmts   Miss  Cover
---------------------------------------------------------
agent.py                                194     22    89%
app.py                                  851    121    86%
data/make_sample.py                     103     11    89%
db.py                                    95      6    94%
energy_baseline.py                      154     12    92%
gemini_service.py                        55      7    87%
mapping.py                              221     10    95%
report.py                               268     18    93%
schemas.py                                7      0   100%
scripts/benchmark_app.py                 64     64     0%
scripts/verify_numbers.py               156     41    74%
tests/test_acceptance.py                 70      0   100%
tests/test_agent_ask.py                 182      1    99%
tests/test_app_data_flow.py             290      0   100%
tests/test_app_performance_cache.py      48      0   100%
tests/test_baseline_integration.py       50      0   100%
tests/test_db_agent.py                   90      0   100%
tests/test_energy_baseline.py            97      0   100%
tests/test_mapping.py                   130      0   100%
tests/test_report.py                    107      0   100%
tests/test_sample_data.py                69      0   100%
tests/test_tools_analysis.py            222      0   100%
tests/test_tools_clean_seu.py            38      0   100%
tests/test_verify_numbers.py             17      0   100%
tests/test_weather_api.py                66      1    98%
tests/test_weather_panel.py             105      0   100%
tools.py                                400     17    96%
weather_api.py                           77      7    91%
weather_panel.py                        105     10    90%
---------------------------------------------------------
TOTAL                                  4331    348    92%
```

Bốn cảnh báo coverage đều là `ResourceWarning: unclosed database`. Các vị trí pandas được in trong cảnh báo là nơi cảnh báo phát ra. Có test dùng `with sqlite3.connect(...)` tại `tests/test_agent_ask.py:170–172` và `tests/test_db_agent.py:192–194`. Kiểm tra riêng bằng DB trong bộ nhớ xác nhận ra khỏi ngữ cảnh giao dịch vẫn còn mở kết nối. Mã ứng dụng bọc kết nối bằng `closing` tại `db.py:106,128,147,156,172`, nên cảnh báo này chưa phải bằng chứng rò kết nối ở luồng ứng dụng.

## Bảng kiểm chứng

| ID | Mục | Trạng thái | Bằng chứng từ mã và kiểm tra thực tế | Rủi ro | Công sức | Prompt đề xuất |
|---|---|---|---|---|---|---|
| S01 | Giới hạn kích thước và loại tệp tải lên | Một phần | `app.py:1379–1389` chỉ cho chọn `csv/xlsx`. `app.py:152–172` xét hậu tố khi phân tích. Widget dùng mức mặc định của Streamlit, đo được `server.maxUploadSize=200` MB trong môi trường hiện tại. `.streamlit/config.toml:1–16` chưa cấu hình giới hạn riêng. `app.py:1319–1344` đọc bytes, băm và ghi độ dài mà chưa kiểm tra dung lượng tại tầng nhập hay chữ ký định dạng. | TB | M | [P01](#p01) |
| S02 | Kiểm soát XLSX nén | Một phần | `app.py:1343–1347` đưa bytes trực tiếp cho `pd.ExcelFile` để lấy sheet và `app.py:170–172` đọc sheet bằng `pd.read_excel`. Pandas dùng `read_only=True` trong `.venv/Lib/site-packages/pandas/io/excel/_openpyxl.py:689–700`. Repo chưa có bước giới hạn số mục, tổng dung lượng giải nén, tỷ lệ nén hoặc bảng quá lớn trước khi parser chạy. | Cao | M | [P01](#p01) |
| S03 | Xác thực và định danh người Approve/Reject | Chưa có | `app.py:1186–1202` ghi quyết định từ nút. `app.py:245–255,1697–1705` tạo phạm vi từ mã phiên UUID và thông tin tệp, chưa phải danh tính người dùng. `db.py:71–76,145–151` chỉ lưu tên cơ hội, trạng thái, giờ và `scope_id`. `run.ps1:57–60` bind loopback khi dùng launcher, giảm phạm vi truy cập mặc định. | Cao | L | [P02](#p02) |
| S04 | Quyền hệ điều hành của `runs.db` | Chưa có | `db.py:18,89–99` đặt DB cạnh mã và mở theo quyền kế thừa. `.gitignore:6` bỏ DB khỏi Git. `Get-Acl` trả `AreAccessRulesProtected=False`, có quyền kế thừa `Authenticated Users: Modify, Synchronize` và `Users: ReadAndExecute, Synchronize`. Quyền hiệu lực còn phụ thuộc tài khoản và cách triển khai. | Cao | M | [P03](#p03) |
| S05 | Khóa API xuất hiện trong log | Một phần | `gemini_service.py:17–43` nạp khóa qua môi trường, `gemini_service.py:94–103` trả lỗi đã giản lược cho UI. `db.py:102–109` ghi tham số và kết quả đã chuyển sang JSON, chưa có lớp lọc bí mật riêng. `agent.py:142–145` đưa chuỗi lỗi công cụ vào kết quả ghi. So sánh khóa hiện tại với `args_json` và `result_json` của **76 run** cho **0 khớp**. Phạm vi phép đo chỉ là khóa hiện tại trong DB hiện tại. | Cao | M | [P03](#p03) |
| L01 | Nội dung `suggest_mapping` gửi tới Gemini | Đã có | `mapping.py:170–206` gửi danh sách tên cột, danh mục đích chuẩn và schema. Các hàng và giá trị ô nằm ngoài payload này. `app.py:1353–1361` dùng bộ ánh xạ từ khóa khi nạp bản nháp. Nút Gemini ở `app.py:1445–1454` kích hoạt yêu cầu riêng. `tests/test_mapping.py:62–89` kiểm chứng phần tên cột bằng mô phỏng. Header có dữ liệu nhạy cảm sẽ đi theo yêu cầu nếu người dùng bấm nút. | TB | S | [P04](#p04) |
| L02 | Ép đầu ra Gemini vào cột chuẩn | Đã có | `mapping.py:22–55` có 31 header trong bảng ánh xạ và 2 trường occupancy bổ sung, tổng **33 cột chuẩn**. `mapping.py:189–217` gửi enum `CANONICAL_COLUMNS` cùng `unmapped`, bắt buộc từng cột nguồn, cấm trường ngoài schema và kiểm lại JSON nhận về. `mapping.py:331–343` từ chối đích sai và nhiều nguồn trùng đích khi áp dụng. | Thấp | S | [P04](#p04) |
| L03 | Công cụ có tác dụng phụ | Một phần | `agent.py:77–82,145–149` chạy `data_quality`, ghi log thành công rồi đặt `measured_df` một lần và cập nhật `clean_df`. Các công cụ hợp lệ khác cũng ghi `runs.db` qua `agent.py:145`. `schemas.py` đưa `data_quality` vào danh mục Gemini và `agent.py:224–239,303–316` cho phép điều phối trong phiên đã xác nhận dữ liệu. Chưa có giao diện công cụ LLM chỉ đọc tách khỏi luồng làm sạch. | TB | M | [P05](#p05) |
| L04 | Cắt 12 tin nhắn và truy vết `run_id` | Một phần | `agent.py:24,182–216` lấy 12 tin cuối và dựng payload từ text. Bằng chứng có cấu trúc được nạp từ `selected_context` có giới hạn riêng hoặc từ tool của lượt hiện tại. `app.py:1294–1309` chỉ gửi `role/content` từ lịch sử và xóa `selected_context` khi dùng xong. Run vẫn lưu theo `db.py:102–111`, nhưng lượt sau thiếu liên kết có cấu trúc để tái nạp run cũ khi text bị cắt. | TB | M | [P05](#p05) |
| L05 | Đối chiếu số trong luồng hỏi đáp | Đã có | `agent.py:265–279` gọi `verify_results(answer, evidence)` trước khi trả lời. `scripts/verify_numbers.py:156–186` đối chiếu cục bộ với các run đã trích dẫn. `app.py:1229–1237,1313–1315` gắn cảnh báo và hiện nguồn. Công cụ CLI là luồng `verify_answer` riêng ở `scripts/verify_numbers.py:124–154,188–213`. Xem A01 về độ chính xác của đối chiếu. | Cao | M | [P06](#p06) |
| L06 | Ghi model fallback thực tế vào run | Chưa có | `gemini_service.py:53–80` chọn và giữ `active_model` trong lượt gọi. `db.py:59–66,102–110` chỉ lưu thời điểm, tên tool, tham số, kết quả và scope. `agent.py:145,275–279` chưa lưu model thực tế hoặc lý do chuyển model theo lượt hỏi đáp. | TB | M | [P07](#p07) |
| M01 | Ngưỡng chấp nhận R², CV(RMSE), NMBE | Một phần | `energy_baseline.py:117–145` tính các chỉ số và `report.py:329–337` đưa vào báo cáo. `energy_baseline.py:212–254` lọc dữ liệu, dự đoán và tạo cảnh báo theo độ phủ, ngoại suy đơn biến, dự đoán âm hay R² chưa xác định. Chưa có quy tắc đạt hoặc không đạt theo R², CV(RMSE), NMBE. | Cao | M | [P08](#p08) |
| M02 | p-value, VIF, Durbin–Watson | Chưa có | `energy_baseline.py:104–147` dùng OLS, hạng và `condition_number`. `tools.py:570–600` dùng OLS, kiểm tra hạng và trả R². Hai hàm chưa tính p-value, VIF hoặc Durbin–Watson. Chỉ số điều kiện hỗ trợ nhận diện độ nhạy nhưng khác VIF theo biến. | Cao | M | [P08](#p08) |
| M03 | Khoảng tin cậy của mức tiết kiệm | Chưa có | `energy_baseline.py:150–169` chỉ trả điểm ước lượng thực đo, tham chiếu, tiết kiệm kWh và %. `energy_baseline.py:235–266` tổng hợp các ngày đủ điều kiện theo tháng và toàn kỳ. Chưa có phương sai của tổng tiết kiệm hay khoảng tin cậy. | Cao | L | [P09](#p09) |
| M04 | Số quan sát tối thiểu | Một phần | `energy_baseline.py:92–102` và `tools.py:570–576` cần `n >= p + 1` với `p` là số hệ số kể cả hệ số chặn. Điều này cho phép 3 quan sát với một biến hoặc 4 với hai biến, nếu đủ hạng. Chưa có cỡ mẫu hoặc độ phủ tối thiểu phục vụ đánh giá mô hình ngoài điều kiện đại số. | Cao | M | [P08](#p08) |
| M05 | Động cơ hồi quy dùng chung cho EnPI và EnB | Chưa có | `tools.py:552–600` khớp bằng `_fit_enpi`. `energy_baseline.py:92–147,212` có `_fit_baseline` riêng, cũng chuẩn hóa biến và gọi `np.linalg.lstsq`. Các hàm chia sẻ trợ giúp dữ liệu ở `energy_baseline.py:13`, song chưa chia sẻ hàm khớp OLS. | TB | M | [P08](#p08) |
| M06 | Ngoại suy bình quân ngày nhân 365 | Một phần | `tools.py:738–765,777–780` đếm ngày có số đo, chia điện cho số ngày đó, nhân 365 và ghi giả định ngày đầy đủ, đại diện. Kiểm tra bằng một ngày giả lập 100 kWh tạo **36.500 kWh/năm**. Chưa có quy tắc chặn mẫu quá ngắn, khoảng đo trong ngày bị thiếu hoặc kỳ lệch mùa. | Cao | M | [P10](#p10) |
| M07 | Tương tác giữa các cơ hội tiết kiệm | Chưa có | `tools.py:744–781` tính từng cơ hội độc lập bằng `applies_to` và `saving_pct`. `config.yaml:13–25` khai báo từng phương án. `report.py:109–128` nhóm theo quyết định, chưa có đồ thị phụ thuộc, loại trừ hoặc kiểm soát cộng trùng giữa phương án. | Cao | L | [P10](#p10) |
| M08 | Cân bằng `kwh_total` với tổng công tơ | Một phần | `tools.py:111–137,151–165,254–274` ưu tiên công tơ tổng hợp lệ trong toàn tệp và tính chênh lệch trên dòng đủ toàn bộ số đo. `app.py:576–595` hiển thị số dòng và chênh lệch. Chưa có ngưỡng tuyệt đối/tương đối, cảnh báo lệch chiều, hoặc đếm số kỳ có chênh lệch vượt mức. | Cao | M | [P11](#p11) |
| M09 | Cảnh báo `kwh_other` quá lớn | Chưa có | `mapping.py:22–55` ánh xạ phụ tải khác. `tools.py:330–350` chỉ xếp hạng Pareto với quy tắc chung. `app.py:44–57,745–768` có nhãn và hiển thị nhóm này theo Pareto, chưa có cảnh báo riêng theo tỷ trọng, độ phủ hoặc biến động. | TB | S | [P11](#p11) |
| M10 | Điều chỉnh không thường quy | Chưa có | `schemas.py:56–75` nhận ngày, phạm vi công tơ và biến, chưa nhận hồ sơ sự kiện. `energy_baseline.py:212–242,268–273` điều chỉnh theo biến và nêu giả định ranh giới đo, chưa nhập thay đổi tĩnh, định lượng điều chỉnh riêng hay lưu phê duyệt thay đổi. | Cao | L | [P09](#p09) |
| D01 | Bản gốc bất biến | Một phần | `app.py:1325–1361,1535–1559` giữ `raw_df`, bản đã ánh xạ và bản sạch trong phiên. XLSX có bytes `workbook_content` để đổi sheet. `mapping.py:343–344` và `tools.py:278–290` tạo bản sao khi biến đổi. Chưa có lưu trữ byte gốc bất biến hoặc kiểm thử toàn vẹn qua lần chạy khác. | Cao | L | [P12](#p12) |
| D02 | Cờ chất lượng cho từng giá trị bị sửa | Một phần | `tools.py:294–326` có mask loại và nội suy, trả số lượng theo cột. `mapping.py:343–396` đếm `mapping_issues`. `app.py:1573–1601` hiển thị thống kê. Chưa lưu mask hoặc nguồn lý do tại từng ô trong DB hoặc hồ sơ kiểm toán. | Cao | M | [P12](#p12) |
| D03 | Băm tệp nguồn | Đã có | `app.py:1325–1336` băm SHA-256 trên bytes tệp. `app.py:245–255` đưa fingerprint vào phạm vi quyết định. `report.py:148–167` giữ tên tệp, sheet và metadata, chưa ghi trực tiếp digest nguồn vào hồ sơ xuất. | TB | S | [P12](#p12) |
| D04 | Endpoint Open-Meteo | Đã có | `weather_api.py:16,112–131` gọi `https://api.open-meteo.com/v1/forecast` với `past_hours=24` và `forecast_hours=24`. Đây là Forecast API. Các giờ gần đây đến từ cùng endpoint. Các phép CDD dùng nhiệt độ đã ánh xạ tại `mapping.py:389–395`. | Thấp | S | [P12](#p12) |
| O01 | Retry khi gặp HTTP 429 | Một phần | `gemini_service.py:14,36–43,64–80,101–102` retry 500, 503, 504 và hiển thị thông báo riêng cho 429. Gemini thiếu retry 429. `weather_api.py:136–141` nhận biết 429 và `weather_panel.py:18,38–48,103–106` đợi 5 phút trước lần thử tiếp theo. Phần thời tiết chưa dùng `Retry-After`. | TB | M | [P07](#p07) |
| O02 | SQLite WAL | Chưa có | `db.py:45–99` dùng giao dịch migration và timeout 10 giây. Repo chưa thiết lập `journal_mode=WAL`. `PRAGMA journal_mode` đọc từ DB hiện tại trả `delete`. | TB | M | [P13](#p13) |
| O03 | Sao lưu và phục hồi SQLite | Chưa có | `db.py:89–182` có mở DB, ghi và đọc. `run.ps1:35–63` chỉ quản lý môi trường và khởi chạy. Kiểm kê `git ls-files -co --exclude-standard` chưa có script, lịch hoặc bài kiểm thử phục hồi. `PRAGMA quick_check` hiện trả `ok`. Sao lưu ngoài repo không thuộc phép kiểm tra này. | Cao | M | [P13](#p13) |
| O04 | CI | Chưa có | Kiểm kê `git ls-files -co --exclude-standard` chưa thấy workflow CI hoặc cấu hình pipeline. `requirements.txt:1–11` có pytest, và `README.md` hướng dẫn chạy test thủ công. | TB | M | [P14](#p14) |
| O05 | Lint chính thức và kết quả Ruff | Một phần | `docs/AGENTS.md:18–19` yêu cầu Ruff. `requirements.txt:1–11` chưa khai báo Ruff. Kiểm kê repo chưa có cấu hình hoặc bước CI chạy lint. Phiên audit chạy Ruff độc lập và có 3 lỗi E402 tại `scripts/benchmark_app.py:20–22`. | TB | S | [P14](#p14) |
| O06 | Type-check tự động | Chưa có | `requirements.txt:1–11` và kiểm kê repo chưa có mypy/Pyright hay cấu hình tương ứng. Môi trường Python và danh sách lệnh hiện tại cũng chưa có hai công cụ này. Mã có type hints, ví dụ `energy_baseline.py:172–182` và `agent.py:35`. | TB | M | [P14](#p14) |
| O07 | Kích thước `app.py` và phụ thuộc `st.session_state` | Một phần | `app.py` có **1.735 dòng, 75.670 byte và 16 tham chiếu AST** tới `st.session_state`. Các nhóm dùng trạng thái nằm ở `app.py:85–111,230–264,1377,1462,1509,1584,1697–1699`. Tính toán ở `tools.py` và `energy_baseline.py` có **0** tham chiếu. `weather_panel.py:161` có một tham chiếu của UI. `agent.py:35,52–56,145–149` dùng `store` được truyền vào. Tính toán đã tách, phần điều phối giao diện vẫn tập trung trong tệp lớn. | TB | M | [P14](#p14) |
| O08 | Dockerfile | Chưa có | Kiểm kê `git ls-files -co --exclude-standard` chưa thấy Dockerfile, Containerfile hay Compose. `run.ps1:8–60` và `requirements.txt:1–11` là đường khởi động hiện tại. | TB | M | [P14](#p14) |

## Rủi ro nghiêm trọng phát hiện thêm

| ID | Mục | Trạng thái | Bằng chứng và hậu quả | Rủi ro | Công sức | Prompt đề xuất |
|---|---|---|---|---|---|---|
| A01 | Đối chiếu số đúng chỉ số, đơn vị và kỳ | Chưa có | `scripts/verify_numbers.py:75–95,156–178` gom các số từ toàn kết quả run và so sánh giá trị. Kiểm tra cục bộ cho `verified=True` với câu “Chiller used 1200 kWh” khi run có `total_kwh=1200` và `chiller_kwh=800`. `app.py:1229–1237,1313–1315` vẫn trình bày câu trả lời và chỉ gắn cảnh báo nếu bộ kiểm tra đánh rớt. Số liệu sai chỉ số có thể được xem là đã xác minh. | Cao | M | [P06](#p06) |
| A02 | Mở lại quyết định từ phiên khác | Chưa có | `app.py:245–255,1697–1705` đưa UUID phiên mới vào hash phạm vi. `app.py:1145–1147` và `report.py:115` chỉ tra scope đang mở. Cùng tệp, sheet và mapping trong phiên mới có scope khác. Quyết định cũ còn trong SQLite nhưng màn hình và báo cáo có thể hiển thị chờ duyệt sau khi mở lại. | Cao | L | [P02](#p02) |
| A03 | Ngoại suy theo tổ hợp biến | Một phần | `energy_baseline.py:214–219` kiểm tra từng biến theo min/max biên. Kiểm tra cục bộ với 6 ngày khớp mô hình cho thấy một ngày có sản lượng thấp nhất kết hợp giờ vận hành cao nhất được chấp nhận dù tổ hợp nằm ngoài vùng tổ hợp đã đo. Khi các biến thường đi cùng nhau, điện tham chiếu có thể dựa trên tổ hợp chưa được kiểm chứng. | Cao | M | [P08](#p08) |
| A04 | Đánh giá mô hình trên dữ liệu đại diện ngoài mẫu | Chưa có | `energy_baseline.py:104–127` tính sai số trên chính ngày dùng để fit. Với hệ số chặn của OLS, NMBE trong mẫu có thể gần 0 do tổng phần dư gần 0. `report.py:333–337` trình bày các chỉ số, `energy_baseline.py:212–242` dùng mô hình vừa fit để tính kết quả kỳ báo cáo. Chưa có holdout theo thời gian hoặc kiểm tra độ ổn định giữa kỳ để phát hiện mô hình yếu. | Cao | M | [P08](#p08) |

## Prompt thực hiện đề xuất

### P01

Bổ sung giới hạn upload theo loại và dung lượng ở tầng ứng dụng, nhất quán với cấu hình Streamlit. Kiểm tra cấu trúc XLSX trước khi gọi pandas, đặt ngân sách số mục, kích thước và dữ liệu sau giải nén. Giới hạn công việc đọc sheet. Viết test cho CSV/XLSX hợp lệ và trường hợp bị từ chối có thông báo rõ ràng.

### P02

Thiết kế lớp xác thực và phân quyền cho thao tác duyệt. Lưu định danh người thực hiện, thời gian, hồ sơ dữ liệu bền vững và trạng thái vào quyết định. Khôi phục quyết định khi mở lại đúng hồ sơ. Bổ sung migration giữ lịch sử cũ và kiểm thử tách quyền giữa tài khoản.

### P03

Tách thư mục lưu audit khỏi thư mục mã và quy định quyền tối thiểu với hướng dẫn Windows. Thêm kiểm tra quyền lúc khởi chạy hoặc lệnh kiểm tra vận hành. Lọc bí mật khỏi tham số, kết quả lỗi và log theo một quy tắc dùng chung. Kiểm thử bằng khóa giả và xác nhận đường đi của lỗi hợp lệ.

### P04

Chốt hợp đồng payload ánh xạ chỉ gồm nhãn cột và schema. Đặt giới hạn số lượng và độ dài header, kiểm thử cột nhạy cảm, header trùng và JSON sai đích. Dùng `CANONICAL_COLUMNS` làm nguồn duy nhất cho danh mục đích và cập nhật mô tả số cột.

### P05

Tách tác vụ làm sạch có ghi trạng thái khỏi các công cụ chỉ đọc mà Gemini gọi. Gắn `run_id` với từng nguồn trong lịch sử có cấu trúc và phục hồi run đã chọn theo scope sau khi rút gọn hội thoại. Bổ sung kiểm thử hồi quy cho hỏi tiếp về nguồn cũ và việc từ chối run khác scope.

### P06

Ràng buộc từng số được trích dẫn với `run_id`, khóa chỉ số, đơn vị, khoảng thời gian và phép làm tròn được phép. Phân biệt số đã xác minh và số cần kiểm tra trong UI. Thêm test cho trường hợp số của điện tổng bị gán cho chiller, cùng các ca chuyển đơn vị, làm tròn và trích nhầm kỳ.

### P07

Thêm mã lượt gọi Gemini, model đã chọn, số lần thử và lý do fallback với metadata đã lọc bí mật. Thực hiện retry 429 có giới hạn, ưu tiên `Retry-After` khi có, thêm jitter và giữ fallback theo đúng lượt. Kiểm thử timeout, 429 và 503 bằng mô phỏng.

### P08

Hợp nhất lớp fit OLS nhưng giữ hợp đồng EnPI/EnB hiện tại. Định nghĩa cỡ mẫu, độ phủ và tiêu chí R², CV(RMSE), NMBE cho từng mục đích và mức tổng hợp. Thêm kiểm tra biến, p-value, VIF, Durbin–Watson, chia kỳ kiểm tra theo thời gian và đánh giá vùng tổ hợp biến. Hiển thị căn cứ của trạng thái đạt và giữ kết quả mô tả khi mô hình chưa đủ điều kiện dùng để ước tính tiết kiệm.

### P09

Bổ sung khoảng bất định của tổng tiết kiệm với phương pháp phù hợp dữ liệu theo thời gian và ghi giả định. Thêm hồ sơ thay đổi vận hành không thường quy gồm kỳ áp dụng, lý do, dữ liệu căn cứ, điều chỉnh và người duyệt. Lưu toàn bộ phép tính và khoảng bất định theo run.

### P10

Thay quy đổi năm từ bình quân đơn giản bằng chính sách kiểm tra độ phủ ngày, đủ khoảng đo và tính đại diện theo mùa. Bổ sung cách mô tả phương án loại trừ, trùng phụ tải hoặc phụ thuộc để kiểm soát khi ghép nhiều cơ hội. Kiểm thử một ngày đo, một phần ngày và các phương án dùng cùng phụ tải.

### P11

Nâng đối chiếu công tơ lên mức theo từng kỳ với độ phủ, sai lệch kWh và tỷ lệ, ngưỡng cảnh báo cấu hình, ranh giới đo rõ ràng. Cảnh báo tỷ trọng hoặc biến động phụ tải khác và chỉ rõ dữ liệu thiếu. Kiểm thử công tơ tổng trống toàn tệp, thiếu một kỳ và tải thành phần có chênh lệch bù trừ.

### P12

Lưu tệp nguồn bất biến theo SHA-256 với mapping và phiên bản xử lý. Gắn cờ theo thời điểm và cột cho dữ liệu bị loại, nội suy và còn thiếu. Đưa digest, cờ và nguồn nhiệt độ vào hồ sơ audit. Giữ dự báo từ Forecast API có nhãn nguồn rõ ràng nếu sau này nhập dữ liệu lịch sử từ dịch vụ archive.

### P13

Thiết kế cấu hình WAL và checkpoint theo cách triển khai thực tế, có kiểm thử ghi đồng thời và khôi phục sau lỗi. Bổ sung quy trình sao lưu SQLite nhất quán, giữ phiên bản, quyền lưu bản sao và kiểm thử khôi phục định kỳ trên dữ liệu giả lập.

### P14

Thêm pipeline CI chạy pytest, coverage, Ruff và type-check với công cụ dev ghim phiên bản. Sửa các lỗi lint đã tái hiện. Tách dần các trang khỏi `app.py` qua API trạng thái rõ ràng rồi bổ sung Dockerfile có kiểm tra khởi động và thể tích lưu audit. Kiểm thử các luồng nhập liệu, tính toán và xuất báo cáo sau khi đóng gói.

## Phạm vi thay đổi

Lúc bắt đầu, `git status` đã có thay đổi ở `README.md` và `docs/bao-cao-he-thong.md`, ba tệp `.gitkeep` đã xóa và `docs/AGENTS.md` chưa được theo dõi. Phần phát sinh của lần audit là `docs/audit/gap-analysis.md`. Kiểm chứng dựa trên đối chiếu danh sách đường dẫn và SHA-256 trước/sau của các thay đổi có từ trước. `git status` hiển thị tệp audit mới vì tệp chưa được đưa vào index.
