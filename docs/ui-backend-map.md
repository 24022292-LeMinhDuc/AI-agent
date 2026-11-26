# Phân tích chức năng và giao diện

Virtual Energy Engineer là ứng dụng phân tích dữ liệu điện lịch sử chạy trên máy người dùng. Streamlit quản lý giao diện và trạng thái phiên, Plotly vẽ biểu đồ, pandas và NumPy thực hiện phép tính, SQLite lưu kết quả và quyết định. Trợ lý Gemini gọi các phép tính qua JSON Schema và dẫn nguồn theo `run_id`.

| Trang | Công việc | Kết quả và tầng xử lý |
|---|---|---|
| **Dữ liệu** | Chọn mẫu nhà máy, tòa nhà, mẫu có lỗi hoặc nhập CSV/XLSX, kiểm tra ánh xạ và đơn vị | `mapping.py` nhận các nhóm thời gian, vận hành, biến EnB, điện, nhiệt, chi phí và phát thải. `tools.clean` ghi nhận dòng trùng, dòng xung đột, giá trị bị loại, nội suy và dữ liệu còn thiếu. |
| **Dashboard** | Theo dõi theo ngày, tuần hoặc tháng, so sánh hai tháng và xem thời tiết theo giờ | `dashboard_analysis` cung cấp kWh, nhiệt độ từ tệp, sản lượng, chi phí ước tính và độ phủ. `compare_periods` đối chiếu tổng kWh và bình quân trên ngày có số đo theo từng công tơ. `weather_panel` dùng Open-Meteo cho địa điểm đã chọn. |
| **SEU & EnPI** | Xác định hạng mục sử dụng năng lượng đáng kể, đánh giá hiệu suất và tính đường cơ sở | `seu_pareto` xếp hạng theo điện năng. `enpi_regression` tính kWh/tấn và hồi quy. `energy_baseline` ước tính điện tham chiếu tại điều kiện kỳ báo cáo và cho thấy chênh lệch với thực đo. |
| **Cơ hội** | Xem giả định, so sánh phương án và ghi quyết định | `list_opportunities` ước tính điện và tiền tiết kiệm, ROI và hoàn vốn. SQLite lưu trạng thái đã duyệt hoặc từ chối. |
| **Báo cáo** | Xem phạm vi và tải hồ sơ phân tích | `report.py` tạo DOCX, Markdown và JSON kèm thông tin về tệp nguồn, giả định, quyết định và mã lần chạy. Hồ sơ đưa vào kết quả đường cơ sở nếu phiên đã tính. |
| **Hỏi đáp** | Đặt câu hỏi hoặc mở diễn giải từ một kết quả phân tích | `agent.ask` dùng kết quả đã tính của phiên. Đối chiếu tự động kiểm tra giá trị xuất hiện trong nội dung được trích dẫn. |

## Cách đọc kết quả

