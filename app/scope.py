"""Device folder selection, independent of document topics and filenames."""
from pathlib import PurePosixPath


def folder_path(value):
    if not isinstance(value, str):
        raise ValueError("文件夹路径需要是文字")
    value = value.strip().rstrip("/")
    if not value or len(value) > 1000 or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("请输入有效的设备文件夹路径")
    parts = value.split("/")
    if parts[0] != "Document" or any(part in ("", ".", "..") for part in parts):
        raise ValueError("文件夹必须位于 Document 下，不能包含空目录、. 或 ..")
    if value.lower().endswith(".pdf"):
        raise ValueError("请选择文件夹，而不是 PDF 文件")
    return str(PurePosixPath(value))


def in_folders(path, folders):
    return any(path == root or path.startswith(root+"/") for root in folders)


def normalize_folders(values):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("同步文件夹需要是最多 100 项的列表")
    result = []
    for path in sorted({folder_path(value) for value in values}, key=lambda value: (len(value.split("/")), value)):
        if not in_folders(path, result):
            result.append(path)
    return result
