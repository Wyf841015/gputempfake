#!/bin/bash
#
# GPUTempFake CGI 入口
#
# 路由（基址 /cgi/ThirdParty/gputempfake/index.cgi/）：
#   /                     → index.html
#   /static/<file>        → www 下的静态文件
#   /api/status           → JSON：硬件探测 + 接管状态
#   /api/install          → POST，一键接管
#   /api/uninstall        → POST，一键还原
#   /api/log              → 最近 N 行运行日志
#
# 说明：
# - 无常驻进程，所有动作都在这一个脚本里按请求执行；fvOS 的 trim_http_cgi 以 root 拉起，
#   配合 config/privilege 的 run-as=root，可以直接写 /etc/systemd 并重启 resmon_service。
# - 变更类接口（install/uninstall）要求 POST + X-Requested-With 头 + 同源 Referer，
#   避免被第三方页面用 <img>/<form> 跨站触发（这是 root 级操作，不能只靠「点到了」）。

set -u

APPDEST="${TRIM_APPDEST:-/var/apps/gputempfake/target}"
APPVAR="${TRIM_PKGVAR:-/vol3/@appdata/gputempfake}"
WWW="$APPDEST/ui/www"
BINDIR="$APPDEST/ui/bin"
VAR="${GPUTF_VAR:-$APPVAR}"
LIB="$VAR/lib/libgputempfake.so"
FAKE="$VAR/fakehwmon/temp1_input"
LOGDIR="$VAR/log"
APPLOG="$LOGDIR/app.log"
STATEF="$LOGDIR/state.conf"
DROPIN_DIR=/etc/systemd/system/resmon_service.service.d
DROPIN="$DROPIN_DIR/zz-gputempfake.conf"
SERVICE=resmon_service
VERSION="0.1.1"

mkdir -p "$LOGDIR" 2>/dev/null

# ─────────────────────────── 工具函数 ───────────────────────────
json_esc() {
    # 转义进 JSON 字符串：反斜杠、双引号、控制字符
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/ }
    s=${s//$'\r'/ }
    s=${s//$'\t'/ }
    printf '%s' "$s"
}

send_json() {
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '%s\n' "$1"
}

send_redirect() {
    printf 'Status: 302 Found\r\n'
    printf 'Location: %s\r\n\r\n' "$1"
}

send_file() {
    local f=$1
    [ -f "$f" ] || { printf 'Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nnot found\n'; return; }
    case "$f" in
        *.html) ct='text/html; charset=utf-8' ;;
        *.css)  ct='text/css; charset=utf-8' ;;
        *.js)   ct='application/javascript; charset=utf-8' ;;
        *.json) ct='application/json; charset=utf-8' ;;
        *.png)  ct='image/png' ;;
        *.svg)  ct='image/svg+xml' ;;
        *)      ct='application/octet-stream' ;;
    esac
    printf 'Content-Type: %s\r\n' "$ct"
    printf 'Cache-Control: no-cache\r\n'
    printf '\r\n'
    cat "$f"
}

# 读一个温度文件，返回毫摄氏度（无效值返回空）
read_temp() {
    local f=$1 v
    [ -r "$f" ] || return 1
    v=$(head -c 32 "$f" 2>/dev/null | tr -dc '0-9-')
    [ -n "$v" ] || return 1
    if [ "$v" -ge 1000 ] 2>/dev/null && [ "$v" -le 200000 ] 2>/dev/null; then
        printf '%s' "$v"
    else
        return 1
    fi
}

# CPU 温度探测：结果写进全局 CPU_T_MILLI / CPU_T_SRC
# 注意：不能用 $(cpu_temp) 取返回值的写法 —— 命令替换是子 shell，
# 里面赋的 CPU_SRC 传不出来（上一版就踩了这个坑，source 一直是空）。
CPU_T_MILLI=""
CPU_T_SRC=""
cpu_temp_probe() {
    CPU_T_MILLI=""
    CPU_T_SRC=""
    local z v
    for z in /sys/class/thermal/thermal_zone*/temp; do
        [ -r "$z" ] || continue
        v=$(read_temp "$z") || continue
        CPU_T_MILLI="$v"
        CPU_T_SRC="$z"
        return 0
    done
    for z in /sys/class/hwmon/hwmon*/temp1_input; do
        [ -r "$z" ] || continue
        v=$(read_temp "$z") || continue
        CPU_T_MILLI="$v"
        CPU_T_SRC="$z"
        return 0
    done
    return 1
}

