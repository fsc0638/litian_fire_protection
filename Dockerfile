# 消防圖審系統 API（ARM64／x86 皆可建）
FROM python:3.12-slim
# 中文字型：CAD 原樣圖（review.cadview）的文字用；沒有的話中文字會變方框
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY data/lawdb ./data/lawdb
COPY data/tables ./data/tables
COPY data/review ./data/review
# 以無家目錄的帳號執行；ezdxf、matplotlib 等套件的快取放暫存區
ENV XDG_CACHE_HOME=/tmp/.cache MPLCONFIGDIR=/tmp/.cache/matplotlib
# ezdxf 的字型快取（$XDG_CACHE_HOME/ezdxf/font_manager_cache.json）在建置時就建好（匯入 ezdxf 字型模組時沒有快取就掃描系統字型），
# 執行身分直接讀，不必每個子行程重掃；/tmp/.cache 跟 /tmp 一樣開放寫入，其他套件仍可放自己的快取
# matplotlib 的字型清單同樣在建置時建好（資料夾開放寫入：不可寫時 matplotlib 會每個子行程另開暫存資料夾重掃）
RUN python -c "from ezdxf.fonts import fonts; assert fonts.font_manager.has_font('NotoSansCJK-Regular.ttc'), '找不到中文字型'" \
 && python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.font_manager" \
 && chmod 1777 /tmp/.cache /tmp/.cache/matplotlib && chmod -R a+rX /tmp/.cache/ezdxf /tmp/.cache/matplotlib
USER 65534:65534
EXPOSE 8000
CMD ["uvicorn", "litian.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
