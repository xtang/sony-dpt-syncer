#!/usr/bin/env python3
"""Pair with a Sony DPT and upload this project's study PDF.

Run using .tools/dpt-venv/bin/python scripts/dpt_transfer.py --addr DEVICE_IP.
The optional file courier lets Codex perform each HTTP request with curl.
"""
import argparse
import builtins
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dptrp1.dptrp1 import DigitalPaper

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "output/pdf/N2高频考点与易错辨析_SonyDPT.pdf"
AUTH = ROOT / ".tools/dpt-auth"


def save_json(path, value):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_clock(device):
    # The datetime resource is write-only on DPT-RP1. Read the server's clock
    # from a fresh authenticated response instead.
    response=device._get_endpoint("/system/configs/timezone")
    response.raise_for_status()
    value=response.headers.get("Date")
    if not value:
        raise RuntimeError("Device response did not include its Date header")
    normalized=re.sub(r"(?:GMT|UTC)([+-])(\d{2}):?(\d{2})$",r"\1\2\3",value)
    parsed=parsedate_to_datetime(normalized)
    if parsed.tzinfo is None:
        parsed=parsed.replace(tzinfo=timezone.utc)
    parsed=parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00","Z"),parsed


def synchronize_clock(device,reauthenticate=None):
    before,_=read_clock(device)
    now=datetime.now(timezone.utc).replace(microsecond=0)
    response=device._put_endpoint("/system/configs/datetime",data={"value":now.isoformat().replace("+00:00","Z")})
    response.raise_for_status()
    # A large correction can expire the session issued under the old clock.
    if reauthenticate is not None:
        reauthenticate()
    after,actual=read_clock(device)
    difference=abs((datetime.now(timezone.utc)-actual).total_seconds())
    if difference>15:
        raise RuntimeError(f"Device clock verification failed: {difference:.1f} seconds difference")
    return dict(before=before,after=after,difference_seconds=round(difference,2))


