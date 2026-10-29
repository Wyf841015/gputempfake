#!/bin/bash
#
# 共享动作库：被 index.cgi（前台）和 rootd.sh（root 后台助手）共用
#
# 为什么要拆出后台助手：
#   trim_http_cgi 拉起 CGI 时到底用什么 uid，实测无法从外部确认（直接 sudo 跑
#   只能证明「我是 root 跑的」，不能证明它）。一旦它把 CGI 降到非 root，
#   写 /etc/systemd + 重启系统服务就全废。所以这里不赌：
#     - CGI 是 root   → 直接执行动作
#     - CGI 不是 root → 写请求文件，由 cmd/main 以 root 拉起的 rootd.sh 执行
#   失败模式的代价只是一次 IPC，不是「按钮点了没反应」。
#
# 依赖外部设置：APPDEST VAR LIB FAKE LOGDIR DROPIN BINDIR SERVICE

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

# 结果写进全局 CPU_T_MILLI / CPU_T_SRC（不能用 $(...) 取，子 shell 会丢赋值）
CPU_T_MILLI=""
CPU_T_SRC=""
cpu_temp_probe() {
    CPU_T_MILLI=""
    CPU_T_SRC=""
    local z v
    for z in /sys/class/thermal/thermal_zone*/temp; do
        [ -r "$z" ] || continue
        v=$(read_temp "$z") || continue
        CPU_T_MILLI="$v"; CPU_T_SRC="$z"; return 0
    done
    for z in /sys/class/hwmon/hwmon*/temp1_input; do
        [ -r "$z" ] || continue
        v=$(read_temp "$z") || continue
        CPU_T_MILLI="$v"; CPU_T_SRC="$z"; return 0
    done
    return 1
}

real_gpu_sensors() {
    local f dev cls
    for f in /sys/class/drm/card*/device/hwmon/hwmon*/temp*_input; do
        [ -e "$f" ] && printf '%s\n' "$f"
    done
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
    # cardN 才是显卡；card0-DP-1 / card0-HDMI-A-1 是显示连接器
    local c b
    for c in /sys/class/drm/card[0-9]*; do
        [ -e "$c" ] || continue
        b=$(basename "$c")
        case "$b" in
            card[0-9]|card[0-9][0-9]) printf '%s\n' "$b" ;;
        esac
    done
}

resmon_pid() { pgrep -x "$SERVICE" 2>/dev/null | head -1; }
resmon_active() { systemctl is-active "$SERVICE" 2>/dev/null | tr -d '\n'; }

lib_in_resmon() {
    local pid=$1
    [ -n "$pid" ] || pid=$(resmon_pid)
    [ -n "$pid" ] || { printf '0'; return; }
    if grep -q gputempfake "/proc/$pid/maps" 2>/dev/null; then printf '1'; return; fi
    if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
        sudo -n grep -q gputempfake "/proc/$pid/maps" 2>/dev/null && { printf '1'; return; }
    fi
    printf '0'
}

sha_of() { [ -f "$1" ] && sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

pick_lib() {
    case "$(uname -m)" in
        x86_64)  printf '%s' "$BINDIR/libgputempfake-x86_64.so" ;;
        aarch64) printf '%s' "$BINDIR/libgputempfake-aarch64.so" ;;
        *)       printf '' ;;
    esac
}

