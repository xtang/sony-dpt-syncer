#!/usr/bin/env python3
"""Read-only inventory and PDF download for inspecting DPT handwriting."""
import argparse
import hashlib
import json
import os
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath

from dptrp1.dptrp1 import DigitalPaper
from dpt_transfer import AUTH,ROOT,Courier,PDF,save_json


def baselines():
    result={}
    for name in ['dpt-transfer.json','dpt-exams-transfer.json','dpt-exams-portrait/transfer.json']:
        path=ROOT/'output/analysis'/name
        if not path.exists():continue
        report=json.loads(path.read_text())
        files=report.get('files') or [dict(remote_path=report['remote_path'],local_path=str(PDF),
                                         bytes=report['bytes'],sha256=report['sha256'])]
        for item in files:
            result[item['remote_path']]=item
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--addr',required=True)
    parser.add_argument('--courier')
    parser.add_argument('--remote',action='append')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    os.umask(0o077)
    output=args.output or ROOT/'output/notes'/datetime.now().strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True,exist_ok=True)
    relay=Courier(args.courier,args.addr) if args.courier else None
    device=DigitalPaper(addr=args.addr);device.session.trust_env=False
    if relay:device.session.send=relay.send
    try:
        device.authenticate((AUTH/'deviceid.dat').read_text().strip(),(AUTH/'privatekey.dat').read_bytes())
        known=baselines()
        if args.remote:
            downloaded=[]
            for remote in args.remote:
                info=device.list_document_info(remote)
                response=device._get_endpoint('/documents/'+info['entry_id']+'/file')
                response.raise_for_status()
                payload=response.content
                if not payload.startswith(b'%PDF-'):
                    raise ValueError('Device returned a non-PDF response')
                name=hashlib.sha256(remote.encode()).hexdigest()[:8]+'_'+PurePosixPath(remote).name
                local=output/name;local.write_bytes(payload)
                base=known.get(remote)
                digest=hashlib.sha256(payload).hexdigest()
                downloaded.append(dict(remote_path=remote,local_path=str(local),bytes=len(payload),
                                       sha256=digest,baseline=base,changed_from_upload=bool(base and digest!=base['sha256']),
                                       device_info=info))
            report=dict(stage='complete',mode='download',captured_at=datetime.now(timezone.utc).isoformat(),
                        output=str(output),files=downloaded)
            save_json(output/'downloads.json',report)
        else:
            response=device._get_endpoint('/documents2?entry_type=all')
            response.raise_for_status()
            data=response.json();save_json(output/'inventory.json',data)
            entries=data['entry_list']
            documents=[e for e in entries if e.get('entry_type')!='folder']
            ranked=sorted(documents,key=lambda e:e.get('modified_date',''),reverse=True)
            candidates=[]
            for entry in ranked[:8]:
                path=entry.get('entry_path','')
                candidates.append(dict(remote_path=path,name=entry.get('entry_name') or entry.get('file_name'),
                                       modified_date=entry.get('modified_date'),created_date=entry.get('created_date'),
                                       file_size=entry.get('file_size'),known_uploaded_file=path in known,
                                       fields=list(entry)))
            report=dict(stage='complete',mode='inventory',captured_at=datetime.now(timezone.utc).isoformat(),
                        output=str(output),documents=len(documents),candidates=candidates)
            save_json(output/'inventory-summary.json',report)
        if relay:relay.state(**report)
        print(json.dumps(report,ensure_ascii=False),flush=True)
    except Exception as error:
        if relay:relay.state(stage='error',error=type(error).__name__,detail=str(error))
        raise


if __name__=='__main__':main()
