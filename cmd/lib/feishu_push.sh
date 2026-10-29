#!/bin/bash
### 安装/升级统计推送公共库（gputempfake）
###
### 用法：
###   source "$(dirname "${BASH_SOURCE[0]}")/lib/feishu_push.sh"
###   feishu_push_install "gputempfake" "${TRIM_APPVER}"
###   feishu_push_upgrade "gputempfake" "${TRIM_OLD_APPVER}" "${TRIM_APPVER}"
###
### 推送策略（两种机器行为不同）：
###   · 普通机器：**每次安装、每次升级都推送一条**（不去重）
###   · 开发机：同一事件只推一次，避免开发阶段反复装卸把表格刷满
###     开发机由标记文件识别（打包时不带任何机器标识）：
###       /etc/gputempfake-dev-mode
###       /var/cache/.gputempfake_stats/dev-dedupe
###      环境变量 GPUTEMPFAKE_DEV_DEDUPE=1 也可以强制打开
###     开发机账本 /var/cache/.gputempfake_stats/pushed 记：install / upgrade:<版本号>
###   · 推送失败不记账，下次还能重试
###
### 开关（调试用）：
###   FEISHU_PUSH_DRY=1        只打印不联网
###   FEISHU_PUSH_SYNC=1       前台同步执行（默认走后台，不拖慢安装）
###   GPUTEMPFAKE_STATS_DIR=…  改状态目录（测试用，默认 /var/cache/.gputempfake_stats）
###   GPUTEMPFAKE_DEV_DEDUPE=1 强制按开发机去重

FEISHU_APP_ID="cli_aa9c4a61e8789bc9"
FEISHU_APP_SECRET="1JjTelglJK23XY2KXCiztge5TKBMQISg"
FEISHU_APP_TOKEN="JXxibJOYcaC7ZFsuKmecTwibnjf"
FEISHU_TABLE_ID="tbl1kmuwQu2CToeN"

# 路径延迟解析：每个函数调用时再取，改 GPUTEMPFAKE_STATS_DIR 立即生效（也方便测试）
_fs_state_dir() { printf '%s' "${GPUTEMPFAKE_STATS_DIR:-/var/cache/.gputempfake_stats}"; }
_fs_ledger()    { printf '%s/pushed' "$(_fs_state_dir)"; }
_fs_uuid_file() { printf '%s/device-uuid' "$(_fs_state_dir)"; }
_fs_log_dir()   { printf '%s/log' "${TRIM_PKGVAR:-}"; }

_fs_log() {
    local line="[$(_fs_now)] $*"
    local d; d=$(_fs_log_dir)
    if [ -n "${TRIM_PKGVAR:-}" ] && [ -d "${d}" ]; then
        printf '%s\n' "${line}" >> "${d}/app.log" 2>/dev/null
    fi
    printf '%s\n' "${line}" >&2
}

_fs_now() { date '+%F %T'; }

# ── 设备稳定 UUID ─────────────────────────────────────────────
_fs_device_uuid() {
    local raw=""

    if [ -r /etc/machine-id ]; then
        raw=$(tr -d '[:space:]' < /etc/machine-id 2>/dev/null)
    fi

    if [ -z "${raw}" ] || [ "${#raw}" -lt 16 ]; then
        raw=$(grep -m1 'Serial' /proc/cpuinfo 2>/dev/null | awk '{print $3}' | tr -d '[:space:]')
    fi

    if [ -z "${raw}" ] || [ "${#raw}" -lt 16 ]; then
        local d uf; d=$(_fs_state_dir); uf=$(_fs_uuid_file)
        mkdir -p "${d}" 2>/dev/null
        if [ -f "${uf}" ]; then
            raw=$(tr -d '[:space:]' < "${uf}" 2>/dev/null)
        else
            raw=$(cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '[:space:]-')
            printf '%s' "${raw}" > "${uf}" 2>/dev/null && \
                chmod 644 "${uf}" 2>/dev/null
        fi
    fi

    raw=$(printf '%-32s' "${raw}" | tr ' ' '0')
    printf '%s-%s-%s-%s-%s' \
        "${raw:0:8}" "${raw:8:4}" "${raw:12:4}" "${raw:16:4}" "${raw:20:12}"
}

# ── 去重账本（仅开发机使用）─────────────────────────────────
# 普通机器直接返回 1（不去重）→ 每次安装/升级都推一条
_fs_dev_dedupe() {
    [ "${GPUTEMPFAKE_DEV_DEDUPE:-0}" = "1" ] && return 0
    [ -f /etc/gputempfake-dev-mode ] && return 0
    [ -f "$(_fs_state_dir)/dev-dedupe" ] && return 0
    return 1
}

_fs_seen() {
    local key="$1"
    local l; l=$(_fs_ledger)
    [ -f "${l}" ] || return 1
    grep -qxF "${key}" "${l}" 2>/dev/null
}

_fs_mark() {
    local key="$1"
    local d l; d=$(_fs_state_dir); l=$(_fs_ledger)
    mkdir -p "${d}" 2>/dev/null
    if command -v flock >/dev/null 2>&1; then
        # 同一个锁文件串行化，避免 install/upgrade 同时跑时双写
        (
            flock -w 5 9 || exit 0
            _fs_seen "${key}" || printf '%s\n' "${key}" >> "${l}"
            chmod 644 "${l}" 2>/dev/null
        ) 9>"${d}/.lock"
    else
        _fs_seen "${key}" || printf '%s\n' "${key}" >> "${l}"
    fi
}

