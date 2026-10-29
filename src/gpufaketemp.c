/*
 * gpufaketemp.c —— 让不提供 GPU 温度传感器的机型（如 Intel Apollo Lake / J3355 的
 * i915）在 resmon_service 读取 GPU 温度时，拿到 CPU 温度。
 *
 * 为什么需要
 * ----------
 * fnOS 的 resmon_service 通过 libhwinfo.so 用 glob() 在 GPU 设备目录下找温度节点：
 *     %s/hwmon/hwmon-XXX/temp-YYY_input   （星级通配，写成文字避免提前闭合注释）
 *   %s = /sys/class/drm/card0/device  或  /sys/bus/pci/devices/0000:00:02.0
 * 这类机型的 i915 内核层没有 hwmon 温度节点，glob 匹配 0 个 → 上层拿不到温度 → UI 空白。
 *
 * 做法
 * ----
 * LD_PRELOAD 注入 resmon_service，拦截三组函数：
 *   1. glob()/glob64()  —— 只对「GPU 设备 + hwmon + temp」的 pattern 生效；真实匹配为 0 个时，
 *      把一个真实存在的文件当成匹配结果返回。其它 pattern 一律原样透传。
 *   2. open()/open64()/fopen() —— 打开那个假文件前，先写入当前 CPU 温度（毫摄氏度）。
 *   3. read()/pread64()/close() —— 记录假文件的 fd；若上层缓存了 fd（不再重新 open），
 *      在读之前同样刷新（200ms 节流，避免每次都写盘）。
 *
 * 安全阀
 * ------
 * - 取不到 CPU 温度时**不注入**，宁可保持空白也不给一个假值。
 * - 全部内部读写走 raw syscall，绕开被自己 hook 的 libc，不会递归。
 * - 本库被删/路径失效时，glibc 只会打一行 warning 并继续启动 ——
 *   失败模式是「退回没有 GPU 温度」，资源监控本身不会挂。
 *
 * 可调环境变量（安装脚本会写进 systemd drop-in）
 * ---------------------------------------------
 *   HWSHIM_FAKE  假温度文件路径（默认 /run/gputempfake/temp1_input）
 *   HWSHIM_TRACE 打开 trace 日志（设为 1 时记录探测/注入/读取）
 *   HWSHIM_TRACEFILE  trace 输出路径
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <glob.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <stdarg.h>

#define FAKE_DEFAULT     "/run/gputempfake/temp1_input"
#define TRACE_DEFAULT    "/run/gputempfake/trace.log"

static const char *env_or(const char *name, const char *dflt)
{
    const char *v = getenv(name);
    return (v && *v) ? v : dflt;
}
static const char *fake_path(void)
{
    static const char *p = NULL;
    if (!p) p = env_or("HWSHIM_FAKE", FAKE_DEFAULT);
    return p;
}
static const char *trace_path(void)
{
    static const char *p = NULL;
    if (!p) p = env_or("HWSHIM_TRACEFILE", TRACE_DEFAULT);
    return p;
}

/* ---- raw syscall 封装：避开被 hook 的 libc，防递归 ---- */
static int raw_read_file(const char *path, char *buf, size_t n)
{
    int fd = (int)syscall(SYS_openat, AT_FDCWD, path, O_RDONLY);
    if (fd < 0) return -1;
    long r = syscall(SYS_read, fd, buf, n - 1);
    syscall(SYS_close, fd);
    if (r <= 0) return -1;
    buf[r] = '\0';
    return (int)r;
}

static int raw_write_file(const char *path, const char *s)
{
    int fd = (int)syscall(SYS_openat, AT_FDCWD, path,
                          O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;
    long r = syscall(SYS_write, fd, s, strlen(s));
    syscall(SYS_close, fd);
    return r > 0 ? 0 : -1;
}

static long now_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (long)ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* ---- trace（默认关闭；每秒一行会把盘写满）---- */
static int trace_enabled(void)
{
    static int on = -1;
    if (on < 0) {
        const char *e = getenv("HWSHIM_TRACE");
        on = (e && *e && strcmp(e, "0") != 0);
    }
    return on;
}

static void trace(const char *kind, const char *pattern, int rc, long n)
{
    if (!trace_enabled()) return;
    int fd = (int)syscall(SYS_openat, AT_FDCWD, trace_path(),
                          O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) return;
    char line[1024];
    time_t t = time(NULL);
    struct tm tm_;
    localtime_r(&t, &tm_);
    char ts[24];
    strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &tm_);
    int len = snprintf(line, sizeof line, "%s %-8s rc=%d n=%ld  %s\n",
                       ts, kind, rc, n, pattern ? pattern : "(null)");
    if (len > 0) syscall(SYS_write, fd, line, (size_t)len);
    syscall(SYS_close, fd);
}

