#!/usr/bin/env python3
"""
Assemble a Windows (x86-64) Python with Tkinter and PyInstaller on a Linux host,
without running any Windows installer. Used by build_on_pi.sh / build_with_wine.sh.

It downloads pinned Windows builds of CPython and its DLL dependencies from
conda-forge, unpacks them into one folder, and adds PyInstaller from PyPI.

    python3 tools/fetch_winpython.py build/winpython
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

CHANNEL = "https://conda.anaconda.org/conda-forge/win-64/"
PACKAGES = [
    "python-3.12.16-hb12b558_0_cpython.conda",
    "libpython-3.12.16-hfb4101e_0_cpython.conda",
    "tk-8.6.13-h967ab96_4.conda",
    "openssl-3.6.5-hf411b9b_0.conda",
    "libffi-3.7.0-h3d046cb_1.conda",
    "libzlib-1.3.2-hfd05255_3.conda",
    "libexpat-2.9.0-hac47afa_0.conda",
    "liblzma-5.8.3-hfd05255_1.conda",
    "bzip2-1.0.8-h0ad9c76_10.conda",
    "libsqlite-3.54.1-hf5d6505_101.conda",
    "vc14_runtime-14.51.36247-hb06f545_44.conda",
]
PIP_PACKAGES = ["pyinstaller==6.22.3", "pyinstaller-hooks-contrib", "altgraph", "pefile",
                "pywin32-ctypes", "packaging", "setuptools"]


def unzstd(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except ImportError:
        pass
    if not shutil.which("zstd"):
        sys.exit("Need the 'zstd' tool:  sudo apt install zstd")
    return subprocess.run(["zstd", "-dc"], input=data, stdout=subprocess.PIPE, check=True).stdout


def safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    root = os.path.realpath(dest)
    for member in tar:
        if not (member.isfile() or member.isdir()):
            continue
        target = os.path.realpath(os.path.join(dest, member.name))
        if target != root and not target.startswith(root + os.sep):
            sys.exit("Unsafe path in package: " + member.name)
        if hasattr(tarfile, "data_filter"):
            tar.extract(member, dest, filter="data")
        else:
            tar.extract(member, dest)


def main() -> None:
    dest = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "build/winpython")
    cache = os.path.join(os.path.dirname(dest), "downloads")
    os.makedirs(cache, exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    for name in PACKAGES:
        local = os.path.join(cache, name)
        if not os.path.exists(local):
            print("downloading", name, flush=True)
            with urllib.request.urlopen(CHANNEL + name, timeout=120) as resp, open(local + ".part", "wb") as out:
                shutil.copyfileobj(resp, out)
            os.replace(local + ".part", local)
        with zipfile.ZipFile(local) as outer:
            inner = next(n for n in outer.namelist() if n.startswith("pkg-") and n.endswith(".tar.zst"))
            payload = unzstd(outer.read(inner))
        with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
            safe_extract(tar, dest)
    if not os.path.exists(os.path.join(dest, "python.exe")):
        sys.exit("python.exe missing after unpacking - package layout changed?")

    # conda keeps dependency DLLs (OpenSSL, Tcl/Tk, zlib, ...) in Library\bin; put them
    # next to python.exe so Windows/Wine finds them without PATH tweaks.
    libbin = os.path.join(dest, "Library", "bin")
    for fname in os.listdir(libbin):
        if fname.lower().endswith(".dll") and not os.path.exists(os.path.join(dest, fname)):
            shutil.copy2(os.path.join(libbin, fname), os.path.join(dest, fname))

    site = os.path.join(dest, "Lib", "site-packages")
    print("installing PyInstaller (Windows build) from PyPI", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--upgrade",
                    "--target", site, "--platform", "win_amd64", "--python-version", "3.12",
                    "--implementation", "cp", "--only-binary=:all:", *PIP_PACKAGES], check=True)
    print("Windows Python ready in", dest)


if __name__ == "__main__":
    main()
