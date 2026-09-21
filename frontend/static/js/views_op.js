/* ==========================================================
   VIGIA — Telas operacionais: painel, mosaico ao vivo e câmeras
   ========================================================== */
'use strict';

/* ==========================================================
   PAINEL
   ========================================================== */
function labels24(){
  const out = [], agora = new Date();
  for(let i = 23; i >= 0; i--){
    const d = new Date(agora.getTime() - i * 36e5);
    out.push(i % 4 === 0 ? pad(d.getHours()) + 'h' : '');
  }
  return out;
}

/** Converte a série do servidor (hora, tipo, total) em 24 baldes por tipo. */
function serie24(resumo){
  const vazios = () => new Array(24).fill(0);
  const out = {FACIAL: vazios(), OBJETO: vazios(), ACAO: vazios()};
  const agora = new Date();
  const base = new Date(agora.getTime() - 23 * 36e5);
  base.setMinutes(0, 0, 0);
  (resumo.serie_horaria || []).forEach(r => {
    const i = Math.floor((new Date(r.hora).getTime() - base.getTime()) / 36e5);
    if(i >= 0 && i < 24 && out[r.tipo]) out[r.tipo][i] += r.total;
  });
  return out;
}

async function renderPainel(alvo){
  const [resumo, resumoAlertas, esquema, ultimas, alertasAbertos] = await Promise.all([
    API.resumoDeteccoes(24),
    API.pode('alerts') ? API.resumoAlertas() : Promise.resolve({}),
    API.pode('biometrics') ? API.esquemaIds() : Promise.resolve({prefixos:{}}),
    API.deteccoes({tipo:'FACIAL', apenas_match:true, por_pagina:6}),
    API.pode('alerts') ? API.alertas({status:'ABERTO', por_pagina:6}) : Promise.resolve({itens:[]})
  ]);

  const t = resumo.por_tipo || {};
  const fac = t.FACIAL || 0, obj = t.OBJETO || 0, act = t.ACAO || 0;
  const total = resumo.total || 0;
  const series = serie24(resumo);
  const online = APP.cameras.filter(c => c.status === 'ONLINE').length;
  const abertos = (resumoAlertas.abertos || 0) + (resumoAlertas.em_andamento || 0);
  const pref = esquema.prefixos || {};

  const maxTop = Math.max(1, ...(resumo.top_cameras || []).map(c => c.total));
  const porSetor = {};
  (resumo.top_cameras || []).forEach(c => { porSetor[c.setor] = (porSetor[c.setor] || 0) + c.total; });
  const setores = Object.entries(porSetor).sort((a,b) => b[1]-a[1]).slice(0,6)
    .map(([k,v]) => ({k, v, cor:'var(--acc)'}));

  alvo.innerHTML = `
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-green">
      <div class="kpi-top"><span class="kpi-lbl">Câmeras on-line</span><span class="kpi-ico">${ico('cam',15)}</span></div>
      <div class="kpi-val">${online}<span style="font-size:15px;color:var(--txt-3)">/${APP.cameras.length}</span></div>
      <div class="kpi-delta ${online === APP.cameras.length ? 'up' : 'nt'}">${APP.cameras.length ? fmtPct(online/APP.cameras.length*100) : '—'} de disponibilidade</div>
    </div>
    <div class="kpi">
      <div class="kpi-top"><span class="kpi-lbl">Detecções · 24h</span><span class="kpi-ico">${ico('box',15)}</span></div>
      <div class="kpi-val" id="kpiTotal">${fmtNum(total)}</div>
      <div class="kpi-delta up">${ico('up',11)} ${fmtNum(fac)} faciais · ${fmtNum(obj)} objetos · ${fmtNum(act)} ações</div>
    </div>
    <div class="kpi k-violet">
      <div class="kpi-top"><span class="kpi-lbl">Identificações positivas</span><span class="kpi-ico">${ico('face',15)}</span></div>
      <div class="kpi-val">${fmtNum(resumo.identificacoes_positivas || 0)}</div>
      <div class="kpi-delta nt">de ${fmtNum(fac)} capturas faciais</div>
    </div>
    <div class="kpi ${abertos > 8 ? 'k-red' : 'k-amber'}">
      <div class="kpi-top"><span class="kpi-lbl">Alertas em aberto</span><span class="kpi-ico">${ico('bell',15)}</span></div>
      <div class="kpi-val">${abertos}</div>
      <div class="kpi-delta ${resumoAlertas.criticos_pendentes ? 'dn' : 'nt'}">${resumoAlertas.criticos_pendentes || 0} de severidade crítica</div>
    </div>
  </div>

  <div class="grid" style="grid-template-columns:${window.innerWidth < 1100 ? '1fr' : '1.55fr 1fr'};gap:14px;margin-bottom:14px" id="dashRow1">
    <div class="panel">
      <div class="panel-hd"><h3>Fluxo de detecções</h3><span class="sub">últimas 24 horas</span><span class="sp"></span>
        <div class="legend">
          <span><i style="background:var(--acc)"></i>Facial</span>
          <span><i style="background:var(--green)"></i>Objetos</span>
          <span><i style="background:var(--violet)"></i>Ações</span>
        </div></div>
      <div class="panel-bd">${areaChart([
        {data: series.OBJETO, cor:'var(--green)'},
        {data: series.FACIAL, cor:'var(--acc)'},
        {data: series.ACAO,   cor:'var(--violet)'}
      ], {labels: labels24(), h:214})}</div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Composição</h3><span class="sub">24h</span></div>
      <div class="panel-bd" style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;justify-content:center">
        ${donut([
          {v:obj, cor:'var(--green)'}, {v:fac, cor:'var(--acc)'}, {v:act, cor:'var(--violet)'}
        ], {centro: fmtNum(total), sub:'EVENTOS'})}
        <div style="display:flex;flex-direction:column;gap:9px;min-width:130px">
          ${[['Objetos',obj,'var(--green)'],['Facial',fac,'var(--acc)'],['Ações',act,'var(--violet)']].map(([k,v,c]) => `
            <div style="display:flex;align-items:center;gap:8px">
              <i style="width:9px;height:9px;border-radius:2px;background:${c};flex:none"></i>
              <span style="flex:1;font-size:12.5px;color:var(--txt-2)">${k}</span>
              <b class="mono" style="font-size:13px">${fmtNum(v)}</b>
              <span class="mono" style="font-size:10.5px;color:var(--txt-4);width:38px;text-align:right">${total ? Math.round(v/total*100) : 0}%</span>
            </div>`).join('')}
        </div>
      </div>
    </div>
  </div>

  <div class="grid cols-3" style="margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Câmeras mais acionadas</h3><span class="sub">24h</span></div>
      <div class="rank">
        ${(resumo.top_cameras || []).slice(0,6).map((c, i) => `
          <div class="rank-row">
            <span class="rank-n">${pad(i+1)}</span>
            <div class="rank-bd"><b>${esc(c.nome)}</b>
              <span class="sub">${esc(c.codigo)} · ${esc(c.setor)}</span>
              <div class="rank-track"><i style="width:${c.total/maxTop*100}%;background:var(--acc)"></i></div>
            </div>
            <span class="rank-val">${c.total}</span>
          </div>`).join('') || vazio('cam','Sem detecções no período.')}
      </div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Detecções por setor</h3><span class="sub">24h</span></div>
      <div class="panel-bd">${barsChart(setores, {h:224})}</div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Base biométrica</h3><span class="sp"></span>
        ${API.pode('biometrics') ? `<button class="btn btn-ghost btn-sm" data-ir="biometrics">Abrir ${ico('chev',12)}</button>` : ''}</div>
      <div class="panel-bd">
        <div class="id-scheme" style="margin-bottom:13px">
          ${['M','F','N'].map(s => {
            const p = pref[s] || {};
            return `<div class="id-card ${s.toLowerCase()}">
              <div class="pfx">${esc(p.prefixo || ('BIO-'+s))}</div>
              <div class="lbl">${esc(p.rotulo || '')}</div>
              <div class="cnt">${fmtNum(p.registros || 0)} registros</div></div>`;
          }).join('')}
        </div>
        <div class="note">${ico('info',15)}<div>O sexo é atributo <b>do cadastro</b>, informado no registro da pessoa — nunca inferido pelo modelo a partir da imagem. Por isso o prefixo do ID é estável e não muda entre capturas.</div></div>
      </div>
    </div>
  </div>

  <div class="split">
    <div class="panel">
      <div class="panel-hd"><h3>Identificações recentes</h3><span class="sp"></span>
        <button class="btn btn-ghost btn-sm" data-ir="facial">Ver todas ${ico('chev',12)}</button></div>
      <div class="panel-bd tight" id="dashFacial"></div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Alertas prioritários</h3><span class="sp"></span>
        ${API.pode('alerts') ? `<button class="btn btn-ghost btn-sm" data-ir="alerts">Ver todos ${ico('chev',12)}</button>` : ''}</div>
      <div class="panel-bd tight" id="dashAlerts"></div>
    </div>
  </div>`;

  $$('[data-ir]', alvo).forEach(b => b.onclick = () => irPara(b.dataset.ir));

  // Identificações recentes
  const itens = ultimas.itens || [];
  $('#dashFacial').innerHTML = itens.length ? `<div class="rank">${itens.map(d => {
    const p = d.pessoa, st = STATUS_PESSOA[p.status] || STATUS_PESSOA.CIDADAO;
    return `<div class="rank-row clickable" data-det="${esc(d.codigo)}" style="cursor:pointer">
      <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="38" height="38"></canvas>
      <div class="rank-bd"><b>${esc(p.nome)}</b>
        <span class="sub">${esc(p.id_biometrico)} · ${esc((d.camera ? d.camera.nome : '').slice(0,26))}</span></div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
        <span class="chip ${st.cls}">${esc(st.lbl)}</span>
        <span class="mono" style="font-size:10.5px;color:var(--txt-3)">${relTime(d.ocorrido_em)} · ${pct100(d.similaridade)}</span>
      </div></div>`;
  }).join('')}</div>` : vazio('face','Nenhuma identificação positiva recente.');
  pintarFaces($('#dashFacial'));
  $$('#dashFacial .rank-row').forEach(r => r.onclick = () => abrirDeteccao(r.dataset.det));

  // Alertas prioritários
  const alr = (alertasAbertos.itens || []).sort((a,b) => b.severidade - a.severidade);
  $('#dashAlerts').innerHTML = alr.length ? `<div class="rank">${alr.map(a => `
    <div class="rank-row clickable" data-alr="${esc(a.codigo)}" style="cursor:pointer">
      <span class="sev sev-${a.severidade}" style="min-height:30px"></span>
      <div class="rank-bd"><b>${esc(a.titulo)}</b>
        <span class="sub">${esc((a.camera ? a.camera.nome : '').slice(0,30))} · ${relTime(a.disparado_em)}</span></div>
      <span class="chip ${(STATUS_ALERTA[a.status]||{}).cls}">${esc((STATUS_ALERTA[a.status]||{}).lbl)}</span>
    </div>`).join('')}</div>`
    : `<div class="empty">${ico('check',30)}<p>Nenhum alerta em aberto.</p><small>Todas as ocorrências foram tratadas.</small></div>`;
  $$('#dashAlerts .rank-row').forEach(r => r.onclick = () => abrirAlerta(r.dataset.alr));
}

