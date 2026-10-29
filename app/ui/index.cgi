#!/bin/bash
#
# GPUTempFake CGI 入口
#
# 路由（基址 /cgi/ThirdParty/gputempfake/index.cgi/）：
#   /                  → index.html
#   /static/<file>     → www 下的静态文件
#   /api/status        → JSON：硬件探测 + 接管状态（含变更接口要用的 token）
#   /api/install?t=…   → POST，一键接管
#   /api/uninstall?t=… → POST，一键还原
#   /api/log           → 最近日志
#
# 执行身份：本脚本可能以 root、也可能以普通用户被 trim_http_cgi 拉起，所以不假设：
#   - root    → 直接执行系统动作
#   - 非 root → 写请求文件交给 rootd.sh（由 cmd/main 以 root 拉起）
#
# 防跨站：用 token（从 /api/status 取），不强制自定义头 ——
# 自定义头经 trim_http_cgi 是否透传无法确认，早期版本死等 X-Requested-With
# 就是「按钮点了没反应」的嫌疑来源。token 只有同源页面读得到，够用。

set -u

APPDEST="${TRIM_APPDEST:-/var/apps/gputempfake/target}"
VAR="${TRIM_PKGVAR:-/vol3/@appdata/gputempfake}"
SERVICE=resmon_service
LIB="$VAR/lib/libgputempfake.so"
FAKE="$VAR/fakehwmon/temp1_input"
LOGDIR="$VAR/log"
BINDIR="$APPDEST/ui/bin"
DROPIN=/etc/systemd/system/resmon_service.service.d/zz-gputempfake.conf
REQ="$VAR/req"
RES="$VAR/res"
WWW="$APPDEST/ui/www"
VERSION="0.1.0"

mkdir -p "$LOGDIR" "$REQ" "$RES" 2>/dev/null

[ -r "$APPDEST/ui/lib/actions.sh" ] && . "$APPDEST/ui/lib/actions.sh"

# ───────────────────────── 输出 ─────────────────────────
send_json() {
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '%s\n' "$1"
}

send_file() {
    local f=$1
    if [ ! -f "$f" ]; then
        printf 'Status: 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nnot found\n'
        return
    fi
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

# ───────────────────────── token ─────────────────────────
TOKENFILE="$LOGDIR/token"
ensure_token() {
    if [ ! -s "$TOKENFILE" ]; then
        head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKENFILE" 2>/dev/null
        # 644：CGI 有可能以非 root 运行，得读得到 token。它不是密钥，
        # 只是防跨站的随机串（同源页面才拿得到），本地可读没有额外风险。
        chmod 644 "$TOKENFILE" 2>/dev/null
    fi
    tr -d '\n' < "$TOKENFILE" 2>/dev/null
}

# ───────────────────────── 请求留痕 ─────────────────────────
# 页面每 5 秒轮询一次 status，不设上限的话这个文件会一直长：
# 每小时约 720 行（90KB），开着一周就是十几 MB。超过 256KB 只留最后 200 行。
LOG_MAX_BYTES=262144
cap_log() {
    local f=$1 sz
    [ -f "$f" ] || return 0
    sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
    [ "$sz" -gt "$LOG_MAX_BYTES" ] || return 0
    tail -n 200 "$f" > "$f.tmp" 2>/dev/null && mv -f "$f.tmp" "$f"
}

log_req() {
    cap_log "$LOGDIR/requests.log"
    echo "[$(date '+%F %T')] ${REQUEST_METHOD:-GET} ${REL_PATH:-?} uid=$(id -u) user=$(id -un 2>/dev/null) token=$1 xhr=${HTTP_X_REQUESTED_WITH:-none} ref=${HTTP_REFERER:-none}" \
        >> "$LOGDIR/requests.log" 2>/dev/null
}

# ───────────────────────── 动作派发 ─────────────────────────
rootd_pid() {
    local f="$LOGDIR/rootd.pid" p
    [ -s "$f" ] || return 1
    p=$(tr -dc '0-9' < "$f" 2>/dev/null)
    [ -n "$p" ] || return 1
    [ -r "/proc/$p/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q rootd.sh || return 1
    printf '%s' "$p"
}

dispatch_action() {
    local action=$1 id i out=""

    if [ "$(id -u)" = "0" ]; then
        case "$action" in
            install)   action_install ;;
            uninstall) action_uninstall ;;
            *)         res_fail "未知动作 $action" ;;
        esac
        return
    fi

    if ! rootd_pid >/dev/null; then
        res_fail "当前 CGI 以 $(id -un)（uid $(id -u)）运行，且后台助手没在跑。请到应用中心停止再启动一次本应用后重试"
        return
    fi

    id=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N)
    printf 'action=%s\n' "$action" > "$REQ/$id.req"
    for i in $(seq 1 120); do
        if [ -f "$RES/$id.json" ]; then
            out=$(cat "$RES/$id.json" 2>/dev/null)
            rm -f "$RES/$id.json"
            break
        fi
        sleep 0.5
    done
    if [ -z "$out" ]; then
        rm -f "$REQ/$id.req" 2>/dev/null
        res_fail "后台助手 60 秒内没返回结果，请看运行日志"
    else
        printf '%s' "$out"
    fi
}

