(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let page = 1;
  let generation = 0;
  function query() { return new URLSearchParams({month:$('month').value,region:$('region').value,page:String(page)}); }
  function setDownload(ready, href) {
    $('download').setAttribute('aria-disabled', ready ? 'false' : 'true');
    if (ready) { $('download').href = href; $('download').removeAttribute('tabindex'); }
    else { $('download').removeAttribute('href'); $('download').tabIndex = -1; }
  }
  async function load() {
    const current = ++generation;
    const params = query();
    document.body.dataset.loading = 'true';
    $('apply').disabled = true;
    $('prev').disabled = true;
    $('next').disabled = true;
    setDownload(false);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/source?' + params, {cache:'no-store',signal:controller.signal});
      if (!response.ok) throw new Error('Source unavailable');
      const data = await response.json();
      if (current !== generation) return;
      if (!Array.isArray(data.rows) || !Number.isFinite(data.total)) throw new Error('Invalid source data');
      if (data.rows.length) {
        $('body').replaceChildren(...data.rows.map(row => {
          const tr = document.createElement('tr');
          ['date','category','region','amount'].forEach(key => {
            const td = document.createElement('td');
            td.textContent = key === 'amount' ? Number(row[key]).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}) : row[key];
            if (key === 'amount') td.className = 'numeric';
            tr.append(td);
          });
          return tr;
        }));
      } else {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 4; td.className = 'table-empty'; td.textContent = '当前筛选范围没有记录。';
        tr.append(td); $('body').replaceChildren(tr);
      }
      $('count').textContent = '筛选结果 ' + data.total.toLocaleString('zh-CN') + ' 条';
      $('page').textContent = '第 ' + page + ' / ' + Math.max(1, Math.ceil(data.total / 10)) + ' 页';
      $('prev').disabled = page === 1;
      $('next').disabled = page * 10 >= data.total;
      setDownload(data.total > 0, '/api/download?' + params);
      $('source-error').hidden = true;
    } catch (_) {
      if (current !== generation) return;
      $('source-error').hidden = false;
      $('count').textContent = '未能更新';
      if (!$('body').querySelector('td:not(.table-empty)')) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 4; td.className = 'table-empty'; td.textContent = '数据暂不可用，请点击“重新加载”。';
        tr.append(td); $('body').replaceChildren(tr);
      }
    } finally {
      clearTimeout(timeout);
      if (current === generation) { delete document.body.dataset.loading; $('apply').disabled = false; }
    }
  }
  $('apply').addEventListener('click', () => { page = 1; load(); });
  $('prev').addEventListener('click', () => { if (page > 1) { page--; load(); } });
  $('next').addEventListener('click', () => { page++; load(); });
  $('retry-source').addEventListener('click', load);
  load();
})();
