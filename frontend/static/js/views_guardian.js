/* ===================== Aizon Guardian =====================
   Sinais de socorro e de possível violência, para avaliação humana.

   A tela mostra o que a IA viu — o gesto Signal for Help, sinais de
   trajetória (possível agressão, queda, contato com fuga, corrida) — reunido
   em ocorrências com prioridade. Nada aqui afirma crime nem aponta pessoa:
   toda ocorrência termina numa avaliação humana justificada, e abrir uma
   ocorrência ou assistir ao vídeo fica na trilha de auditoria. */

const GUARD = {dias: 30, filtro: {status: '', prioridade: ''}, pagina: 1, atualizando: null};
const GUARD_PRIO = {
  CRITICA: {rot: 'Crítica', cor: 'var(--red)', cls: 'gp-critica'},
  ALTA:    {rot: 'Alta',    cor: '#F0712C',    cls: 'gp-alta'},
  MEDIA:   {rot: 'Média',   cor: 'var(--amber)', cls: 'gp-media'},
  BAIXA:   {rot: 'Baixa',   cor: 'var(--acc)', cls: 'gp-baixa'},
};
const GUARD_STATUS = {NOVA: 'Nova', EM_ANALISE: 'Em análise', RISCO_CONFIRMADO: 'Risco confirmado',
                      FALSO_ALARME: 'Falso alarme', ENCERRADA: 'Encerrada'};
const guardPrio = p => `<span class="gp ${(GUARD_PRIO[p] || {}).cls || ''}">${esc((GUARD_PRIO[p] || {}).rot || p)}</span>`;
const guardStatus = s => `<span class="gs gs-${esc(s.toLowerCase())}">${esc(GUARD_STATUS[s] || s)}</span>`;

