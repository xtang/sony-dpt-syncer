#!/usr/bin/env python3
"""Convert all non-Note N2 exams from PDF page 3, keeping resumable OCR."""
import argparse
import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ocr_pdf import format_page, recognize_page


def atomic_text(path, text):
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(text, encoding="utf-8")
    pending.replace(path)


def inventory(source_dir, output_dir):
    documents = []
    excluded = []
    for source in sorted(source_dir.glob("*.pdf")):
        if "note" in source.stem.lower():
            excluded.append(str(source))
            continue
        date = re.search(r"(\d{4})(?:年|\.)(\d{1,2})", source.stem)
        if not date:
            raise ValueError(f"Cannot identify exam date: {source}")
        year, month = map(int, date.groups())
        exam_id = f"{year:04d}-{month:02d}-N2"
        output = output_dir / exam_id
        info = subprocess.check_output(["pdfinfo", str(source)], text=True)
        count = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for folder in ("ocr", "pages", "corrections"):
            (output / folder).mkdir(parents=True, exist_ok=True)
        # The first, manually corrected sample is already finished. Preserve it.
        existing_sample = (
            exam_id == "2024-12-N2"
            and (output / f"{exam_id}.md").exists()
            and (output / "ocr-manifest.json").exists()
            and all((output / "ocr" / f"page-{n:02d}.json").exists()
                    for n in range(1, count + 1))
        )
        documents.append({
            "id": exam_id, "year": year, "month": month, "source": str(source),
            "source_sha256": digest, "pages": count, "first_page": 1 if existing_sample else 3,
            "existing_sample": existing_sample, "output": str(output),
        })
    return documents, excluded


