# Japanese N2 study tools

Local tools for converting JLPT N2 exam PDFs to Markdown, analyzing recurring
language patterns, creating study materials, and preparing PDFs for a Sony DPT-RP1.
The preferred exam layout is portrait, with one original page per reading page.

## 墨记：DPT 笔记与学习工作台

浏览器中管理 DPT 文件、同步批注、识别手写问题、保存解答与复习记录。
支持自填地址、密钥与视觉模型名称的 OpenAI 兼容接口。

```sh
# 在普通终端部署到服务器（要求远端 Docker / Compose）
bash scripts/deploy.sh
# 部署后在浏览器直接访问脚本输出的地址，默认 部署脚本输出的网页地址
```

详见 [使用、部署、模型边界与数据备份](docs/study-app.md)。

## Setup

Use macOS with Python 3.12. PDF builders currently use the system fonts
`/System/Library/Fonts/STHeiti Light.ttc` and `STHeiti Medium.ttc`.
Install Poppler (`pdfinfo`, `pdftoppm`) and Tesseract with Japanese `jpn` language
data, then create a Python environment:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run commands below from the repository root. Place the original exam PDFs in
`N2真题/`; filenames must include the examination year and month, for example
`2024.07N2.pdf`. Files with `Note` in their names are excluded from batch OCR.

## Workflow

Generate resumable OCR results, frequency analysis, vocabulary, and the study guide:

```sh
python scripts/batch_ocr.py
python scripts/analyze_exams.py
python scripts/build_vocabulary_list.py
python scripts/build_n2_guide.py
```

For a Tesseract installation outside `PATH`, pass `--tesseract /path/to/tesseract`
and `--tessdata /path/to/tessdata` to `batch_ocr.py`.
The default batch OCR skips the first two pages; its original 2024-12 sample is
preserved when already present. OCR text is a draft and can contain transcription
errors. The exam PDFs below use the original page images rather than OCR text.

Build and validate the portrait exam PDFs:

```sh
# Generate crop metadata required by the portrait builder, plus the legacy layout.
python scripts/build_dpt_exams.py --workers 3
python scripts/build_dpt_portrait.py
python scripts/validate_dpt_exams.py --portrait
```

The current collection contains 29 exams. Validation checks this inventory and
renders every PDF page for visual review. The 2024 exam PDFs retain source page 2,
which already contains questions.

Results are written to:

- `output/markdown/`: per-exam Markdown, page images, OCR caches, and corrections.
- `output/analysis/`: frequency data, layout manifests, validation and transfer records.
- `output/vocabulary/`: the 150-word vocabulary list.
- `output/pdf/`: study guide and exam PDFs; the portrait set is in `DPT真题_竖屏版/`.
- `tmp/pdfs/`: temporary PDF previews used for visual checks.

## Transfer to DPT

Connect the computer and reader to the same Wi-Fi network. Replace `DEVICE_IP`
with the reader's current IP address:

```sh
python scripts/dpt_transfer.py \
  --addr DEVICE_IP \
  --batch-dir output/pdf/DPT真题_竖屏版 \
  --remote-folder Document/N2真题_竖屏版 \
  --report-path output/analysis/dpt-exams-portrait/transfer.json \
  --open-page 2
```

Initial pairing asks for the PIN displayed on the reader. Pairing credentials are
stored in `.tools/dpt-auth/`. Uploaded PDFs are downloaded again to verify their
SHA-256 hashes. Each authenticated transfer first calibrates the reader's clock
to the computer and verifies it, preserving the reader's configured time zone.
An existing document at the destination is preserved, and the new
copy receives a timestamp suffix. The optional `--courier` argument supports the
file-based HTTP relay used by Codex in its restricted execution environment; a
normal terminal session does not need it.

To inspect the device clock and check that the portrait exam folder remains
available, run `python scripts/dpt_clock.py --addr DEVICE_IP`. Add `--set` to
calibrate the clock to the computer's current time without uploading documents.

## Version control

Git tracks the Python scripts, this documentation, and dependency versions.
Original PDFs, ZIP archives, generated outputs, temporary files, local tools,
virtual environments, and pairing credentials stay local and are ignored.
The ignored OCR output also contains any manually corrected exam pages; retain
those local study materials separately when moving to another computer.
