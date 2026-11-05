(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let signature = '';
  let busy = false;
  let timer;
  let hasData = false;
  let runtimeReady = window.parent === window;
  let latestExportsReady = false;
  let dataState = 'waiting';
  let downloadBusy = false;
  let downloadNoticeKind = '';
  const downloadNotice = document.createElement('p');
  downloadNotice.className = 'download-feedback';
  downloadNotice.setAttribute('role', 'status');
  downloadNotice.hidden = true;
  $('export-status').closest('.exports-panel').append(downloadNotice);
  function showDownloadNotice(message, kind = '') {
    downloadNoticeKind = kind;
    downloadNotice.textContent = message;
    downloadNotice.className = 'download-feedback' + (kind ? ' is-' + kind : '');
    downloadNotice.hidden = !message;
  }
  function downloads(ready, message) {
    const enabled = ready && runtimeReady && !downloadBusy;
    document.querySelectorAll('[data-file]').forEach(link => {
      if (enabled) { link.href = link.dataset.file; link.removeAttribute('tabindex'); link.setAttribute('aria-disabled', 'false'); link.title = '下载本轮实际生成的文件'; }
      else { link.removeAttribute('href'); link.tabIndex = -1; link.setAttribute('aria-disabled', 'true'); link.title = message || '当前文件暂不可下载'; }
    });
    $('export-status').textContent = downloadBusy ? '正在下载，请稍候' : message || (enabled ? '当前版本已生成' : '完成分析后可下载');
  }
  async function readDashboard() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/dashboard', {cache:'no-store', signal:controller.signal});
      if (!response.ok) throw new Error('API unavailable');
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  function chartLayout() {
    return {
      margin:{t:8,b:40,l:47,r:8}, paper_bgcolor:'transparent', plot_bgcolor:'transparent',
      font:{family:'-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif',size:10,color:'#71717a'},
      xaxis:{showgrid:false,zeroline:false,tickfont:{size:10},fixedrange:false},
      yaxis:{gridcolor:'#f0f0f2',zeroline:false,tickfont:{size:9},rangemode:'tozero'},
      showlegend:false, hoverlabel:{bgcolor:'#27272a',bordercolor:'#27272a',font:{color:'#fff',size:11}}
    };
  }
  function showWaiting() {
    if (hasData || signature) {
      if (window.Plotly && typeof Plotly.purge === 'function') {
        Plotly.purge('category');
        Plotly.purge('trend');
      } else {
        $('category').replaceChildren();
        $('trend').replaceChildren();
      }
    }
    signature = '';
    hasData = false;
    latestExportsReady = false;
    dataState = 'waiting';
    if (!downloadBusy) showDownloadNotice('');
    $('version').textContent = '待发布';
    $('version').className = 'badge';
    $('title').textContent = '等待第一轮分析';
    $('description').textContent = 'Qoder 执行分析后，数据、图表和导出文件会在这里自动更新。';
    ['rows','amount','categories'].forEach(id => { $(id).textContent = '—'; });
    $('category-note').textContent = '独立核算后发布';
    $('identity').textContent = '分析结果尚未发布';
    $('category').hidden = true;
    $('trend').hidden = true;
    $('chart-empty').hidden = false;
    $('trend-empty').hidden = false;
    downloads(false, '完成分析后可下载');
  }
  async function render(data) {
    if (!Number.isFinite(data.row_count) || !Number.isFinite(data.total_amount) || !data.category_totals || !data.daily_totals) throw new Error('Invalid dashboard');
    hasData = true;
    $('version').textContent = '版本 ' + data.revision + ' · 已核算';
    $('version').className = 'badge badge-success';
    $('title').textContent = data.title || '报表分析';
    $('description').textContent = (data.filters && data.filters.exclude_categories && data.filters.exclude_categories.length ? '已排除：' + data.filters.exclude_categories.join('、') : '全部分类') + ' · 结果来自分析 Sandbox 的实际文件';
    $('rows').textContent = data.row_count.toLocaleString('zh-CN');
    $('amount').textContent = data.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
    $('categories').textContent = Object.keys(data.category_totals).length;
    $('category-note').textContent = Object.prototype.hasOwnProperty.call(data.category_totals, '现场演示') ? '绿色标记现场新增分类' : '鼠标悬停查看金额';
    const nextSignature = String(data.revision) + ':' + (data.input_sha256 || '');
    if (signature !== nextSignature) {
      if (!window.Plotly) throw new Error('Chart library unavailable');
      $('chart-empty').hidden = true;
      $('trend-empty').hidden = true;
      $('category').hidden = false;
      $('trend').hidden = false;
      const config = {responsive:true, displayModeBar:false, locale:'zh-CN'};
      await Promise.all([
        Plotly.react('category', [{type:'bar',x:Object.keys(data.category_totals),y:Object.values(data.category_totals),marker:{color:Object.keys(data.category_totals).map(name => name === '现场演示' ? '#10b981' : '#52525b')},hovertemplate:'%{x}<br>金额 %{y:,.2f}<extra></extra>'}], chartLayout(), config),
        Plotly.react('trend', [{type:'scatter',mode:'lines+markers',x:Object.keys(data.daily_totals),y:Object.values(data.daily_totals),line:{color:'#3f3f46',width:2},marker:{size:4,color:'#3f3f46'},hovertemplate:'%{x}<br>金额 %{y:,.2f}<extra></extra>'}], {...chartLayout(),margin:{t:8,b:38,l:47,r:8},xaxis:{...chartLayout().xaxis,nticks:6,type:'date',tickformat:'%m/%d'}}, config)
      ]);
      signature = nextSignature;
    }
    const identity = data.identity || {};
    $('identity').textContent = '分析实例 ' + (identity.analysis_sandbox_id || '—') + '\n浏览器实例 ' + (identity.browser_sandbox_id || '—') + '\n输入 SHA-256 ' + (data.input_sha256 || '—');
    dataState = 'ready';
    latestExportsReady = data.exports_ready !== false;
    if (latestExportsReady && downloadNoticeKind === 'pending') showDownloadNotice('');
    downloads(latestExportsReady, runtimeReady ? (latestExportsReady ? '当前版本已生成' : '文件生成中，请稍候') : '执行环境不可用，下载已暂停');
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    clearTimeout(timer);
    try {
      const data = await readDashboard();
      if (data.waiting) {
        showWaiting();
      } else {
        await render(data);
      }
      $('dashboard-error').hidden = true;
    } catch (_) {
      latestExportsReady = false;
      dataState = 'error';
      $('dashboard-error').hidden = false;
      $('dashboard-error-text').textContent = hasData ? '暂时无法更新。当前显示最近一次结果，正在重试。' : '暂时无法读取分析结果，请检查执行环境。';
      $('version').textContent = hasData ? '连接中断 · 保留结果' : '等待连接';
      $('version').className = 'badge badge-warning';
      downloads(false, '连接恢复后可下载');
    } finally { busy = false; timer = setTimeout(refresh, 1800); }
  }
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== window.parent || !event.data) return;
    if (event.data.type === 'demo-resize') {
      if (signature && window.Plotly && Plotly.Plots && typeof Plotly.Plots.resize === 'function') {
        Plotly.Plots.resize('category');
        Plotly.Plots.resize('trend');
      }
      return;
    }
    if (event.data.type !== 'demo-runtime') return;
    runtimeReady = ['ready','legacy'].includes(event.data.state);
    const availableMessage = dataState === 'error' ? '连接恢复后可下载' : dataState === 'waiting' ? '完成分析后可下载' : latestExportsReady ? '当前版本已生成' : '文件生成中，请稍候';
    downloads(latestExportsReady, runtimeReady ? availableMessage : (event.data.state === 'expired' ? '本轮已到期，请重新创建环境' : '执行环境不可用，下载已暂停'));
  });
  document.querySelectorAll('[data-file]').forEach(link => {
    link.addEventListener('click', async event => {
      event.preventDefault();
      if (downloadBusy || !runtimeReady || !latestExportsReady || link.getAttribute('aria-disabled') === 'true') return;
      const allowed = ['/files/data.csv', '/files/analysis.xlsx', '/files/report.html', '/files/analysis.json'];
      const path = link.dataset.file;
      if (!allowed.includes(path)) return;
      const filename = path.slice(path.lastIndexOf('/') + 1);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      downloadBusy = true;
      link.setAttribute('aria-busy', 'true');
      showDownloadNotice('正在取得 ' + filename + '，页面会保留当前分析结果。');
      downloads(false);
      try {
        const response = await fetch(path, {cache:'no-store', signal:controller.signal});
        if (response.status === 409) {
          latestExportsReady = false;
          showDownloadNotice('文件仍在生成，请稍后重试。生成完成后下载入口会自动恢复。', 'pending');
          refresh();
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const blob = await response.blob();
        if (!blob.size) throw new Error('Empty file');
        const url = URL.createObjectURL(blob);
        const downloadDocument = window.parent === window ? document : window.parent.document;
        const anchor = downloadDocument.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        anchor.hidden = true;
        downloadDocument.body.append(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        showDownloadNotice('已下载 ' + filename + '。');
      } catch (_) {
        latestExportsReady = false;
        dataState = 'error';
        showDownloadNotice('下载失败，请检查连接后重试。当前页面与分析结果已保留。', 'error');
        refresh();
      } finally {
        clearTimeout(timeout);
        downloadBusy = false;
        link.removeAttribute('aria-busy');
        const message = dataState === 'error' ? '连接恢复后可下载' : latestExportsReady ? '当前版本已生成' : '文件生成中，请稍候';
        downloads(latestExportsReady, runtimeReady ? message : '执行环境不可用，下载已暂停');
      }
    });
  });
  $('retry-dashboard').addEventListener('click', refresh);
  refresh();
})();
