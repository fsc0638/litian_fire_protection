"""docs/部署.md 的「磁碟空間」：清建置快取的清單程式只挑本系統已不用的層；門檻的說明與程式、部署設定一致。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from litian import api

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "部署.md").read_text(encoding="utf-8")
SECTION = DOC[DOC.index("## 磁碟空間"):DOC.index("## 轉檔器升級後")]


def _step(word: str) -> str:
    """Dockerfile 裡含 word 的那一步，照 BuildKit 建置步驟說明（Description）的寫法：續行併成一行。"""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", "")
    return next(s for s in text.splitlines() if word in s and not s.startswith("#"))


def _du(records: list[tuple], old_format: bool) -> bytes:
    """照 docker buildx du --verbose 的格式產生輸出。records：(ID, 下面那層, 還被映像使用, 建置步驟)。
    old_format：舊版 buildx 的「Parent: ID」單行；新版是「Parents:」下一行一個「 - ID」。"""
    out = []
    for n, (rid, parent, shared, desc) in enumerate(records):
        lines = [f"ID:\t\t{rid}"]
        if parent:
            lines += [f"Parent:\t\t{parent}"] if old_format else ["Parents:", f" - {parent}"]
        lines += ["Created at:\t2026-10-01 01:02:03.456 +0000 UTC", "Mutable:\tfalse", "Reclaimable:\ttrue",
                  f"Shared:\t\t{str(shared).lower()}", "Size:\t\t612.3MB", f"Description:\t[{n % 9 + 1}/9] {desc}",
                  "Usage count:\t2", "Last used:\t3 days ago", "Type:\t\tregular"]
        out.append("\n".join(lines) + "\n")
    return "\n".join(out).encode()


@pytest.mark.skipif(shutil.which("awk") is None, reason="沒有 awk")
@pytest.mark.parametrize("old_format", [False, True])
def test_cache_cleanup_lists_only_our_unused_layers(tmp_path, old_format):
    prog = re.search(r"buildx du --verbose \| awk -v RS= '(.*?)' \\\n  \| sort -rn", SECTION, re.S).group(1)
    (tmp_path / "list.awk").write_text(prog, encoding="utf-8")
    dep, own = _step("tomllib"), _step("--no-deps")
    records = [
        ("base", "", True, "pulled from docker.io/library/python:3.12-slim"),
        ("font", "base", True, _step("fonts-noto-cjk")),
        ("work", "font", True, "WORKDIR /app"),
        ("pyproj", "work", True, "COPY pyproject.toml ./"),
        # 目前在跑的這一版：還被映像使用，不列
        ("dep", "pyproj", True, dep), ("src", "dep", True, "COPY src ./src"), ("own", "src", True, own),
        # 前幾次部署的程式層（疊在目前的套件層上，映像已刪）與 09 的測試層
        ("src1", "dep", False, "COPY src ./src"), ("own1", "src1", False, own),
        ("test1", "own1", False, "RUN pip install --no-cache-dir -q pytest"),
        # pyproject.toml 改之前的套件層
        ("pyproj0", "work", False, "COPY pyproject.toml ./"), ("dep0", "pyproj0", False, dep),
        ("src0", "dep0", False, "COPY src ./src"),
        # 舊 Dockerfile：先複製程式再整包安裝
        ("srcold", "pyproj", False, "COPY src ./src"), ("pipold", "srcold", False, "RUN pip install --no-cache-dir ."),
        ("lawold", "pipold", False, "COPY data/lawdb ./data/lawdb"),
        # 別的映像：同樣的安裝指令，但底下沒有本系統的中文字型層
        ("other", "base", False, "RUN apt-get install -y curl"),
        ("otherpip", "other", False, "RUN pip install --no-cache-dir ."),
        ("otherreq", "other", False, "RUN pip install --no-cache-dir -r /tmp/requirements.txt"),
    ]
    out = subprocess.run(["awk", "-v", "RS=", "-f", str(tmp_path / "list.awk")], input=_du(records, old_format),
                         capture_output=True, check=True).stdout.decode().split()
    listed = {rid: int(d) for d, rid in zip(out[::2], out[1::2])}
    # 數字＝離套件安裝層幾層；文件照這個數字由大到小刪，上面的層一定先刪
    assert listed == {"pipold": 0, "lawold": 1, "dep0": 0, "src0": 1, "src1": 1, "own1": 2, "test1": 3}


def test_disk_thresholds_documented_as_fixed():
    # 正式環境的 api 容器只收 docker-compose.yml 列出的環境變數：門檻沒傳進去，文件就要寫明在 .env 設了不會生效
    compose = (ROOT / "deploy" / "oracle" / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("UPLOAD_MIN_FREE_GB", "DISK_WARN_GB"):
        assert var not in compose, f"{var} 已傳進容器：請改寫 docs/部署.md「磁碟空間」的門檻說明"
        assert var in SECTION
    assert "不會生效" in SECTION
    assert f"門檻（{api.UPLOAD_MIN_FREE_GB:g} GB、{api.DISK_WARN_GB:g} GB）是固定的" in SECTION
