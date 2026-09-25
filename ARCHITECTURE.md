# L3A Architecture Record

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (K4 L3A). Tài liệu này mô tả các quyết định kỹ thuật, phân định trách nhiệm tác nhân, giao thức Agent-to-Agent (A2A), quản lý bằng chứng (evidence) và các bất biến kiểm tra tính đúng đắn.

---

## 1. System overview

Hệ thống xử lý khiếu nại hoạt động theo mô hình điều phối tuần tự kết hợp chuyên môn hóa (Hierarchical Specialist Workflow with Independent Verification). Quy trình xử lý cho mỗi case từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace diễn ra như sau:

```text
                           ┌──────────────────────────┐
                           │   Coordinator / Router   │◄── inputs/<case_id>.json
                           └─────────────┬────────────┘
                                         │ (task_assigned / handoff)
          ┌──────────────────────────────┼──────────────────────────────┐
          ▼                              ▼                              ▼
 ┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
 │ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
 └────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
          │                              │                              │
          │ get_order                    │ get_payment                  │ get_shipment
          │ get_order_items              │                              │
          ▼                              ▼                              ▼
    [MCP Gateway]                  [MCP Gateway]                  [MCP Gateway]
          │                              │                              │
          │ tool_result_consumed         │ tool_result_consumed         │ tool_result_consumed
          └──────────────────────────────┼──────────────────────────────┘
                                         │ (handoff)
                                         ▼
                                ┌──────────────────┐
                                │   Policy Agent   │◄── get_policy [MCP Gateway]
                                └────────┬─────────┘
                                         │ (handoff / policy_decided)
                                         ▼
                                ┌──────────────────┐
                                │  Verifier Agent  │─── Invariant & Consistency Checks
                                └────────┬─────────┘
                                         │ (verification_completed)
                                         ▼
                           ┌──────────────────────────┐
                           │   Coordinator / Finalize │─── outputs/<case_id>.json
                           └──────────────────────────┘─── traces/trace.jsonl
```

### Luồng thực thi chi tiết theo case:
1. **Case Ingestion**: CLI Runner đọc dữ liệu case từ `inputs/<case_id>.json`, khởi tạo `TraceWriter`, ghi nhận sự kiện `case_received` (actor: `coordinator`).
2. **Triage & Delegation**: `coordinator` phân tích khiếu nại ban đầu, trích xuất entity gợi ý (`order_id`, `customer_id`), phát hành sự kiện `task_assigned` cho `order-agent`.
3. **Specialist Investigation**:
   - `order-agent` truy vấn MCP Gateway lấy thông tin đơn hàng và danh sách mặt hàng, emit `tool_result_consumed`, sau đó thực hiện `handoff` sang `payment-agent`.
   - `payment-agent` đối chiếu lịch sử thanh toán, phát hiện giao dịch trùng lặp, chia thanh toán (split payment) hoặc hoàn tiền đang xử lý, emit `tool_result_consumed`, `handoff` sang `shipment-agent`.
   - `shipment-agent` kiểm tra tiến độ giao vận, so sánh ngày cam kết giao hàng với ngày giao thực tế, xác định lỗi trễ hạn thuộc về bên bán hay đơn vị vận chuyển, emit `tool_result_consumed`, `handoff` sang `policy-agent`.
4. **Policy Determination**: `policy-agent` tra cứu quy định sàn qua `get_policy`, phân tích tính hợp lệ của khiếu nại, đề xuất nguyên nhân gốc (`ranked_causes`), phân định trách nhiệm (`responsible_parties`) và hạn mức hoàn tiền (`financial_resolution`), emit `policy_decided` và `handoff` sang `verifier`.
5. **Verification & Audit**: `verifier` kiểm tra toàn bộ các bất biến (Invariants): cấu trúc JSON schema, tính sở hữu của bằng chứng, tính nhất quán giữa lỗi và số tiền hoàn, emit `verification_completed`.
6. **Case Finalization**: `coordinator` đóng gói payload output đạt chuẩn ghi vào `outputs/<case_id>.json` và emit sự kiện `case_finalized`.

---

## 2. Agent ownership

Nhằm đảm bảo nguyên tắc đặc quyền tối thiểu (Principle of Least Privilege), mỗi agent chỉ có quyền hạn tiếp nhận thông tin và gọi các MCP tools thuộc phạm vi nghiệp vụ được phân công:

