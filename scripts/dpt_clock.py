#!/usr/bin/env python3
"""Inspect a paired DPT clock; --set calibrates it to the computer's UTC time."""
import argparse
import json
import os
from datetime import datetime,timezone
from pathlib import Path

from dptrp1.dptrp1 import DigitalPaper
from dpt_transfer import AUTH,ROOT,Courier,read_clock,save_json,synchronize_clock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr",required=True)
    parser.add_argument("--courier")
    parser.add_argument("--set",action="store_true")
    args=parser.parse_args()
    os.umask(0o077)
    relay=Courier(args.courier,args.addr) if args.courier else None
    device=DigitalPaper(addr=args.addr)
    device.session.trust_env=False
    if relay:device.session.send=relay.send
    try:
        device.authenticate((AUTH/"deviceid.dat").read_text().strip(),(AUTH/"privatekey.dat").read_bytes())
        current,_=read_clock(device)
        zone=device.get_timezone()
        entries=device.list_objects_in_folder("Document/N2真题_竖屏版")
        record=dict(stage="inspected",address=args.addr,before=current,timezone=zone,
                    computer_local=datetime.now().astimezone().isoformat(timespec="seconds"),
                    existing_pairing_works=True,exam_entries_found=len(entries))
        transfer=ROOT/"output/analysis/dpt-exams-portrait/transfer.json"
        if transfer.exists():
            expected={f["remote_path"] for f in json.loads(transfer.read_text())["files"]}
            actual={e.get("entry_path") or "Document/N2真题_竖屏版/"+(e.get("entry_name") or e.get("file_name") or "") for e in entries}
            record["expected_exam_files_present"]=len(expected&actual)
            record["expected_exam_files_total"]=len(expected)
        if args.set:
            record["calibration"]=synchronize_clock(device,reauthenticate=lambda:device.authenticate((AUTH/"deviceid.dat").read_text().strip(),(AUTH/"privatekey.dat").read_bytes()))
            record["timezone_after"]=device.get_timezone()
            assert record["timezone_after"]==zone
        record["stage"]="complete"
        report=ROOT/"output/analysis/dpt-clock.json"
        report.parent.mkdir(parents=True,exist_ok=True)
        save_json(report,record)
        if relay:relay.state(**record)
        print(json.dumps(record,ensure_ascii=False),flush=True)
    except Exception as error:
        if relay:relay.state(stage="error",error=type(error).__name__,detail=str(error))
        raise


if __name__=="__main__":main()