# 真实存在的 GPU 温度传感器（有就说明这台机器不需要接管）
real_gpu_sensors() {
    local f
    for f in /sys/class/drm/card*/device/hwmon/hwmon*/temp*_input; do
        [ -e "$f" ] && printf '%s\n' "$f"
    done
    # 独显走 PCI sysfs 的情况
    local dev cls
    for dev in /sys/bus/pci/devices/*; do
        [ -r "$dev/class" ] || continue
        cls=$(cat "$dev/class" 2>/dev/null)
        case "$cls" in
            0x03*)
                for f in "$dev"/hwmon/hwmon*/temp*_input; do
                    [ -e "$f" ] && printf '%s\n' "$f"
                done
                ;;
        esac
    done
}

gpu_cards() {
    # 只取真正的显卡节点 cardN；card0-DP-1 / card0-HDMI-A-1 是显示连接器，不是 GPU
    local c b
    for c in /sys/class/drm/card[0-9]*; do
        [ -e "$c" ] || continue
        b=$(basename "$c")
        case "$b" in
            card[0-9]|card[0-9][0-9]) printf '%s\n' "$b" ;;
        esac
    done
}

resmon_pid() {
    pgrep -x resmon_service 2>/dev/null | head -1
}

resmon_active() {
    systemctl is-active "$SERVICE" 2>/dev/null | tr -d '\n'
}

lib_in_resmon() {
    local pid flag
    pid=$(resmon_pid)
    [ -n "$pid" ] || { printf '0'; return; }
    flag=0
    if grep -q gputempfake "/proc/$pid/maps" 2>/dev/null; then
        flag=1
    else
        # 普通用户读不到 /proc/<root进程>/maps，用 sudo 兜底（本身就是 root 时不需要）
        if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
            sudo -n grep -q gputempfake "/proc/$pid/maps" 2>/dev/null && flag=1
        fi
    fi
    printf '%s' "$flag"
}

sha_of() {
    [ -f "$1" ] || { printf ''; return; }
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
}

pick_lib() {
    case "$(uname -m)" in
        x86_64)  printf '%s' "$BINDIR/libgputempfake-x86_64.so" ;;
        aarch64) printf '%s' "$BINDIR/libgputempfake-aarch64.so" ;;
        *)       printf '' ;;
    esac
}

# 变更类接口的同源校验
guard_mutation() {
    if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
        send_json '{"ok":false,"error":"该操作只接受 POST"}'
        return 1
    fi
    local xhr="${HTTP_X_REQUESTED_WITH:-}"
    if [ "$xhr" != "XMLHttpRequest" ]; then
        send_json '{"ok":false,"error":"缺少 X-Requested-With 头，拒绝执行（防跨站触发）"}'
        return 1
    fi
    local ref="${HTTP_REFERER:-}"
    local host="${HTTP_HOST:-}"
    if [ -n "$ref" ] && [ -n "$host" ]; then
        case "$ref" in
            "http://$host/"*|"https://$host/"*) : ;;
            *)
                send_json "$(printf '{"ok":false,"error":"Referer 与站点不一致，拒绝执行"}')"
                return 1
                ;;
        esac
    fi
    return 0
}

# ─────────────────────────── 动作：接管 ───────────────────────────
do_install() {
    local err="" lib src
    [ "$(id -u)" = "0" ] || err="CGI 不是以 root 运行（当前 uid=$(id -u)），无法写 /etc/systemd"
    if [ -z "$err" ]; then
        src=$(pick_lib)
        [ -n "$src" ] && [ -f "$src" ] || err="找不到架构 $(uname -m) 对应的库文件：$src"
    fi
    if [ -z "$err" ]; then
        [ -f "/etc/systemd/system/$SERVICE.service" ] || [ -f "/usr/trim/bin/$SERVICE" ] || err="本机没有 $SERVICE，接管无意义"
    fi
    if [ -z "$err" ]; then
        cpu_temp_probe || err="读不到任何 CPU 温度，接管后只会显示假数据，已中止"
    fi
    if [ -n "$err" ]; then
        send_json "$(printf '{"ok":false,"error":"%s"}' "$(json_esc "$err")")"
        return
    fi

    mkdir -p "$VAR/lib" "$VAR/fakehwmon" "$LOGDIR" || true
    chmod 755 "$VAR" "$VAR/lib" "$VAR/fakehwmon" 2>/dev/null

    install -m 755 -o root -g root "$src" "$LIB" || {
        send_json '{"ok":false,"error":"复制库文件失败"}'
        return
    }

    # 预置一个当前值，避免接管瞬间读到空文件
    cpu_temp_probe && printf '%s\n' "$CPU_T_MILLI" > "$FAKE"
    chmod 644 "$FAKE" 2>/dev/null

    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN" <<EOF
# 由 GPUTempFake 生成 —— 卸载应用或在界面点「一键还原」会自动删除本文件
[Service]
Environment=LD_PRELOAD=$LIB
Environment=HWSHIM_FAKE=$FAKE
Environment=HWSHIM_TRACEFILE=$LOGDIR/trace.log
EOF
    chmod 644 "$DROPIN"

    systemctl daemon-reload >/dev/null 2>&1
    systemctl restart "$SERVICE" >/dev/null 2>&1

    # 等它起来再验证，别急着报成功
    local i loaded=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.5
        loaded=$(lib_in_resmon)
        [ "$loaded" = "1" ] && break
    done

    {
        echo "installed_at=$(date '+%F %T')"
        echo "arch=$(uname -m)"
        echo "lib=$LIB"
        echo "lib_sha256=$(sha_of "$LIB")"
        echo "loaded=$loaded"
    } > "$STATEF"

    echo "[$(date '+%F %T')] install ok arch=$(uname -m) loaded=$loaded lib=$LIB" >> "$APPLOG"

    if [ "$loaded" = "1" ]; then
        send_json '{"ok":true,"loaded":true,"message":"接管完成，resmon_service 已加载拦截库"}'
    else
        send_json '{"ok":true,"loaded":false,"message":"已写入注入配置并重启服务，但没在进程内存里看到库；请查看状态页或服务日志"}'
    fi
}

