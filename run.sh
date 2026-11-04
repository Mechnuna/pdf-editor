#!/bin/sh
# Запуск: ./run.sh [файл.pdf]
cd "$(dirname "$0")" && exec ./venv/bin/python -m pdfeditor "$@"