function guardDuracao(s){
  if(s == null) return '—';
  if(s < 90) return `${Math.round(s)} s`;
  if(s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1).replace('.', ',')} h`;
}

function guardCobertura(v){
  if(!v) return `<span class="gc gc-off">${ico('clock', 12)} aguardando a câmera</span>`;
  if(v.gesto_disponivel === false) return `<span class="gc gc-off">${ico('alert', 12)} detector de mãos indisponível</span>`;
  if(v.gesto_legivel === true) return `<span class="gc gc-ok">${ico('check', 12)} gesto legível · mãos ~${v.mao_mediana_px} px</span>`;
  if(v.gesto_legivel === false) return `<span class="gc gc-baixo" title="Mãos abaixo de 48 px: os dedos não se separam. Só os sinais de trajetória valem aqui.">${ico('info', 12)} mãos pequenas (~${v.mao_mediana_px} px): só trajetória</span>`;
  return `<span class="gc gc-off">${ico('clock', 12)} medindo as mãos…</span>`;
}

function guardCartao(o){
  return `<button class="gcard ${(GUARD_PRIO[o.prioridade] || {}).cls || ''}" data-oc="${esc(o.codigo)}">
    <span class="gcard-barra"></span>
    <span class="gcard-corpo">
      <span class="gcard-topo">${guardPrio(o.prioridade)}<b>${esc(o.codigo)}</b>${guardStatus(o.status)}<span class="sp"></span>
        <span class="td-ts">${relTime(o.ultimo_sinal)}</span></span>
      <span class="gcard-cam">${ico('cam', 13)} ${esc(o.camera ? o.camera.nome : '')}</span>
      <span class="gcard-sinais">${o.sinais_resumo.map(s => `<i>${esc(s)}</i>`).join('')}</span>
      <span class="gcard-pe">${o.n_sinais} sinal(is) · confiança máx. ${Math.round((o.confianca || 0) * 100)}%
        ${o.tem_clipe ? ` · ${ico('play', 11)} vídeo` : ''}</span>
    </span></button>`;
}

function guardLinhaTempo(itens){
  const fim = Date.now(), ini = fim - 24 * 3600e3;
  const marcas = itens.filter(o => new Date(o.inicio).getTime() >= ini);
  const horas = Array.from({length: 8}, (_, i) => ini + i * 3 * 3600e3);   // a 9ª seria "agora"
  return `<div class="gtl">
    <div class="gtl-trilho">
      ${horas.map(h => `<span class="gtl-hora" style="left:${(h - ini) / (fim - ini) * 100}%">${String(new Date(h).getHours()).padStart(2, '0')}h</span>`).join('')}
      ${marcas.map(o => { const x = (new Date(o.inicio).getTime() - ini) / (fim - ini) * 100;
        return `<button class="gtl-marca ${(GUARD_PRIO[o.prioridade] || {}).cls}" data-oc="${esc(o.codigo)}" style="left:${x}%"
          title="${esc(o.codigo)} · ${esc((GUARD_PRIO[o.prioridade] || {}).rot)} · ${esc(o.camera ? o.camera.nome : '')} · ${fmtDT(o.inicio)}"></button>`; }).join('')}
      <span class="gtl-agora" style="left:100%">agora</span>
    </div>
    <div class="gtl-leg">${Object.entries(GUARD_PRIO).map(([k, v]) => `<span><i class="${v.cls}"></i>${v.rot}</span>`).join('')}
      <span class="sp"></span><span class="td-ts">${marcas.length} ocorrência(s) nas últimas 24 h</span></div>
  </div>`;
}

function guardBarras(lista, chave, rotulo, cor){
  const max = Math.max(1, ...lista.map(x => x[chave]));
  if(!lista.some(x => x[chave])) return `<span class="hint">Sem ocorrências no período.</span>`;
  return lista.map(x => `<div class="gbar"><span class="gbar-n">${esc(x[rotulo])}</span>
    <span class="gbar-t"><i style="width:${x[chave] / max * 100}%;background:${typeof cor === 'function' ? cor(x) : cor}"></i></span>
    <b>${fmtNum(x[chave])}</b></div>`).join('');
}

async function guardCarregarHistorico(){
  const box = $('#guardHist');
  if(!box) return;
  try{
    const r = await API.guardianOcorrencias({...GUARD.filtro, pagina: GUARD.pagina, por_pagina: 15});
    const cont = $('#guardCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} ocorrência(s)`;
    box.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl guard-tbl">
      <thead><tr><th>Ocorrência</th><th>Prioridade</th><th>Sinais</th><th>Câmera</th><th>Início</th><th>Situação</th></tr></thead>
      <tbody>${r.itens.map(o => `<tr class="clickable" data-oc="${esc(o.codigo)}" style="--cat:${(GUARD_PRIO[o.prioridade] || {}).cor || 'var(--acc)'}">
        <td><b class="fc-cam mono">${esc(o.codigo)}</b><span class="fc-sub">${o.n_sinais} sinal(is) · confiança ${Math.round((o.confianca || 0) * 100)}%</span></td>
        <td>${guardPrio(o.prioridade)}</td>
        <td><div class="gcard-sinais">${o.sinais_resumo.map(x => `<i>${esc(x)}</i>`).join('')}</div></td>
        <td><b class="fc-cam">${esc(o.camera ? o.camera.nome : '')}</b><span class="fc-sub mono">${esc(o.camera ? o.camera.codigo : '')}</span></td>
        <td><span class="fc-hora mono">${fmtDT(o.inicio)}</span><span class="fc-sub">${relTime(o.inicio)}</span></td>
        <td>${guardStatus(o.status)}</td></tr>`).join('') || `<tr><td colspan="6">${vazio('guardian', 'Nenhuma ocorrência.', 'Os sinais das câmeras com o Guardian ligado aparecem aqui.')}</td></tr>`}
      </tbody></table></div>${pagerHTML(r.total, r.pagina, r.por_pagina, 'guard')}`;
    $$('tr[data-oc]', box).forEach(t => t.onclick = () => guardAbrir(t.dataset.oc));
    $$('[data-pg]', box).forEach(b => b.onclick = () => { GUARD.pagina = +b.dataset.pg.split(':')[1]; guardCarregarHistorico(); });
  }catch(e){ box.innerHTML = erroBloco(e); }
}

async function guardAtualizarPainel(){
  if(APP.view !== 'guardian' || !$('#guardAbertas')) return;
  try{
    const [est, abertas, recentes] = await Promise.all([
      API.guardianEstado(), API.guardianOcorrencias({abertas: true, por_pagina: 30}),
      API.guardianOcorrencias({desde: new Date(Date.now() - 24 * 3600e3).toISOString(), por_pagina: 200})]);
    const a = est.abertas, total = Object.values(a).reduce((x, y) => x + y, 0);
    $('#guardKCrit').textContent = fmtNum(a.CRITICA || 0);
    $('#guardKCritBox').classList.toggle('gk-alerta', (a.CRITICA || 0) > 0);
    $('#guardKAbertas').textContent = fmtNum(total);
    $('#guardKHoje').textContent = fmtNum(est.hoje);
    $('#guardAbertas').innerHTML = abertas.itens.length ? abertas.itens.map(guardCartao).join('')
      : vazio('guardian', 'Nenhuma ocorrência aberta.', 'Quando a IA vir um sinal de risco, ele aparece aqui para avaliação.');
    $('#guardAbertasN').textContent = `${abertas.total} aberta(s)`;
    $('#guardTempo').innerHTML = guardLinhaTempo(recentes.itens);
    for(const c of est.cameras){
      const el = $(`[data-gcob="${CSS.escape(c.codigo)}"]`);
      if(el) el.innerHTML = guardCobertura(c.ao_vivo);
    }
    $$('#guardAbertas [data-oc], #guardTempo [data-oc]').forEach(b => b.onclick = () => guardAbrir(b.dataset.oc));
  }catch(e){ /* a próxima volta tenta de novo */ }
}

async function guardCarregarEstatisticas(){
  try{
    const s = await API.guardianEstatisticas({dias: GUARD.dias});
    $('#guardKFalso').textContent = s.taxa_falso_alarme == null ? '—' : `${Math.round(s.taxa_falso_alarme * 100)}%`;
    $('#guardKTempo').textContent = guardDuracao(s.tempo_medio_avaliacao_s);
    $('#guardPorSinal').innerHTML = guardBarras(s.por_sinal, 'total', 'rotulo', 'linear-gradient(90deg,#8A226A,#E45A31)');
    $('#guardPorPrio').innerHTML = guardBarras(Object.entries(s.por_prioridade).map(([k, v]) => ({k, r: GUARD_PRIO[k].rot, v})),
      'v', 'r', x => GUARD_PRIO[x.k].cor);
    $('#guardPorStatus').innerHTML = guardBarras(Object.entries(s.por_status).map(([k, v]) => ({r: GUARD_STATUS[k], v})),
      'v', 'r', 'var(--acc)');
  }catch(e){ erroToast(e); }
}

async function renderGuardian(alvo){
  clearInterval(GUARD.atualizando);
  const est = await API.guardianEstado();
  const cams = (APP.cameras && APP.cameras.length ? APP.cameras : await API.cameras())
    .filter(c => est.cameras.some(g => g.codigo === c.codigo));
  const gesto = est.servico.gesto_disponivel ? 'ativo' : (est.servico.ativo ? 'carregando' : 'desligado');
  const kpi = (cls, id, lbl, icone, sub, boxId) => `<div class="kpi ${cls}" ${boxId ? `id="${boxId}"` : ''}>
    <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone, 15)}</span></div>
    <div class="kpi-val" id="${id}">—</div><div class="kpi-delta nt">${sub}</div></div>`;
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Aizon Guardian</h2><span class="cm-sub">sinais de socorro e de possível violência, para avaliação humana</span><span class="sp"></span>
    <div class="toolbar"><select class="inp" id="guardDias">${[7, 30, 90].map(d => `<option value="${d}" ${GUARD.dias === d ? 'selected' : ''}>últimos ${d} dias</option>`).join('')}</select></div></div>

  <div class="guard-kpis">
    ${kpi('k-red', 'guardKCrit', 'Críticas abertas', 'alert', 'socorro ou sinais combinados', 'guardKCritBox')}
    ${kpi('k-amber', 'guardKAbertas', 'Abertas', 'bell', 'aguardando avaliação')}
    ${kpi('', 'guardKHoje', 'Hoje', 'clock', 'ocorrências abertas hoje')}
    ${kpi('k-violet', 'guardKFalso', 'Falso alarme', 'x', 'das avaliadas no período')}
    ${kpi('k-green', 'guardKTempo', 'Tempo até avaliar', 'check', 'média do período')}
  </div>
  <p class="fc-nota guard-nota" title="A IA identifica o gesto Signal for Help e sinais de trajetória (possível agressão, queda, contato com fuga, corrida). Abrir uma ocorrência ou assistir ao vídeo fica na trilha de auditoria (LGPD).">${ico('shield', 13)}<span><b>Indícios, não conclusões:</b> a IA não confirma crimes nem aponta culpados — toda ocorrência exige avaliação humana justificada.</span></p>

  <div class="guard-grade">
    <div class="panel"><div class="panel-hd"><h3>Câmeras com o Guardian</h3><span class="sub">${est.cameras.length} câmera(s)</span><span class="sp"></span>
        <span class="epi-vivo ${gesto === 'ativo' ? '' : 'guard-off'}">gesto de socorro ${gesto}</span></div>
      <div class="panel-bd">${cams.length ? `<div class="guard-cams ${cams.length === 1 ? 'uma' : ''}">${cams.slice(0, 4).map(c => `
        <div class="guard-cam"><div class="guard-tela"><canvas data-gcam="${esc(c.codigo)}"></canvas></div>
          <div class="guard-cam-pe"><div><b>${esc(c.nome)}</b><span class="mono">${esc(c.codigo)}</span></div>
            <div class="guard-cob" data-gcob="${esc(c.codigo)}">${guardCobertura((est.cameras.find(g => g.codigo === c.codigo) || {}).ao_vivo)}</div></div></div>`).join('')}</div>
        ${cams.length > 4 ? `<p class="hint" style="margin:10px 0 0">Mostrando 4 de ${cams.length}; todas seguem sendo analisadas.</p>` : ''}`
        : `<div class="note">${ico('info', 15)}<div>Nenhuma câmera com o Guardian ligado. Ligue em <b>Câmeras › Configurar › Aizon Guardian</b>.
           O gesto de socorro só é legível em câmeras próximas (recepção, balcão, entrada), com a mão grande na imagem;
           nas demais, o Guardian usa os sinais de trajetória.</div></div>`}</div></div>
    <div class="panel guard-painel"><div class="panel-hd"><h3>Ocorrências abertas</h3><span class="sub" id="guardAbertasN"></span></div>
      <div class="panel-bd" id="guardAbertas">${carregando()}</div></div>
  </div>

  <div class="panel" style="margin-top:14px"><div class="panel-hd"><h3>Linha do tempo</h3><span class="sub">últimas 24 horas · clique numa marca para abrir</span></div>
    <div class="panel-bd" id="guardTempo">${carregando()}</div></div>

  <div class="guard-grade2">
    <div>
      <div class="veic-hist-hd" style="margin-top:0"><h3>Histórico de ocorrências</h3><span>clique numa linha para avaliar</span></div>
      <div class="cm-barra fc-barra">
        <select class="inp" id="guardFPrio"><option value="">Todas as prioridades</option>
          ${Object.entries(GUARD_PRIO).map(([k, v]) => `<option value="${k}" ${GUARD.filtro.prioridade === k ? 'selected' : ''}>${v.rot}</option>`).join('')}</select>
        <select class="inp" id="guardFStatus"><option value="">Todas as situações</option>
          ${Object.entries(GUARD_STATUS).map(([k, v]) => `<option value="${k}" ${GUARD.filtro.status === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
        <span class="sp"></span><span class="cm-cont" id="guardCont"></span>
      </div>
      <div class="panel"><div class="panel-bd tight" id="guardHist">${carregando()}</div></div>
    </div>
    <div class="panel guard-dist"><div class="panel-hd"><h3>Distribuição no período</h3><span class="sub" id="guardDistSub">últimos ${GUARD.dias} dias</span></div>
      <div class="panel-bd">
        <section><h4>Sinais que abriram ocorrências</h4><div id="guardPorSinal">${carregando()}</div></section>
        <section><h4>Por prioridade</h4><div id="guardPorPrio"></div></section>
        <section><h4>Por situação</h4><div id="guardPorStatus"></div></section>
      </div></div>
  </div>`;

  for(const c of cams.slice(0, 4)){
    FEED.register($(`[data-gcam="${CSS.escape(c.codigo)}"]`), c, {zonas: false, boxes: true});
  }
  if(cams.length) FEED.start();
  $('#guardDias').onchange = e => { GUARD.dias = +e.target.value; $('#guardDistSub').textContent = `últimos ${GUARD.dias} dias`; guardCarregarEstatisticas(); };
  const filtrar = () => { GUARD.filtro = {prioridade: $('#guardFPrio').value, status: $('#guardFStatus').value};
                          GUARD.pagina = 1; guardCarregarHistorico(); };
  $('#guardFPrio').onchange = filtrar; $('#guardFStatus').onchange = filtrar;
  await Promise.all([guardAtualizarPainel(), guardCarregarHistorico(), guardCarregarEstatisticas()]);
  GUARD.atualizando = setInterval(() => {
    if(APP.view !== 'guardian'){ clearInterval(GUARD.atualizando); return; }
    guardAtualizarPainel();
  }, 5000);
}

/* Evento ao vivo: ocorrência nova ou que subiu de prioridade. */
let _guardDebounce = null;
function guardianAoVivo(d){
  if(APP.view !== 'guardian') return;
  clearTimeout(_guardDebounce);
  _guardDebounce = setTimeout(() => { guardAtualizarPainel(); guardCarregarHistorico(); }, 400);
}

async function guardAbrir(codigo){
  let o;
  try{ o = await API.guardianOcorrencia(codigo); }catch(e){ erroToast(e); return; }
  const fechada = !['NOVA', 'EM_ANALISE'].includes(o.status);
  const midia = o.tem_clipe
    ? `<img class="guard-video" src="${API.base}/guardian/ocorrencias/${encodeURIComponent(o.codigo)}/clipe?token=${encodeURIComponent(API.access)}&t=${Date.now()}" alt="Vídeo de evidência">`
    : (o.tem_imagem ? `<img class="guard-video" src="${API.base}/guardian/ocorrencias/${encodeURIComponent(o.codigo)}/imagem?token=${encodeURIComponent(API.access)}" alt="Imagem do sinal">`
       : `<div class="guard-sem">${vazio('cam', 'Sem imagem guardada.', '')}</div>`);
  const box = abrirModal(`
    <div class="modal-hd"><h3>${esc(o.codigo)} ${guardPrio(o.prioridade)} ${guardStatus(o.status)}
      <span class="sub">${esc(o.camera ? o.camera.nome : '')} · ${fmtDT(o.inicio)}</span></h3>
      <button class="x" data-close aria-label="Fechar">${ico('x', 16)}</button></div>
    <div class="modal-bd">
      <div class="guard-ficha">
        <div>
          <div class="guard-midia">${midia}
            <span class="guard-midia-tag">${o.tem_clipe ? `${ico('play', 11)} vídeo de evidência · ${GUARD_CLIPE_ROTULO}` : 'imagem do sinal'}</span></div>
          ${o.tem_clipe && o.tem_imagem ? `<button class="btn btn-ghost btn-sm" id="guardRever" style="margin-top:8px">${ico('play', 13)} Rever o vídeo</button>` : ''}
          <div class="eyebrow" style="margin-top:16px">Sinais vistos pela IA</div>
          <div class="guard-sinais">${o.sinais.map(s => `<div class="gsin gsin-${esc(s.tipo)}">
            <span class="gsin-t">${new Date(s.em).toLocaleTimeString('pt-BR')}</span>
            <span class="gsin-c"><b>${esc(s.rotulo)}</b><small>${Math.round(s.confianca * 100)}% de confiança · registro
              <a href="#" data-det="${esc(s.deteccao)}">${esc(s.deteccao)}</a></small>
              ${s.detalhe ? `<small>${esc(s.detalhe)}</small>` : ''}</span></div>`).join('')}</div>
        </div>
        <div>
          <dl class="kv">
            <dt>Prioridade</dt><dd>${guardPrio(o.prioridade)} <span class="td-ts">pontuação ${o.pontuacao}</span></dd>
            <dt>Câmera</dt><dd>${esc(o.camera ? `${o.camera.nome} (${o.camera.codigo})` : '')}</dd>
            <dt>Período</dt><dd>${fmtDT(o.inicio)} → ${fmtDT(o.ultimo_sinal)}</dd>
            <dt>Confiança máx.</dt><dd>${Math.round((o.confianca || 0) * 100)}%</dd>
            ${o.alerta ? `<dt>Alerta</dt><dd>${esc(o.alerta)}</dd>` : ''}
          </dl>
          <div class="guard-aval">
            <div class="eyebrow">Avaliação humana</div>
            <p class="veic-sub">A IA indica possibilidade, não fato. Confira o vídeo antes de decidir; a justificativa é obrigatória e fica registrada.</p>
            <div class="segm guard-segm" id="guardAvStatus">
              ${[['EM_ANALISE', 'Em análise'], ['RISCO_CONFIRMADO', 'Risco confirmado'], ['FALSO_ALARME', 'Falso alarme'], ['ENCERRADA', 'Encerrar']]
                .map(([k, v]) => `<button data-s="${k}" class="${o.status === k ? 'on' : ''}">${v}</button>`).join('')}</div>
            <textarea class="inp" id="guardAvNota" rows="3" placeholder="O que se viu no vídeo e o que foi feito (mín. 10 caracteres)"></textarea>
            <button class="btn btn-primary btn-sm" id="guardAvSalvar">${ico('check', 14)} Registrar avaliação</button>
            ${fechada ? `<p class="veic-sub" style="margin-top:8px">Ocorrência já avaliada. Uma nova avaliação reabre o histórico com a sua justificativa.</p>` : ''}
          </div>
          <div class="eyebrow" style="margin-top:16px">Histórico</div>
          <div class="guard-hist">${o.historico.slice().reverse().map(h => `<div class="gh-item ${h.por === 'IA' ? 'gh-ia' : 'gh-pessoa'}">
            <span class="gh-quem">${h.por === 'IA' ? 'IA' : esc(h.por)}</span>
            <span class="gh-txt">${h.status ? `<b>${esc(GUARD_STATUS[h.status] || h.status)}</b> — ` : ''}${esc(h.texto || '')}
              <small>${fmtDT(h.em)}</small></span></div>`).join('')}</div>
        </div>
      </div>
    </div>`, true);
  let escolha = null;
  $$('#guardAvStatus button', box).forEach(b => b.onclick = () => {
    escolha = b.dataset.s; $$('#guardAvStatus button', box).forEach(x => x.classList.toggle('on', x === b));
  });
  $$('[data-det]', box).forEach(a => a.onclick = e => { e.preventDefault(); fecharModal(); abrirDeteccao(a.dataset.det); });
  const rever = $('#guardRever', box);
  if(rever) rever.onclick = () => { const v = $('.guard-video', box); v.src = v.src.replace(/&t=\d+/, '&t=' + Date.now()); };
  $('#guardAvSalvar', box).onclick = async () => {
    const nota = $('#guardAvNota', box).value.trim();
    if(!escolha){ toast('Escolha a situação', 'Em análise, risco confirmado, falso alarme ou encerrar.', 'warn'); return; }
    if(nota.length < 10){ toast('Justificativa curta', 'Descreva em pelo menos 10 caracteres o que foi visto.', 'warn'); return; }
    try{
      await API.guardianAvaliar(o.codigo, {status: escolha, nota});
      toast('Avaliação registrada', `${o.codigo}: ${GUARD_STATUS[escolha]}.`, 'ok');
      fecharModal();
      guardAtualizarPainel(); guardCarregarHistorico(); guardCarregarEstatisticas();
    }catch(e){ erroToast(e); }
  };
}
const GUARD_CLIPE_ROTULO = 'segundos antes e depois do sinal, cifrado no servidor';