def write_document(document):
    output = Path(document["output"])
    pages = {}
    for number in range(3, document["pages"] + 1):
        path = output / "ocr" / f"page-{number:02d}.json"
        if not path.exists():
            return False
        pages[number] = json.loads(path.read_text())
    source = Path(document["source"])
    markdown = [
        f"# {document['year']} 年 {document['month']:02d} 月 JLPT N2 真题",
        "",
        f"> 考试年月：{document['year']} 年 {document['month']:02d} 月。",
        f"> 来源：{source.name}；原 PDF 共 {document['pages']} 页。",
        "> 按要求跳过 PDF 第 1、2 页，从第 3 页开始转换，保留原 PDF 页码。",
        "> 以下为日文 OCR 初稿，尚未逐字校订；假名、汉字、题号、下划线、注音及表格可能识别有误。每页附完整原图供核对。",
        "> 识别副本中自动淡化彩色批注；原页图片保留原色。覆盖印刷文字的批注仍可能影响识别。",
        "",
    ]
    warnings = []
    for number, data in pages.items():
        correction = output / "corrections" / f"page-{number:02d}.md"
        body = correction.read_text().strip() if correction.exists() else format_page(data)
        status = "已对照原页修订" if correction.exists() else "机器 OCR，待校订"
        if not body.strip():
            body = "> 本页未识别到有效文字，请查看原页图片。"
            warnings.append(number)
        markdown.extend([
            f'<a id="page-{number:02d}"></a>', "", f"## PDF 第 {number} 页", "",
            f"[查看原页](pages/page-{number:02d}.jpg) · {status}", "", body, "", "---", "",
        ])
    atomic_text(output / f"{document['id']}.md", "\n".join(markdown))
    corrected = [
        number for number in pages
        if (output / "corrections" / f"page-{number:02d}.md").exists()
    ]
    manifest = {
        "exam_id": document["id"], "year": document["year"], "month": document["month"],
        "source": document["source"], "source_sha256": document["source_sha256"],
        "pages": document["pages"], "skipped_pages": [1, 2],
        "processed_pages": list(pages), "dpi": next(iter(pages.values()))["dpi"],
        "engine": "Tesseract 5.5.4 / tessdata_best jpn",
        "crop_mode": "full", "remove_color_annotations_for_ocr": True,
        "status": "OCR draft; not a verbatim verified transcription",
        "visually_corrected_pages": corrected, "pages_without_text": warnings,
        "page_line_counts": {str(n): len(p["lines"]) for n, p in pages.items()},
    }
    atomic_text(output / "ocr-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return True


def write_index(documents, excluded, output_dir, failures, elapsed):
    rows = []
    for document in documents:
        output = Path(document["output"])
        complete = (output / f"{document['id']}.md").exists()
        processed = document["pages"] if document["existing_sample"] else document["pages"] - 2
        rows.append({
            **document, "processed_page_count": processed,
            "status": "complete" if complete else "incomplete",
            "markdown": f"{document['id']}/{document['id']}.md",
        })
    manifest = {
        "documents": rows, "excluded_note_files": excluded,
        "new_document_count": sum(not d["existing_sample"] for d in documents),
        "new_processed_page_count": sum(d["pages"] - 2 for d in documents if not d["existing_sample"]),
        "failures": failures, "elapsed_seconds": round(elapsed),
    }
    atomic_text(output_dir / "batch-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    text = [
        "# N2 真题 Markdown 索引", "",
        f"共 {len(rows)} 套：本次新增 {manifest['new_document_count']} 套、{manifest['new_processed_page_count']} 页，另保留此前完成的 2024 年 12 月样本。",
        "",
        "本次新增的试卷均跳过 PDF 第 1、2 页，正文开头注明考试年月；文件名含 Note 的 PDF 不转换。此前完成的样本保留原结果。",
        "",
        "以下文件为 OCR 初稿，尚未全部逐字校订。每页附原图链接；表格、注音、下划线及有批注遮挡的位置需结合原图核对。分享 Markdown 时请同时保留对应的 pages 目录。",
        "",
        "| 考试年月 | 原 PDF 页数 | 转换页数 | Markdown | 状态 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        state = "已转换（既有样本）" if row["existing_sample"] else (
            "已转换" if row["status"] == "complete" else "处理中")
        text.append(
            f"| {row['year']} 年 {row['month']:02d} 月 | {row['pages']} | {row['processed_page_count']} | "
            f"[打开]({row['markdown']}) | {state} |")
    text.extend(["", "## 排除的 Note 文件", ""])
    text.extend("- " + Path(path).name for path in excluded)
    atomic_text(output_dir / "README.md", "\n".join(text) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("N2真题"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/markdown"))
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--tessdata")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--regenerate-only", action="store_true",
                        help="Rebuild Markdown and index from cached pages without invoking OCR")
    args = parser.parse_args()
    started = time.monotonic()
    documents, excluded = inventory(args.source_dir, args.output_dir)
    if args.regenerate_only:
        prior_manifest = args.output_dir / "batch-manifest.json"
        prior_elapsed = json.loads(prior_manifest.read_text()).get("elapsed_seconds", 0) if prior_manifest.exists() else 0
        for document in documents:
            if not document["existing_sample"] and not write_document(document):
                raise SystemExit(f"Incomplete OCR cache: {document['id']}")
        write_index(documents, excluded, args.output_dir, [], prior_elapsed)
        print(f"Regenerated {len(documents)} document entries from OCR cache.")
        return
    jobs = [
        (document, page)
        for document in documents if not document["existing_sample"]
        for page in range(3, document["pages"] + 1)
    ]
    pending_by_document = {
        d["id"]: d["pages"] - 2 for d in documents if not d["existing_sample"]
    }
    failures = []
    progress = args.output_dir / "batch-progress.json"
    write_index(documents, excluded, args.output_dir, [], 0)
    print(f"Queued {len(jobs)} pages in {len(pending_by_document)} new documents; excluded {len(excluded)} Note PDFs.", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_jobs = {
            pool.submit(recognize_page, page, Path(document["source"]), Path(document["output"]),
                        args.tesseract, args.tessdata, args.dpi, "full", True,
                        document["source_sha256"]): (document, page)
            for document, page in jobs
        }
        for completed, future in enumerate(as_completed(future_jobs), 1):
            document, page = future_jobs[future]
            try:
                data = future.result()
                pending_by_document[document["id"]] -= 1
                if not pending_by_document[document["id"]]:
                    write_document(document)
                    print(f"DONE {document['id']} ({document['pages'] - 2} pages)", flush=True)
                print(f"{completed}/{len(jobs)} {document['id']} p{page:02d}: {len(data['lines'])} lines", flush=True)
            except Exception as error:
                details = getattr(error, "stderr", b"")
                if isinstance(details, bytes):
                    details = details.decode(errors="replace")
                failures.append({"exam": document["id"], "page": page,
                                 "error": str(error), "details": details})
                print(f"ERROR {document['id']} p{page}: {error} {details}", flush=True)
            atomic_text(progress, json.dumps({
                "completed_pages": completed, "total_pages": len(jobs),
                "failures": failures, "elapsed_seconds": round(time.monotonic() - started),
                "finished_documents": [name for name, remaining in pending_by_document.items() if remaining == 0],
                "last_exam": document["id"], "last_page": page,
            }, ensure_ascii=False, indent=2) + "\n")
    write_index(documents, excluded, args.output_dir, failures, time.monotonic() - started)
    if failures:
        raise SystemExit(f"{len(failures)} pages failed; rerun to resume.")
    print(f"Complete: {len(jobs)} new pages; {time.monotonic() - started:.0f}s.", flush=True)


if __name__ == "__main__":
    main()
