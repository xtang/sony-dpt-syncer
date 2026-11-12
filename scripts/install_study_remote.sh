#!/usr/bin/env bash
set -euo pipefail
umask 077
app_root="$HOME/apps/n2-study"
incoming="$app_root/incoming"
cd "$incoming"
# Verify both payloads and reject archive traversal before extracting anything.
python3 - <<'PY'
import hashlib,json,tarfile
from pathlib import Path,PurePosixPath
manifest=json.loads(Path('manifest.json').read_text())
assert len(manifest['files'])==2
for row in manifest['files']:
    assert row['name'] in ('n2-study-code.tar.gz','n2-study-private-seed.tar.gz')
    path=Path(row['name'])
    assert path.stat().st_size==row['bytes']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            name=PurePosixPath(member.name)
            assert not name.is_absolute() and '..' not in name.parts
            assert member.isfile() or member.isdir()
assert manifest['release'].isalnum()
PY
release_id="$(python3 -c 'import json; print(json.load(open("manifest.json"))["release"])')"
release_dir="$app_root/releases/$release_id"
if [[ -e "$release_dir" ]]; then
  echo 'This release already exists. Rebuild the package to create a new release.' >&2
  exit 1
fi
mkdir -p "$release_dir" "$app_root/backups"
tar --no-same-owner --same-permissions -xzf n2-study-code.tar.gz -C "$release_dir"
if [[ ! -d "$app_root/data" ]]; then
  tar --no-same-owner -xzf n2-study-private-seed.tar.gz -C "$app_root"
elif [[ -f "$app_root/data/study.sqlite3" ]]; then
  STUDY_DB="$app_root/data/study.sqlite3" STUDY_BACKUP="$app_root/backups/study-$release_id.sqlite3" python3 - <<'PY'
import os,sqlite3
with sqlite3.connect(os.environ['STUDY_DB']) as source,sqlite3.connect(os.environ['STUDY_BACKUP']) as target:
    source.backup(target)
PY
else
  echo 'Data directory exists without a database. Refusing to overwrite it.' >&2
  exit 1
fi
chmod 700 "$app_root/data"
# Resolve network settings from private deployment options or the existing installation.
export DPT_INSTALL_ROOT="$app_root"
APP_LISTEN_ADDRESS="$(python3 - <<'PY'
import ipaddress,json,os,re,socket
from pathlib import Path
root=Path(os.environ['DPT_INSTALL_ROOT'])
options_path=root/'incoming/deploy-options.json'
options=json.loads(options_path.read_text()) if options_path.exists() else {}
old={}
old_env=root/'current/.env'
if old_env.exists():
    for line in old_env.read_text().splitlines():
        key,separator,value=line.partition('=')
        if separator:old[key.strip()]=value.strip().strip('"').strip("'")
settings_path=root/'data/settings.json'
settings=json.loads(settings_path.read_text()) if settings_path.exists() else {}
reader=options.get('DPT_ADDRESS') or settings.get('device_address') or old.get('DPT_ADDRESS') or ''
if reader and not re.fullmatch(r'[A-Za-z0-9.:-]{1,200}',reader):
    raise SystemExit('Invalid DPT_ADDRESS')
listen=options.get('APP_LISTEN_ADDRESS') or os.environ.get('APP_LISTEN_ADDRESS') or old.get('APP_LISTEN_ADDRESS')
if not listen and reader:
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as connection:
        connection.connect((reader,8080))
        listen=connection.getsockname()[0]
if not listen:
    raise SystemExit('Configure APP_LISTEN_ADDRESS or DPT_ADDRESS before the first deployment')
address=ipaddress.ip_address(listen)
if address.version!=4 or not address.is_private or address.is_loopback or address.is_unspecified:
    raise SystemExit('A private LAN IPv4 address is required')
hosts=options.get('APP_ALLOWED_HOSTS') or old.get('APP_ALLOWED_HOSTS') or str(address)
if not isinstance(hosts,str) or not re.fullmatch(r'[A-Za-z0-9.,:-]{1,2000}',hosts):
    raise SystemExit('Invalid APP_ALLOWED_HOSTS')
