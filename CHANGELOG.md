# Nhật ký thay đổi

## Chưa phát hành

- Bổ sung cân bằng GJ/TOE, hiệu suất lò hơi và chiller, phân tích chi phí TOU, hệ số phụ tải và cos φ. Dashboard và ước tính cơ hội tiết kiệm dùng cơ cấu khung giờ khi cấu hình nhiều đơn giá. Với 100 kWh cao điểm, cấu hình mới cho 315.100 VNĐ so với 250.000 VNĐ theo đơn giá mẫu cũ. Dịch 10 kWh sang thấp điểm tiết kiệm 20.050 VNĐ theo bộ giá mẫu.
- Hỏi đáp AI và CLI dùng chung bộ đối chiếu số theo trích dẫn. Trợ lý tự sửa tối đa hai lượt trên bằng chứng từ SQLite và hiển thị trạng thái kiểm chứng ngay dưới câu trả lời.
- EnB và M&V hỗ trợ HDD, bốn dạng Change-Point và chọn mô hình tự động. OLS vẫn là mặc định của hàm và công cụ.
- Kết quả có kiểm định hệ số, ngưỡng độ phù hợp, FSU 90% và 95%, bảng NRA và CUSUM theo ngày. Markdown và Word có phần tổng hợp M&V, còn JSON giữ dữ liệu chi tiết theo `run_id`.
- Công thức tiết kiệm cộng NRA có dấu trên các ngày so sánh. Ví dụ, cơ sở chuẩn hóa 318 kWh và thực đo 302 kWh cho 16 kWh tiết kiệm trước NRA. Với NRA +7 kWh, kết quả là 23 kWh, tương đương 7,08% của đường cơ sở sau NRA là 325 kWh.
