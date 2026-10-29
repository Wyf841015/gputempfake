#!/bin/bash
# 双架构编译拦截库。x86_64 用本机 gcc，aarch64 用交叉工具链。
# 交叉工具链：apt install gcc-aarch64-linux-gnu
set -e
cd "$(dirname "$0")"
OUT="$(cd .. && pwd)/app/ui/bin"
mkdir -p "$OUT"

echo "== x86_64 =="
gcc -O2 -Wall -Wextra -shared -fPIC -o "$OUT/libgputempfake-x86_64.so" gpufaketemp.c -ldl

echo "== aarch64 =="
if command -v aarch64-linux-gnu-gcc >/dev/null 2>&1; then
    aarch64-linux-gnu-gcc -O2 -Wall -Wextra -shared -fPIC \
        -o "$OUT/libgputempfake-aarch64.so" gpufaketemp.c -ldl
else
    echo "  ⚠ 缺 aarch64-linux-gnu-gcc，跳过 arm64（apt install gcc-aarch64-linux-gnu）"
fi

# 减小体积：跨架构的库要用对应工具链的 strip，本机 strip 剥不了 aarch64
strip --strip-unneeded "$OUT/libgputempfake-x86_64.so" 2>/dev/null || true
if command -v aarch64-linux-gnu-strip >/dev/null 2>&1; then
    aarch64-linux-gnu-strip --strip-unneeded "$OUT/libgputempfake-aarch64.so" 2>/dev/null || true
fi
echo
file "$OUT"/*.so
ls -l "$OUT"/*.so | awk '{print "  ",$5,$9}'
