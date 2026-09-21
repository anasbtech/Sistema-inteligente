/* ==========================================================
   VIGIA — Registros de detecção e base biométrica
   ========================================================== */
'use strict';

const PAG = {facial:1, objetos:1, acoes:1, bio:1, alertas:1, audit:1};
const FILTRO = {
  facial:  {q:'', status:'', cam:'', match:''},
  objetos: {q:'', classe:'', cam:''},
  acoes:   {q:'', classe:'', cam:''},
  bio:     {q:'', sexo:'', status:''},
  alertas: {status:'', severidade:''},
  audit:   {q:'', acao:''}
};
const POR_PAGINA = 14;

const opcoesCameras = (filtro, sel) => APP.cameras
  .filter(filtro || (() => true))
  .map(c => `<option value="${esc(c.codigo)}" ${sel === c.codigo ? 'selected' : ''}>${esc(c.codigo)} — ${esc(c.nome.slice(0,28))}</option>`)
  .join('');

function ligarPager(raiz, chave, recarregar){
  $$('[data-pg]', raiz).forEach(b => b.onclick = () => {
    PAG[chave] = +b.dataset.pg.split(':')[1];
    recarregar();
  });
}

const barraConfianca = (v, cor) => `<div class="conf">
  <div class="conf-bar"><i style="width:${Math.round((v||0)*100)}%;background:${cor}"></i></div>
  <b>${pct100(v)}</b></div>`;

/* ==========================================================
   RECONHECIMENTO FACIAL
   ========================================================== */