values=dict(listen=str(address),reader=reader,hosts=hosts)
(root/'data/deploy-network.json').write_text(json.dumps(values))
print(address)
PY
)"
APP_ALLOWED_HOSTS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hosts"])' "$app_root/data/deploy-network.json")"
DPT_ADDRESS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["reader"])' "$app_root/data/deploy-network.json")"
export APP_LISTEN_ADDRESS APP_ALLOWED_HOSTS DPT_ADDRESS
cd "$release_dir"
python3 -m app.web_auth --file "$app_root/data/web-access.json"
old_release=""
if [[ -L "$app_root/current" ]]; then old_release="$(readlink "$app_root/current")"; fi
export APP_UID="$(id -u)" APP_GID="$(id -g)" STUDY_DATA_DIR="$app_root/data" RELEASE_ID="$release_id"
cd "$release_dir"
printf 'APP_UID=%s\nAPP_GID=%s\nSTUDY_DATA_DIR=%s\nRELEASE_ID=%s\nAPP_LISTEN_ADDRESS=%s\nAPP_ALLOWED_HOSTS=%s\nDPT_ADDRESS=%s\n' "$APP_UID" "$APP_GID" "$STUDY_DATA_DIR" "$RELEASE_ID" "$APP_LISTEN_ADDRESS" "$APP_ALLOWED_HOSTS" "$DPT_ADDRESS" > .env
docker compose -p n2-study build
# Do not replace another project already occupying the port.
if [[ -z "$old_release" ]] && python3 - <<'PY'
import socket
with socket.socket() as connection:
    connection.settimeout(2)
    raise SystemExit(0 if connection.connect_ex(('127.0.0.1',4387))==0 else 1)
PY
then
  echo 'Port 4387 is in use; no running service was changed.' >&2
  exit 1
fi
rollback() {
  echo 'New release failed its health check. Restoring the previous release.' >&2
  if [[ -n "$old_release" ]]; then
    (unset RELEASE_ID APP_LISTEN_ADDRESS APP_ALLOWED_HOSTS DPT_ADDRESS; cd "$old_release"; docker compose -p n2-study up -d --no-build)
  else
    docker compose -p n2-study down
  fi
}
if ! docker compose -p n2-study up -d --no-build; then rollback; exit 1; fi
healthy=0
for attempt in $(seq 1 30); do
  if python3 - <<'PY'
import json,os,urllib.request
try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result=json.load(opener.open('http://'+os.environ['APP_LISTEN_ADDRESS']+':4387/api/health',timeout=2))
    assert result['ok'] and result['service']=='n2-study'
except Exception:
    raise SystemExit(1)
PY
  then healthy=1; break; fi
  sleep 2
done
if [[ "$healthy" != 1 ]]; then
  docker compose -p n2-study logs --tail 40
  rollback
  exit 1
fi
ln -s "$release_dir" "$app_root/current.new"
mv -Tf "$app_root/current.new" "$app_root/current"
# Keep a source-only Git history on the deployment host. Private files are outside this tree.
if command -v git >/dev/null; then
  repo="$app_root/source"
  if [[ ! -d "$repo/.git" ]]; then mkdir -p "$repo"; git -C "$repo" init -q; fi
  tar --no-same-owner --same-permissions -xzf "$incoming/n2-study-code.tar.gz" -C "$repo"
  git -C "$repo" add app scripts tests docs Dockerfile compose.yaml .dockerignore .gitignore README.md
  if ! git -C "$repo" diff --cached --quiet; then
    git -C "$repo" -c user.name='N2 Study Deploy' -c user.email='n2-study@localhost' commit -qm "Deploy study app $release_id"
  fi
fi
# Credentials are now in the private data volume; remove the duplicate transport archive.
rm -f "$incoming/n2-study-private-seed.tar.gz"
printf 'http://%s:4387\n' "$APP_LISTEN_ADDRESS" > "$app_root/service-url.txt"
printf '健康检查通过：浏览器直接打开 http://%s:4387 ，版本 %s\n' "$APP_LISTEN_ADDRESS" "$release_id"
