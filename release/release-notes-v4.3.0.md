# SHTUCodeProxy v4.3.0

## Highlights

- Added configurable default streaming. When a client omits `stream`, SHTUCodeProxy now uses `default_stream` from the app config. Explicit `stream=true` and `stream=false` requests are always respected.
- Fixed GPT-5.5 / Responses non-streaming compatibility for both `/v1/responses` and `/v1/messages` client routes.
- Preserved structured multimodal content such as images, files, audio, and documents for compatible upstream routes instead of flattening supported parts to text.

## Validation

- Re-tested GLM, DeepSeek, Qwen, and GPT-5.5 against real upstream APIs.
- Covered streaming, non-streaming, tool calls, multi-turn tool results, tool switching, and image input where supported.
- Local `smoke_test.py` and syntax checks passed before packaging.

## Packages

- `SHTUCodeProxy-v4.3.0-windows-x64.exe`
- `SHTUCodeProxy-v4.3.0-windows-x64.zip`
- `SHTUCodeProxy-v4.3.0-source-linux-macos.zip`

Linux native desktop and headless executable packages must be generated on a Linux build host or CI runner.