# ── 网络部分 ─────────────────────────────────────────────────
_fs_ip_location() {
    local resp city region country
    resp=$(curl -s --connect-timeout 4 --max-time 6 "https://ipinfo.io/json" 2>/dev/null)
    city=$(printf '%s' "${resp}"    | grep -o '"city"[^,]*'    | cut -d'"' -f4)
    region=$(printf '%s' "${resp}"  | grep -o '"region"[^,]*'  | cut -d'"' -f4)
    country=$(printf '%s' "${resp}" | grep -o '"country"[^,]*' | cut -d'"' -f4)
    printf '%s %s %s' "${city}" "${region}" "${country}"
}

_fs_token() {
    local resp
    resp=$(curl -s --connect-timeout 4 --max-time 8 \
        -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
        -H "Content-Type: application/json" \
        -d "{\"app_id\":\"${FEISHU_APP_ID}\",\"app_secret\":\"${FEISHU_APP_SECRET}\"}" 2>/dev/null)
    printf '%s' "${resp}" | grep -o '"tenant_access_token":"[^"]*"' | cut -d'"' -f4
}

# 把字符串转成 JSON 安全形式（只处理我们实际会用到的字符）
_fs_esc() {
    local s="$1"
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '%s' "${s}"
}

# ── 主流程 ───────────────────────────────────────────────────
# _fs_push <app> <ver> <oldver> <status> <dedupe-key>
_fs_push() {
    local app="$1" ver="$2" oldver="$3" status="$4" key="$5"
    local uuid tok loc payload fields dedupe=0

    # 只有开发机做去重；普通机器每次安装/升级都真推
    if _fs_dev_dedupe; then
        dedupe=1
        if _fs_seen "${key}"; then
            _fs_log "[stats] ${key} 已推送过（开发机去重），跳过"
            return 0
        fi
    fi

    uuid=$(_fs_device_uuid)

    # 应用名称 / 版本号 / 旧版本号 / 安装状态 在多维表里都是「单选」字段：
    # 空值不能传空字符串（会被静默丢弃），首次安装时整个 旧版本号 都不传。
    fields="\"ID\": \"$( _fs_esc "${uuid}")\""
    fields="${fields}, \"应用名称\": \"$(_fs_esc "${app}")\""
    fields="${fields}, \"版本号\": \"$(_fs_esc "${ver}")\""
    fields="${fields}, \"安装日期\": \"$(_fs_esc "$(date '+%Y年%-m月%-d日 %H:%M')")\""
    fields="${fields}, \"安装状态\": \"$(_fs_esc "${status}")\""
    if [ -n "${oldver}" ]; then
        fields="${fields}, \"旧版本号\": \"$(_fs_esc "${oldver}")\""
    fi
    payload="{\"fields\": {${fields}}}"

    if [ "${FEISHU_PUSH_DRY:-0}" = "1" ]; then
        loc="(dry-run 不查询)"
    else
        loc=$(_fs_ip_location)
        if [ -n "${loc// /}" ]; then
            fields="${fields}, \"安装位置\": \"$(_fs_esc "${loc}")\""
            payload="{\"fields\": {${fields}}}"
        fi
    fi

    if [ "${FEISHU_PUSH_DRY:-0}" = "1" ]; then
        _fs_log "[stats] DRY ${key} → ${payload}"
        return 0
    fi

    tok=$(_fs_token)
    if [ -z "${tok}" ]; then
        _fs_log "[stats] 取 token 失败，${key} 不记账，下次可重试"
        return 1
    fi

    local resp code
    resp=$(curl -s --connect-timeout 4 --max-time 10 \
        -X POST "https://open.feishu.cn/open-apis/bitable/v1/apps/${FEISHU_APP_TOKEN}/tables/${FEISHU_TABLE_ID}/records" \
        -H "Authorization: Bearer ${tok}" \
        -H "Content-Type: application/json" \
        -d "${payload}" 2>/dev/null)
    code=$(printf '%s' "${resp}" | grep -o '"code":[0-9-]*' | head -1 | cut -d: -f2)

    if [ "${code}" = "0" ]; then
        [ "${dedupe}" = "1" ] && _fs_mark "${key}"
        _fs_log "[stats] ${key} 推送成功 (${app} ${ver})"
        return 0
    fi

    _fs_log "[stats] ${key} 推送失败 code=${code:-无响应}，不记账，下次可重试"
    return 1
}

# 后台执行，不拖慢安装/升级（fnOS 对回调有时长限制）
_fs_spawn() {
    local app="$1" ver="$2" oldver="$3" status="$4" key="$5"
    if [ "${FEISHU_PUSH_SYNC:-0}" = "1" ]; then
        _fs_push "${app}" "${ver}" "${oldver}" "${status}" "${key}"
        return $?
    fi
    local self="${BASH_SOURCE[0]}"
    setsid bash -c "
        source '${self}' >/dev/null 2>&1
        TRIM_PKGVAR='${TRIM_PKGVAR:-}'
        _fs_push '${app}' '${ver}' '${oldver}' '${status}' '${key}'
    " </dev/null >/dev/null 2>&1 &
}

# 首次安装：本机只推一次
feishu_push_install() {
    local app="$1" ver="$2"
    _fs_spawn "${app}" "${ver}" "" "首次安装成功" "install"
}

# 升级：每个版本只推一次
feishu_push_upgrade() {
    local app="$1" oldver="$2" newver="$3"
    [ -z "${newver}" ] && return 0
    _fs_spawn "${app}" "${newver}" "${oldver}" "升级成功" "upgrade:${newver}"
}
