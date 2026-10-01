# LibreDWG 0.14 轉檔容器（ARM64／x86 皆可建）。建置內容（context）＝ repo 根目錄：
#   docker build -f deploy/oracle/libredwg/Dockerfile -t litian-libredwg:0.14 <repo>
# 常駐服務＝converter.py（監看交接資料夾，見 docker-compose.yml 的 converter）；pipeline_check.py 為一次性實測。
# 原始碼包的 SHA-256 取自 GitHub 官方 release 資產摘要（2026-09-30 讀取）。
# 執行時一律：--network none、限記憶體、限 CPU、唯讀根目錄（見 03_libredwg_test.sh）。

FROM debian:bookworm-slim AS build
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275
# 預設單核編譯，降低對主機上其他程序的影響
ARG MAKE_JOBS=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config curl ca-certificates xz-utils texinfo python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL -o libredwg.tar.xz "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz" \
 && echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum -c - \
 && tar xf libredwg.tar.xz
WORKDIR /src/libredwg-${LIBREDWG_VERSION}
RUN nice -n 19 ./configure --prefix=/opt/libredwg --disable-bindings --disable-static \
 && nice -n 19 make -j"${MAKE_JOBS}" \
 && make install \
 && /opt/libredwg/bin/dwg2dxf --version

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/libredwg /opt/libredwg
ENV PATH=/opt/libredwg/bin:$PATH \
    LD_LIBRARY_PATH=/opt/libredwg/lib
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir ezdxf==1.4.4
COPY tools/dwg2dxf/repair_dxf.py deploy/oracle/libredwg/pipeline_check.py deploy/oracle/libredwg/converter.py /app/
USER 65534:65534
ENTRYPOINT ["/opt/venv/bin/python", "/app/pipeline_check.py"]