json_esc() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/ }
    s=${s//$'\r'/ }
    s=${s//$'\t'/ }
    printf '%s' "$s"
}

res_ok()   { printf '{"ok":true,%s}' "$1"; }
res_fail() { printf '{"ok":false,"error":"%s"}' "$(json_esc "$1")"; }

# ─────────────────── 动作：接管（必须 root）───────────────────
action_install() {
    local err="" src pid_before pid_after loaded=0 i

    [ "$(id -u)" = "0" ] || { res_fail "执行身份是 $(id -un)（uid $(id -u)），不是 root，写不了 /etc/systemd"; return; }
    [ -n "$(pick_lib)" ] && [ -f "$(pick_lib)" ] || { res_fail "找不到架构 $(uname -m) 对应的拦截库"; return; }
    src=$(pick_lib)
    [ -f "/etc/systemd/system/$SERVICE.service" ] || [ -f "/usr/trim/bin/$SERVICE" ] || { res_fail "本机没有 $SERVICE"; return; }
    cpu_temp_probe || { res_fail "读不到任何 CPU 温度，接管后只会显示假数据，已中止"; return; }

    mkdir -p "$VAR/lib" "$VAR/fakehwmon" "$LOGDIR" 2>/dev/null
    chmod 755 "$VAR" "$VAR/lib" "$VAR/fakehwmon" 2>/dev/null

    install -m 755 -o root -g root "$src" "$LIB" || { res_fail "复制库文件失败"; return; }
    printf '%s\n' "$CPU_T_MILLI" > "$FAKE"
    chmod 644 "$FAKE" 2>/dev/null

    mkdir -p "$(dirname "$DROPIN")"
    cat > "$DROPIN" <<EOF
# 由 GPUTempFake 生成 —— 卸载应用或在界面点「一键还原」会自动删除本文件
[Service]
Environment=LD_PRELOAD=$LIB
Environment=HWSHIM_FAKE=$FAKE
Environment=HWSHIM_TRACEFILE=$LOGDIR/trace.log
EOF
    chmod 644 "$DROPIN"

    pid_before=$(resmon_pid)
    systemctl daemon-reload >/dev/null 2>&1
    systemctl restart "$SERVICE" >/dev/null 2>&1
    pid_after=$(resmon_pid)

    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        sleep 0.5
        loaded=$(lib_in_resmon "$pid_after")
        [ "$loaded" = "1" ] && break
    done

    printf 'installed_at=%s\narch=%s\nlib=%s\nlib_sha256=%s\nloaded=%s\n' \
        "$(date '+%F %T')" "$(uname -m)" "$LIB" "$(sha_of "$LIB")" "$loaded" > "$LOGDIR/state.conf"
    echo "[$(date '+%F %T')] install ok by=$(id -un) loaded=$loaded pid=${pid_before}->${pid_after}" >> "$LOGDIR/app.log"

    if [ "$loaded" = "1" ]; then
        printf '{"ok":true,"loaded":true,"message":"接管完成，%s 已加载拦截库"}' "$SERVICE"
    else
        printf '{"ok":true,"loaded":false,"message":"已写入注入并重启服务，但进程内存里没看到库；稍等几秒刷新，或查看服务状态"}'
    fi
}

# ─────────────────── 动作：还原（必须 root）───────────────────
action_uninstall() {
    local removed=0 loaded=1 i pid dir

    [ "$(id -u)" = "0" ] || { res_fail "执行身份是 $(id -un)（uid $(id -u)），不是 root，删不了 /etc/systemd"; return; }

    dir=$(dirname "$DROPIN")
    if [ -f "$DROPIN" ]; then rm -f "$DROPIN"; removed=1; fi
    # 只在目录空的时候才删，别动别的应用的 drop-in
    rmdir "$dir" 2>/dev/null || true

    systemctl daemon-reload >/dev/null 2>&1
    systemctl restart "$SERVICE" >/dev/null 2>&1

    pid=$(resmon_pid)
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        sleep 0.5
        loaded=$(lib_in_resmon "$pid")
        [ "$loaded" = "0" ] && break
    done

    rm -f "$LIB" "$FAKE" 2>/dev/null
    rm -rf "$VAR/fakehwmon" 2>/dev/null
    printf 'uninstalled_at=%s\n' "$(date '+%F %T')" > "$LOGDIR/state.conf"
    echo "[$(date '+%F %T')] uninstall ok by=$(id -un) removed_dropin=$removed loaded_after=$loaded" >> "$LOGDIR/app.log"

    if [ "$loaded" = "1" ]; then
        printf '{"ok":true,"loaded":true,"removed_dropin":%s,"message":"注入配置已删除，但进程里仍看到库；请稍后刷新确认"}' \
            "$([ "$removed" = "1" ] && echo true || echo false)"
    else
        printf '{"ok":true,"loaded":false,"removed_dropin":%s,"message":"已还原为系统原始状态"}' \
            "$([ "$removed" = "1" ] && echo true || echo false)"
    fi
}