/** Atualiza o KPI do painel quando chega evento novo, sem recarregar a tela. */
function painelNovoEvento(){
  const el = $('#kpiTotal');
  if(!el) return;
  const n = parseInt(el.textContent.replace(/\D/g, ''), 10) || 0;
  el.textContent = fmtNum(n + 1);
}

/* ==========================================================
   MONITORAMENTO AO VIVO
   ========================================================== */
async function renderAoVivo(alvo){
  const setores = [...new Set(APP.cameras.map(c => c.setor))].sort();

  alvo.innerHTML = `
  <div class="live-shell">
    <div class="live-bar">
      <div class="segm" id="layoutSeg">
        ${[[1,'1×1'],[4,'2×2'],[9,'3×3'],[16,'4×4']].map(([n,l]) =>
          `<button data-n="${n}" class="${APP.mosaico === n ? 'on' : ''}">${l}</button>`).join('')}
      </div>
      <select class="inp" id="liveSetor" style="min-width:150px">
        <option value="">Todos os setores</option>
        ${setores.map(s => `<option value="${esc(s)}" ${APP.setorMosaico === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}
      </select>
      <div style="display:flex;align-items:center;gap:5px">
        <button class="btn btn-ghost btn-sm btn-icon" id="pgPrev" aria-label="Página anterior">${ico('lt',14)}</button>
        <span class="mono" id="pgInfo" style="font-size:11.5px;color:var(--txt-3);min-width:56px;text-align:center"></span>
        <button class="btn btn-ghost btn-sm btn-icon" id="pgNext" aria-label="Próxima página">${ico('chev',14)}</button>
      </div>
      <span class="sp"></span>
      <button class="btn btn-ghost btn-sm" id="btnPause">${ico('pause',13)} <span>Pausar</span></button>
      <button class="btn btn-ghost btn-sm" id="btnFs">${ico('expand',13)} Tela cheia</button>
    </div>
    <div class="live-main">
      <div class="live-col"><div class="mosaic g${APP.mosaico}" id="mosaic"></div></div>
      <aside class="live-side">
        <div class="side-hd"><h4>Eventos ao vivo</h4><span class="dot g pulse" id="wsDot"></span></div>
        <div class="evt-stream" id="evtStream">${carregando('Aguardando eventos…')}</div>
        <div class="side-hd" style="border-top:1px solid var(--line-soft);border-bottom:0"><h4>Controle PTZ</h4></div>
        <div style="padding:0 13px 14px" id="ptzBox"></div>
      </aside>
    </div>
  </div>`;

  $$('#layoutSeg button').forEach(b => b.onclick = () => {
    APP.mosaico = +b.dataset.n; APP.paginaMosaico = 0; irPara('live');
  });
  $('#liveSetor').onchange = e => { APP.setorMosaico = e.target.value; APP.paginaMosaico = 0; montarMosaico(); };
  $('#pgPrev').onclick = () => { if(APP.paginaMosaico > 0){ APP.paginaMosaico--; montarMosaico(); } };
  $('#pgNext').onclick = () => {
    if((APP.paginaMosaico+1) * APP.mosaico < camerasFiltradas().length){ APP.paginaMosaico++; montarMosaico(); }
  };
  $('#btnPause').onclick = () => {
    APP.pausado = !APP.pausado;
    APP.pausado ? FEED.stop() : FEED.start();
    $('#btnPause').innerHTML = (APP.pausado ? ico('play',13) : ico('pause',13)) +
      `<span>${APP.pausado ? 'Retomar' : 'Pausar'}</span>`;
    toast(APP.pausado ? 'Mosaico pausado' : 'Mosaico retomado',
      APP.pausado ? 'A gravação nos servidores continua ativa.' : '', 'info', 2600);
  };
  $('#btnFs').onclick = () => {
    const el = $('#mosaic');
    if(!document.fullscreenElement){
      if(el.requestFullscreen) el.requestFullscreen().catch(() =>
        toast('Tela cheia indisponível', 'O navegador bloqueou a solicitação.', 'warn'));
    }else document.exitFullscreen();
  };

  montarMosaico();
  await carregarStreamDeEventos();
  renderPTZ();
}

const camerasFiltradas = () =>
  APP.cameras.filter(c => !APP.setorMosaico || c.setor === APP.setorMosaico);

function camerasVisiveis(){
  const t = camerasFiltradas();
  return t.slice(APP.paginaMosaico * APP.mosaico, APP.paginaMosaico * APP.mosaico + APP.mosaico);
}

function montarMosaico(){
  const m = $('#mosaic'); if(!m) return;
  m.className = 'mosaic g' + APP.mosaico;
  const todas = camerasFiltradas(), vis = camerasVisiveis();
  const paginas = Math.max(1, Math.ceil(todas.length / APP.mosaico));
  $('#pgInfo').textContent = `${APP.paginaMosaico+1} / ${paginas}`;
  $('#pgPrev').disabled = APP.paginaMosaico === 0;
  $('#pgNext').disabled = APP.paginaMosaico >= paginas - 1;

  FEED.clear();
  if(!APP.camSelecionada || !vis.find(c => c.codigo === APP.camSelecionada)){
    const primeira = vis.find(c => c.status === 'ONLINE') || vis[0];
    APP.camSelecionada = primeira ? primeira.codigo : null;
  }

  m.innerHTML = Array.from({length: APP.mosaico}).map((_, i) => {
    const c = vis[i];
    if(!c) return `<div class="tile"><div class="tile-empty">Sem sinal atribuído</div></div>`;
    const off = c.status !== 'ONLINE';
    return `<div class="tile ${off ? 'offline' : ''} ${c.codigo === APP.camSelecionada ? 'sel' : ''}" data-cam="${esc(c.codigo)}">
      ${off ? `<div class="tile-off">${ico('alert',22)}<span>${c.status === 'OFFLINE' ? 'Sem sinal' : 'Em manutenção'}</span>
               <span style="color:var(--txt-4);font-size:9.5px">${esc(c.codigo)}</span></div>`
            : `<canvas data-feed="${esc(c.codigo)}"></canvas>`}
      <div class="tile-osd">
        <div class="osd-top">
          <div class="osd-name">${esc(c.codigo)}<small>${esc(c.nome)}</small></div>
          ${off ? '' : `<div class="osd-rec"><span class="dot pulse"></span>REC</div>`}
        </div>
        <div class="osd-bot">
          <div class="osd-ts osd-ts-live">—</div>
          <div class="osd-tags">
            ${c.analitico_facial ? '<span class="osd-tag" style="color:#F0A02A;border-color:rgba(240,160,42,.5)">FACIAL</span>' : ''}
            ${c.analitico_objeto ? '<span class="osd-tag" style="color:#2FB67C;border-color:rgba(47,182,124,.5)">OBJ</span>' : ''}
            ${c.analitico_acao   ? '<span class="osd-tag" style="color:#B49BF0;border-color:rgba(140,111,224,.5)">AÇÃO</span>' : ''}
            ${c.ptz              ? '<span class="osd-tag" style="color:#5FC4F0;border-color:rgba(95,196,240,.5)">PTZ</span>' : ''}
          </div>
        </div>
      </div>
    </div>`;
  }).join('');

  $$('#mosaic canvas[data-feed]').forEach(cv => {
    const cam = camPorCodigo(cv.dataset.feed);
    if(cam) FEED.register(cv, cam, {zonas: APP.mosaico <= 4, noise: APP.mosaico <= 9});
  });
  $$('#mosaic .tile[data-cam]').forEach(t => {
    t.onclick = () => {
      APP.camSelecionada = t.dataset.cam;
      $$('#mosaic .tile').forEach(x => x.classList.remove('sel'));
      t.classList.add('sel');
      renderPTZ();
    };
    t.ondblclick = () => abrirCamera(t.dataset.cam);
  });

  if(!APP.pausado) FEED.start();
}

function renderPTZ(){
  const box = $('#ptzBox'); if(!box) return;
  const c = camPorCodigo(APP.camSelecionada);
  if(!c){ box.innerHTML = '<p class="hint">Selecione uma câmera no mosaico.</p>'; return; }

  box.innerHTML = `
    <div style="font-size:11.5px;color:var(--txt-2);margin-bottom:9px">
      <b style="display:block;color:var(--txt-1);font-size:12.5px">${esc(c.nome)}</b>
      <span class="mono" style="color:var(--txt-3)">${esc(c.codigo)} · ${esc(c.resolucao)} · ${c.fps}fps · ${esc(c.codec)}</span>
    </div>
    ${c.ptz ? `<div style="display:flex;gap:12px;align-items:center;margin-bottom:11px">
      <div class="ptz">
        <button data-ptz="up" style="transform:rotate(-45deg)">${ico('up',13)}</button>
        <button data-ptz="up">${ico('up',13)}</button>
        <button data-ptz="up" style="transform:rotate(45deg)">${ico('up',13)}</button>
        <button data-ptz="left">${ico('lt',13)}</button>
        <button class="mid" data-ptz="home">${ico('home',12)}</button>
        <button data-ptz="right">${ico('chev',13)}</button>
        <button data-ptz="down" style="transform:rotate(45deg)">${ico('dn',13)}</button>
        <button data-ptz="down">${ico('dn',13)}</button>
        <button data-ptz="down" style="transform:rotate(-45deg)">${ico('dn',13)}</button>
      </div>
      <div style="display:flex;flex-direction:column;gap:5px">
        <button class="btn btn-soft btn-sm" data-ptz="zoom_in">${ico('zin',13)} Zoom +</button>
        <button class="btn btn-soft btn-sm" data-ptz="zoom_out">${ico('zout',13)} Zoom −</button>
      </div>
    </div>` : `<div class="note" style="margin-bottom:11px;padding:9px 11px;font-size:11.5px">${ico('info',14)}<div>Câmera fixa — sem controle PTZ.</div></div>`}
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button class="btn btn-soft btn-sm" id="ptzCfg">${ico('cog',13)} Configurar</button>
      <button class="btn btn-soft btn-sm" id="ptzFicha">${ico('info',13)} Ficha</button>
    </div>
    <div style="margin-top:12px;display:grid;gap:6px">
      <div class="sys-row"><span>Latência</span><b>${c.latencia_ms || 0} ms</b></div>
      <div class="sys-row"><span>Bitrate</span><b>${fmtNum(c.bitrate_kbps)} kbps</b></div>
      <div class="sys-row"><span>Limiar facial</span><b>${Math.round(c.limiar_facial*100)}%</b></div>
      <div class="sys-row"><span>Zonas</span><b>${(c.zonas||[]).length}</b></div>
    </div>`;

  $$('[data-ptz]', box).forEach(b => b.onclick = async () => {
    const cmd = b.dataset.ptz;
    // Efeito imediato na cena sintética — o comando real vai por ONVIF.
    const sc = FEED.scenes.get(c.codigo);
    if(sc){
      if(cmd === 'zoom_in')  sc.cfg = {...sc.cfg, hy: Math.max(.16, sc.cfg.hy - .03)};
      if(cmd === 'zoom_out') sc.cfg = {...sc.cfg, hy: Math.min(.46, sc.cfg.hy + .03)};
      if(cmd === 'home')     sc.cfg = SCENE_CFG[c.cena] || SCENE_CFG.rua;
      if(cmd === 'left')  sc.agents.forEach(a => a.u += .12);
      if(cmd === 'right') sc.agents.forEach(a => a.u -= .12);
      if(cmd === 'up')    sc.agents.forEach(a => a.d = Math.max(.18, a.d - .05));
      if(cmd === 'down')  sc.agents.forEach(a => a.d = Math.min(.94, a.d + .05));
    }
    try{
      await API.ptz(c.codigo, cmd);
      toast('Comando PTZ', `${c.codigo} · ${cmd}`, 'info', 1800);
    }catch(e){
      toast('PTZ', e.message, 'warn', 3200);
    }
  });
  $('#ptzCfg').onclick   = () => editarCamera(c.codigo);
  $('#ptzFicha').onclick = () => abrirCamera(c.codigo);
}

async function carregarStreamDeEventos(){
  const s = $('#evtStream'); if(!s) return;
  try{
    const r = await API.deteccoes({por_pagina: 14});
    s.innerHTML = (r.itens || []).map(eventoHTML).join('') ||
      vazio('live','Nenhum evento ainda.','As detecções aparecem aqui assim que ocorrem.');
    pintarSnaps(s);
    $$('.evt', s).forEach(e => e.onclick = () => abrirDeteccao(e.dataset.det));
  }catch(e){ s.innerHTML = erroBloco(e); }
}

function eventoHTML(d){
  const cor = d.tipo === 'FACIAL' ? 'var(--acc)' : d.tipo === 'OBJETO' ? 'var(--green)' : 'var(--violet)';
  let titulo, sub;
  if(d.tipo === 'FACIAL'){
    titulo = d.pessoa ? d.pessoa.nome : 'Face não identificada';
    sub = d.pessoa ? d.pessoa.id_biometrico : `${pct100(d.similaridade)} — abaixo do limiar`;
  }else if(d.tipo === 'OBJETO'){
    titulo = rotuloClasse(d); sub = `confiança ${pct100(d.confianca)}`;
  }else{
    titulo = rotuloClasse(d); sub = `${duracao(d.duracao_s)} · ${d.envolvidos || 1} pessoa(s)`;
  }
  const codCam = d.camera ? d.camera.codigo : '';
  return `<div class="evt" data-det="${esc(d.codigo)}">
    <canvas data-snap="${snapAttr(d)}" width="50" style="border-radius:3px;flex:none"></canvas>
    <div class="evt-bd">
      <b>${esc(titulo)}</b>
      <p>${esc(sub)}</p>
      <div class="row">
        <span class="osd-tag" style="color:${cor};border-color:${cor};background:transparent">${d.tipo}</span>
        <span class="mono" style="font-size:10px;color:var(--txt-4)">${esc(codCam)} · ${fmtTime(d.ocorrido_em)}</span>
      </div>
    </div></div>`;
}

/** Evento chegado pelo WebSocket: monta o cartão sem consultar a API de novo. */
function aoVivoNovoEvento(d){
  const s = $('#evtStream'); if(!s) return;
  if(s.querySelector('.empty')) s.innerHTML = '';
  const falso = {
    codigo: d.codigo, tipo: d.tipo, classe: d.classe, bbox: d.bbox,
    confianca: d.confianca, similaridade: d.similaridade,
    ocorrido_em: d.ocorrido_em, duracao_s: d.duracao_s, envolvidos: d.envolvidos,
    camera: {codigo: d.camera_codigo, nome: d.camera_nome,
             cena: (camPorCodigo(d.camera_codigo) || {}).cena || 'rua'},
    pessoa: d.pessoa
  };
  s.insertAdjacentHTML('afterbegin', eventoHTML(falso));
  pintarSnaps(s);
  const el = s.firstElementChild;
  if(el) el.onclick = () => abrirDeteccao(el.dataset.det);
  while(s.children.length > 20) s.lastElementChild.remove();
}

/* ==========================================================
   CÂMERAS
   ========================================================== */
function renderOnce(canvas, cam){
  const r = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(80, Math.round((r.width || 160) * dpr));
  const h = Math.round(w * 9 / 16);
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d', {alpha: false});

  if(cam.status !== 'ONLINE'){
    ctx.fillStyle = '#0A0E13'; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = 'rgba(232,71,77,.35)'; ctx.lineWidth = 1;
    for(let x = -h; x < w; x += 10){ ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x+h,h); ctx.stroke(); }
    ctx.fillStyle = '#E8474D';
    ctx.font = `600 ${Math.round(w/16)}px "IBM Plex Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.fillText(cam.status === 'OFFLINE' ? 'SEM SINAL' : 'MANUTENÇÃO', w/2, h/2 + 4);
    return;
  }
  const sc = FEED.scene(cam);
  const agentes = drawScene(ctx, sc, w, h, {noite: FEED.noite});
  drawBoxes(ctx, sc, agentes, w, h, w/640);
  drawFx(ctx, sc, w, h, false);
}

