#!/usr/bin/env python3
"""Convert a scanned Japanese PDF into Markdown and retain page-level OCR.

Requires Pillow, Poppler (pdfinfo/pdftoppm), Tesseract, and jpn/jpn_vert data.
Usage: python ocr_pdf.py INPUT.pdf OUTPUT_DIR --tesseract PATH --tessdata DIR
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageChops


def recognize_page(number, source, output, engine, tessdata, dpi,
                   crop_mode="full", remove_annotations=True, source_sha256=None):
    stem = f"page-{number:02d}"
    destination = output / "ocr" / f"{stem}.json"
    image_path = output / "pages" / f"{stem}.jpg"
    text_path = output / "ocr" / f"{stem}.txt"
    settings = {"source_sha256": source_sha256, "dpi": dpi,
                "crop_mode": crop_mode, "remove_annotations": remove_annotations,
                "pipeline_version": 2}
    if destination.exists() and image_path.exists() and text_path.exists():
        cached = json.loads(destination.read_text())
        clean_fields = not any(
            "\t" in word.get("text", "") or "\n" in word.get("text", "")
            for line in cached["lines"] for word in line.get("words", [])
        )
        if clean_fields and (source_sha256 is None or cached.get("settings") == settings):
            return cached
    with tempfile.TemporaryDirectory(prefix="n2-ocr-") as work:
        prefix = str(Path(work) / stem)
        subprocess.run(
            ["pdftoppm", "-f", str(number), "-l", str(number), "-singlefile",
             "-r", str(dpi), "-png", str(source), prefix], check=True,
            capture_output=True, timeout=120,
        )
        with Image.open(prefix + ".png") as image:
            width, height = image.size
            rgb = image.convert("RGB")
            rgb.save(image_path, quality=88)
            # Older exams print questions close to the page edges: preserve the
            # full content by default, and retain an unmodified color reference.
            crop = ((int(width * .065), int(height * .04), int(width * .92), int(height * .945))
                    if crop_mode == "body" else (0, 0, width, height))
            body = rgb.crop(crop)
            if remove_annotations:
                red, green, blue = body.split()
                brightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
                darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
                colored = ImageChops.subtract(brightest, darkest).point(
                    lambda value: 255 if value > 45 else 0)
                body.paste("white", mask=colored)
            body.convert("L").save(prefix + "-body.png")
        command = [engine, prefix + "-body.png", prefix, "-l", "jpn", "--psm", "6",
                   "-c", "tessedit_create_tsv=1", "-c", "tessedit_create_txt=1"]
        if tessdata:
            command.extend(["--tessdata-dir", tessdata])
        environment = dict(os.environ, OMP_THREAD_LIMIT="1")
        subprocess.run(command, check=True, capture_output=True, env=environment, timeout=300)
        # Tesseract TSV is unquoted. A literal quote in Japanese text must not
        # consume following rows as one multiline CSV field.
        rows = list(csv.DictReader(io.StringIO(Path(prefix + ".tsv").read_text()),
                                   delimiter="\t", quoting=csv.QUOTE_NONE))
        groups = {}
        for row in rows:
            if row["level"] != "5" or not row["text"].strip():
                continue
            key = tuple(row[k] for k in ("block_num", "par_num", "line_num"))
            groups.setdefault(key, []).append({
                "text": row["text"], "confidence": float(row["conf"]),
                "x": int(row["left"]) + crop[0], "y": int(row["top"]) + crop[1],
                "width": int(row["width"]), "height": int(row["height"]),
            })
        lines = []
        for words in groups.values():
            words.sort(key=lambda w: w["x"])
            lines.append({
                "text": " ".join(w["text"] for w in words), "words": words,
                "x": min(w["x"] for w in words), "y": min(w["y"] for w in words),
                "height": max(w["height"] for w in words),
            })
        lines.sort(key=lambda line: (line["y"], line["x"]))
        data = {"page": number, "width": width, "height": height,
                "dpi": dpi, "engine": "Tesseract 5.5.4 / tessdata_best jpn",
                "settings": settings, "lines": lines}
        # JSON is the completion marker: write it last and replace atomically.
        text_path.write_text(Path(prefix + ".txt").read_text(), encoding="utf-8")
        pending = destination.with_suffix(".json.tmp")
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(destination)
        return data


def clean_text(text):
    # Japanese OCR emits spaces between its word tokens. Keep spaces next to
    # digits so question/option labels remain distinguishable.
    text = re.sub(r"(?<=[^\W\d_]) +(?=[^\W\d_])", "", text)
    text = re.sub(r" +([。、，．！？：；）」』】〉》])", r"\1", text)
    text = re.sub(r"([（「『【〈《]) +", r"\1", text)
    text = re.sub(r"(?<=[。、！？）」』】]) +(?=[^\W\d_])", "", text)
    return text.strip()


def format_page(data):
    paragraphs = []
    previous = None
    for line in data["lines"]:
        text = clean_text(line["text"])
        text = text.replace("_", r"\_").replace("*", r"\*")
        if not text:
            continue
        if previous and line["y"] - (previous["y"] + previous["height"]) > data["height"] * .012:
            paragraphs.append("")
        # A boxed question number is often returned as [2| or |8|. Only
        # normalize labels with an explicit digit and both visible box edges.
        boxed = re.match(r"^[\[|【「(（]\s*(\d{1,2})\s*[\]|】」)）]\s*(.+)$", text)
        option_row = re.match(r"^1[.．。]?\s+(.+?)\s+2[.．。]?\s*(.+?)\s+3[.．。]?\s*(.+?)\s+4[.．。]?\s*(.+)$", text)
        heading = re.match(r"^[問間岡]題\s*(\d+)(.*)$", text)
        if heading:
            text = f"問題 {heading[1]}{heading[2]}"
            paragraphs.extend(["", "### " + text, ""])
        elif boxed:
            paragraphs.extend(["", f"#### {boxed[1]}. {boxed[2]}", ""])
        elif option_row:
            paragraphs.append("")
            paragraphs.extend(f"- {n}　{option}" for n, option in enumerate(option_row.groups(), 1))
            paragraphs.append("")
        elif re.match(r"^(?:\d{1,2}[.．)]|\d{2}\s+)\s*", text):
            paragraphs.extend(["", "#### " + text, ""])
        else:
            # A hard line break retains choice rows, dialogue, and blanks.
            paragraphs.append(text + "  ")
        previous = line
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paragraphs)).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--tessdata")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--skip-first-pages", type=int, default=0)
    parser.add_argument("--crop-mode", choices=("full", "body"), default="full")
    args = parser.parse_args()
    for folder in ("pages", "ocr", "corrections"):
        (args.output / folder).mkdir(parents=True, exist_ok=True)
    info = subprocess.check_output(["pdfinfo", str(args.source)], text=True)
    count = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
    first = args.skip_first_pages + 1
    if not 1 <= first <= count:
        parser.error("--skip-first-pages must leave at least one page")
    date = re.search(r"(\d{4})(?:年|\.)(\d{1,2})", args.source.stem)
    year, month = map(int, date.groups()) if date else (None, None)
    source_sha256 = hashlib.sha256(args.source.read_bytes()).hexdigest()
    exam_id = f"{year:04d}-{month:02d}-N2" if date else args.source.stem
    title = f"{year} 年 {month:02d} 月 JLPT N2 真题" if date else args.source.stem
    pages = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(recognize_page, n, args.source, args.output,
                               args.tesseract, args.tessdata, args.dpi, args.crop_mode,
                               True, source_sha256): n
                   for n in range(first, count + 1)}
        for future in as_completed(futures):
            data = future.result()
            pages[data["page"]] = data
            print(f"Page {data['page']}/{count}: {len(data['lines'])} lines", flush=True)
    corrected = sorted(int(p.stem.split("-")[1]) for p in (args.output / "corrections").glob("page-*.md")
                       if first <= int(p.stem.split("-")[1]) <= count)
    corrected_description = "、".join(map(str, corrected)) or "无"
    markdown = [f"# {title}（OCR 初稿）", "",
                f"> 来源：{args.source.name}，共 {count} 页。日文 OCR 自动识别，尚未全篇逐字校订。",
                f"> 从 PDF 第 {first} 页开始转换，跳过前 {args.skip_first_pages} 页。",
                "> 保留原 PDF 页序，每页附原图链接。已校对页保留题目下划线；其余页的下划线、星号和假名仍需核对。",
                f"> 已对照原页修订的 PDF 页：{corrected_description}。其余页为机器识别初稿。",
                ""]
    for number in range(first, count + 1):
        correction = args.output / "corrections" / f"page-{number:02d}.md"
        body = correction.read_text().strip() if correction.exists() else format_page(pages[number])
        status = "已对照原页修订" if correction.exists() else "机器 OCR，待校订"
        markdown.extend([f'<a id="page-{number:02d}"></a>', "", f"## PDF 第 {number} 页", "",
                         f"[查看原页](pages/page-{number:02d}.jpg) · {status}", "",
                         body, "", "---", ""])
    (args.output / f"{exam_id}.md").write_text("\n".join(markdown))
    manifest = {"source": str(args.source), "pages": count,
                "source_sha256": source_sha256,
                "exam_id": exam_id, "year": year, "month": month,
                "skipped_pages": list(range(1, first)), "processed_pages": list(range(first, count + 1)),
                "dpi": args.dpi, "engine": "Tesseract 5.5.4", "language": "jpn",
                "status": "OCR draft; not a verbatim verified transcription",
                "visually_corrected_pages": corrected,
                "page_line_counts": {str(n): len(pages[n]["lines"]) for n in sorted(pages)}}
    (args.output / "ocr-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
