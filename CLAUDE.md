MỤC TIÊU: demo MVP kiểm toán năng lượng (phân tích dữ liệu lịch sử): upload, AI mapping, dashboard, SEU, EnPI, cơ hội + ROI, hỏi đáp, xuất báo cáo. Không làm SCADA, RAG, dự báo ML, điều khiển HVAC.
NGUYÊN TẮC BẤT BIẾN:
(1) LLM không bao giờ tự tính toán; số liệu năng lượng đến từ hàm tất định trong tools.py và energy_baseline.py (NumPy/pandas).
(2) Dữ liệu nằm phía server; LLM chỉ truyền tham số nhỏ, mỗi lời gọi công cụ phải qua kiểm JSON Schema trước khi chạy.
(3) Mỗi lần chạy công cụ ghi vào SQLite runs.db (thời gian, tên, tham số, kết quả) và trả về run_id.
(4) Khuyến nghị chỉ là đề xuất, cần người dùng Approve/Reject.
(5) Tỷ lệ tiết kiệm, giá điện, vốn đầu tư là GIẢ ĐỊNH, luôn gắn nhãn đó khi hiển thị.
(6) ROI (%) và thời gian hoàn vốn (tháng) là hai chỉ số riêng.
(7) Không hardcode API key; đọc từ GEMINI_API_KEY.
TÊN CỘT CHUẨN sau mapping được khai báo trong mapping.py. Các cột điện năng bắt đầu bằng "kwh_". Cột kwh_total dành cho công tơ tổng. Cột cdd được tính từ temp_c và cdd_base_temp_c.
QUY ƯỚC: type hints; docstring tiếng Việt ngắn; hàm trong tools.py là hàm thuần, không I/O; mỗi hàm có test pytest trong tests/; cấu hình Gemini tập trung tại gemini_service.py, model mặc định "gemini-3.8-flash".
MỖI PHIÊN: làm đúng một việc, chạy pytest, tóm tắt các file đã đổi.
