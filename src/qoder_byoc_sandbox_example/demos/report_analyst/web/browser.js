(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const frame = $('frame');
  let frameBusy = false;
  let frameTimer;
  let currentUrl;
  let failures = 0;
  let hasFrame = false;
  let evidenceBusy = false;
  async function fetchJson(path) {
    const response = await fetch(path, {cache:'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  }
  async function refreshFrame() {
    if (frameBusy) return;
    frameBusy = true;
    clearTimeout(frameTimer);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch('/live/frame?t=' + Date.now(), {cache:'no-store', signal:controller.signal});
      if (!response.ok) throw new Error('Frame unavailable');
      const blob = await response.blob();
      if (!blob.type.startsWith('image/') || !blob.size) throw new Error('Invalid frame');
      const nextUrl = URL.createObjectURL(blob);
      const image = new Image();
      image.src = nextUrl;
      try { await image.decode(); } catch (error) { URL.revokeObjectURL(nextUrl); throw error; }
      const previous = currentUrl;
      currentUrl = nextUrl;
      frame.src = nextUrl;
      if (previous) URL.revokeObjectURL(previous);
      frame.hidden = false;
      hasFrame = true;
      failures = 0;
      $('frame-empty').hidden = true;
      $('frame-paused').hidden = true;
      $('frame-live').hidden = false;
      $('status').textContent = '实时连接';
      $('status').className = 'badge badge-success';
    } catch (_) {
      failures++;
      $('frame-live').hidden = true;
      $('status').textContent = failures < 3 && !hasFrame ? '连接中' : '等待连接';
      $('status').className = 'badge ' + (failures >= 3 ? 'badge-warning' : '');
      if (hasFrame) {
        $('frame-paused').hidden = false;
      } else {
        $('frame-empty').hidden = false;
        $('frame-empty-title').textContent = failures < 3 ? '正在连接浏览器' : '暂时没有浏览器画面';
        $('frame-empty-message').textContent = failures < 3 ? '实例准备好后，这里会显示 Agent 的实际操作。' : '浏览器可能仍在准备或实例已停止。请检查向导与 Browser Sandbox，页面会自动重试。';
      }
    } finally {
      clearTimeout(timeout);
      frameBusy = false;
      frameTimer = setTimeout(refreshFrame, hasFrame && !failures ? 600 : 1800);
    }
  }
  function renderEvidence(records) {
    $('evidence').textContent = records.length ? JSON.stringify(records, null, 2) : '暂无传递记录';
    const transfer = records.find(item => item.kind === 'transfer.json');
    const append = records.find(item => item.kind === 'append.json');
    if (!transfer) return;
    const matches = transfer.source_sha256 && transfer.source_sha256 === transfer.target_sha256;
    $('transfer-status').textContent = matches ? 'SHA-256 一致' : '哈希待核对';
    $('transfer-status').className = 'badge ' + (matches ? 'badge-success' : 'badge-warning');
    const summary = $('evidence-summary');
    summary.replaceChildren();
    const stats = document.createElement('div');
    stats.className = 'evidence-stats';
    const count = document.createElement('strong');
    count.textContent = Number(transfer.rows).toLocaleString('zh-CN') + ' 条记录';
    const size = document.createElement('span');
    size.className = 'muted';
    size.textContent = Number(transfer.bytes).toLocaleString('zh-CN') + ' 字节 · 浏览器真实下载';
    stats.append(count, size);
    summary.append(stats);
    [['Browser', transfer.browser_path], ['Analysis', transfer.analysis_path]].forEach(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'evidence-path';
      const title = document.createElement('span');
      title.textContent = label;
      const path = document.createElement('code');
      path.textContent = value || '—';
      row.append(title, path);
      summary.append(row);
    });
    if (append && append.record) {
      const note = document.createElement('p');
      note.className = 'evidence-append';
      note.textContent = '已追加：' + append.record.category + ' · ' + Number(append.record.amount).toLocaleString('zh-CN') + '；源报表保持不变。';
      summary.append(note);
    }
  }
  async function refreshEvidence() {
    if (evidenceBusy) return;
    evidenceBusy = true;
    try { const data = await fetchJson('/api/evidence'); if (Array.isArray(data)) renderEvidence(data); }
    catch (_) { if (!$('evidence-summary').querySelector('.evidence-stats')) $('evidence-summary').textContent = '暂未取得传递证据。执行环境恢复后会自动更新。'; }
    finally { evidenceBusy = false; setTimeout(refreshEvidence, 2400); }
  }
  fetchJson('/api/identity').then(data => {
    $('identity').textContent = 'Browser  ' + (data.browser_sandbox_id || '—') + '\nAnalysis ' + (data.analysis_sandbox_id || '—');
  }).catch(() => { $('identity').textContent = '当前无法取得实例信息，请检查运行状态。'; });
  $('retry-frame').addEventListener('click', refreshFrame);
  window.addEventListener('beforeunload', () => { if (currentUrl) URL.revokeObjectURL(currentUrl); });
  refreshFrame();
  refreshEvidence();
})();