async function renderFacial(alvo){
  const f = FILTRO.facial;
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Registros de reconhecimento facial</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="fcQ" value="${esc(f.q)}" placeholder="ID biométrico…"></div>
      <select class="inp" id="fcMatch">
        <option value="">Todas as capturas</option>
        <option value="true" ${f.match==='true'?'selected':''}>Com correspondência</option>
        <option value="false" ${f.match==='false'?'selected':''}>Não identificadas</option>
      </select>
      <select class="inp" id="fcCam"><option value="">Todas as câmeras</option>${opcoesCameras(c => c.analitico_facial, f.cam)}</select>
    </div></div>
  <div class="note" style="margin-bottom:13px">${ico('info',15)}<div>Cada correspondência positiva vincula a captura a um <b>identificador biométrico permanente</b> (BIO-M / BIO-F / BIO-N). O identificador nunca muda entre capturas, e o sexo vem do cadastro da pessoa — não de inferência sobre a imagem.</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="fcCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#fcCorpo');
    corpo.innerHTML = carregando();
    FILTRO.facial = {q: $('#fcQ').value.trim(), match: $('#fcMatch').value, cam: $('#fcCam').value};
    try{
      const r = await API.deteccoes({
        tipo: 'FACIAL', pagina: PAG.facial, por_pagina: POR_PAGINA,
        camera: FILTRO.facial.cam || undefined,
        apenas_match: FILTRO.facial.match || undefined,
        id_biometrico: /^BIO-[MFN]-\d{6}$/i.test(FILTRO.facial.q) ? FILTRO.facial.q.toUpperCase() : undefined
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Pessoa identificada</th><th>ID biométrico</th>
          <th>Situação</th><th>Similaridade</th><th>Câmera</th><th>Data / hora</th><th>Revisão</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => {
          const p = d.pessoa, st = p ? (STATUS_PESSOA[p.status]||{}) : null;
          return `<tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td>${p ? `<div class="face-cell">
                <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="30" height="30"></canvas>
                <div class="face-meta"><b>${esc(p.nome)}</b><span>${esc({M:'Masculino',F:'Feminino',N:'Não informado'}[p.sexo])}</span></div></div>`
              : `<span style="color:var(--txt-3)">Não identificada</span><br>
                 <span class="td-ts">abaixo do limiar de ${Math.round((d.limiar_aplicado||0)*100)}%</span>`}</td>
            <td class="td-id">${p ? esc(p.id_biometrico) : '—'}</td>
            <td>${st ? `<span class="chip ${st.cls}">${esc(st.lbl)}</span>` : '<span class="chip c-gray">Desconhecida</span>'}</td>
            <td>${barraConfianca(d.similaridade, p ? 'var(--green)' : 'var(--amber)')}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,26))}</span></td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}<br><span style="color:var(--txt-4)">${relTime(d.ocorrido_em)}</span></td>
            <td>${d.revisado ? `<span class="chip c-green">${esc(d.veredito||'Revisado')}</span>` : '<span class="chip c-amber">Pendente</span>'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="8">${vazio('face','Nenhum registro encontrado.','Ajuste os filtros ou aguarde novas capturas.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'facial')}`;
      pintarFaces(corpo); pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'facial', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#fcQ').oninput = debounce(() => { PAG.facial = 1; carregar(); }, 350);
  ['fcMatch','fcCam'].forEach(id => $('#'+id).onchange = () => { PAG.facial = 1; carregar(); });
  await carregar();
}

/* ==========================================================
   OBJETOS
   ========================================================== */
async function renderObjetos(alvo){
  const classes = Object.entries(ROTULO_OBJ);
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Detecção de objetos</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="obClasse"><option value="">Todas as classes</option>
        ${classes.map(([k,v]) => `<option value="${k}" ${FILTRO.objetos.classe===k?'selected':''}>${esc(v)}</option>`).join('')}</select>
      <select class="inp" id="obCam"><option value="">Todas as câmeras</option>${opcoesCameras(null, FILTRO.objetos.cam)}</select>
    </div></div>
  <div class="panel" style="margin-bottom:14px"><div class="panel-hd"><h3>Classes detectadas</h3><span class="sub">últimas 24 horas</span></div>
    <div class="panel-bd" id="obGrafico">${carregando()}</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="obCorpo">${carregando()}</div></div></div>`;

  // O gráfico consulta a contagem por classe em paralelo.
  (async () => {
    try{
      const contagens = await Promise.all(classes.map(async ([k]) => {
        const r = await API.deteccoes({tipo:'OBJETO', classe:k, por_pagina:1,
          desde: new Date(Date.now() - 864e5).toISOString()});
        return {k: ROTULO_OBJ[k], v: r.total, cor: COR_CLASSE[k] || 'var(--acc)'};
      }));
      const g = $('#obGrafico');
      if(g) g.innerHTML = barsChart(contagens.filter(c => c.v > 0), {h:206});
    }catch(e){ const g = $('#obGrafico'); if(g) g.innerHTML = erroBloco(e); }
  })();

  const carregar = async () => {
    const corpo = $('#obCorpo');
    corpo.innerHTML = carregando();
    FILTRO.objetos = {classe: $('#obClasse').value, cam: $('#obCam').value, q:''};
    try{
      const r = await API.deteccoes({
        tipo:'OBJETO', pagina: PAG.objetos, por_pagina: POR_PAGINA,
        classe: FILTRO.objetos.classe || undefined,
        camera: FILTRO.objetos.cam || undefined
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Classe</th><th>Confiança</th><th>Câmera</th>
          <th>Placa</th><th>Data / hora</th><th>Severidade</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => `
          <tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td><b style="color:${COR_CLASSE[d.classe]||'var(--txt-1)'}">${esc(rotuloClasse(d))}</b><br><span class="td-id">${esc(d.codigo)}</span></td>
            <td>${barraConfianca(d.confianca, COR_CLASSE[d.classe]||'var(--acc)')}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td class="mono">${d.placa ? `<b style="letter-spacing:.08em">${esc(d.placa)}</b>` : '—'}</td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}</td>
            <td><span class="chip ${CLS_SEV[d.severidade]}">${esc(SEVERIDADE[d.severidade])}</span></td>
          </tr>`).join('') || `<tr><td colspan="7">${vazio('box','Nenhuma detecção encontrada.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'objetos')}`;
      pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'objetos', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  ['obClasse','obCam'].forEach(id => $('#'+id).onchange = () => { PAG.objetos = 1; carregar(); });
  await carregar();
}

/* ==========================================================
   AÇÕES
   ========================================================== */
async function renderAcoes(alvo){
  const classes = Object.entries(ROTULO_ACAO_UI);
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Análise de ações e comportamento</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="acClasse"><option value="">Todas as ações</option>
        ${classes.map(([k,v]) => `<option value="${k}" ${FILTRO.acoes.classe===k?'selected':''}>${esc(v)}</option>`).join('')}</select>
      <select class="inp" id="acCam"><option value="">Todas as câmeras</option>${opcoesCameras(c => c.analitico_acao, FILTRO.acoes.cam)}</select>
    </div></div>
  <div class="split" style="margin-bottom:14px">
    <div class="panel"><div class="panel-hd"><h3>Ações por tipo</h3><span class="sub">24h</span></div>
      <div class="panel-bd" id="acGrafico">${carregando()}</div></div>
    <div class="panel"><div class="panel-hd"><h3>Modelo de análise</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Arquitetura</dt><dd>Trajetórias + ByteTrack</dd>
          <dt>Sinais</dt><dd>velocidade, permanência, densidade, cruzamento de linha</dd>
          <dt>Classes ativas</dt><dd>${classes.length} comportamentos</dd>
          <dt>Janela</dt><dd>90 amostras por trilha</dd>
        </dl>
        <div class="note" style="margin-top:14px">${ico('alert',15)}<div>Eventos comportamentais são <b>indícios</b>, não conclusões. Toda ação de severidade crítica exige confirmação humana antes do despacho de guarnição.</div></div>
      </div></div>
  </div>
  <div class="panel"><div class="panel-bd tight"><div id="acCorpo">${carregando()}</div></div></div>`;

  (async () => {
    try{
      const contagens = await Promise.all(classes.map(async ([k]) => {
        const r = await API.deteccoes({tipo:'ACAO', classe:k, por_pagina:1,
          desde: new Date(Date.now() - 864e5).toISOString()});
        return {k: ROTULO_ACAO_UI[k], v: r.total, cor:'var(--violet)'};
      }));
      const g = $('#acGrafico');
      if(g) g.innerHTML = barsChart(contagens.filter(c => c.v > 0), {h:206});
    }catch(e){ const g = $('#acGrafico'); if(g) g.innerHTML = erroBloco(e); }
  })();

  const carregar = async () => {
    const corpo = $('#acCorpo');
    corpo.innerHTML = carregando();
    FILTRO.acoes = {classe: $('#acClasse').value, cam: $('#acCam').value, q:''};
    try{
      const r = await API.deteccoes({
        tipo:'ACAO', pagina: PAG.acoes, por_pagina: POR_PAGINA,
        classe: FILTRO.acoes.classe || undefined, camera: FILTRO.acoes.cam || undefined
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Ação detectada</th><th>Confiança</th>
          <th>Duração</th><th>Envolvidos</th><th>Câmera</th><th>Data / hora</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => `
          <tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td style="padding-top:13px"><div style="display:flex;gap:8px;align-items:center">
              <span class="sev sev-${d.severidade}" style="height:28px"></span>
              <span><b>${esc(rotuloClasse(d))}</b><br><span class="td-id">${esc(d.codigo)}</span></span></div></td>
            <td>${barraConfianca(d.confianca, 'var(--violet)')}</td>
            <td class="mono">${duracao(d.duracao_s)}</td>
            <td class="mono">${d.envolvidos || 1}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}<br><span style="color:var(--txt-4)">${relTime(d.ocorrido_em)}</span></td>
          </tr>`).join('') || `<tr><td colspan="7">${vazio('run','Nenhum evento comportamental encontrado.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'acoes')}`;
      pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'acoes', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  ['acClasse','acCam'].forEach(id => $('#'+id).onchange = () => { PAG.acoes = 1; carregar(); });
  await carregar();
}

/* ==========================================================
   DETALHE DA DETECÇÃO
   ========================================================== */
async function abrirDeteccao(codigo){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let d;
  try{ d = await API.deteccao(codigo); }
  catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const p = d.pessoa;
  const podeRevisar = API.pode('edit') && API.pode('facial') && !d.revisado;
  const naoIdentificada = d.tipo === 'FACIAL' && !p;

  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd">
      <h3>${esc(d.tipo === 'FACIAL' ? (p ? 'Identificação positiva' : 'Captura não identificada') : rotuloClasse(d))}</h3>
      <span class="chip ${d.tipo === 'FACIAL' ? 'c-blue' : d.tipo === 'OBJETO' ? 'c-green' : 'c-violet'}">${esc(d.codigo)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="split" style="gap:16px">
        <div>
          <div class="eyebrow" style="margin-bottom:7px">Quadro da detecção</div>
          <div style="border:1px solid var(--line);border-radius:var(--r-m);overflow:hidden;position:relative;background:#000">
            <canvas id="detBig" style="width:100%;display:block"></canvas>
            <div class="watermark">VIGIA · ${esc(d.camera?d.camera.codigo:'')} · ${fmtDT(d.ocorrido_em)}</div>
          </div>
          <p class="hint" style="margin-top:8px">Chave da evidência: <span class="mono">${esc(d.snapshot_key || '—')}</span></p>
        </div>
        <div>
          ${p ? `<div class="eyebrow" style="margin-bottom:7px">Registro biométrico vinculado</div>
          <div style="display:flex;gap:12px;align-items:center;padding:11px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-800);margin-bottom:13px">
            <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="56" height="56"></canvas>
            <div style="min-width:0">
              <b style="display:block;font-size:14px">${esc(p.nome)}</b>
              <span class="mono" style="font-size:12px;color:var(--acc-2)">${esc(p.id_biometrico)}</span>
              <div style="margin-top:5px"><span class="chip ${(STATUS_PESSOA[p.status]||{}).cls}">${esc((STATUS_PESSOA[p.status]||{}).lbl)}</span></div>
            </div></div>` : ''}
          <dl class="kv">
            <dt>Evento</dt><dd class="mono">${esc(d.codigo)}</dd>
            <dt>Data / hora</dt><dd class="mono">${fmtDT(d.ocorrido_em)}</dd>
            <dt>Câmera</dt><dd>${esc(d.camera?d.camera.nome:'')}<br><span class="mono" style="font-size:11px;color:var(--txt-3)">${esc(d.camera?d.camera.codigo:'')} · ${esc(d.camera?d.camera.setor:'')}</span></dd>
            ${d.tipo === 'FACIAL' ? `
              <dt>Similaridade</dt><dd class="mono"><b style="color:${p?'var(--green)':'var(--amber)'}">${pct100(d.similaridade)}</b> (limiar ${Math.round((d.limiar_aplicado||0)*100)}%)</dd>
              <dt>Qualidade da face</dt><dd class="mono">${pct100(d.qualidade_face)}</dd>
              <dt>Sexo (cadastro)</dt><dd>${p ? esc({M:'Masculino',F:'Feminino',N:'Não informado'}[p.sexo]) : '—'}</dd>
              <dt>Embedding</dt><dd class="mono" style="font-size:11px">512-D · distância cosseno</dd>`
            : d.tipo === 'OBJETO' ? `
              <dt>Classe</dt><dd>${esc(rotuloClasse(d))}</dd>
              <dt>Confiança</dt><dd class="mono"><b>${pct100(d.confianca)}</b> (limiar ${Math.round((d.limiar_aplicado||0)*100)}%)</dd>
              ${d.placa ? `<dt>Placa (OCR)</dt><dd class="mono"><b style="letter-spacing:.1em">${esc(d.placa)}</b></dd>` : ''}`
            : `
              <dt>Ação</dt><dd>${esc(rotuloClasse(d))}</dd>
              <dt>Confiança</dt><dd class="mono"><b>${pct100(d.confianca)}</b></dd>
              <dt>Duração</dt><dd class="mono">${duracao(d.duracao_s)}</dd>
              <dt>Envolvidos</dt><dd class="mono">${d.envolvidos || 1} pessoa(s)</dd>`}
            <dt>Severidade</dt><dd><span class="chip ${CLS_SEV[d.severidade]}">${esc(SEVERIDADE[d.severidade])}</span></dd>
            <dt>Revisão</dt><dd>${d.revisado ? `<span class="chip c-green">${esc(d.veredito||'Revisado')}</span>` : '<span class="chip c-amber">Pendente</span>'}</dd>
            ${d.hash_evidencia ? `<dt>Hash</dt><dd class="mono" style="font-size:10.5px;word-break:break-all;color:var(--txt-3)">${esc(d.hash_evidencia)}</dd>` : ''}
          </dl>
        </div>
      </div>
      ${naoIdentificada ? `<div class="note" style="margin-top:15px">${ico('alert',15)}<div>Similaridade abaixo do limiar da câmera. A captura <b>não</b> foi atribuída a nenhuma pessoa e não gera identificação. O recorte é descartado no prazo de retenção, salvo vinculação a inquérito.</div></div>` : ''}
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${p && API.pode('biometrics') ? `<button class="btn btn-soft" id="detPessoa">${ico('db',14)} Abrir ficha</button>` : ''}
      ${d.camera ? `<button class="btn btn-soft" id="detCam">${ico('live',14)} Ver câmera</button>` : ''}
      ${podeRevisar ? `<button class="btn btn-danger" id="detFalso">Falso positivo</button>
                       <button class="btn btn-primary" id="detConf">${ico('check',14)} Confirmar</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  requestAnimationFrame(() => {
    const big = $('#detBig');
    if(big){
      const w = Math.round(big.getBoundingClientRect().width);
      big.style.height = Math.round(w * .6) + 'px';
      drawSnapshot(big, {codigo:d.codigo, tipo:d.tipo, classe:d.classe, bbox:d.bbox,
                         cena:(d.camera&&d.camera.cena)||'rua'}, w);
      carregarRecorte(big, d.codigo);
    }
  });

  const bp = $('#detPessoa');
  if(bp) bp.onclick = () => { fecharModal(); abrirPessoa(d.pessoa.id_biometrico); };
  const bc = $('#detCam');
  if(bc) bc.onclick = () => { fecharModal(); abrirCamera(d.camera.codigo); };

  if(podeRevisar){
    const revisar = async veredito => {
      try{
        await API.revisar(d.codigo, veredito);
        fecharModal();
        toast(veredito === 'Confirmado' ? 'Detecção confirmada' : 'Marcado como falso positivo',
          `${d.codigo} — registrado na trilha de auditoria.`,
          veredito === 'Confirmado' ? 'ok' : 'warn');
        irPara(APP.view);
      }catch(e){ erroToast(e); }
    };
    $('#detConf').onclick  = () => revisar('Confirmado');
    $('#detFalso').onclick = () => revisar('Falso positivo');
  }
}

/* ==========================================================
   BASE BIOMÉTRICA
   ========================================================== */
const ROTULO_SEXO = {M:'Masculino', F:'Feminino', N:'Não informado'};

async function renderBiometria(alvo){
  const esquema = await API.esquemaIds();
  const pref = esquema.prefixos || {};
  const b = FILTRO.bio;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Base biométrica</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="bioQ" value="${esc(b.q)}" placeholder="Nome, ID, CPF ou mandado…"></div>
      <select class="inp" id="bioSexo"><option value="">Todos os sexos</option>
        ${['M','F','N'].map(s => `<option value="${s}" ${b.sexo===s?'selected':''}>${ROTULO_SEXO[s]}</option>`).join('')}</select>
      <select class="inp" id="bioStatus"><option value="">Todas as situações</option>
        ${Object.entries(STATUS_PESSOA).map(([k,v]) => `<option value="${k}" ${b.status===k?'selected':''}>${esc(v.lbl)}</option>`).join('')}</select>
      ${API.pode('edit') ? `<button class="btn btn-primary btn-sm" id="bioNovo">${ico('plus',14)} Novo registro</button>` : ''}
    </div></div>

  <div class="panel" style="margin-bottom:14px">
    <div class="panel-hd"><h3>Esquema de identificadores</h3>
      <span class="sub">um prefixo por sexo · sequência independente · identificador permanente</span></div>
    <div class="panel-bd">
      <div class="id-scheme">
        ${['M','F','N'].map(s => {
          const p = pref[s] || {};
          const cor = {M:'var(--acc-2)', F:'#F08CBE', N:'var(--txt-2)'}[s];
          return `<div class="id-card ${s.toLowerCase()}">
            <div class="pfx">${esc(p.prefixo || 'BIO-'+s)}-000000</div>
            <div class="lbl">${esc(p.rotulo || ROTULO_SEXO[s])}</div>
            <div class="cnt">${fmtNum(p.registros||0)} registros · próximo: <b style="color:${cor}">${esc(p.proximo||'—')}</b></div>
          </div>`;
        }).join('')}
      </div>
      <div class="note" style="margin-top:13px">${ico('lock',15)}<div><b>Regra de integridade:</b> ${esc(esquema.regra || '')}</div></div>
    </div></div>

  <div class="panel"><div class="panel-bd tight"><div id="bioCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#bioCorpo');
    corpo.innerHTML = carregando();
    FILTRO.bio = {q: $('#bioQ').value.trim(), sexo: $('#bioSexo').value, status: $('#bioStatus').value};
    try{
      const r = await API.pessoas({
        pagina: PAG.bio, por_pagina: POR_PAGINA,
        q: FILTRO.bio.q || undefined,
        sexo: FILTRO.bio.sexo || undefined,
        status: FILTRO.bio.status || undefined
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:52px">Face</th><th>Nome</th><th>ID biométrico</th><th>Sexo</th>
          <th>Situação</th><th>Qualidade</th><th>Detecções</th><th>Última captura</th></tr></thead>
        <tbody>${(r.itens||[]).map(p => {
          const st = STATUS_PESSOA[p.status] || {};
          const cor = {M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo];
          return `<tr class="clickable" data-p="${esc(p.id_biometrico)}">
            <td><canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="36" height="36"></canvas></td>
            <td><b>${esc(p.nome)}</b>${p.alcunha ? `<br><span class="td-ts">${esc(p.alcunha)}</span>` : ''}</td>
            <td class="td-id"><b style="color:${cor}">${esc(p.id_biometrico)}</b></td>
            <td><span class="chip" style="color:${cor};border-color:${cor}55;background:transparent">${esc(ROTULO_SEXO[p.sexo])}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span></td>
            <td>${barraConfianca(p.qualidade, p.qualidade > .85 ? 'var(--green)' : p.qualidade > .75 ? 'var(--amber)' : 'var(--red)')}
                <span class="td-ts">${p.amostras} amostra(s)</span></td>
            <td class="mono"><b>${fmtNum(p.total_deteccoes)}</b></td>
            <td class="td-ts">${p.ultima_deteccao ? fmtDT(p.ultima_deteccao) + '<br><span style="color:var(--txt-4)">' + esc((p.ultima_camera||'').slice(0,22)) + '</span>' : '—'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="8">${vazio('db','Nenhum registro encontrado.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'bio')}`;
      pintarFaces(corpo);
      $$('tr[data-p]', corpo).forEach(t => t.onclick = () => abrirPessoa(t.dataset.p));
      ligarPager(corpo, 'bio', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#bioQ').oninput = debounce(() => { PAG.bio = 1; carregar(); }, 350);
  ['bioSexo','bioStatus'].forEach(id => $('#'+id).onchange = () => { PAG.bio = 1; carregar(); });
  const novo = $('#bioNovo'); if(novo) novo.onclick = () => editarPessoa(null);
  await carregar();
}

async function abrirPessoa(idBio){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let p, capturas;
  try{
    [p, capturas] = await Promise.all([API.pessoa(idBio), API.capturasDaPessoa(idBio, 8)]);
  }catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const st = STATUS_PESSOA[p.status] || {};
  const cor = {M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo];
  const sexoMudou = p.sexo !== p.sexo_emissao;

  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd"><h3>Ficha biométrica</h3><span class="chip ${st.cls}">${esc(st.lbl)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:17px">
        <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="104" height="104" style="border-radius:var(--r-m)"></canvas>
        <div style="flex:1;min-width:210px">
          <b style="font-size:19px;display:block;font-family:var(--f-disp);letter-spacing:.01em">${esc(p.nome)}</b>
          ${p.alcunha ? `<span style="color:var(--txt-3);font-size:12.5px">${esc(p.alcunha)}</span>` : ''}
          <div style="margin:9px 0;display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <span class="mono" style="font-size:16px;font-weight:600;color:${cor}">${esc(p.id_biometrico)}</span>
            <span class="chip" style="color:${cor};border-color:${cor}55;background:transparent">${esc(ROTULO_SEXO[p.sexo])}</span>
          </div>
          <p class="hint" style="max-width:52ch">${esc(st.desc||'')}. Identificador emitido em ${fmtDate(p.criado_em)} e permanente desde então.</p>
        </div>
      </div>
      ${sexoMudou ? `<div class="note" style="margin-bottom:15px">${ico('info',15)}<div>O sexo deste cadastro foi corrigido para <b>${esc(ROTULO_SEXO[p.sexo])}</b> após a emissão. O identificador segue <b>${esc(p.id_biometrico)}</b> — o prefixo registra o que constava no ato da emissão, e reemitir quebraria o vínculo com as capturas já registradas.</div></div>` : ''}
      <div class="split" style="gap:16px">
        <dl class="kv">
          <dt>Nascimento</dt><dd class="mono">${p.nascimento ? fmtDate(p.nascimento) : '—'}</dd>
          <dt>CPF</dt><dd class="mono">${esc(p.cpf || '—')}</dd>
          <dt>RG</dt><dd class="mono">${esc(p.rg || '—')}</dd>
          <dt>Unidade</dt><dd>${esc(p.unidade || '—')}</dd>
          ${p.mandado ? `<dt>Mandado</dt><dd class="mono" style="color:var(--red)">${esc(p.mandado)}</dd>
          <dt>Tipificação</dt><dd>${esc(p.tipificacao)}</dd>
          <dt>Órgão</dt><dd>${esc(p.orgao_expedidor)}</dd>` : ''}
        </dl>
        <dl class="kv">
          <dt>Qualidade do template</dt><dd class="mono">${pct100(p.qualidade)}</dd>
          <dt>Amostras faciais</dt><dd class="mono">${p.amostras}</dd>
          <dt>Total de detecções</dt><dd class="mono"><b>${fmtNum(p.total_deteccoes)}</b></dd>
          <dt>Última captura</dt><dd class="mono">${p.ultima_deteccao ? fmtDT(p.ultima_deteccao) : '—'}</dd>
          <dt>Cadastrado em</dt><dd class="mono">${fmtDate(p.criado_em)}</dd>
          <dt>Base legal</dt><dd>${esc(p.base_legal)}</dd>
        </dl>
      </div>
      <div class="eyebrow" style="margin:17px 0 8px">Histórico de capturas</div>
      ${capturas.length ? `<div class="rank" style="border:1px solid var(--line-soft);border-radius:var(--r-m)">
        ${capturas.map(c => `<div class="rank-row clickable" data-det="${esc(c.codigo)}" style="cursor:pointer">
          <div class="rank-bd"><b>${esc(c.camera)}</b><span class="sub">${fmtDT(c.ocorrido_em)} · ${esc(c.camera_codigo)}</span></div>
          <span class="mono" style="font-size:12px;color:var(--green)">${pct100(c.similaridade)}</span>
        </div>`).join('')}</div>` : '<p class="hint">Nenhuma captura registrada para este identificador.</p>'}
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${API.pode('edit') ? `<button class="btn btn-soft" id="pAmostra">${ico('snap',14)} Adicionar foto</button>
                            <button class="btn btn-primary" id="pEdit">${ico('edit',14)} Editar cadastro</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  $$('[data-det]', box).forEach(r => r.onclick = () => { fecharModal(); abrirDeteccao(r.dataset.det); });

  const be = $('#pEdit'); if(be) be.onclick = () => { fecharModal(); editarPessoa(p.id_biometrico); };
  const ba = $('#pAmostra'); if(ba) ba.onclick = () => enviarFoto(p.id_biometrico);
}

/** Upload de foto para extrair o template — a imagem em si não é guardada. */
function enviarFoto(idBio){
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = async () => {
    const arq = input.files[0];
    if(!arq) return;
    toast('Processando', 'Extraindo o template facial da imagem…', 'info', 3000);
    try{
      const r = await API.enviarAmostra(idBio, arq);
      toast('Amostra adicionada',
        `Qualidade ${pct100(r.qualidade_amostra)} · ${r.amostras} amostra(s) no template. ${r.observacao}`,
        'ok', 6500);
      fecharModal();
      abrirPessoa(idBio);
    }catch(e){
      erroToast(e, 'Não foi possível processar a imagem');
    }
  };
  input.click();
}

async function editarPessoa(idBio){
  const novo = !idBio;
  let p = null;
  if(!novo){
    try{ p = await API.pessoa(idBio); }
    catch(e){ return erroToast(e); }
  }
  const sexoAtual = p ? p.sexo : 'M';

  abrirModal(`
    <div class="modal-hd"><h3>${novo ? 'Novo registro biométrico' : 'Editar cadastro'}</h3>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:14px;align-items:center;padding:12px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-800);margin-bottom:16px">
        <canvas id="pvFace" class="bioface" width="64" height="64"></canvas>
        <div>
          <div class="eyebrow">Identificador ${novo ? 'a emitir' : 'permanente'}</div>
          <div class="mono" id="pvId" style="font-size:19px;font-weight:600;margin-top:3px">…</div>
          <div class="hint" id="pvHint" style="margin-top:3px"></div>
        </div>
      </div>
      <div class="form-grid">
        <div class="field full"><label for="pNome">Nome completo</label>
          <input class="inp" id="pNome" value="${p?esc(p.nome):''}" placeholder="Nome civil conforme documento"></div>
        <div class="field"><label for="pSexo">Sexo (registro civil)</label>
          <select class="inp" id="pSexo">
            ${['M','F','N'].map(s => `<option value="${s}" ${sexoAtual===s?'selected':''}>${ROTULO_SEXO[s]}</option>`).join('')}
          </select>
          <span class="hint">${novo ? 'Define o prefixo do identificador. Escolha definitiva no momento da emissão.'
                                    : 'Alterar <b>não</b> muda o ID já emitido.'}</span></div>
        <div class="field"><label for="pNasc">Data de nascimento</label>
          <input class="inp" id="pNasc" type="date" value="${p&&p.nascimento?esc(String(p.nascimento).slice(0,10)):''}"></div>
        <div class="field"><label for="pCpf">CPF</label>
          <input class="inp mono" id="pCpf" value="${p?esc(p.cpf||''):''}" placeholder="000.000.000-00"></div>
        <div class="field"><label for="pRg">RG</label>
          <input class="inp mono" id="pRg" value="${p?esc(p.rg||''):''}" placeholder="00.000.000-0"></div>
        <div class="field"><label for="pAlc">Alcunha (opcional)</label>
          <input class="inp" id="pAlc" value="${p?esc(p.alcunha||''):''}"></div>
        <div class="field"><label for="pUni">Unidade responsável</label>
          <select class="inp" id="pUni">${['1º BPM','2º BPM','3º BPM','DHPP','Delegacia Central']
            .map(u => `<option ${p&&p.unidade===u?'selected':''}>${u}</option>`).join('')}</select></div>
        <div class="field full"><label for="pStatus">Situação cadastral</label>
          <select class="inp" id="pStatus">${Object.entries(STATUS_PESSOA)
            .map(([k,v]) => `<option value="${k}" ${p&&p.status===k?'selected':''}>${esc(v.lbl)} — ${esc(v.desc)}</option>`).join('')}</select></div>
        <div class="field full" id="mandadoBox" hidden>
          <label for="pMand">Mandado judicial</label>
          <input class="inp mono" id="pMand" value="${p?esc(p.mandado||''):''}" placeholder="MP-2026/0000">
          <span class="hint">Obrigatório para a situação “Procurado”. Vincula o alerta ao processo de origem.</span></div>
        <div class="field full"><label for="pObs">Observações</label>
          <textarea class="inp" id="pObs" placeholder="Informações complementares relevantes à identificação">${p?esc(p.observacoes||''):''}</textarea></div>
      </div>
      <div class="note" style="margin-top:15px">${ico('shield',15)}<div>O cadastro armazena um <b>template vetorial irreversível</b> (512 dimensões), não a fotografia original. O tratamento é registrado na trilha de auditoria com finalidade, base legal e responsável.</div></div>
    </div>
    <div class="modal-ft">
      ${!novo ? `<button class="btn btn-danger" id="pDel">${ico('trash',14)} Desativar registro</button>` : ''}
      <span style="flex:1"></span>
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="pSave">${ico('check',14)} ${novo ? 'Emitir identificador' : 'Salvar alterações'}</button>
    </div>`);

  const atualizarPreview = async () => {
    const sx = $('#pSexo').value;
    let idPrev = p ? p.id_biometrico : '…';
    if(novo){
      try{ idPrev = (await API.proximoId(sx)).proximo_id; }catch(e){ idPrev = 'BIO-' + sx + '-…'; }
    }
    $('#pvId').textContent = idPrev;
    $('#pvId').style.color = {M:'#35A7DE', F:'#E05F9E', N:'#9BB0C3'}[novo ? sx : p.sexo_emissao];
    $('#pvHint').innerHTML = novo
      ? 'Sequência independente por sexo — nenhum outro registro receberá este número.'
      : (sx !== p.sexo
          ? `Sexo do cadastro passará a <b>${esc(ROTULO_SEXO[sx])}</b>; o identificador <b>permanece ${esc(p.id_biometrico)}</b>.`
          : `Emitido em ${fmtDate(p.criado_em)} · ${fmtNum(p.total_deteccoes)} captura(s) vinculada(s).`);
    drawBioFace($('#pvFace'), hash32(idPrev), novo ? sx : p.sexo_emissao, 64);
  };
  const atualizarMandado = () => { $('#mandadoBox').hidden = $('#pStatus').value !== 'PROCURADO'; };

  $('#pSexo').onchange = atualizarPreview;
  $('#pStatus').onchange = atualizarMandado;
  await atualizarPreview();
  atualizarMandado();

  const bd = $('#pDel');
  if(bd) bd.onclick = async () => {
    const motivo = await confirmar('Desativar registro',
      `Desativar <b>${esc(p.id_biometrico)}</b> — ${esc(p.nome)}?<br><br>O identificador não será reutilizado, o template biométrico é apagado e o histórico de capturas permanece íntegro.`,
      {rotulo:'Desativar', perigo:true, exigeTexto:'Motivo da desativação'});
    if(!motivo) return;
    try{
      await API.desativarPessoa(p.id_biometrico, motivo);
      fecharModal();
      toast('Registro desativado', `${p.id_biometrico} removido das buscas 1:N.`, 'warn');
      irPara('biometrics');
    }catch(e){ erroToast(e); }
  };

  $('#pSave').onclick = async () => {
    const dados = {
      nome: $('#pNome').value.trim(),
      sexo: $('#pSexo').value,
      status: $('#pStatus').value,
      alcunha: $('#pAlc').value.trim(),
      nascimento: $('#pNasc').value || null,
      cpf: $('#pCpf').value.trim() || null,
      rg: $('#pRg').value.trim() || null,
      mandado: $('#pMand').value.trim(),
      unidade: $('#pUni').value,
      observacoes: $('#pObs').value.trim()
    };
    if(dados.nome.length < 5){ toast('Nome inválido', 'Informe o nome civil completo.', 'err'); return; }

    const btn = $('#pSave');
    btn.disabled = true;
    try{
      if(novo){
        const criado = await API.criarPessoa(dados);
        fecharModal();
        toast('Identificador emitido', `${criado.id_biometrico} — ${criado.nome}`, 'ok', 5500);
        irPara('biometrics');
        setTimeout(() => abrirPessoa(criado.id_biometrico), 400);
      }else{
        const sexoMudou = dados.sexo !== p.sexo;
        await API.salvarPessoa(p.id_biometrico, dados);
        fecharModal();
        toast('Cadastro atualizado',
          sexoMudou ? `${p.id_biometrico} mantido — o identificador não muda com a correção do sexo.`
                    : `${p.id_biometrico} — ${dados.nome}`, 'ok', 5500);
        irPara('biometrics');
      }
    }catch(e){ erroToast(e); }
    finally{ btn.disabled = false; }
  };
}
