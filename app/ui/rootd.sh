#!/bin/bash
#
# rootd.sh —— root 后台助手
#
# 由 cmd/main start 以 root 拉起（应用 config/privilege 是 run-as=root，
# 生命周期脚本实测以 root 运行）。CGI 若本身就是 root 则不会用到这里；
# 一旦 trim_http_cgi 把 CGI 降到非 root，前台就把请求写成文件丢进 req/，
# 这里执行动作并把结果写进 res/，前台轮询取结果。
#
# 请求文件：<VAR>/req/<id>.req     内容：action=install|uninstall
# 结果文件：<VAR>/res/<id>.json
#
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

. "$APPDEST/ui/lib/actions.sh"

mkdir -p "$LOGDIR" "$REQ" "$RES" 2>/dev/null
chmod 700 "$REQ" "$RES" 2>/dev/null

# 单实例：抢不到锁就直接退出（避免多个 rootd 同时处理同一个请求）
exec 9>"$LOGDIR/rootd.lock" || exit 1
flock -n 9 || { echo "[$(date '+%F %T')] another rootd holds the lock, exit" >> "$LOGDIR/rootd.log"; exit 0; }

if [ "$(id -u)" != "0" ]; then
    echo "[$(date '+%F %T')] rootd 不是 root（uid=$(id -u)），无法执行系统级动作" >> "$LOGDIR/rootd.log"
    exit 1
fi

echo "$$" > "$LOGDIR/rootd.pid" 2>/dev/null
echo "[$(date '+%F %T')] rootd started pid=$$" >> "$LOGDIR/rootd.log"

cleanup() {
    rm -f "$LOGDIR/rootd.pid" 2>/dev/null
    echo "[$(date '+%F %T')] rootd stopped pid=$$" >> "$LOGDIR/rootd.log"
    exit 0
}
trap cleanup TERM INT

while :; do
    for f in "$REQ"/*.req; do
        [ -e "$f" ] || continue
        id=$(basename "$f" .req)
        action=$(sed -n 's/^action=//p' "$f" 2>/dev/null | head -1)
        echo "[$(date '+%F %T')] req $id action=$action" >> "$LOGDIR/rootd.log"

        out=""
        case "$action" in
            install)   out=$(action_install) ;;
            uninstall) out=$(action_uninstall) ;;
            *)         out='{"ok":false,"error":"未知动作"}' ;;
        esac
        [ -n "$out" ] || out='{"ok":false,"error":"动作没有返回结果"}'

        # 先写结果再删请求：前台晚一秒读到也没关系，反过来会丢结果
        printf '%s\n' "$out" > "$RES/$id.json.tmp" && mv -f "$RES/$id.json.tmp" "$RES/$id.json"
        rm -f "$f"
    done

    # 顺手清掉超过 10 分钟的结果文件，别无限堆
    find "$RES" -name '*.json' -mmin +10 -delete 2>/dev/null
    sleep 0.3
done
