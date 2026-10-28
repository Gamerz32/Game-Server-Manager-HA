#!/bin/sh
set -eu
exec uvicorn manager.main:app --host 0.0.0.0 --port 8099
