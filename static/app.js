'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 3}).format(value);
const money = value => new Intl.NumberFormat('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(value);
const routes = {
  overview: ['运营概览', '看清库存状态，把需要处理的事项放在前面。', 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z'],
  stock: ['库存台账', '按商品、批次和库位查询；出库采用先到期先出。', 'M3 7l9-4 9 4v10l-9 4-9-4z M3 7l9 4 9-4 M12 11v10'],
  alerts: ['效期预警', '优先处理过期与临期批次，及时补足低库存商品。', 'M12 3 2 21h20L12 3z M12 9v5 M12 17v1'],
  movements: ['出入库流水', '每次库存变动都有批次、单号与操作记录。', 'M3 7h16 M15 3l4 4-4 4 M21 17H5 M9 13l-4 4 4 4'],
  counts: ['库存盘点', '核对实物与账面库存，记录差异并同步调整。', 'M8 4H5v17h14V4h-3 M8 2h8v5H8z M8 12l2 2 5-5 M8 18h7'],
  products: ['商品资料', '维护商品编码、计量单位、安全库存与效期阈值。', 'M4 3h16v18H4z M8 7h8 M8 12h8 M8 17h4']
};
const kindName = {inbound:'入库', outbound:'出库', damage:'报损', count:'盘点'};
let state = null, route = 'overview', filters = {q:'', status:'', category:''};
let modalAction = '', modalExtra = {}, busy = false, request = null, refreshBusy = false;

function icon(path) { return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>`; }
$('#nav').innerHTML = Object.entries(routes).map(([key, [name,, path]]) => `<a class="nav-link" href="#${key}" data-route="${key}">${icon(path)}${name}</a>`).join('');

function notify(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = false;
}

async function load() {
  if (refreshBusy) return;
  refreshBusy = true;
  $('#refresh').disabled = true;
  $('#refresh').textContent = '刷新中…';
  $('#error').hidden = true;
  $('#content').setAttribute('aria-busy', 'true');
  try {
    const response = await fetch('/api/state');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败');
    state = data;
    $('#date').textContent = data.today;
    $('#mode').textContent = data.mode === 'demo' ? '演示工作区 · 示例数据' : '本地工作区';
    $('#receive').disabled = false;
    render();
  } catch (error) {
    $('#error').textContent = `无法加载库存：${error.message}。请确认本地服务仍在运行，然后点击“刷新”。`;
    $('#error').hidden = false;
    if (!state) $('#content').innerHTML = '<div class="empty">连接尚未建立</div>';
  } finally {
    refreshBusy = false;
    $('#content').setAttribute('aria-busy', 'false');
    $('#refresh').disabled = false;
    $('#refresh').textContent = '刷新';
  }
}

function navigate() {
  const next = location.hash.slice(1);
  route = routes[next] ? next : 'overview';
  filters = {q:'', status:'', category:''};
  $('#notice').hidden = true;
  if (state) render();
}

function render() {
  const [title, subtitle] = routes[route];
  $('#title').textContent = title;
  $('#breadcrumb').textContent = title;
  $('#subtitle').textContent = subtitle;
  document.title = `${title} · 鲜仓`;
  for (const link of document.querySelectorAll('.nav-link')) {
    if (link.dataset.route === route) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  }
  if (route === 'overview') {
    renderOverview();
    return;
  }
  let intro = '';
  if (route === 'alerts') intro = '<div class="info-band">到期当天仍可正常出库；次日标记为过期并阻止出库。临期阈值按商品单独设置，过期商品可登记报损。</div>';
  if (route === 'counts') intro = '<div class="info-band">在库存台账中选择批次并点击“盘点”。提交后立即调整库存；有差异时请记录原因，历史流水会完整保留。</div>';
  const exportKind = route === 'movements' ? 'movements' : route === 'counts' ? 'counts' : 'stock';
  const exportLabel = route === 'movements' ? '全部流水' : route === 'counts' ? '全部盘点' : '全部库存';
  $('#content').innerHTML = `${intro}<section class="panel" aria-label="${title}">${toolbar(exportKind, exportLabel)}<div id="table-content"></div></section>`;
  $('#search').addEventListener('input', e => { filters.q = e.target.value; renderTable(); });
  $('#filter-status')?.addEventListener('change', e => { filters.status = e.target.value; renderTable(); });
  $('#filter-category')?.addEventListener('change', e => { filters.category = e.target.value; renderTable(); });
  renderTable();
}

function toolbar(exportKind, label) {
  const stockPage = ['stock','alerts'].includes(route);
  const statuses = route === 'alerts' ? [['','全部预警'],['expiring','临期'],['expired','过期']] : stockPage ? [['','全部在库'],['normal','正常'],['expiring','临期'],['expired','过期'],['empty','已清零'],['low','低库存']] : route === 'movements' ? [['','全部类型'], ...Object.entries(kindName)] : [];
  const categories = [...new Set(state.products.map(p => p.category))];
  return `<div class="toolbar"><label class="search-label"><span>搜索</span><input id="search" type="search" placeholder="商品名称、编码或批次号" value="${esc(filters.q)}"></label>
    ${statuses.length ? `<label><span>${stockPage?'库存状态':'业务类型'}</span><select id="filter-status">${statuses.map(([key, label])=>`<option value="${key}" ${filters.status===key?'selected':''}>${label}</option>`).join('')}</select></label>`:''}
    ${['stock','alerts','products'].includes(route)?`<label><span>商品分类</span><select id="filter-category"><option value="">全部分类</option>${categories.map(c=>`<option ${filters.category===c?'selected':''}>${esc(c)}</option>`).join('')}</select></label>`:''}
    ${route==='products'?'<button class="button primary" data-action="products">＋ 新建商品</button>':`<a class="button secondary" href="/api/export?kind=${exportKind}">导出${label}</a>`}
    ${route==='stock'?'<button class="button primary" data-action="outbound">新建出库</button>':''}</div>`;
}

function metric(label, value, unit, hint, warning = false) {
  return `<div class="metric ${warning?'warning':''}"><div class="metric-label">${label}</div><div class="metric-value">${value}${unit?`<em>${unit}</em>`:''}</div><small>${hint}</small></div>`;
}

function renderOverview() {
  const s = state.summary;
  const risk = state.batches.filter(b=>['expired','expiring'].includes(b.status));
  const low = state.products.filter(p=>p.low_stock);
  $('#content').innerHTML = `
    ${state.mode==='demo'?'<div class="info-band">演示工作区：初始商品、供应商与业务记录均为示例。你新增的操作会保存至本地数据库。</div>':''}
    <section class="metrics" aria-label="库存概况">
      ${metric('库存成本金额', money(s.stock_value), '元', '含临期、过期库存 · 按批次成本计价')}
      ${metric('在库批次', num(s.batches), '批', `${s.products} 种商品 · 按批次追踪`)}
      ${metric('需关注批次', num(s.expiring+s.expired), '批', `临期 ${s.expiring} 批 / 过期 ${s.expired} 批`, true)}
      ${metric('今日出库', num(s.today_outbound), '笔', `今日报损成本 ${money(s.today_damage_value)} 元`)}
    </section>
    <div class="overview-grid"><section class="panel"><div class="panel-heading"><div><h2>近 7 日作业</h2><p>按业务单据计数，同单多批次合并为一笔</p></div><a href="#movements">查看流水 →</a></div>${chart()}<div class="legend"><span><i></i>入库笔数</span><span><i class="out"></i>出库笔数</span></div></section>
    <section class="panel"><div class="panel-heading"><div><h2>待处理事项</h2><p>先处理效期，再关注补货</p></div></div><div class="attention">
      <div class="attention-item"><div><p>过期批次</p><small>停止出库，核实后登记报损</small></div><span class="badge danger">${s.expired} 批</span></div>
      <div class="attention-item"><div><p>临期批次</p><small>优先拣选，减少效期损耗</small></div><span class="badge warning">${s.expiring} 批</span></div>
      <div class="attention-item"><div><p>低库存商品</p><small>可用数量低于安全库存</small></div><strong>${low.length}<small> 种</small></strong></div>
      <a class="attention-link" href="#alerts">查看预警与补货清单 →</a></div></section></div>
    <section class="panel"><div class="panel-heading"><div><h2>优先关注的库存</h2><p>按到期时间排序，优先展示有库存的临期与过期批次</p></div><a href="#stock">全部库存 →</a></div>${stockTable(risk.slice(0,5), true)}<div class="table-footer"><span>共 ${risk.length} 批需关注库存</span><span>先到期先出 · 过期批次禁止出库</span></div></section>`;
}

function chart() {
  const max = Math.max(2, ...state.trend.flatMap(d=>[d.inbound,d.outbound]));
  let svg = `<svg viewBox="0 0 560 175" role="img" aria-label="近七日入库与出库单据数量"><title>近七日入库与出库笔数</title>`;
  for (let i=0;i<3;i++) {
    const y = 20+i*55;
    svg += `<line x1="32" x2="548" y1="${y}" y2="${y}" class="grid"/><text x="8" y="${y+4}">${num(max*(1-i/2))}</text>`;
  }
  state.trend.forEach((d,i)=>{
    const x=48+i*72;
    for (const [index,kind] of ['inbound','outbound'].entries()) {
      const height = d[kind]/max*110;
      svg+=`<rect x="${x+index*21}" y="${130-height}" width="16" height="${height}" rx="3" class="${kind==='inbound'?'in':'out'}"><title>${d.date} ${kindName[kind]} ${d[kind]} 笔</title></rect>`;
    }
    svg+=`<text x="${x+16}" y="156" text-anchor="middle">${d.date.slice(5).replace('-','/')}</text>`;
  });
  return `<div class="chart">${svg}</svg></div>`;
}

function badge(batch) {
  if (batch.status==='empty') return '<span class="badge neutral">已清零</span>';
  if (batch.status==='expired') return `<span class="badge danger">已过期 ${Math.abs(batch.days_left)} 天</span>`;
  if (batch.status==='expiring') return `<span class="badge warning">${batch.days_left===0?'今日到期':`剩 ${batch.days_left} 天`}</span>`;
  return '<span class="badge">正常</span>';
}

function table(headers, rows, emptyMessage='没有匹配的记录') {
  if (!rows.length) return `<div class="empty"><h3>${emptyMessage}</h3><p>试试调整筛选条件，或新增一笔仓库作业。</p></div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(cells=>`<tr>${cells.map((cell,i)=>`<td data-label="${headers[i]}">${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function stockTable(batches, compact=false) {
  return table(['商品 / 批次','库位','在库数量','到期日期','效期状态','操作'], batches.map(b=>[
    `<div class="product-cell"><strong>${esc(b.name)}</strong><small>${esc(b.sku)} · ${esc(b.code)}</small></div>`,
    esc(b.location), `<span class="num"><span class="stock-qty">${num(b.qty)}</span><span class="unit">${esc(b.unit)}</span></span>`,
    esc(b.expires_on), badge(b), `<div class="cell-actions"><button class="text-button" data-action="count" data-id="${b.id}">盘点</button>${b.qty>0?`<button class="text-button ${b.status==='expired'?'danger-button':''}" data-action="damage" data-id="${b.id}">报损</button>`:''}</div>`
  ]), compact?'暂无效期风险':'没有匹配的库存');
}

function matches(item) {
  const q=filters.q.trim().toLowerCase();
  return (!q || [item.name,item.sku,item.code,item.location,item.reference,item.operator].some(v=>String(v||'').toLowerCase().includes(q))) && (!filters.category || item.category===filters.category);
}

function renderTable() {
  let html='', count=0;
  if (['stock','alerts'].includes(route)) {
    let rows=state.batches.filter(matches);
    if (route==='alerts') rows=rows.filter(b=>['expired','expiring'].includes(b.status));
    if (filters.status==='low') rows=rows.filter(b=>state.products.some(p=>p.id===b.product_id&&p.low_stock));
    else if(filters.status) rows=rows.filter(b=>b.status===filters.status);
    else rows=rows.filter(b=>b.qty>0);
    count=rows.length;
    html=stockTable(rows);
    if (route==='alerts') {
      const low=state.products.filter(p=>p.low_stock&&matches(p));
      html+=`<div class="panel-heading"><div><h2>补货建议清单</h2><p>按安全库存计算缺口；不含在途与采购计划。</p></div></div>`;
      html+=table(['商品','可用库存','安全库存','建议补足','操作'],low.map(p=>[
        `<div class="product-cell"><strong>${esc(p.name)}</strong><small>${esc(p.sku)}</small></div>`,
        `${num(p.available)} ${esc(p.unit)}`,`${num(p.min_stock)} ${esc(p.unit)}`,`<span class="low-text">${num(p.min_stock-p.available)} ${esc(p.unit)}</span>`,
        `<button class="text-button" data-action="inbound" data-product="${p.id}">登记入库</button>`
      ]),'暂无低库存商品');
    }
  } else if(route==='products') {
    const rows=state.products.filter(matches); count=rows.length;
    html=table(['商品名称 / 编码','分类','可用库存','安全库存','临期阈值','操作'],rows.map(p=>[
      `<div class="product-cell"><strong>${esc(p.name)}</strong><small>${esc(p.sku)}</small></div>`,esc(p.category),
      `<span class="${p.low_stock?'low-text':''}">${num(p.available)} ${esc(p.unit)}${p.low_stock?' · 待补货':''}</span>`,`${num(p.min_stock)} ${esc(p.unit)}`,`${p.warning_days} 天`,
      `<div class="cell-actions"><button class="text-button" data-action="products" data-product="${p.id}">编辑</button><button class="text-button" data-action="inbound" data-product="${p.id}">入库</button></div>`
    ]),'还没有商品资料');
  } else if(route==='movements') {
    const rows=state.movements.filter(matches).filter(m=>!filters.status||m.kind===filters.status); count=rows.length;
    html=table(['时间 / 单号','类型','商品 / 批次','变动数量','变动后库存','操作人 / 备注'],rows.map(m=>[
      `<div>${esc(m.created_at.replace('T',' ').slice(0,19))}<br><small>${esc(m.reference||'—')}</small></div>`,
      `<span class="badge ${m.kind==='damage'?'danger':m.kind==='inbound'?'blue':'neutral'}">${kindName[m.kind]}</span>`,
      `<div class="product-cell"><strong>${esc(m.name)}</strong><small>${esc(m.code)}</small></div>`,
      `<span class="num ${m.delta<0?'negative':'positive'}">${m.delta>0?'+':''}${num(m.delta)} ${esc(m.unit)}</span>`,
      `${num(m.balance)} ${esc(m.unit)}`,`<div>${esc(m.operator)}<br><small>${esc(m.note||'—')}</small></div>`
    ]));
  } else if(route==='counts') {
    const rows=state.counts.filter(matches); count=rows.length;
    html=table(['盘点时间','商品 / 批次','账面数量','实盘数量','差异','操作人 / 原因'],rows.map(c=>[
      esc(c.created_at.replace('T',' ').slice(0,19)),`<div class="product-cell"><strong>${esc(c.name)}</strong><small>${esc(c.code)}</small></div>`,
      `${num(c.expected)} ${esc(c.unit)}`,`${num(c.actual)} ${esc(c.unit)}`,`<span class="${c.difference<0?'negative':c.difference>0?'positive':''}">${c.difference>0?'+':''}${num(c.difference)} ${esc(c.unit)}</span>`,
      `<div>${esc(c.operator)}<br><small>${esc(c.reason)}</small></div>`
    ]),'暂无盘点记录');
  }
  $('#table-content').innerHTML=html+`<div class="table-footer"><span role="status" aria-live="polite">筛选结果 ${count} 条${route==='alerts'?'效期记录':''}</span><span>导出包含全部记录，不受页面筛选影响</span></div>`;
}

function field(label,name,value='',type='text',options={}) {
  return `<label class="${options.full?'full':''}"><span>${label}${options.optional?'':' *'}</span><input name="${name}" type="${type}" value="${esc(value)}" ${options.optional?'':'required'} ${options.readonly?'readonly':''} ${options.maxLength?`maxlength="${options.maxLength}"`:''} ${type==='number'?`step="${options.step||'0.001'}" min="${options.min??0}" ${options.max!==undefined?`max="${options.max}"`:''}`:''} ${options.placeholder?`placeholder="${esc(options.placeholder)}"`:''}></label>`;
}

function selectField(label,name,values,selected='') {
  return `<label class="full"><span>${label} *</span><select name="${name}" required>${values.map(([id,label])=>`<option value="${esc(id)}" ${String(id)===String(selected)?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
}

function openModal(action, options={}) {
  if(!state || busy) return;
  if(['inbound','outbound'].includes(action)&&!state.products.length) {
    notify('请先建立商品资料，再进行出入库。');
    action='products'; options={};
  }
  modalAction=action; modalExtra={}; request=null;
  let fields='', help='', title='', submit='确认保存';
  let operator=''; try { operator=localStorage.getItem('fresh-stock-operator')||''; } catch {}
  const product=state.products.find(p=>p.id===Number(options.product));
  if(action==='products') {
    title=product?'编辑商品资料':'新建商品'; help='商品编码与计量单位创建后固定；安全库存与预警天数可随时调整。';
    if(product) modalExtra.id=product.id;
    fields=field('商品名称','name',product?.name||'','text',{full:true,maxLength:80})+field('商品编码','sku',product?.sku||'','text',{readonly:!!product,maxLength:40,placeholder:'VEG-003'})+
      field('计量单位','unit',product?.unit||'kg','text',{readonly:!!product,maxLength:10})+field('商品分类','category',product?.category||'蔬菜','text',{maxLength:30})+
      field('安全库存','min_stock',product?.min_stock??10,'number')+field('临期预警天数','warning_days',product?.warning_days??3,'number',{step:1,min:0,max:365});
  } else if(action==='inbound') {
    title='新建入库'; help='按批次记录日期、库位和成本。同一批次再次入库时，批次信息必须一致。'; submit='确认入库';
    fields=selectField('选择商品','product_id',state.products.map(p=>[p.id,`${p.name} / ${p.sku} / ${p.unit}`]),product?.id)+
      field('到货批次号','code','','text',{maxLength:60,placeholder:'例如 B20261021-01'})+field('库位','location','','text',{maxLength:40,placeholder:'冷藏-A01'})+
      field('供应商','supplier','','text',{full:true,maxLength:80})+field('入库数量','qty','','number',{min:.001})+field('单位采购成本（元）','cost','','number',{step:.01})+
      field('生产日期','produced_on',state.today,'date')+field('到期日期','expires_on',state.today,'date')+field('关联单号','reference','','text',{optional:true,full:true,maxLength:80});
  } else if(action==='outbound') {
    title='新建出库'; help='自动从未过期批次中按到期日由近到远分配。库存不足时不会扣减任何批次。'; submit='按效期分配并出库';
    fields=selectField('选择商品','product_id',state.products.map(p=>[p.id,`${p.name} / 可用 ${num(p.available)} ${p.unit}`]),product?.id)+
      field('出库数量','qty','','number',{min:.001})+field('订单 / 出库单号','reference','','text',{maxLength:80,placeholder:'OUT-20261021-01'});
  } else {
    const batch=state.batches.find(b=>b.id===Number(options.id));
    if(!batch) return;
    modalExtra={batch_id:batch.id,version:batch.version};
    fields=`<div class="batch-context"><strong>${esc(batch.name)} · ${esc(batch.code)}</strong>${esc(batch.location)} / 当前库存 ${num(batch.qty)} ${esc(batch.unit)} / 到期 ${esc(batch.expires_on)}</div>`;
    if(action==='count') {
      title='批次库存盘点'; submit='提交盘点并调整库存'; help='请输入现场实际数量。系统将保存盘点差异并调整库存；若期间库存变动，会要求重新核对。';
      fields+=field(`实际数量（${batch.unit}）`,'actual',batch.qty,'number',{full:true});
    } else {
      title='登记库存报损'; submit='确认报损'; help='记录破损、腐损或过期等原因。提交后扣减该批次库存并保留流水。';
      fields+=field(`报损数量（${batch.unit}）`,'qty','','number',{min:.001,max:batch.qty,full:true});
    }
  }
  if(action!=='products') {
    const required=['count','damage'].includes(action);
    fields+=field('操作人','operator',operator,'text',{full:true,maxLength:30,placeholder:'填写你的姓名'})+
      `<label class="full"><span>${required?'原因 / 处理说明 *':'备注（选填）'}</span><textarea name="note" maxlength="300" ${required?'required':''} placeholder="${required?'说明差异或损耗原因，便于后续追溯':'填写本次作业的补充说明'}"></textarea></label>`;
  }
  $('#dialog-title').textContent=title; $('#dialog-help').textContent=help;
  $('#fields').innerHTML=fields; $('#submit').textContent=submit; $('#submit').disabled=false;
  $('#form-error').hidden=true; $('#form').setAttribute('aria-busy','false');
  $('#dialog').showModal();
  $('#fields input:not([readonly]), #fields select')?.focus();
}

async function submit(event) {
  event.preventDefault();
  if(busy) return;
  const values={...Object.fromEntries(new FormData($('#form'))),...modalExtra};
  const signature=JSON.stringify(values);
  if(!request || request.signature!==signature) request={signature,id:crypto.randomUUID()};
  values.request_id=request.id;
  busy=true; $('#form').setAttribute('aria-busy','true'); $('#form-error').hidden=true;
  const label=$('#submit').textContent;
  $('#submit').textContent='正在提交…';
  for(const button of $('#dialog').querySelectorAll('button')) button.disabled=true;
  try {
    const response=await fetch(`/api/${modalAction}`,{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':state.token},body:JSON.stringify(values)});
    const result=await response.json();
    if(!response.ok) throw new Error(result.error||'操作未完成');
    if(values.operator) { try {localStorage.setItem('fresh-stock-operator',values.operator);} catch {} }
    $('#dialog').close();
    await load();
    notify(result.message+(result.allocations?`：${result.allocations.map(a=>`${a.batch} ${num(a.qty)}`).join('；')}`:''));
  } catch(error) {
    $('#form-error').textContent=error instanceof TypeError?'连接中断，请检查服务后重试。重试相同请求不会重复扣减库存。':error.message;
    $('#form-error').hidden=false;
    $('#form-error').focus();
  } finally {
    busy=false; $('#form').setAttribute('aria-busy','false');
    for(const button of $('#dialog').querySelectorAll('button')) button.disabled=false;
    $('#submit').textContent=label;
  }
}

document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');
  if(button) openModal(button.dataset.action,button.dataset);
});
$('.skip').addEventListener('click',event=>{event.preventDefault();$('#main').focus();});
$('#refresh').addEventListener('click',load);
$('#receive').addEventListener('click',()=>openModal('inbound'));
$('#form').addEventListener('submit',submit);
$('#close').addEventListener('click',()=>{if(!busy) $('#dialog').close();});
$('#cancel').addEventListener('click',()=>{if(!busy) $('#dialog').close();});
$('#dialog').addEventListener('cancel',event=>{if(busy) event.preventDefault();});
window.addEventListener('hashchange',navigate);
navigate(); load();