class Courier:
    def __init__(self, directory, address):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.address = address
        self.number = 0

    def state(self, **value):
        save_json(self.directory / "state.json", value)

    def pin(self, prompt):
        self.state(stage="waiting_pin")
        path = self.directory / "pin.txt"
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if path.exists():
                pin = path.read_text().strip()
                path.unlink()
                if not pin.isdigit():
                    raise ValueError("PIN must contain digits only")
                return pin
            time.sleep(0.2)
        raise TimeoutError("Timed out waiting for the device PIN")

    def send(self, prepared, **kwargs):
        parsed = urlsplit(prepared.url)
        if parsed.hostname != self.address or parsed.port not in (8080, 8443):
            raise ValueError("Unexpected request destination")
        self.number += 1
        prefix = self.directory / f"request-{self.number:03d}"
        body_path = prefix.with_suffix(".body")
        headers_path = prefix.with_suffix(".headers")
        status_path = prefix.with_suffix(".status")
        config_path = prefix.with_suffix(".curl")

        def quoted(value):
            if "\n" in str(value) or "\r" in str(value):
                raise ValueError("Newline in curl configuration value")
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        options = [
            "silent", "show-error", "insecure", 'noproxy = "*"',
            "connect-timeout = 5", "max-time = 120",
            "url = " + quoted(prepared.url),
            "request = " + quoted(prepared.method),
            "output = " + quoted(body_path),
            "dump-header = " + quoted(headers_path),
            "write-out = " + quoted(f"%output{{{status_path}}}%{{http_code}}"),
        ]
        for name, value in prepared.headers.items():
            options.append("header = " + quoted(f"{name}: {value}"))
        if prepared.body is not None:
            payload = prepared.body
            if hasattr(payload, "read"):
                payload = payload.read()
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            payload_path = prefix.with_suffix(".payload")
            payload_path.write_bytes(payload)
            options.append("data-binary = " + quoted("@" + str(payload_path)))
        config_path.write_text("\n".join(options) + "\n", encoding="utf-8")
        self.state(stage="http", number=self.number, config=str(config_path),
                   method=prepared.method, endpoint=parsed.path)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if status_path.exists() and status_path.stat().st_size >= 3:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("HTTP courier did not return a response")
        code = int(status_path.read_text().strip())
        if not code:
            raise requests.ConnectionError("curl could not reach the device")
        response = requests.Response()
        response.status_code = code
        response._content = body_path.read_bytes()
        response._content_consumed = True
        response.url = prepared.url
        response.request = prepared
        for line in headers_path.read_text(encoding="iso-8859-1").splitlines():
            if line.startswith("HTTP/"):
                response.headers.clear()
            elif ":" in line:
                name, value = line.split(":", 1)
                response.headers[name] = value.strip()
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        for suffix in (".body", ".headers", ".status", ".curl", ".payload"):
            prefix.with_suffix(suffix).unlink(missing_ok=True)
        self.state(stage="processing", number=self.number, http_status=code)
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr", required=True)
    parser.add_argument("--courier")
    parser.add_argument("--batch-dir", type=Path)
    parser.add_argument("--remote-folder", default="Document")
    parser.add_argument("--open-page", type=int, default=1)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--finalize-only",action="store_true",help="Finish a fully verified batch without uploading again")
    parser.add_argument("--report-path",type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    AUTH.mkdir(mode=0o700, parents=True, exist_ok=True)
    relay = Courier(args.courier, args.addr) if args.courier else None
    dp = DigitalPaper(addr=args.addr)
    dp.session.trust_env = False
    if relay:
        dp.session.send = relay.send
        builtins.input = relay.pin
    key_path, id_path = AUTH / "privatekey.dat", AUTH / "deviceid.dat"
    try:
        if not (key_path.exists() and id_path.exists()):
            result = dp.register()
            if not result:
                raise RuntimeError("The pairing protocol did not complete")
            ca, key, client_id = result
            key_path.write_text(key)
            id_path.write_text(client_id)
            (AUTH / "device-ca.pem").write_text(ca)
            print("PAIRING_COMPLETE", flush=True)
        dp.authenticate(id_path.read_text().strip(), key_path.read_bytes())
        clock=synchronize_clock(dp,reauthenticate=lambda:dp.authenticate(id_path.read_text().strip(),key_path.read_bytes()))
        print("CLOCK_SYNCHRONIZED",flush=True)
        files=sorted(args.batch_dir.glob("*.pdf")) if args.batch_dir else [PDF]
        if not files:
            raise ValueError("No PDFs to upload")
        remote_folder=args.remote_folder.rstrip("/")
        if remote_folder!="Document":
            dp.new_folder(remote_folder)
        # Do not overwrite an annotated copy that may already exist on the reader.
        documents = dp.list_documents()
        occupied = {item["entry_path"] for item in documents}
        completed=[]
        report_path=args.report_path or ROOT/"output/analysis"/("dpt-exams-transfer.json" if args.batch_dir else "dpt-transfer.json")
        if args.finalize_only:
            previous=json.loads(report_path.read_text())
            completed=previous["files"]
            assert previous["address"]==args.addr and len(completed)==len(files)
            assert {str(p) for p in files}=={f["local_path"] for f in completed}
            for entry in completed:
                assert entry["readback_verified"]
                assert hashlib.sha256(Path(entry["local_path"]).read_bytes()).hexdigest()==entry["sha256"]
            remote=completed[-1]["remote_path"]
        for pdf in ([] if args.finalize_only else files):
            remote = remote_folder + "/" + pdf.name
            if remote in occupied:
                remote = remote_folder + "/" + pdf.stem + datetime.now().strftime("_%Y%m%d_%H%M%S") + ".pdf"
            dp.upload_file(str(pdf), remote)
            uploaded = dp.download(remote)
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            if hashlib.sha256(uploaded).hexdigest() != digest:
                raise RuntimeError("The downloaded copy differs from the local PDF: "+pdf.name)
            completed.append(dict(local_path=str(pdf),remote_path=remote,bytes=pdf.stat().st_size,
                                  sha256=digest,readback_verified=True))
            save_json(report_path,dict(stage="uploading",address=args.addr,files=completed,total_files=len(files)))
            print(json.dumps(dict(completed=len(completed),total=len(files),file=pdf.name,
                                  readback_verified=True),ensure_ascii=False),flush=True)
            if relay:
                relay.state(stage="progress",completed=len(completed),total=len(files),file=pdf.name)
        info = dp.list_document_info(remote)
        dp.display_document(info["entry_id"], page=args.open_page)
        preview_saved=False
        if args.screenshot:
            time.sleep(3)
            args.screenshot.parent.mkdir(parents=True,exist_ok=True)
            try:
                # v0.1.20's public screenshot helper passes an unsupported params
                # argument to _get_endpoint. Use its documented endpoint directly.
                preview=dp._get_endpoint("/system/controls/screen_shot2?query=jpeg")
                args.screenshot.write_bytes(preview.content)
                preview_saved=True
            except Exception as preview_error:
                print("SCREENSHOT_UNAVAILABLE:",type(preview_error).__name__,flush=True)
        report = dict(stage="complete", address=args.addr,remote_folder=remote_folder,
                      remote_path=remote,files=completed,total_files=len(files),
                      bytes=sum(f["bytes"] for f in completed),
                      readback_verified=True, opened_on_device=True,opened_page=args.open_page,
                      preview_saved=preview_saved,clock=clock)
        save_json(report_path, report)
        if relay:
            relay.state(**report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception as error:
        if relay:
            relay.state(stage="error", error=type(error).__name__, detail=str(error))
        raise


if __name__ == "__main__":
    main()
