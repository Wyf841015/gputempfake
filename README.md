# GPU 温度接管（GPUTempFake）

给**内核层就没有 GPU 温度传感器**的机型补一个读数，让 fnOS「资源监控」里的「GPU 温度」不再空白。

> 典型机型：Intel Celeron J3355（Apollo Lake）这类 i915 核显。
> 这类机器 CPU 温度齐全（`coretemp` / `acpitz` / `x86_pkg_temp`），
> 但 `/sys/class/drm/card0/device/hwmon` 下**根本没有温度节点** ——
> 不是驱动没装，是内核层面就不导出。

![界面](Preview/screenshot_overview.png)

## 它到底做了什么

fnOS 的 `resmon_service` 通过 `libhwinfo.so` 用 `glob()` 探测传感器，GPU 温度走的是：

```
/sys/class/drm/card0/device/hwmon/hwmon*/temp*_input
```

本应用把一个 20KB 的拦截库 `libgputempfake.so` 通过 `LD_PRELOAD` 注入 `resmon_service`，拦截三组函数：

| 拦截 | 行为 |
|---|---|
| `glob()` / `glob64()` | **只**对「GPU 设备 + hwmon + temp」这种 pattern 生效；真实匹配 0 个时，返回一个替代文件把结果补上。其它 pattern 全部原样透传 |
| `open()` / `open64()` / `fopen()` | 打开替代文件前，先把当前 CPU 温度（毫摄氏度）写进去 |
| `read()` / `pread64()` / `close()` | 跟踪替代文件的 fd —— 万一上层缓存了 fd、不再重新 open，也在读之前补一次刷新（200ms 节流） |

替代文件的内容 = **CPU 温度**，所以资源监控里显示的是「用 CPU 温度顶替的 GPU 温度」。

## 一句话：会不会影响系统

| 动作 | 影响 |
|---|---|
| **装 fpk** | 一个 nologin 系统用户 + 约 280KB 目录。不监听端口、不装内核模块、不改启动项、不联网 |
| **点「一键接管」** | 新增 **1 个** systemd drop-in 文件 + `systemctl restart resmon_service`（1~2 秒，只影响资源监控页） |
| **点「一键还原」** | 删掉那个 drop-in + 重启服务，恢复原样 |
| **卸载应用** | 自动撤掉注入并重启服务，不留悬挂的 `LD_PRELOAD` |
| **注入失败** | glibc 只打一行 warning 继续启动 —— 失败模式是「GPU 温度回到空白」，**不会**把资源监控搞挂 |

存储、共享、Docker、相册、媒体库等其它服务都不受影响（不碰内核、不碰驱动、不碰网络）。

## 安全边界

- **取不到 CPU 温度就不注入**：宁可保持空白，也不给一个假数字
- **真实传感器优先**：本机若已经有 GPU 温度节点，界面判定「不适用」并禁用按钮 —— 那种情况下接管只会把真值盖掉
- **写入范围只有一处**：`/etc/systemd/system/resmon_service.service.d/zz-gputempfake.conf`
- **还原是真删**：删除 drop-in → `daemon-reload` → 重启服务 → 删库和替代文件，并**核验库是否真从进程内存消失**（不是只看命令返回码）
- **变更接口要 token**：`/api/status` 下发随机 token，`install`/`uninstall` 必须带 token + POST，防跨站触发
- **root 动作有兜底通道**：CGI 若被降权运行，动作转由 `cmd/main` 以 root 拉起的 `rootd.sh` 执行，只认 `install` / `uninstall` 两个固定动作，不接受任意命令

## 安装与使用

1. 应用中心安装 `gputempfake.fpk`（自带 x86_64 / aarch64 两个库，按架构自动选）
2. 打开应用 → 自动检测本机情况，顶部显示「接管中 / 未接管 / 不适用」+ 原因
3. 点「**一键接管**」→ 写注入配置、重启 `resmon_service`，并**验证库是否真的加载进进程**
4. 打开 fnOS「资源监控」→ GPU 温度会显示与 CPU 接近的数值
5. 想撤销：点「**一键还原**」（**需要点两次**，第一次会提示确认）

界面里还有一个「本机检测结果」表：GPU 设备、真实传感器、CPU 温度来源、服务状态、库是否加载、注入配置与文件路径 —— 出问题先看这张表。