# ───────────────────────── 状态 ─────────────────────────
do_status() {
    local cpu_json="" tmp n=0 f sensors_json="" cards_json="" c m=0
    local dropin_exists=false lib_exists=false fake_exists=false
    local fake_val="" fake_c="" pid loaded service_active
    local applicable=true reason="" state="not_installed" statefile="" rootd=false tok

    cpu_temp_probe
    if [ -n "$CPU_T_MILLI" ]; then
        cpu_json=$(printf '{"milli":%s,"celsius":%s,"source":"%s"}' \
            "$CPU_T_MILLI" "$(awk "BEGIN{printf \"%.1f\", $CPU_T_MILLI/1000}")" "$(json_esc "$CPU_T_SRC")")
    else
        cpu_json='null'
    fi

    while IFS= read -r f; do
        [ -n "$f" ] || continue
        tmp=$(read_temp "$f") || tmp=""
        [ "$n" -gt 0 ] && sensors_json="$sensors_json,"
        sensors_json="$sensors_json$(printf '{"path":"%s","milli":%s}' "$(json_esc "$f")" "${tmp:-null}")"
        n=$((n + 1))
    done <<< "$(real_gpu_sensors)"

    while IFS= read -r c; do
        [ -n "$c" ] || continue
        [ "$m" -gt 0 ] && cards_json="$cards_json,"
        cards_json="$cards_json\"$(json_esc "$c")\""
        m=$((m + 1))
    done <<< "$(gpu_cards)"

    [ -f "$DROPIN" ] && dropin_exists=true
    [ -f "$LIB" ] && lib_exists=true
    [ -f "$FAKE" ] && fake_exists=true
    if [ -f "$FAKE" ]; then
        fake_val=$(head -c 32 "$FAKE" 2>/dev/null | tr -dc '0-9')
        [ -n "$fake_val" ] && fake_c=$(awk "BEGIN{printf \"%.1f\", $fake_val/1000}")
    fi

    pid=$(resmon_pid)
    loaded=$(lib_in_resmon "$pid")
    service_active=$(resmon_active)
    rootd_pid >/dev/null && rootd=true
    tok=$(ensure_token)

    if [ ! -f "/etc/systemd/system/$SERVICE.service" ] && [ ! -f "/usr/trim/bin/$SERVICE" ]; then
        applicable=false; reason="本机没有 $SERVICE（不是 fnOS 或版本差异），接管无从下手"
    elif [ -n "$sensors_json" ]; then
        applicable=false; reason="本机已有真实 GPU 温度传感器（$n 个），接管会把真值覆盖成 CPU 温度，因此禁用"
    elif [ -z "$cpu_json" ] || [ "$cpu_json" = "null" ]; then
        applicable=false; reason="读不到任何 CPU 温度，接管后没有可用的替代值"
    elif [ -z "$(pick_lib)" ]; then
        applicable=false; reason="当前架构 $(uname -m) 没有对应的拦截库"
    fi

    if [ "$dropin_exists" = true ] && [ "$lib_exists" = true ]; then
        state="installed"
    elif [ "$dropin_exists" = true ] || [ "$lib_exists" = true ] || [ "$fake_exists" = true ]; then
        state="partial"
    fi

    [ -f "$LOGDIR/state.conf" ] && statefile=$(tr '\n' ' ' < "$LOGDIR/state.conf" | sed 's/  / /g')

    send_json "$(cat <<JSON
{
  "ok": true,
  "version": "$VERSION",
  "token": "$tok",
  "arch": "$(uname -m)",
  "kernel": "$(uname -r)",
  "running_as": "$(id -un 2>/dev/null)",
  "uid": $(id -u),
  "is_root": $([ "$(id -u)" = "0" ] && echo true || echo false),
  "rootd_running": $rootd,
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
    local n="${QUERY_N:-80}" body="" trace="" rl=""
    [ -f "$LOGDIR/app.log" ] && body=$(tail -n "$n" "$LOGDIR/app.log" 2>/dev/null)
    [ -f "$LOGDIR/trace.log" ] && trace=$(tail -n "$n" "$LOGDIR/trace.log" 2>/dev/null)
    [ -f "$LOGDIR/requests.log" ] && rl=$(tail -n 20 "$LOGDIR/requests.log" 2>/dev/null)
    [ -f "$LOGDIR/rootd.log" ] && rl="$rl"$'\n'"$(tail -n 10 "$LOGDIR/rootd.log" 2>/dev/null)"
    send_json "$(printf '{"ok":true,"app":"%s","trace":"%s","requests":"%s"}' \
        "$(json_esc "$body")" "$(json_esc "$trace")" "$(json_esc "$rl")")"
}

# ───────────────────────── 守卫：token ─────────────────────────
query_token() { printf '%s' "$QUERY" | tr '&' '\n' | sed -n 's/^t=//p' | head -1; }

guard_mutation() {
    local expect t
    if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
        send_json "$(res_fail "该操作只接受 POST（收到 ${REQUEST_METHOD:-GET}）")"
        return 1
    fi
    expect=$(ensure_token)
    t=$(query_token)
    if [ -z "$t" ] || [ "$t" != "$expect" ]; then
        send_json "$(res_fail "令牌校验失败（页面可能过期，刷新页面后重试）")"
        return 1
    fi
    return 0
}

# ───────────────────────── 路由 ─────────────────────────
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
        log_req "n/a"
        do_status
        ;;
    api/install)
        log_req "$([ -n "$(query_token)" ] && echo present || echo missing)"
        guard_mutation && send_json "$(dispatch_action install)"
        ;;
    api/uninstall)
        log_req "$([ -n "$(query_token)" ] && echo present || echo missing)"
        guard_mutation && send_json "$(dispatch_action uninstall)"
        ;;
    api/log)
        log_req "n/a"
        do_log
        ;;
    static/*)
        send_file "$WWW/${REL_PATH#static/}"
        ;;
    index.html|"")
        send_file "$WWW/index.html"
        ;;
    *)
        send_file "$WWW/$REL_PATH"
        ;;
esac
exit 0
