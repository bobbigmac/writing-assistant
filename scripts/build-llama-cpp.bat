@echo off
REM Build llama-cpp-python from source with Haswell-safe CPU flags + CUDA 12.4
REM Output goes to build-log.txt so we don't buffer it in PowerShell memory.
REM
REM This script is launched detached via Start-Process to avoid memory issues.

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" amd64 >nul 2>&1
set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4
set CMAKE_ARGS=-DGGML_CUDA=on -DGGML_NATIVE=off -DGGML_AVX=on -DGGML_AVX2=on -DGGML_FMA=on -DGGML_F16C=on -DGGML_AVX512=off -DGGML_CCACHE=off -DCMAKE_CUDA_ARCHITECTURES=89

cd /d w:\projects\writing-assistant
.venv\Scripts\python.exe -m pip install llama-cpp-python --no-build-isolation --force-reinstall > build-log.txt 2>&1

if errorlevel 1 (
    echo BUILD FAILED >> build-log.txt
) else (
    echo BUILD SUCCEEDED >> build-log.txt
    REM Preserve the wheel
    .venv\Scripts\python.exe -m pip cache list llama-cpp-python >> build-log.txt 2>&1
)
