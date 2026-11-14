: << 'CMDBLOCK'
@echo off
setlocal EnableExtensions
set "CURATOR_PY=%~dp0curator.py"
if not exist "%CURATOR_PY%" (
  echo {"ok": false, "error_code": "CURATOR_SCRIPT_NOT_FOUND", "error": "curator.py was not found next to curator.cmd."} 1>&2
  exit /b 1
)
py -3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  py -3 "%CURATOR_PY%" %*
  exit /b
)
python -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  python "%CURATOR_PY%" %*
  exit /b
)
python3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  python3 "%CURATOR_PY%" %*
  exit /b
)
echo {"ok": false, "error_code": "PYTHON_NOT_FOUND", "error": "Python 3.9+ was not found on PATH."} 1>&2
exit /b 1
CMDBLOCK

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
CURATOR_PY="$SCRIPT_DIR/curator.py"
if [ ! -f "$CURATOR_PY" ]; then
  printf '%s\n' '{"ok": false, "error_code": "CURATOR_SCRIPT_NOT_FOUND", "error": "curator.py was not found next to curator.cmd."}' >&2
  exit 1
fi
for _py in python3 python; do
  if command -v "$_py" >/dev/null 2>&1 && "$_py" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" </dev/null >/dev/null 2>&1; then
    exec "$_py" "$CURATOR_PY" "$@"
  fi
done
printf '%s\n' '{"ok": false, "error_code": "PYTHON_NOT_FOUND", "error": "Python 3.9+ was not found on PATH."}' >&2
exit 1
