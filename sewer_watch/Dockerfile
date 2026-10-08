ARG BUILD_FROM
FROM $BUILD_FROM

# Python (stdlib only), PDF text extraction, and OCR for scanned minutes
RUN apk add --no-cache \
      python3 \
      poppler-utils \
      tesseract-ocr \
      tesseract-ocr-data-eng

COPY app /app

CMD ["python3", "-u", "/app/main.py"]