let CAM_FILTRO = {q:'', setor:'', status:''};

async function renderCameras(alvo){
  const setores = [...new Set(APP.cameras.map(c => c.setor))].sort();
  const cont = s => APP.cameras.filter(c => c.status === s).length;

  alvo.innerHTML = `
  <div class="sec-hd">
    <h2>Parque de câmeras</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="camQ" value="${esc(CAM_FILTRO.q)}" placeholder="Buscar por nome, código, IP ou setor…"></div>
      <select class="inp" id="camSetor"><option value="">Todos os setores</option>${setores.map(s => `<option ${CAM_FILTRO.setor===s?'selected':''}>${esc(s)}</option>`).join('')}</select>
      <select class="inp" id="camStatus">
        <option value="">Todos os status</option>
        <option value="ONLINE" ${CAM_FILTRO.status==='ONLINE'?'selected':''}>On-line</option>
        <option value="OFFLINE" ${CAM_FILTRO.status==='OFFLINE'?'selected':''}>Sem sinal</option>
        <option value="MANUTENCAO" ${CAM_FILTRO.status==='MANUTENCAO'?'selected':''}>Manutenção</option>
      </select>
      ${API.pode('edit') && API.pode('cams') ? `<button class="btn btn-primary btn-sm" id="camNova">${ico('plus',14)} Nova câmera</button>` : ''}
    </div>
  </div>
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-green"><div class="kpi-top"><span class="kpi-lbl">On-line</span><span class="kpi-ico">${ico('cam',15)}</span></div><div class="kpi-val">${cont('ONLINE')}</div><div class="kpi-delta nt">transmitindo agora</div></div>
    <div class="kpi k-red"><div class="kpi-top"><span class="kpi-lbl">Sem sinal</span><span class="kpi-ico">${ico('alert',15)}</span></div><div class="kpi-val">${cont('OFFLINE')}</div><div class="kpi-delta dn">requer verificação em campo</div></div>
    <div class="kpi k-amber"><div class="kpi-top"><span class="kpi-lbl">Manutenção</span><span class="kpi-ico">${ico('cog',15)}</span></div><div class="kpi-val">${cont('MANUTENCAO')}</div><div class="kpi-delta nt">fora de operação programada</div></div>
    <div class="kpi k-violet"><div class="kpi-top"><span class="kpi-lbl">Com análise facial</span><span class="kpi-ico">${ico('face',15)}</span></div><div class="kpi-val">${APP.cameras.filter(c => c.analitico_facial).length}</div><div class="kpi-delta nt">de ${APP.cameras.length} dispositivos</div></div>
  </div>
  <div class="panel"><div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl" id="camTbl">
    <thead><tr><th style="width:96px">Pré-visualização</th><th>Câmera</th><th>Setor</th><th>Status</th><th>Stream</th><th>Analíticos</th><th>Latência</th><th style="width:90px"></th></tr></thead>
    <tbody></tbody></table></div></div></div>`;

  const aplicar = () => {
    const q = $('#camQ').value.toLowerCase().trim();
    CAM_FILTRO = {q, setor: $('#camSetor').value, status: $('#camStatus').value};
    const lista = APP.cameras.filter(c =>
      (!q || [c.nome, c.codigo, c.ip, c.setor, c.fabricante, c.modelo].join(' ').toLowerCase().includes(q)) &&
      (!CAM_FILTRO.setor || c.setor === CAM_FILTRO.setor) &&
      (!CAM_FILTRO.status || c.status === CAM_FILTRO.status));

    $('#camTbl tbody').innerHTML = lista.map(c => {
      const st = STATUS_CAMERA[c.status] || {};
      return `<tr class="clickable" data-cam="${esc(c.codigo)}">
        <td><div style="width:84px;aspect-ratio:16/9;border-radius:3px;overflow:hidden;border:1px solid var(--line-soft);background:#000">
          <canvas data-prev="${esc(c.codigo)}" style="width:100%;height:100%;display:block"></canvas></div></td>
        <td><b>${esc(c.nome)}</b><br><span class="td-id">${esc(c.codigo)} · ${esc(c.fabricante)} ${esc(c.modelo)}</span></td>
        <td>${esc(c.setor)}</td>
        <td><span class="chip ${st.cls}">${esc(st.lbl)}</span></td>
        <td class="mono" style="font-size:11px">${esc(c.resolucao)}<br><span style="color:var(--txt-4)">${c.fps}fps · ${esc(c.codec)}</span></td>
        <td style="padding-top:13px"><div style="display:flex;gap:3px;flex-wrap:wrap">
          ${c.analitico_facial ? '<span class="chip c-amber">Facial</span>' : ''}
          ${c.analitico_objeto ? '<span class="chip c-green">Objetos</span>' : ''}
          ${c.analitico_acao ? '<span class="chip c-violet">Ações</span>' : ''}
          ${c.ptz ? '<span class="chip c-blue">PTZ</span>' : ''}</div></td>
        <td class="mono">${c.status === 'ONLINE' ? (c.latencia_ms || 0) + ' ms' : '—'}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn btn-ghost btn-sm btn-icon" data-ver="${esc(c.codigo)}" title="Ficha">${ico('eye',14)}</button>
          ${API.pode('edit') && API.pode('cams') ? `<button class="btn btn-ghost btn-sm btn-icon" data-edit="${esc(c.codigo)}" title="Configurar">${ico('edit',14)}</button>` : ''}
        </td></tr>`;
    }).join('') || `<tr><td colspan="8">${vazio('cam','Nenhuma câmera corresponde aos filtros.')}</td></tr>`;

    requestAnimationFrame(() => $$('#camTbl [data-prev]').forEach(cv => {
      const cam = camPorCodigo(cv.dataset.prev);
      if(!cam) return;
      renderOnce(cv, cam);                       // algo visível enquanto o quadro real não chega
      const url = FEED.fonteSnapshot ? FEED.fonteSnapshot(cam) : null;
      if(!url) return;
      const img = new Image();
      img.onload = () => pintarAoVivo(cv.getContext('2d', {alpha: false}), img, cv.width, cv.height);
      img.onerror = () => {};                    // câmera muda: fica a cena sintética
      img.src = url;
    }));
    $$('#camTbl [data-ver]').forEach(b => b.onclick = e => { e.stopPropagation(); abrirCamera(b.dataset.ver); });
    $$('#camTbl [data-edit]').forEach(b => b.onclick = e => { e.stopPropagation(); editarCamera(b.dataset.edit); });
    $$('#camTbl tr[data-cam]').forEach(t => t.onclick = () => abrirCamera(t.dataset.cam));
  };

  $('#camQ').oninput = debounce(aplicar, 200);
  $('#camSetor').onchange = aplicar;
  $('#camStatus').onchange = aplicar;
  const nova = $('#camNova'); if(nova) nova.onclick = () => editarCamera(null);
  aplicar();
}

