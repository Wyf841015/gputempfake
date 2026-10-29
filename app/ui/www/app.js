/* GPUTempFake 前端：vanilla JS，无外部依赖（fnOS WebView 里 CDN 不一定可达） */
(function () {
  'use strict';

  var p = location.pathname;
  var i = p.indexOf('index.cgi');
  var BASE = i >= 0 ? p.slice(0, i + 'index.cgi/'.length) : '/cgi/ThirdParty/gputempfake/index.cgi/';

  var $ = function (id) { return document.getElementById(id); };
  var toastTimer = null;
  var uninstallArmed = false;
  var armTimer = null;
  var busy = false;

  /* ── 主题 ── */
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    $('themeBtn').textContent = t === 'dark' ? '☾' : '☀';
    try { localStorage.setItem('gputempfake.theme', t); } catch (e) {}
  }
  function initTheme() {
    var t = 'dark';
    try { t = localStorage.getItem('gputempfake.theme') || 'dark'; } catch (e) {}
    applyTheme(t);
  }

  function toast(msg, kind) {
    var el = $('toast');
    el.textContent = msg;
    el.className = 'toast show ' + (kind || '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = 'toast ' + (kind || ''); }, 4200);
  }

  function api(path, opts) {
    return fetch(BASE + path, opts || {}).then(function (r) {
      return r.text().then(function (txt) {
        var data;
        try { data = JSON.parse(txt); }
        catch (e) { throw new Error('返回不是 JSON：' + txt.slice(0, 120)); }
        if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  function fmtTemp(c) {
    return (c === null || c === undefined || isNaN(c)) ? '—' : c.toFixed(1);
  }

  function pill(text, kind) {
    return '<span class="pill ' + (kind || '') + '">' + text + '</span>';
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* ── 渲染状态 ── */
  function render(d) {
    $('version').textContent = 'v' + d.version;

    var badge = $('stateBadge');
    var title = $('stateTitle');
    var reason = $('stateReason');

    if (!d.applicable) {
      badge.className = 'state-badge warn';
      badge.textContent = '不适用';
      title.textContent = '这台机器不需要接管';
      reason.textContent = d.reason || '';
    } else if (d.state === 'installed' && d.service.lib_loaded) {
      badge.className = 'state-badge on';
      badge.textContent = '接管中';
      title.textContent = 'GPU 温度已由 CPU 温度顶替';
      reason.textContent = '资源监控里的 GPU 温度会显示为 CPU 温度（当前 ' +
        fmtTemp(d.cpu_temp ? d.cpu_temp.celsius : null) + '°C）。';
    } else if (d.state === 'installed') {
      badge.className = 'state-badge warn';
      badge.textContent = '已配置未加载';
      title.textContent = '注入配置在，但进程里没看到拦截库';
      reason.textContent = '可能是 resmon_service 刚重启或加载失败，点一次「一键接管」可重装，或刷新页面再看。';
    } else if (d.state === 'partial') {
      badge.className = 'state-badge bad';
      badge.textContent = '残留';
      title.textContent = '上次接管没清理干净';
      reason.textContent = '有点残留文件。点「一键还原」清干净，再决定是否接管。';
    } else {
      badge.className = 'state-badge off';
      badge.textContent = '未接管';
      title.textContent = '尚未接管';
      reason.textContent = '这台机器没有真实的 GPU 温度传感器。点「一键接管」让资源监控显示 CPU 温度。';
    }

    $('fakeTemp').textContent = d.installed.fake_celsius !== null && d.installed.fake_celsius !== undefined
      ? fmtTemp(d.installed.fake_celsius) : '—';
    $('cpuTemp').textContent = fmtTemp(d.cpu_temp ? d.cpu_temp.celsius : null);

    /* 明细 */
    $('kGpu').innerHTML = (d.gpu_cards && d.gpu_cards.length)
      ? d.gpu_cards.map(function (c) { return pill(esc(c)); }).join(' ')
      : '<span class="muted">未发现 /sys/class/drm/card*</span>';

    $('kSensors').innerHTML = (d.real_gpu_sensors && d.real_gpu_sensors.length)
      ? d.real_gpu_sensors.map(function (s) {
          return pill(esc(s.path) + (s.milli ? ' · ' + fmtTemp(s.milli / 1000) + '°C' : ''), 'ok');
        }).join(' ')
      : '<span class="muted">无（正是接管能派上用场的原因）</span>';

    $('kCpuSrc').innerHTML = d.cpu_temp
      ? pill(esc(d.cpu_temp.source), 'ok') + ' ' + fmtTemp(d.cpu_temp.celsius) + '°C'
      : pill('读不到', 'bad');

    var svcKind = d.service.active === 'active' ? 'ok' : 'bad';
    $('kService').innerHTML = pill(esc(d.service.name) + ' · ' + esc(d.service.active), svcKind) +
      ' <span class="muted">pid ' + d.service.pid + '</span>';

    $('kLoaded').innerHTML = d.service.lib_loaded ? pill('已加载', 'ok') : pill('未加载', '');
    $('kDropin').innerHTML = d.installed.dropin_exists
      ? pill('存在', 'ok') + ' <code>' + esc(d.installed.dropin_path) + '</code>'
      : '<span class="muted">不存在</span>';
    $('kLib').innerHTML = d.installed.lib_exists
      ? pill('存在', 'ok') + ' <code>' + esc(d.installed.lib_path) + '</code>'
      : '<span class="muted">不存在</span>';
    $('kFake').innerHTML = d.installed.fake_exists
      ? pill(fmtTemp(d.installed.fake_celsius) + '°C', 'ok') + ' <code>' + esc(d.installed.fake_path) + '</code>'
      : '<span class="muted">不存在</span>';
    $('kEnv').innerHTML = pill(esc(d.arch)) + ' ' + pill(esc(d.kernel)) + ' ' +
      pill('uid ' + d.uid + ' · ' + esc(d.running_as), d.is_root ? 'ok' : 'bad');

    /* 按钮可用性 */
    var ib = $('installBtn'), ub = $('uninstallBtn');
    ib.disabled = busy || !d.applicable || !d.is_root || d.state === 'installed';
    ub.disabled = busy || !d.is_root || (d.state === 'not_installed' && !d.installed.lib_exists);
    if (!d.applicable) {
      ib.title = d.reason || '';
    } else if (d.state === 'installed') {
      ib.title = '已经在接管中';
      ib.textContent = '已接管';
    } else {
      ib.title = '';
      ib.textContent = '一键接管';
    }
    if (!d.is_root) {
      ib.title = ub.title = '当前 CGI 不是以 root 运行（uid ' + d.uid + '），无法写入系统目录';
    }
  }

  function load() {
    return api('api/status').then(render).catch(function (e) {
      $('stateBadge').className = 'state-badge bad';
      $('stateBadge').textContent = '读取失败';
      $('stateTitle').textContent = '拿不到状态';
      $('stateReason').textContent = e.message;
    });
  }

  function setBusy(v) {
    busy = v;
    $('installBtn').disabled = v;
    $('uninstallBtn').disabled = v;
    if (v) {
      $('installBtn').textContent = '执行中…';
    }
  }

  /* ── 动作 ── */
  function doInstall() {
    if (busy) return;
    setBusy(true);
    toast('正在接管…', '');
    api('api/install', {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (r) {
      toast(r.message || '完成', r.ok && r.loaded ? 'ok' : 'err');
      return load();
    }).catch(function (e) {
      toast('接管失败：' + e.message, 'err');
    }).then(function () { setBusy(false); });
  }

  function armUninstall() {
    if (busy) return;
    var b = $('uninstallBtn');
    if (!uninstallArmed) {
      uninstallArmed = true;
      b.classList.add('arming');
      b.textContent = '确认还原？再点一次';
      clearTimeout(armTimer);
      armTimer = setTimeout(function () {
        uninstallArmed = false;
        b.classList.remove('arming');
        b.textContent = '一键还原';
      }, 6000);
      return;
    }
    clearTimeout(armTimer);
    uninstallArmed = false;
    b.classList.remove('arming');
    b.textContent = '一键还原';
    setBusy(true);
    toast('正在还原…', '');
    api('api/uninstall', {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (r) {
      toast(r.message || '已还原', r.ok && !r.loaded ? 'ok' : 'err');
      return load();
    }).catch(function (e) {
      toast('还原失败：' + e.message, 'err');
    }).then(function () { setBusy(false); });
  }

  function loadLog() {
    api('api/log?n=120').then(function (r) {
      var txt = (r.app || '（暂无应用日志）');
      if (r.trace) txt += '\n\n── trace ──\n' + r.trace;
      $('logBox').textContent = txt;
    }).catch(function (e) {
      $('logBox').textContent = '读取失败：' + e.message;
    });
  }

  /* ── 绑定（不用 inline onclick：fnOS WebView 里 onclick 属性不可靠） ── */
  function bind() {
    $('themeBtn').onclick = function () {
      var cur = document.documentElement.getAttribute('data-theme');
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    };
    $('installBtn').onclick = doInstall;
    $('uninstallBtn').onclick = armUninstall;
    $('logBtn').onclick = loadLog;

    initTheme();
    load();
    /* 页面可见时每 5 秒刷一次；不可见就停，别白耗 */
    setInterval(function () {
      if (document.visibilityState === 'visible' && !busy) load();
    }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