| Actor | Input | Trách nhiệm | Output/handoff | Quyền hạn MCP Tools |
| :--- | :--- | :--- | :--- | :--- |
| **coordinator** | `inputs/<case_id>.json` | Phân tích sơ bộ khiếu nại, trích xuất thực thể, điều phối vòng đời case, ủy quyền điều tra | Bàn giao cho `order-agent` qua `task_assigned` | Không gọi tool MCP trực tiếp |
| **order-agent** | `case_id`, `order_id`, claim context | Kiểm tra sự tồn tại của đơn hàng, trạng thái đơn hàng (delivered, canceled, unavailable), trích xuất danh sách mặt hàng, giá bán, phí vận chuyển và danh sách `seller_id` | `handoff` sang `payment-agent` kèm danh sách item & seller | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` |
| **payment-agent** | `case_id`, `order_id`, payment context | Kiểm tra phương thức thanh toán, số lần giao dịch, tổng tiền đã thu, phát hiện trùng lặp (`duplicate_charge`), lệch tiền thanh toán (`payment_mismatch`), chia tiền thanh toán hợp lệ (`valid_split_payment`), trạng thái hoàn tiền | `handoff` sang `shipment-agent` kèm dữ liệu tài chính | `get_order_payments` (hoặc `get_payment`), `get_payment_timeline`, `get_refund_timeline` |
| **shipment-agent** | `case_id`, `order_id`, shipment dates | So sánh `order_estimated_delivery_date` với `order_delivered_customer_date` và `shipping_limit_date`. Xác định trễ hạn do seller chuẩn bị chậm hay do đơn vị vận chuyển | `handoff` sang `policy-agent` kèm kết luận về logistics | `get_shipment_summary` (hoặc `get_shipment`) |
| **policy-agent** | Tổng hợp claim + kết quả từ order, payment, shipment | Tra cứu chính sách sàn, xác định tính hợp lệ của khiếu nại (`supported` / `unsupported`), mã hóa vấn đề chính (`primary_issue`), đề xuất khoản hoàn tiền (`financial_resolution`) | `handoff` sang `verifier` kèm dự thảo giải quyết | `get_policy` |
| **verifier** | Dữ liệu điều tra tổng hợp và dự thảo output | Thẩm tra độc lập các bất biến kiểm tra: Schema, Provenance, Consistency, Confidence bounds. Từ chối hoặc phê duyệt kết quả trước khi finalize | Trả về payload hợp lệ cho `coordinator` để xuất file | Không gọi tool MCP trực tiếp |

---

## 3. A2A protocol

Giao thức trao đổi giữa các Agent (Agent-to-Agent Protocol) được thiết kế theo mô hình Directed Acyclic Graph (DAG) nhằm đảm bảo tốc độ và tính có thể kiểm chứng:

1. **Correlation & Message Envelope**:
   - Mọi thông điệp và sự kiện phát sinh đều gắn liền với định danh `case_id` làm correlation ID.
   - Trace event tuân thủ nghiêm ngặt chuẩn `day09-trace-event-v1`:
     - `event_id`: Chuỗi duy nhất dạng `evt_<token>`.
     - `case_id`: Khớp với case đang xử lý.
     - `event_type`: Thuộc danh mục enum hợp lệ (`case_received`, `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`, `case_finalized`).
     - `occurred_at`: Thời gian chuẩn ISO-8601 UTC kết thúc bằng `Z`.
     - `actor` và `target`: Tên định danh của agent phát và nhận.
     - `decision_code`: Mã quyết định có thể quan sát được (ví dụ: `INVESTIGATE_ORDER`, `ORDER_CONFIRMED`, `LOGISTICS_DELAY_DETECTED`, `POLICY_REFUND_APPROVED`, `VERIFIED_CONSISTENT`).
2. **Handoff Conditions**:
   - Specialist kích hoạt `handoff` khi đã thu thập đủ evidence cần thiết cho domain của mình hoặc gặp điều kiện dừng sớm (ví dụ: đơn hàng bị hủy -> chuyển nhanh đến payment kiểm tra hoàn tiền).
3. **Loop Prevention & Timeout**:
   - Luồng chuyển giao một chiều (DAG). Không cho phép gọi vòng lặp ngược lại (No cyclic ping-pong).
   - Timeout cho mỗi thao tác chuyên biệt: tối đa 15 giây.
4. **Observable Trail**:
   - Tuyệt đối không ghi chain-of-thought, prompt bí mật hay thông tin nhạy cảm vào file trace. Chỉ trace các sự kiện và mã quyết định khách quan.

---

## 4. Evidence lifecycle

Quy trình quản lý vòng đời bằng chứng (Evidence Lifecycle) bảo đảm tính xác thực và tính toàn vẹn kiểm toán (Audit Provenance):

1. **Truy vấn Gateway**:
   - Khi specialist gọi `gateway.call(tool_name, case_id=case_id, ...)`, tham số `case_id` luôn được truyền chính xác để máy chủ MCP ghi nhận audit log cho đúng case.
2. **Xác thực cấu trúc MCP Evidence**:
   - Phản hồi từ Gateway được kiểm tra hợp lệ với schema `day09-mcp-evidence-v1`: phải có `evidence_ref`, `result_hash`, `domain`, và `data`.
3. **Lưu trữ & Tiêu thụ bằng chứng (Consumption)**:
   - Ngay sau khi nhận evidence, agent phát sinh sự kiện `tool_result_consumed` với `actor`, `tool_name` và danh sách `evidence_refs`.
   - `evidence_ref` được lưu vào bộ nhớ ngữ cảnh của case.
4. **Ánh xạ Evidence vào Output**:
   - Các `evidence_ref` xác thực được đưa vào trường `evidence_refs` của root output.
   - Gắn `evidence_refs` cụ thể vào từng `claim_assessments[].evidence_refs` trực tiếp minh chứng cho nhận định khiếu nại.
5. **Quy tắc bất biến về Provenance (Hard Gates)**:
   - Không tự sinh hoặc can thiệp chỉnh sửa chuỗi `evidence_ref` (định dạng `ev_...`).
   - Không tái sử dụng `evidence_ref` giữa các case khác nhau (nghiêm cấm cross-case evidence).

---

## 5. Failure policy

Chiến lược xử lý sự cố trong quá trình vận hành hệ thống:

| Sự cố (Failure) | Retry? | Phương án Fallback | Mã sự kiện Trace |
| :--- | :---: | :--- | :--- |
| **MCP Timeout / Connection Drop** | Tối đa 2 lần (Exponential backoff 1s, 2s) | Nếu tiếp tục thất bại: Đánh giá `primary_issue: "insufficient_evidence"`, `case_status: "needs_investigation"`, `confidence: 0.3`, hoàn tiền = 0.0 BRL | `decision_code: "MCP_TIMEOUT_FALLBACK"` |
| **Entity Not Found (Mã đơn/mã hàng không tồn tại)** | Không retry | Xác định khiếu nại dựa trên thông tin sai lệch: đánh giá `primary_issue: "unsupported_claim"`, `case_status: "no_action"`, `confidence: 0.9`, hoàn tiền = 0.0 BRL | `decision_code: "ENTITY_NOT_FOUND"` |
| **Source Conflict (Mâu thuẫn giữa các nguồn dữ liệu)** | Không retry | Ghi nhận chi tiết vào mảng `data_conflicts`, ưu tiên dữ liệu từ hệ thống thanh toán/logistics chính thức thay vì lời khai khách hàng | `decision_code: "DATA_CONFLICT_RESOLVED"` |
| **Invalid Specialist Result / Schema Malformed** | 1 lần điều chỉnh | Verifier chuẩn hóa dữ liệu về giá trị an toàn mặc định, hạ `confidence: 0.4`, ghi nhận `needs_investigation` | `decision_code: "VERIFIER_FALLBACK_APPLIED"` |

Nguyên tắc xử lý: Mọi cơ chế thử lại (retry) phải có giới hạn cụ thể và bảo đảm tính idempotent. Không chuyển trạng thái thiếu dữ liệu thành dữ liệu phỏng đoán (không hallucinate evidence).

---

## 6. Verification invariants

Trước khi xuất file kết quả `outputs/<case_id>.json` và emit `verification_completed`, Verifier Agent bắt buộc phải thẩm định và đảm bảo các điều kiện bất biến sau:

1. **Schema Invariant**: Output phải vượt qua kiểm tra hợp lệ với schema `contracts/schemas/l3a-output-v2.schema.json`.
2. **Entity Scope Invariant**: Toàn bộ ID trong `affected_entities` (`order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`) phải là tập con của các ID xuất hiện trong case hoặc trong dữ liệu do MCP Gateway trả về cho case này.
3. **Evidence Provenance Invariant**:
   - Tất cả các mã trong `evidence_refs` phải tuân theo regex `^ev_[A-Za-z0-9_-]{20,96}$`.
   - Toàn bộ `evidence_refs` trong output phải là các mã đã được thu thập qua các lệnh gọi MCP hợp lệ của chính case đó.
4. **Claim Assessment Linkage**:
   - Mọi `claim_id` có trong input case phải có một bản ghi đánh giá tương ứng trong `claim_assessments`.
   - Mỗi claim assessment phải chứa `verdict` hợp lệ và danh sách `evidence_refs` chứng minh liên quan.
5. **Consistency Invariants (Deterministic Rules)**:
   - **Status vs Refund**: Nếu `primary_issue == "unsupported_claim"` hoặc `case_status == "no_action"` thì `recommended_refund_brl` bắt buộc phải bằng `0` hoặc `0.0`, mảng `refund_lines` phải rỗng.
   - **Refund vs Action**: Nếu `recommended_refund_brl > 0` thì `case_status` bắt buộc phải là `"action_required"`.
   - **Refund Total Matching**: Tổng số tiền của các dòng trong `refund_lines` (`sum(amount_brl)`) phải bằng `recommended_refund_brl`.
   - **Party vs Issue**:
     - Nếu `primary_issue == "late_delivery_seller"` -> `responsible_parties` phải có ít nhất một bên mang `party_type: "seller"`.
     - Nếu `primary_issue == "late_delivery_logistics"` -> `responsible_parties` phải có ít nhất một bên mang `party_type: "logistics_provider"`.
     - Nếu `primary_issue == "duplicate_charge"` hoặc `payment_mismatch` -> `responsible_parties` phải gắn với `payment_provider` hoặc `platform`.
   - **No Duplicate Actions**: Mảng `resolution_actions` không chứa các chuỗi trùng lặp (`uniqueItems: true`).
6. **Calibration Invariant**:
   - Giá trị `confidence` phải nằm trong khoảng `[0.0, 1.0]`.
   - Trường hợp có đầy đủ bằng chứng đối soát rõ ràng: `confidence` trong khoảng `[0.85, 0.95]`.
   - Trường hợp có xung đột dữ liệu hoặc thiếu bằng chứng: `confidence` giảm về `[0.3, 0.6]`. Không đặt tùy tiện `1.0` hoặc `0.5`.

---

## 7. Reproducibility

Môi trường và cấu hình đảm bảo khả năng tái lập 100% kết quả đánh giá:

- **Ngôn ngữ & Runtime**: Python 3.12 (tương thích Python >= 3.11).
- **Quản lý Dependencies**: Pin chặt các thư viện cốt lõi trong `pyproject.toml`:
  - `httpx2>=2,<3`
  - `jsonschema[format]>=4.25,<5`
  - `mcp>=2,<3`
  - `python-dotenv>=1.1,<2`
- **Mô hình & Thuật toán**:
  - Phân tích và quyết định dựa trên mô hình luật phối hợp nghiệp vụ xác định (Deterministic Rule-Based Multi-Agent Policy Engine) kết hợp truy vấn MCP Evidence Gateway.
  - Không sử dụng random seed ngoài kiểm soát; mọi phân loại đều có thể kiểm chứng và tái lập nhất quán.
- **Concurrency & Tài nguyên**:
  - Xử lý tuần tự (sequential processing) qua từng case trong `case-set.json` nhằm tuân thủ rate limit và quota kết nối của MCP Gateway.
  - HTTP connection pool: timeout kết nối 30 giây, read timeout 300 giây.
- **Lệnh thực thi chuẩn**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
- **Bảo mật**: Tuyệt đối không commit API Key, token cá nhân hoặc thông tin mật vào tài liệu, mã nguồn hoặc các file kết quả.