/* ---- 取 CPU 温度（毫摄氏度）：x86_pkg_temp → acpitz → coretemp Package ---- */
static long cpu_temp_milli(void)
{
    static const char *srcs[] = {
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone1/temp",
        "/sys/class/thermal/thermal_zone2/temp",
    };
    char buf[64];
    for (unsigned i = 0; i < sizeof srcs / sizeof srcs[0]; i++) {
        if (raw_read_file(srcs[i], buf, sizeof buf) > 0) {
            long v = strtol(buf, NULL, 10);
            /* 合理区间：1℃ ~ 200℃，低于 1℃ 的多半是无效值 */
            if (v >= 1000 && v <= 200000) return v;
        }
    }
    /* 再兜一层 hwmon（coretemp / k10temp 等）*/
    glob_t g;
    memset(&g, 0, sizeof g);
    if (glob("/sys/class/hwmon/hwmon*/temp1_input", 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc; i++) {
            if (raw_read_file(g.gl_pathv[i], buf, sizeof buf) > 0) {
                long v = strtol(buf, NULL, 10);
                if (v >= 1000 && v <= 200000) { globfree(&g); return v; }
            }
        }
        globfree(&g);
    }
    return -1;
}

/* 返回 0 表示写成功，<0 表示取不到温度/写不进去 */
static int refresh_fake(void)
{
    long v = cpu_temp_milli();
    if (v <= 0) return -1;
    char buf[32];
    int n = snprintf(buf, sizeof buf, "%ld\n", v);
    if (n <= 0) return -1;
    return raw_write_file(fake_path(), buf);
}

/* 节流刷新：resmon 每秒读一次，200ms 内重复刷新没意义 */
static void refresh_throttled(void)
{
    static long last = 0;
    long t = now_ms();
    if (t - last < 200) return;
    last = t;
    refresh_fake();
}

/* 这个 pattern 是不是在问 GPU 设备的温度节点？ */
static int is_gpu_temp_pattern(const char *p)
{
    if (!p) return 0;
    if (!strstr(p, "hwmon")) return 0;
    if (!strstr(p, "temp")) return 0;
    /* 只认 GPU：i915 iGPU 固定 0000:00:02.0；drm 下的 cardN；以及显式 gpu 关键字 */
    if (strstr(p, "0000:00:02.0")) return 1;
    if (strstr(p, "/drm/")) return 1;
    if (strstr(p, "card0") || strstr(p, "card1") || strstr(p, "card2")) return 1;
    if (strstr(p, "i915")) return 1;
    if (strstr(p, "gpu")) return 1;
    return 0;
}

static int is_fake(const char *p)
{
    return p && strcmp(p, fake_path()) == 0;
}

/* ---- 假文件 fd 跟踪：应对「上层缓存了 fd、不再重新 open」的情况 ---- */
#define MAX_FAKE_FDS 64
static int fake_fds[MAX_FAKE_FDS];
static int fake_fd_n = 0;

static void track_fd(int fd)
{
    if (fd < 0 || fake_fd_n >= MAX_FAKE_FDS) return;
    fake_fds[fake_fd_n++] = fd;
}
static int is_tracked_fd(int fd)
{
    for (int i = 0; i < fake_fd_n; i++)
        if (fake_fds[i] == fd) return 1;
    return 0;
}
static void untrack_fd(int fd)
{
    for (int i = 0; i < fake_fd_n; i++) {
        if (fake_fds[i] == fd) {
            fake_fds[i] = fake_fds[--fake_fd_n];
            return;
        }
    }
}

static int inject(glob_t *pglob)
{
    char **vec = (char **)malloc(sizeof(char *) * 2);
    if (!vec) return 0;
    vec[0] = strdup(fake_path());
    if (!vec[0]) { free(vec); return 0; }
    vec[1] = NULL;
    pglob->gl_pathv = vec;
    pglob->gl_pathc = 1;
    pglob->gl_offs = 0;
    pglob->gl_flags = 0;
    return 1;
}

static int inject64(glob64_t *pglob)
{
    char **vec = (char **)malloc(sizeof(char *) * 2);
    if (!vec) return 0;
    vec[0] = strdup(fake_path());
    if (!vec[0]) { free(vec); return 0; }
    vec[1] = NULL;
    pglob->gl_pathv = vec;
    pglob->gl_pathc = 1;
    pglob->gl_offs = 0;
    pglob->gl_flags = 0;
    return 1;
}

