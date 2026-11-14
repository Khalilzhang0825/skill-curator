: << 'CMDBLOCK'
@echo off
setlocal EnableExtensions
set "HOOK_PY=%~dp0..\skills\skill-curator\scripts\hook_intercept.py"
if not exist "%HOOK_PY%" (
  echo {}
  exit /b 0
)
py -3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  py -3 "%HOOK_PY%" %*
  exit /b
)
python -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  python "%HOOK_PY%" %*
  exit /b
)
python3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" <NUL >NUL 2>&1 && (
  python3 "%HOOK_PY%" %*
  exit /b
)
echo {}
exit /b 0
CMDBLOCK

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
HOOK_PY="$SCRIPT_DIR/../skills/skill-curator/scripts/hook_intercept.py"
if [ ! -f "$HOOK_PY" ]; then
  printf '{}\n'
  exit 0
fi
for _py in python3 python; do
  if command -v "$_py" >/dev/null 2>&1 && "$_py" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" </dev/null >/dev/null 2>&1; then
    exec "$_py" "$HOOK_PY" "$@"
  fi
done
printf '{}\n'
exit 0
