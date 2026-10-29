# GPU 温度接管（GPUTempFake）

给**内核层就没有 GPU 温度传感器**的机型补一个读数，让 fnOS 资源监控里的「GPU 温度」不再空白。

> 典型机型：Intel Celeron J3355（Apollo Lake）等 i915 核显——CPU 温度有 `coretemp` / `acpitz`，
> GPU 温度在 `/sys/class/drm/card0/device/hwmon` 下**根本不存在**，不是驱动没装。

## 它做了什么

fnOS 的 `resmon_service` 通过 `libhwinfo.so` 用 `glob()` 探测传感器，GPU 温度走的是：

```
/sys/class/drm/card0/device/hwmon/hwmon*/temp*_input
```

本应用把一个小库 `libgputempfake.so` 通过 `LD_PRELOAD` 注入 `resmon_service`，拦截三组函数：

| 拦截 | 行为 |
|---|---|
| `glob()` / `glob64()` | **只**对「GPU 设备 + hwmon + temp」的 pattern 生效；真实匹配 0 个时，返回一个替代文件。其它 pattern 原样透传 |
| `open()` / `open64()` / `fopen()` | 打开替代文件前，先写入当前 CPU 温度（毫摄氏度） |
| `read()` / `pread64()` / `close()` | 跟踪替代文件的 fd，上层若缓存了 fd，也在读之前刷新（200ms 节流） |

替代文件的内容 = **CPU 温度**，所以资源监控里显示的是「用 CPU 温度顶替的 GPU 温度」。

## 安全边界

- **取不到 CPU 温度就不注入**：宁可保持空白，也不给假数据
- **失败不致命**：库缺失/被删时 glibc 只打一行 warning 继续启动，等于回到原始状态；资源监控本身不会挂
- **有真传感器就不让装**：界面检测到真实 GPU 温度节点会判定「不适用」并禁用按钮
- **卸载自动还原**：`uninstall_callback` 删除注入配置 + `daemon-reload` + 重启 `resmon_service`
- **改写只发生在这个文件里**：`/etc/systemd/system/resmon_service.service.d/zz-gputempfake.conf`

## 安装与使用

1. 应用中心安装 `gputempfake.fpk`
2. 打开应用 → 会自动检测本机情况（适用 / 不适用 + 原因）
3. 点「**一键接管**」→ 写入注入配置、重启 `resmon_service`、并**验证库是否真的加载进进程**（不只看命令返回码）
4. 打开 fnOS「资源监控」→ GPU 温度应当显示与 CPU 接近的数值
5. 想撤销：点「**一键还原**」（需点两次确认），系统立刻恢复原样

## 目录与文件

| 路径 | 作用 |
|---|---|
| `<PKGVAR>/lib/libgputempfake.so` | 注入库（按架构从包内选） |
| `<PKGVAR>/fakehwmon/temp1_input` | 替代温度文件（内容实时刷新） |
| `<PKGVAR>/log/app.log` | 接管/还原记录 |
| `<PKGVAR>/log/trace.log` | 调试 trace（默认不写） |
| `/etc/systemd/system/resmon_service.service.d/zz-gputempfake.conf` | 唯一一处系统改动 |

`<PKGVAR>` 默认 `/vol3/@appdata/gputempfake`。

## 已知限制

- **不是真实 GPU 温度**：GPU 满载时 CPU 未必同步升温，只能当「有没有过热」的参考
- **只补温度**：GPU 占用率、频率本来就能读到，不受影响
- **依赖 `resmon_service` 的探测方式**：fnOS 若改成不用 `glob()` 枚举传感器，注入会静默失效（无害，但会显示空白）。届时改钩子即可
- **需要 root**：应用用 `run-as=root` 才能写 `/etc/systemd` 并重启系统服务。自己分发没问题，提交官方应用中心可能被拒

## 从源码构建

```bash
bash src/build-shim.sh      # 编 x86_64 + aarch64（交叉工具链 apt install gcc-aarch64-linux-gnu）
python3 src/make-icons.py   # 重新生成图标
cd . && fnpack build .      # 产物 gputempfake.fpk
```