## 目录与文件

| 路径 | 作用 |
|---|---|
| `<PKGVAR>/lib/libgputempfake.so` | 拦截库（安装时按架构从包内选） |
| `<PKGVAR>/fakehwmon/temp1_input` | 替代温度文件（内容实时刷新） |
| `<PKGVAR>/log/app.log` | 接管/还原记录 |
| `<PKGVAR>/log/requests.log` | 每次 Web 请求留痕（uid / 方法 / token 有无），排查「点了没反应」用 |
| `<PKGVAR>/log/rootd.log` | root 后台助手的执行记录 |
| `<PKGVAR>/log/trace.log` | 拦截库调试 trace（默认关闭） |
| `/etc/systemd/system/resmon_service.service.d/zz-gputempfake.conf` | **唯一一处系统改动** |

`<PKGVAR>` 默认 `/vol3/@appdata/gputempfake`。日志都有 256KB 上限，不会无限长。

## 已知限制

- **显示的不是真实 GPU 温度**：GPU 满载时 CPU 未必同步升温，只能当「有没有过热」的参考
- **只补温度**：GPU 占用率、频率本来就能读到，不受影响
- **依赖 `resmon_service` 的探测方式**：fnOS 若改成不用 `glob()` 枚举传感器，注入会静默失效（无害，但会显示空白）。届时改钩子即可
- **需要 root**：应用用 `run-as=root` 才能写 `/etc/systemd` 并重启系统服务。自己分发没问题，提交官方应用中心可能被拒
- **arm64 只能保证编得出来**：交叉编译通过、能正常加载，但没有 ARM 真机验证过

## 常见问题

**Q：点「一键还原」没反应？**
需要点两次（防误触），第一次点会有提示「再点一次按钮确认还原（6 秒内有效）」。如果两次都没反应，去 `<PKGVAR>/log/requests.log` 看请求有没有到、`uid` 是多少。

**Q：卸载后想彻底清干净？**
fnOS 卸载应用通常不删对应的系统用户。需要的话手动执行 `sudo userdel -r gputempfake`。

**Q：重装新版本没生效？**
fnOS 对已安装应用执行 `install-fpk` **不会替换** target 下的文件。请在 Web UI 里先卸载，再装新的 fpk。

**Q：接管后 GPU 温度一直不变？**
资源监控是按需读取的 —— 打开那个页面时才会去读传感器。不打开时替代文件的 mtime 不动，属正常。

## 从源码构建

```bash
bash src/build-shim.sh      # 编 x86_64 + aarch64 拦截库
                            # 交叉工具链：apt install gcc-aarch64-linux-gnu
python3 src/make-icons.py   # 重新生成图标
fnpack build .              # 产物 gputempfake.fpk（manifest 所在目录执行）
```

## 版本历史

### v0.1.2 (2026-10-29)

- 修「一键还原」点了没反应：不再假设 CGI 以 root 运行，新增 root 后台助手兜底；
  变更接口从「强制自定义请求头」改为 token 校验；按钮可用性按「root 或助手在跑」判断；
  还原的两次确认补了提示；每次请求写入 requests.log
- 手机端状态卡片一大块空白：`.hero-left` 的 `flex: 1 1 300px` 在纵向布局下变成高度基准，
  手机断点内收回；两个温度块标签统一两行高度、数字对齐；左侧补上 °C 单位
- 明细表长路径不再拦腰断字：`table-layout: fixed` + 单元格内单行横向滑动、超出右侧渐隐
- 头部新增赞助按钮（在主题按钮之前），支付宝/微信赞赏码可点开放大
- 请求日志与 rootd 日志加 256KB 上限

### v0.1.1 (2026-10-29)

- 修 CPU 温度来源显示为空（`$(...)` 子 shell 丢全局赋值）
- 只带 `wizard/uninstall`（wizard 文件不合法会被 fnOS 判 `code 10111`）

### v0.1.0 (2026-10-29)

- 首个版本：LD_PRELOAD 注入 `resmon_service`，用 CPU 温度顶替 GPU 温度
- 双架构拦截库（x86_64 / aarch64）、纯 bash CGI 零运行时依赖
- 适用性自动判定、一键接管 / 一键还原、卸载自动清理