# ─────────────────────────── 动作：还原 ───────────────────────────
do_uninstall() {
    [ "$(id -u)" = "0" ] || {
        send_json "$(printf '{"ok":false,"error":"%s"}' "$(json_esc "CGI 不是以 root 运行（uid=$(id -u)）")")"
        return
    }
    local removed=0
    if [ -f "$DROPIN" ]; then
        rm -f "$DROPIN"
        removed=1
    fi
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload >/dev/null 2>&1
    systemctl restart "$SERVICE" >/dev/null 2>&1

    sleep 1
    local i loaded=1
    for i in 1 2 3 4 5 6 7 8 9 10; do
        loaded=$(lib_in_resmon)
        [ "$loaded" = "0" ] && break
        sleep 0.5
    done

    rm -f "$LIB" "$FAKE" 2>/dev/null
    rm -rf "$VAR/fakehwmon" 2>/dev/null
    echo "[$(date '+%F %T')] uninstall removed_dropin=$removed loaded_after=$loaded" >> "$APPLOG"
    printf 'uninstalled_at=%s\n' "$(date '+%F %T')" > "$STATEF"

    send_json "$(printf '{"ok":true,"loaded":%s,"removed_dropin":%s,"message":"%s"}' \
        "$([ "$loaded" = "1" ] && echo true || echo false)" \
        "$([ "$removed" = "1" ] && echo true || echo false)" \
        "$([ "$loaded" = "1" ] && echo "注入配置已删除，但进程里仍看到库，请稍后刷新确认" || echo "已还原为系统原始状态")")"
}

