# Bản vá tương thích Home Assistant 2026.11.0 (mcp 2.x)

Dựa trên `ha-mcp-for-xiaozhi` (main, 17/10/2026). **Chưa được thử trên HA 2026.11 thật.**

## Thay đổi
- `server.py`: bỏ decorator mcp 1.x (`@server.list_tools()`...). Dùng `Server(name, on_list_tools=..., on_call_tool=..., on_list_prompts=..., on_get_prompt=..., on_list_resources=..., on_read_resource=...)`.
  Handler nhận `(ctx, params)` và trả về đủ kiểu kết quả (`ListToolsResult`, `CallToolResult`...). Trường kiểu snake_case (`input_schema`, `is_error`, `mime_type`, `read_only_hint`...).
  Lỗi khi gọi tool được trả về dạng `CallToolResult(is_error=True)` (mcp 2.x không tự bọc nữa).
- `websocket_transport.py`:
  - Parse JSON-RPC bằng `TypeAdapter(JSONRPCMessage)`.
  - Không dùng `entry.runtime_data`; lấy session manager từ `hass.data`.
  - `LLMContext` nhận `Context` thật có user admin (và `requires_admin=True` nếu trường này tồn tại).
  - Task kết nối chạy bằng `entry.async_create_background_task` (tự huỷ khi gỡ/reload entry).
  - Backoff kết nối lại 20s -> 120s thay vì cố định 20s.
  - Che `token=` trong log; log từng message hạ xuống mức DEBUG.
- `session.py`, `types.py`, `__init__.py`: bỏ phụ thuộc `SessionMessage` ở mức import, bỏ `runtime_data`.
- `config_flow.py`: chịu được trường hợp `llm.async_get_apis` trở thành async.
- `manifest.json`: version 0.2.3, `mcp>=2.0.0,<3`, bỏ `voluptuous-openapi` (server dùng `probatio` của core). `hacs.json`: HA >= 2026.11.0.
- `tests/test_server_compat.py`: viết lại cho kiểu đăng ký `on_*`.

## Lưu ý bảo mật
Bản này chạy các lệnh Assist dưới danh nghĩa một người dùng **admin** của HA. Chỉ nên cấp cho Xiaozhi các thực thể đã "công khai" (Settings > Voice assistants > Expose), tránh khoá cửa, camera, báo động.