/* ---------- glob / glob64 ---------- */
static int (*real_glob)(const char *, int, int (*)(const char *, int), glob_t *) = NULL;
static int (*real_glob64)(const char *, int, int (*)(const char *, int), glob64_t *) = NULL;

int glob(const char *pattern, int flags,
         int (*errfunc)(const char *, int), glob_t *pglob)
{
    if (!real_glob) real_glob = (int (*)(const char *, int,
                                int (*)(const char *, int), glob_t *))
                                dlsym(RTLD_NEXT, "glob");
    if (!real_glob) return 3;
    int rc = real_glob(pattern, flags, errfunc, pglob);
    int gpu = is_gpu_temp_pattern(pattern);
    long n = pglob ? (long)pglob->gl_pathc : -1;

    if (gpu) trace(pglob && pglob->gl_pathc ? "HIT" : "MISS", pattern, rc, n);
    else if (pattern && strstr(pattern, "hwmon")) trace("probe", pattern, rc, n);

    if (gpu && pglob && pglob->gl_pathc == 0 && !(flags & GLOB_APPEND)) {
        /* 取不到 CPU 温度就不注入 —— 宁可空白，也不给假数据 */
        if (refresh_fake() == 0 && inject(pglob)) {
            trace("INJECT", pattern, 0, 1);
            return 0;
        }
        trace("NOINJECT", pattern, rc, n);
    }
    return rc;
}

int glob64(const char *pattern, int flags,
           int (*errfunc)(const char *, int), glob64_t *pglob)
{
    if (!real_glob64) real_glob64 = (int (*)(const char *, int,
                                  int (*)(const char *, int), glob64_t *))
                                  dlsym(RTLD_NEXT, "glob64");
    if (!real_glob64) return 3;
    int rc = real_glob64(pattern, flags, errfunc, pglob);
    int gpu = is_gpu_temp_pattern(pattern);
    long n = pglob ? (long)pglob->gl_pathc : -1;

    if (gpu) trace(pglob && pglob->gl_pathc ? "HIT64" : "MISS64", pattern, rc, n);

    if (gpu && pglob && pglob->gl_pathc == 0 && !(flags & GLOB_APPEND)) {
        if (refresh_fake() == 0 && inject64(pglob)) {
            trace("INJECT64", pattern, 0, 1);
            return 0;
        }
    }
    return rc;
}

/* ---------- open / open64 / fopen：打开假文件前刷新 ---------- */
static int  (*real_open)(const char *, int, ...) = NULL;
static int  (*real_open64)(const char *, int, ...) = NULL;
static FILE *(*real_fopen)(const char *, const char *) = NULL;
static int  (*real_close)(int) = NULL;
static ssize_t (*real_read)(int, void *, size_t) = NULL;
static ssize_t (*real_pread64)(int, void *, size_t, off_t) = NULL;

int open(const char *path, int flags, ...)
{
    if (!real_open) real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
    int fake = is_fake(path);
    if (fake) refresh_throttled();
    mode_t m = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap);
    }
    int fd = real_open(path, flags, m);
    if (fake) { track_fd(fd); trace("OPEN", path, 0, fd); }
    return fd;
}

int open64(const char *path, int flags, ...)
{
    if (!real_open64) real_open64 = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open64");
    int fake = is_fake(path);
    if (fake) refresh_throttled();
    mode_t m = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap);
    }
    int fd = real_open64(path, flags, m);
    if (fake) { track_fd(fd); trace("OPEN64", path, 0, fd); }
    return fd;
}

FILE *fopen(const char *path, const char *mode)
{
    if (!real_fopen) real_fopen = (FILE *(*)(const char *, const char *))dlsym(RTLD_NEXT, "fopen");
    if (is_fake(path)) { refresh_throttled(); trace("FOPEN", path, 0, 0); }
    return real_fopen(path, mode);
}

int close(int fd)
{
    if (!real_close) real_close = (int (*)(int))dlsym(RTLD_NEXT, "close");
    if (is_tracked_fd(fd)) untrack_fd(fd);
    return real_close(fd);
}

/* 上层若缓存了 fd，在读之前补一次刷新 */
ssize_t read(int fd, void *buf, size_t n)
{
    if (!real_read) real_read = (ssize_t (*)(int, void *, size_t))dlsym(RTLD_NEXT, "read");
    if (is_tracked_fd(fd)) refresh_throttled();
    return real_read(fd, buf, n);
}

ssize_t pread64(int fd, void *buf, size_t n, off_t off)
{
    if (!real_pread64) real_pread64 = (ssize_t (*)(int, void *, size_t, off_t))dlsym(RTLD_NEXT, "pread64");
    if (is_tracked_fd(fd)) refresh_throttled();
    return real_pread64(fd, buf, n, off);
}
