#!/usr/bin/env python3
"""Validate generated exam PDFs and render them for visual review."""
import hashlib
import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
META=ROOT/"output/analysis/dpt-exams"
TEMP=ROOT/"tmp/pdfs/dpt-exams"


def validate(doc):
    pdf=ROOT/doc["pdf"]
    reader=PdfReader(pdf)
    assert not reader.is_encrypted
    assert pdf.read_bytes().startswith(b"%PDF-1.4")
    assert len(reader.pages)==doc["output_pages"]
    image_count=0;link_count=0
    for number,page in enumerate(reader.pages,1):
        assert abs(float(page.mediabox.width)-doc["page_size_points"][0])<.02
        assert abs(float(page.mediabox.height)-doc["page_size_points"][1])<.02
        text=page.extract_text()
        assert f"{number} / {doc['output_pages']}" in text
        assert "\ufffd" not in text
        resources=page["/Resources"].get_object()
        for value in resources.get("/Font",{}).values():
            font=value.get_object()
            assert "/ToUnicode" in font
            descriptor=font.get("/FontDescriptor")
            assert descriptor and "/FontFile2" in descriptor.get_object()
        objects=resources.get("/XObject",{})
        images=[v.get_object() for v in objects.values() if v.get_object().get("/Subtype")=="/Image"]
        if images:
            assert len(images)==1
            image_count+=1
            assert images[0]["/ColorSpace"]=="/DeviceGray"
            assert images[0]["/Width"]<=doc["pixel_target"][0]+1
            assert images[0]["/Height"]<=doc["pixel_target"][1]+1
        for annotation in page.get("/Annots",[]):
            rect=annotation.get_object()["/Rect"]
            assert 0<=rect[0]<rect[2]<=float(page.mediabox.width)
            assert 0<=rect[1]<rect[3]<=float(page.mediabox.height)
            link_count+=1
    assert image_count==doc["image_pages"]
    first=2 if doc["id"].startswith("2024") else 3
    assert doc["included_source_pages"]==list(range(first,doc["source_pages"]+1))
    assert len(doc["entries"])==len(doc["included_source_pages"])
    for entry in doc["entries"]:
        segments=entry["segments"]
        assert segments[0][0]==0
        assert segments[-1][1]==entry["crop"][3]-entry["crop"][1]
        assert all(a[1]==b[0] for a,b in zip(segments,segments[1:]))
        assert all(b>a for a,b in segments)
    prefix=TEMP/(doc["id"]+"-render")
    for old in TEMP.glob(doc["id"]+"-render-*.jpg"):old.unlink()
    for old in TEMP.glob("overview-"+doc["id"]+"-*.jpg"):old.unlink()
    result=subprocess.run(["pdftoppm","-r","40","-jpeg","-jpegopt","quality=75",
                           str(pdf),str(prefix)],capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    pages=sorted(TEMP.glob(doc["id"]+"-render-*.jpg"))
    assert len(pages)==doc["output_pages"]
    # Every rendered page is included in its document's overview sheets.
    for part,start in enumerate(range(0,len(pages),60),1):
        selected=pages[start:start+60]
        sheet=Image.new("RGB",(1200,math_rows(len(selected))*115),"#d2d2d2")
        draw=ImageDraw.Draw(sheet)
        for i,path in enumerate(selected):
            image=Image.open(path).convert("RGB");image.thumbnail((194,94))
            x=(i%6)*200;y=(i//6)*115
            sheet.paste(image,(x+(200-image.width)//2,y+18))
            draw.text((x+5,y+3),f"{doc['id']} p{start+i+1}",fill="black")
        sheet.save(TEMP/f"overview-{doc['id']}-{part}.jpg",quality=88)
    return dict(id=doc["id"],pdf=doc["pdf"],pages=doc["output_pages"],
                source_pages_preserved=len(doc["entries"]),image_pages=image_count,
                links=link_count,sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                bytes=pdf.stat().st_size,status="passed",all_pages_rendered=True)


def math_rows(count):return (count+5)//6


def main():
    global META,TEMP
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portrait",action="store_true")
    args=parser.parse_args()
    pdf_dir=ROOT/"output/pdf/DPT真题"
    if args.portrait:
        META=ROOT/"output/analysis/dpt-exams-portrait"
        TEMP=ROOT/"tmp/pdfs/dpt-exams-portrait"
        pdf_dir=ROOT/"output/pdf/DPT真题_竖屏版"
    manifest=json.loads((META/"manifest.json").read_text())
    documents=manifest["documents"]
    assert len(documents)==29
    assert len(list(pdf_dir.glob("*.pdf")))==29
    assert not any("Note" in d["source"] for d in documents)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(validate,documents))
    report=dict(status="passed",documents=len(results),pages=sum(x["pages"] for x in results),
                preserved_source_pages=sum(x["source_pages_preserved"] for x in results),
                bytes=sum(x["bytes"] for x in results),files=results,
                checks=["PDF 1.4", "portrait page size" if args.portrait else "landscape page size", "embedded Unicode fonts", "grayscale images",
                        "page labels", "internal links", "continuous source coverage", "all pages rendered"])
    (META/"validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ["status","documents","pages","preserved_source_pages","bytes"]}))


if __name__=="__main__":main()