# ─────────────────────────── 状态 ───────────────────────────
do_status() {
    local cpu_json="" tmp
    cpu_temp_probe
    if [ -n "$CPU_T_MILLI" ]; then
        cpu_json=$(printf '{"milli":%s,"celsius":%s,"source":"%s"}' \
            "$CPU_T_MILLI" "$(awk "BEGIN{printf \"%.1f\", $CPU_T_MILLI/1000}")" "$(json_esc "$CPU_T_SRC")")
    else
        cpu_json='null'
    fi

    # 真实 GPU 传感器列表
    local sensors_json="" n=0 f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        tmp=$(read_temp "$f") || tmp=""
        [ "$n" -gt 0 ] && sensors_json="$sensors_json,"
        sensors_json="$sensors_json$(printf '{"path":"%s","milli":%s}' "$(json_esc "$f")" "${tmp:-null}")"
        n=$((n + 1))
    done <<< "$(real_gpu_sensors)"

    # GPU 设备列表
    local cards="" cards_json="" c m=0
    while IFS= read -r c; do
        [ -n "$c" ] || continue
        [ "$m" -gt 0 ] && cards_json="$cards_json,"
        cards_json="$cards_json\"$(json_esc "$c")\""
        m=$((m + 1))
    done <<< "$(gpu_cards)"

    local dropin_exists=false lib_exists=false fake_exists=false
    [ -f "$DROPIN" ] && dropin_exists=true
    [ -f "$LIB" ] && lib_exists=true
    [ -f "$FAKE" ] && fake_exists=true

    local fake_val="" fake_c=""
    if [ -f "$FAKE" ]; then
        fake_val=$(head -c 32 "$FAKE" 2>/dev/null | tr -dc '0-9')
        [ -n "$fake_val" ] && fake_c=$(awk "BEGIN{printf \"%.1f\", $fake_val/1000}")
    fi

    local pid loaded service_active
    pid=$(resmon_pid)
    loaded=$(lib_in_resmon)
    service_active=$(resmon_active)

    # 适用性判定
    local applicable=true reason="" state="not_installed"
    if [ ! -f "/etc/systemd/system/$SERVICE.service" ] && [ ! -f "/usr/trim/bin/$SERVICE" ]; then
        applicable=false
        reason="本机没有 $SERVICE（不是 fnOS 或版本差异），接管无从下手"
    elif [ -n "$sensors_json" ]; then
        applicable=false
        reason="本机已有真实 GPU 温度传感器（$n 个），接管反而会把真值覆盖成 CPU 温度，因此禁用"
    elif [ -z "$cpu_json" ] || [ "$cpu_json" = "null" ]; then
        applicable=false
        reason="读不到任何 CPU 温度，接管后没有可用的替代值"
    elif [ -z "$(pick_lib)" ]; then
        applicable=false
        reason="当前架构 $(uname -m) 没有对应的拦截库"
    fi
    if [ "$dropin_exists" = true ] && [ "$lib_exists" = true ]; then
        state="installed"
    elif [ "$dropin_exists" = true ] || [ "$lib_exists" = true ] || [ "$fake_exists" = true ]; then
        state="partial"
    fi

    local statefile=""
    [ -f "$STATEF" ] && statefile=$(tr '\n' ' ' < "$STATEF" | sed 's/ /  /g')

    send_json "$(cat <<JSON
{
  "ok": true,
  "version": "$VERSION",
  "arch": "$(uname -m)",
  "kernel": "$(uname -r)",
  "running_as": "$(id -un 2>/dev/null)",
  "uid": $(id -u),
  "is_root": $([ "$(id -u)" = "0" ] && echo true || echo false),
  "applicable": $applicable,
  "reason": "$(json_esc "$reason")",
  "state": "$state",
  "gpu_cards": [$cards_json],
  "real_gpu_sensors": [$sensors_json],
  "cpu_temp": $cpu_json,
  "installed": {
    "dropin_exists": $dropin_exists,
    "dropin_path": "$(json_esc "$DROPIN")",
    "lib_exists": $lib_exists,
    "lib_path": "$(json_esc "$LIB")",
    "lib_sha256": "$(sha_of "$LIB")",
    "fake_exists": $fake_exists,
    "fake_path": "$(json_esc "$FAKE")",
    "fake_milli": ${fake_val:-null},
    "fake_celsius": ${fake_c:-null}
  },
  "service": {
    "name": "$SERVICE",
    "active": "$(json_esc "$service_active")",
    "pid": "${pid:-0}",
    "lib_loaded": $([ "$loaded" = "1" ] && echo true || echo false)
  },
  "state_file": "$(json_esc "$statefile")"
}
JSON
)"
}

do_log() {
    local n="${QUERY_N:-80}"
    local body=""
    [ -f "$APPLOG" ] && body=$(tail -n "$n" "$APPLOG" 2>/dev/null)
    local trace=""
    [ -f "$LOGDIR/trace.log" ] && trace=$(tail -n "$n" "$LOGDIR/trace.log" 2>/dev/null)
    send_json "$(printf '{"ok":true,"app":"%s","trace":"%s"}' "$(json_esc "$body")" "$(json_esc "$trace")")"
}

# ─────────────────────────── 路由 ───────────────────────────
URI_NO_QUERY="${REQUEST_URI:-}"
QUERY="${URI_NO_QUERY#*\?}"
[ "$QUERY" = "$URI_NO_QUERY" ] && QUERY=""
URI_NO_QUERY="${URI_NO_QUERY%%\?*}"
QUERY_N=$(printf '%s' "$QUERY" | tr '&' '\n' | sed -n 's/^n=//p' | head -1)

REL_PATH="${URI_NO_QUERY#*index.cgi}"
REL_PATH="${REL_PATH#/}"
[ -z "$REL_PATH" ] && REL_PATH="index.html"

case "$REL_PATH" in
    api/status)
        do_status
        ;;
    api/install)
        guard_mutation && do_install
        ;;
    api/uninstall)
        guard_mutation && do_uninstall
        ;;
    api/log)
        do_log
        ;;
    static/*)
        send_file "$WWW/${REL_PATH#static/}"
        ;;
    index.html|"")
        send_file "$WWW/index.html"
        ;;
    *)
        # 兜底：当作 www 下的文件
        send_file "$WWW/$REL_PATH"
        ;;
esac
exit 0