async function abrirCamera(codigo){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let c, evts;
  try{
    [c, evts] = await Promise.all([
      API.camera(codigo),
      API.deteccoes({camera: codigo, por_pagina: 6})
    ]);
  }catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const st = STATUS_CAMERA[c.status] || {};
  const itens = evts.itens || [];
  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd"><h3>${esc(c.nome)}</h3><span class="chip ${st.cls}">${esc(c.codigo)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="position:relative;aspect-ratio:16/9;background:#000;border-radius:var(--r-m);overflow:hidden;border:1px solid var(--line);margin-bottom:15px">
        <canvas id="camModalFeed" style="position:absolute;inset:0;width:100%;height:100%"></canvas>
        <div class="tile-osd"><div class="osd-top"><div class="osd-name">${esc(c.codigo)}<small>${esc(c.nome)}</small></div>
        ${c.status === 'ONLINE' ? '<div class="osd-rec"><span class="dot pulse"></span>REC</div>' : ''}</div>
        <div class="osd-bot"><div class="osd-ts osd-ts-live">—</div></div></div>
      </div>
      <div class="split" style="gap:16px">
        <dl class="kv">
          <dt>Código</dt><dd class="mono">${esc(c.codigo)}</dd>
          <dt>Fabricante</dt><dd>${esc(c.fabricante)} ${esc(c.modelo)}</dd>
          <dt>Endereço IP</dt><dd class="mono">${esc(c.ip)}</dd>
          <dt>RTSP</dt><dd class="mono" style="font-size:11px;word-break:break-all">${esc(c.rtsp_url)}</dd>
          <dt>Setor</dt><dd>${esc(c.setor)}</dd>
          <dt>Coordenadas</dt><dd class="mono">${c.latitude != null ? c.latitude.toFixed(5) + ', ' + c.longitude.toFixed(5) : '—'}</dd>
        </dl>
        <dl class="kv">
          <dt>Resolução</dt><dd class="mono">${esc(c.resolucao)} @ ${c.fps}fps</dd>
          <dt>Codec</dt><dd>${esc(c.codec)} · ${fmtNum(c.bitrate_kbps)} kbps</dd>
          <dt>Limiar facial</dt><dd class="mono">${Math.round(c.limiar_facial*100)}%</dd>
          <dt>Limiar objeto</dt><dd class="mono">${Math.round(c.limiar_objeto*100)}%</dd>
          <dt>Retenção</dt><dd>${c.retencao_dias} dias</dd>
          <dt>Latência</dt><dd class="mono">${c.latencia_ms || 0} ms</dd>
        </dl>
      </div>
      <div class="eyebrow" style="margin:16px 0 8px">Zonas configuradas</div>
      <div class="zone-list">${(c.zonas||[]).map(z => `<div class="zone-item">
        <span class="zone-swatch" style="background:${esc(z.cor)}"></span><b>${esc(z.nome)}</b>
        <span class="chip c-gray">${z.tipo === 'LINHA' ? 'Linha virtual' : 'Polígono'}</span>
        <span class="mono" style="font-size:10.5px;color:var(--txt-4)">${(z.pontos||[]).length} pts</span></div>`).join('')
        || '<p class="hint">Nenhuma zona definida.</p>'}</div>
      <div class="eyebrow" style="margin:16px 0 8px">Últimos eventos desta câmera</div>
      ${itens.length ? `<div class="rank" style="border:1px solid var(--line-soft);border-radius:var(--r-m)">
        ${itens.map(d => `<div class="rank-row clickable" data-det="${esc(d.codigo)}" style="cursor:pointer">
          <canvas data-snap="${snapAttr(d)}" width="46"></canvas>
          <div class="rank-bd"><b>${esc(tituloDeteccao(d))}</b><span class="sub">${fmtDT(d.ocorrido_em)}</span></div>
          <span class="chip ${d.tipo === 'FACIAL' ? 'c-blue' : d.tipo === 'OBJETO' ? 'c-green' : 'c-violet'}">${d.tipo}</span>
        </div>`).join('')}</div>` : '<p class="hint">Sem eventos registrados.</p>'}
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${API.pode('edit') && API.pode('cams') ? `<button class="btn btn-soft" id="camGoEdit">${ico('cog',14)} Configurar</button>` : ''}
      <button class="btn btn-primary" id="camGoLive">${ico('live',14)} Ver ao vivo</button>
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarSnaps(box);
  $$('[data-det]', box).forEach(r => r.onclick = () => { fecharModal(); abrirDeteccao(r.dataset.det); });

  const cv = $('#camModalFeed');
  if(c.status === 'ONLINE'){ FEED.register(cv, c, {zonas: true}); FEED.start(); }
  else renderOnce(cv, c);

  const ed = $('#camGoEdit'); if(ed) ed.onclick = () => { fecharModal(); editarCamera(c.codigo); };
  $('#camGoLive').onclick = () => {
    fecharModal();
    APP.camSelecionada = c.codigo; APP.setorMosaico = c.setor;
    APP.mosaico = 4; APP.paginaMosaico = 0;
    irPara('live');
  };
}