- **Chất lượng số đo.** Một thời điểm biểu diễn một khoảng đo. Ứng dụng giữ một bản ghi khi các số đo giống nhau và loại cả nhóm khi số đo tại cùng thời điểm mâu thuẫn. Dashboard dùng dữ liệu đã xử lý. Khi đối chiếu hai tháng, kiểm tra số ngày có số đo và độ phủ từng công tơ trước khi diễn giải chênh lệch tổng kWh.
- **Hiệu suất.** EnPI trực tiếp lấy tổng kWh chia tổng sản lượng trên cùng tập bản ghi hợp lệ. Ngưỡng tích lũy SEU giúp sàng lọc các hạng mục tiêu thụ lớn. EnPI hồi quy dùng sản lượng, độ ngày làm mát (CDD) hoặc cả hai. [ISO 50006:2023](https://www.iso.org/standard/79367.html)
- **Đường cơ sở.** Chọn hai kỳ trong phạm vi ngày của tệp và dùng cùng ranh giới đo. Kỳ cơ sở kết thúc trước kỳ báo cáo. Đầu vào là điện năng tiêu thụ theo ngày. Mô hình bình phương tối thiểu (OLS) khớp kỳ cơ sở từ số đo gốc sau chuẩn hóa đơn vị. Biến điều chỉnh gồm sản lượng, CDD, giờ vận hành, độ ẩm, diện tích sàn thay đổi và mức lấp đầy có đơn vị. Khi có công tơ tổng, dùng điện đo từ cột đó. Khi tổng hợp các phụ tải, mỗi ngày cần đủ số đo của phạm vi đã chọn và đủ biến điều chỉnh.
- **Chênh lệch điện năng.** Mô hình ước tính điện tham chiếu bằng sản lượng hoặc CDD của kỳ báo cáo. Chênh lệch bằng điện tham chiếu trừ điện thực đo trên cùng các ngày đủ điều kiện. Tỷ lệ phần trăm dùng điện tham chiếu làm mẫu số khi mẫu số dương. Ứng dụng loại ngày có biến ngoài khoảng quan sát của kỳ cơ sở hoặc có dự đoán không hợp lệ. Kết quả hiển thị độ phủ và các chỉ số độ phù hợp trong mẫu R², R² hiệu chỉnh, CV(RMSE) và NMBE. [ISO 50015:2014](https://www.iso.org/standard/60043.html) · [DOE FEMP M&V Guidelines 5.0](https://www.energy.gov/sites/default/files/2024-10/mv_guide_5_0.pdf)
- **Đánh giá cơ hội.** Ước tính theo giá điện, tỷ lệ tiết kiệm và chi phí đầu tư trong cấu hình. Quy đổi năm dùng điện bình quân theo ngày có số đo nhân 365. Để xác nhận tiết kiệm của một giải pháp, cần kiểm tra tính đại diện của dữ liệu, thay đổi vận hành, ranh giới đo và độ bất định của kết quả. [DOE FEMP về đo lường và xác minh](https://www.energy.gov/cmei/femp/measurement-and-verification-options-federal-energy-and-water-saving-projects)

CV(RMSE) dùng căn bậc hai của tổng bình phương sai số chia cho bậc tự do phần dư, rồi chuẩn hóa theo điện trung bình kỳ cơ sở. NMBE dùng sai số có dấu theo quy ước điện đo trừ điện khớp mô hình. Cả hai hiển thị theo phần trăm và dùng số ngày hợp lệ trừ số hệ số, kể cả hệ số chặn, để tính bậc tự do.

Cách tổ chức số liệu, hồi quy, biểu đồ và báo cáo tham khảo các chức năng của VERIFI do Bộ Năng lượng Hoa Kỳ cung cấp. [DOE VERIFI](https://www.energy.gov/cmei/ito/ito-software-tools)

## Dữ liệu đầu vào

- Dùng điện năng tiêu thụ theo khoảng đo. Với số đọc công tơ lũy kế hoặc công suất, chuyển đổi sang kWh theo khoảng đo trước khi nhập. Biểu đồ phụ tải và SEU sử dụng các công tơ thành phần có ranh giới độc lập. Điện tổng ưu tiên công tơ tổng khi cột này được ánh xạ.
- Với tòa nhà có diện tích sàn nhất quán, cường độ điện theo kỳ bằng kWh đã ghép với diện tích chia cho diện tích sàn. EUI năng lượng theo định nghĩa ENERGY STAR dùng năng lượng cả năm và diện tích sàn. [ENERGY STAR về EUI](https://www.energystar.gov/buildings/benchmark/understand-metrics/what-eui)
- SEC, EnB tham chiếu và hệ số phát thải có thể được khai báo như các cột của tệp. Tổng phát thải trong báo cáo lấy từ dữ liệu khai báo. Phạm vi Scope 2 cần xác định điện hoặc năng lượng mua ngoài. [GHG Protocol Scope 2](https://ghgprotocol.org/scope-2-guidance)
- Ngày giờ có độ lệch UTC được quy về giờ Việt Nam. Ngày có dấu gạch chéo được đọc theo thứ tự ngày/tháng/năm. Dashboard có thể xem theo ngày, tuần bắt đầu thứ Hai hoặc tháng.
- Số liệu và biểu đồ được xử lý trên máy. Nếu bật Gemini, ánh xạ AI gửi tên cột. Hỏi đáp gửi câu hỏi, lịch sử gần đây và kết quả tổng hợp liên quan.
- Mục **Thời tiết theo giờ** gửi tọa độ đã chọn đến Open-Meteo. Chuỗi theo giờ là dữ liệu mô hình và dự báo dùng để theo dõi vận hành. Nhiệt độ ánh xạ từ tệp là đầu vào lịch sử của CDD, EnPI và đường cơ sở. [Open-Meteo Weather Forecast API](https://open-meteo.com/en/docs)

## Đề xuất phát triển tiếp

1. **Bản đồ độ phủ theo công tơ.** Hiển thị ngày hoặc khoảng đo thiếu, trùng và mâu thuẫn trên lịch. Cho phép xuất các dòng cần sửa để giảm thời gian xử lý tệp nguồn.
2. **Hồ sơ đo lường và xác minh.** Lưu phiên bản đường cơ sở, phạm vi đo, mốc triển khai và thay đổi vận hành. Kiểm tra mô hình trên một tập dữ liệu riêng và thêm khoảng bất định trước khi dùng chênh lệch để xác nhận tiết kiệm. [DOE FEMP M&V Guidelines 5.0](https://www.energy.gov/sites/default/files/2024-10/mv_guide_5_0.pdf)
3. **So sánh kịch bản đầu tư.** Thay đổi giá điện, vốn và tỷ lệ tiết kiệm để xem tác động lên ROI và hoàn vốn. Lưu giả định cùng quyết định để người khác có thể tái lập kết quả.
