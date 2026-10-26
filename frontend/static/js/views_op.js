/* ==========================================================
   SEGURANÇA — Telas operacionais: painel, mosaico ao vivo e câmeras
   ========================================================== */
'use strict';

/* ==========================================================
   PAINEL
   ========================================================== */
/** Distribui a série do servidor nos baldes do intervalo pedido.

   Acima de dois dias o balde deixa de ser a hora e passa a ser o dia: um mês
   em barras de hora são 720 pontos espremidos, que não se leem. */
function seriePeriodo(resumo, inicio, fim){
  const porHora = (fim - inicio) <= 2 * 864e5;
  const passo = porHora ? 36e5 : 864e5;
  const base = porHora
    ? new Date(inicio.getFullYear(), inicio.getMonth(), inicio.getDate(), inicio.getHours())
    : new Date(inicio.getFullYear(), inicio.getMonth(), inicio.getDate());
  const n = Math.max(1, Math.ceil((fim - base) / passo));

  const vazios = () => new Array(n).fill(0);
  const series = {FACIAL: vazios(), OBJETO: vazios(), ACAO: vazios()};
  (resumo.serie_horaria || []).forEach(r => {
    const i = Math.floor((new Date(r.hora).getTime() - base.getTime()) / passo);
    if(i >= 0 && i < n && series[r.tipo]) series[r.tipo][i] += r.total;
  });

  const cada = Math.max(1, Math.ceil(n / 8));
  const labels = Array.from({length: n}, (_, i) => {
    if(i % cada) return '';
    const d = new Date(base.getTime() + i * passo);
    return porHora ? pad(d.getHours()) + 'h' : `${pad(d.getDate())}/${pad(d.getMonth()+1)}`;
  });
  return {series, labels};
}


async function renderPainel(alvo){
  const hojeStr = diaISO(new Date());
  const dataDesdeStr = APP.periodoDesde || hojeStr;
  const dataAteStr = APP.periodoAte || hojeStr;
  // Sem o sufixo Z: a data escolhida no calendário é o dia de quem olha a
  // tela, não o dia em UTC — o toISOString() adiante faz a conversão.
  const inicio = new Date(dataDesdeStr + 'T00:00:00');
  const fim = new Date(dataAteStr + 'T23:59:59.999');
  const retencao = ((APP.cfg || {}).config || {}).retencao_dias || 90;
  const minStr = diaISO(new Date(Date.now() - retencao * 864e5));

  const [resumo, resumoAlertas, esquema, ultimas, alertasAbertos] = await Promise.all([
    API.resumoDeteccoes({desde: inicio.toISOString(), ate: fim.toISOString()}),
    API.pode('alerts') ? API.resumoAlertas() : Promise.resolve({}),
    API.pode('biometrics') ? API.esquemaIds() : Promise.resolve({prefixos:{}}),
    // Rosto ou corpo: os 6 mais recentes de ambos. O tipo aparece em d.tipo.
    API.deteccoes({apenas_match:true, por_pagina:6}),
    API.pode('alerts') ? API.alertas({status:'ABERTO', por_pagina:6}) : Promise.resolve({itens:[]})
  ]);

  const t = resumo.por_tipo || {};
  const fac = t.FACIAL || 0, obj = t.OBJETO || 0, act = t.ACAO || 0;
  const total = resumo.total || 0;
  const {series, labels} = seriePeriodo(resumo, inicio, fim);
  const rotuloPeriodo = dataDesdeStr === dataAteStr
    ? fmtDate(inicio)
    : `${fmtDate(inicio)} – ${fmtDate(fim)}`;
  const online = APP.cameras.filter(c => c.status === 'ONLINE').length;
  const abertos = (resumoAlertas.abertos || 0) + (resumoAlertas.em_andamento || 0);
  const pref = esquema.prefixos || {};

  const maxTop = Math.max(1, ...(resumo.top_cameras || []).map(c => c.total));
  const porSetor = {};
  (resumo.top_cameras || []).forEach(c => { porSetor[c.setor] = (porSetor[c.setor] || 0) + c.total; });
  const setores = Object.entries(porSetor).sort((a,b) => b[1]-a[1])
    .map(([k,v]) => ({k, v, cor:'var(--acc)'}));

  const diasAtras = d => diaISO(new Date(Date.now() - d * 864e5));
  const atalhoAtivo = dataAteStr !== hojeStr ? ''
    : {[hojeStr]:'hoje', [diasAtras(6)]:'7d', [diasAtras(29)]:'30d'}[dataDesdeStr] || '';

  alvo.innerHTML = `
  <div class="toolbar" style="margin-bottom:14px">
    <div class="periodo">
      <label for="inpDesde">Período</label>
      <input class="inp" id="inpDesde" type="date" value="${dataDesdeStr}" min="${minStr}" max="${dataAteStr}">
      <span class="sep">até</span>
      <input class="inp" id="inpAte" type="date" value="${dataAteStr}" min="${dataDesdeStr}" max="${hojeStr}">
    </div>
    <div class="segm" id="periodoAtalhos">
      ${[['hoje','Hoje'],['7d','7 dias'],['30d','30 dias']].map(([k,l]) =>
        `<button data-atalho="${k}" class="${atalhoAtivo === k ? 'on' : ''}">${l}</button>`).join('')}
    </div>
  </div>

  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-green">
      <div class="kpi-top"><span class="kpi-lbl">Câmeras on-line</span><span class="kpi-ico">${ico('cam',15)}</span></div>
      <div class="kpi-val">${online}<span style="font-size:15px;color:var(--txt-3)">/${APP.cameras.length}</span></div>
      <div class="kpi-delta ${online === APP.cameras.length ? 'up' : 'nt'}">${APP.cameras.length ? fmtPct(online/APP.cameras.length*100) : '—'} de disponibilidade</div>
    </div>
    <div class="kpi">
      <div class="kpi-top"><span class="kpi-lbl">Detecções no período</span><span class="kpi-ico">${ico('box',15)}</span></div>
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
      <div class="panel-hd"><h3>Fluxo de detecções</h3><span class="sub">${rotuloPeriodo}</span><span class="sp"></span>
        <div class="legend">
          <span><i style="background:var(--acc)"></i>Facial</span>
          <span><i style="background:var(--green)"></i>Objetos</span>
          <span><i style="background:var(--violet)"></i>Ações</span>
        </div></div>
      <div class="panel-bd">${areaChart([
        {data: series.OBJETO, cor:'var(--green)'},
        {data: series.FACIAL, cor:'var(--acc)'},
        {data: series.ACAO,   cor:'var(--violet)'}
      ], {labels, h:214})}</div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Composição</h3><span class="sub">${rotuloPeriodo}</span></div>
      <div class="panel-bd" style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;justify-content:center">
        ${donut([
          {v:obj, cor:'var(--green)', nome:'Objetos'},
          {v:fac, cor:'var(--acc)',   nome:'Facial'},
          {v:act, cor:'var(--violet)', nome:'Ações'}
        ], {centro: fmtNum(total), sub:'EVENTOS'})}
        <div style="display:flex;flex-direction:column;gap:9px;min-width:146px">
          ${[['Objetos',obj,'var(--green)'],['Facial',fac,'var(--acc)'],['Ações',act,'var(--violet)']].map(([k,v,c]) => `
            <div style="display:flex;align-items:center;gap:8px">
              <i style="width:9px;height:9px;border-radius:2px;background:${c};flex:none"></i>
              <span style="flex:1;font-size:12.5px;color:var(--txt-2)">${k}</span>
              <b class="mono" style="font-size:13px">${fmtNum(v)}</b>
              <span class="mono" style="font-size:10.5px;color:var(--txt-4);width:46px;text-align:right">${pctCurto(v, total)}</span>
            </div>`).join('')}
          <!-- O que a rosca não diz sozinha: das 1.207 leituras do dia, quantas
               foram comportamento e não só "a câmera viu alguém". É o número
               que decide se a vigilância rendeu alguma coisa. -->
          <div class="comp-destaque">
            <span>Eventos de comportamento</span>
            <b class="mono">${fmtNum(act)}</b>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="grid cols-3" style="margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Câmeras mais acionadas</h3><span class="sub">${rotuloPeriodo}</span>
        <span class="sp" style="flex:1"></span>
        <span class="chip c-gray">${(resumo.top_cameras || []).length} de ${APP.cameras.length}</span></div>
      <div class="rank rank-rolavel">
        ${(resumo.top_cameras || []).map((c, i) => `
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
      <div class="panel-hd"><h3>Detecções por setor</h3><span class="sub">${rotuloPeriodo}</span>
        <span class="sp" style="flex:1"></span>
        <span class="chip c-gray">${setores.length} setor(es)</span></div>
      <div class="panel-bd rank-rolavel">${barrasH(setores)}</div>
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
        <div class="note">${ico('info',15)}<div>O sexo é atributo <b>do cadastro</b>, informado no registro da pessoa — nunca inferido pelo modelo a partir da imagem. O identificador não muda entre capturas, exceto na saída de BIO-N: quem entrou sem identificação recebe o prefixo definitivo quando a pessoa for cadastrada.</div></div>
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

  // Seletor de período. Por irPara, e não por renderPainel direto: o
  // tratamento de erro do carregamento está lá, e o painel não pode ficar
  // mudo se a consulta falhar.
  const aplicarPeriodo = (desde, ate) => {
    APP.periodoDesde = desde;
    APP.periodoAte = ate;
    irPara('dash');
  };
  const inpDesde = $('#inpDesde'), inpAte = $('#inpAte');
  // O calendário vazia o campo quando se apaga a data; aí vale o dia de hoje.
  inpDesde.onchange = () => aplicarPeriodo(inpDesde.value || hojeStr, dataAteStr);
  inpAte.onchange = () => aplicarPeriodo(dataDesdeStr, inpAte.value || hojeStr);
  $$('#periodoAtalhos button').forEach(b => b.onclick = () =>
    aplicarPeriodo(diasAtras({hoje:0, '7d':6, '30d':29}[b.dataset.atalho]), hojeStr));

  // Identificações recentes (rosto ou corpo)
  const itens = ultimas.itens || [];
  $('#dashFacial').innerHTML = itens.length ? `<div class="rank">${itens.map(d => {
    const p = d.pessoa, st = STATUS_PESSOA[p.status] || STATUS_PESSOA.CIDADAO;
    const ehRosto = d.tipo === 'FACIAL';
    // Rosto traz similaridade; corpo, confiança. Mostrar qual está disponível.
    const metrica = ehRosto ? (d.similaridade ? pct100(d.similaridade) : '—')
                            : (d.confianca ? pct100(d.confianca) : '—');
    return `<div class="rank-row clickable" data-det="${esc(d.codigo)}" style="cursor:pointer">
      ${ehRosto ? `<canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="38" height="38"></canvas>`
                : `<canvas data-snap="${snapAttr(d)}" width="38" height="48"></canvas>`}
      <div class="rank-bd"><b>${esc(p.nome)}</b>
        <span class="sub">${esc(p.id_biometrico)} · ${esc((d.camera ? d.camera.nome : '').slice(0,26))}</span></div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
        <span class="chip" style="font-size:9.5px;padding:3px 6px">${ehRosto ? 'Rosto' : 'Corpo'}</span>
        <span class="mono" style="font-size:10.5px;color:var(--txt-3)">${relTime(d.ocorrido_em)} · ${metrica}</span>
      </div></div>`;
  }).join('')}</div>` : vazio('face','Nenhuma identificação positiva recente.');
  pintarFaces($('#dashFacial'));
  pintarSnaps($('#dashFacial'));
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
  alvo.innerHTML = `
  <div class="live-shell">
    <div class="live-bar">
      <div class="segm" id="layoutSeg">
        ${[[1,'1×1'],[4,'2×2'],[9,'3×3'],[16,'4×4']].map(([n,l]) =>
          `<button data-n="${n}" class="${APP.mosaico === n ? 'on' : ''}">${l}</button>`).join('')}
      </div>
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
    APP.mosaico = +b.dataset.n; APP.paginaMosaico = 0;
    // Trocar o arranjo sem remontar a tela. Remontar substituiria a casca, e
    // em tela cheia é ela que o navegador está exibindo: a pessoa cairia fora
    // da parede justamente ao usar o controle que existe para não precisar
    // sair. `montarMosaico` já reescreve a grade a partir de `APP.mosaico`.
    $$('#layoutSeg button').forEach(x => x.classList.toggle('on', x === b));
    montarMosaico();
  });
  $('#pgPrev').onclick = () => { if(APP.paginaMosaico > 0){ APP.paginaMosaico--; montarMosaico(); } };
  $('#pgNext').onclick = () => {
    if(!APP.mosaico) return;   // com todas na tela não há página seguinte
    if((APP.paginaMosaico+1) * APP.mosaico < APP.cameras.length){ APP.paginaMosaico++; montarMosaico(); }
  };
  $('#btnPause').onclick = () => {
    APP.pausado = !APP.pausado;
    APP.pausado ? FEED.stop() : FEED.start();
    $('#btnPause').innerHTML = (APP.pausado ? ico('play',13) : ico('pause',13)) +
      `<span>${APP.pausado ? 'Retomar' : 'Pausar'}</span>`;
    toast(APP.pausado ? 'Mosaico pausado' : 'Mosaico retomado',
      APP.pausado ? 'A gravação nos servidores continua ativa.' : '', 'info', 2600);
  };
  // Cobrir o monitor inteiro — abas, barra de endereço e barra de tarefas —
  // só a API de tela cheia do navegador faz. Uma camada fixa da página chega
  // às bordas da janela e para por aí, que foi o que se tentou antes.
  //
  // O preço é a faixa que o navegador exibe no alto anunciando o endereço do
  // servidor. Ela é desenhada fora da página: nenhum código daqui a alcança,
  // nem para reescrever, nem para esconder, nem para traduzir. Some sozinha
  // em poucos segundos, e a nossa entra quando ela sai.
  //
  // A casca inteira, e não só o mosaico: na parede a barra tem de vir junto,
  // senão não há como paginar nem trocar o arranjo sem sair.
  let sumir = null, sumirAviso = null;

  function definirParede(ativo){
    const shell = $('.live-shell');
    const barra = $('.live-bar');
    if(shell) shell.classList.toggle('wall', ativo);
    const b = $('#btnFs');
    if(b) b.innerHTML = `${ico('expand',13)} ${ativo ? 'Sair' : 'Tela cheia'}`;

    clearTimeout(sumir);
    clearTimeout(sumirAviso);
    const aviso = $('.fs-aviso');
    if(aviso) aviso.classList.remove('aparece');
    if(!ativo){
      if(barra) barra.classList.remove('aparece');
      if(shell) shell.onmousemove = null;
      return;
    }
    if(shell){
      let a = aviso;
      if(!a){
        a = document.createElement('div');
        a.className = 'fs-aviso';
        shell.appendChild(a);
      }
      a.innerHTML = 'pressione <kbd>Esc</kbd> para sair';
      // Entra depois que a faixa do navegador saiu. A dela dura perto de três
      // segundos e meio; entrando antes disso, as duas se sobrepõem e a tela
      // fica com dois avisos para a mesma coisa.
      sumirAviso = setTimeout(() => {
        a.classList.add('aparece');
        sumirAviso = setTimeout(() => a.classList.remove('aparece'), 3400);
      }, 4200);
    }
    // Faixa de 70px no alto: perto o bastante para ser intencional, longe o
    // bastante para não piscar a cada vez que o ponteiro cruza a primeira
    // fileira de câmeras.
    if(shell) shell.onmousemove = ev => {
      const perto = ev.clientY < 70;
      const b2 = $('.live-bar');
      if(!b2) return;
      clearTimeout(sumir);
      if(perto){
        // A barra ocupa a mesma faixa do aviso: quem chamou a barra já sabe
        // sair, e dois painéis sobrepostos no topo não se leem.
        clearTimeout(sumirAviso);
        const a2 = $('.fs-aviso');
        if(a2) a2.classList.remove('aparece');
        b2.classList.add('aparece');
        return;
      }
      sumir = setTimeout(() => b2.classList.remove('aparece'), 1200);
    };
  }

  $('#btnFs').onclick = () => {
    const el = $('.live-shell');
    if(!document.fullscreenElement){
      if(el && el.requestFullscreen) el.requestFullscreen({navigationUI: 'hide'}).catch(() =>
        toast('Tela cheia indisponível', 'O navegador bloqueou a solicitação.', 'warn'));
    }else document.exitFullscreen();
  };
  // Quem manda no estado é o navegador, não o botão: o Esc sai da tela cheia
  // sem passar por aqui, e sair pelo Esc tem de desfazer a parede igual.
  document.onfullscreenchange = () => definirParede(!!document.fullscreenElement);

  montarMosaico();
  await carregarStreamDeEventos();
}


// `APP.mosaico` é quantas células o arranjo tem por página; zero significa
// "todas numa tela só", e aí a grade é calculada a partir do parque.
function camerasVisiveis(){
  const t = APP.cameras;
  if(!APP.mosaico) return t;
  return t.slice(APP.paginaMosaico * APP.mosaico, APP.paginaMosaico * APP.mosaico + APP.mosaico);
}

/* O arranjo que faz as câmeras saírem maiores dentro da área disponível.

   Não basta dividir por raiz quadrada: a célula tem de ficar em 16:9, que é o
   formato do que a câmera entrega, senão a imagem é cortada nas bordas para
   preencher um retângulo de outro feitio — era o que deixava o mosaico
   desalinhado, com metade de uma pessoa na borda de uma célula e sobra preta
   em outra. Então testa-se cada número de colunas e fica o que dá a maior
   célula sem estourar nem a largura nem a altura. */
function montarMosaico(){
  const m = $('#mosaic'); if(!m) return;
  const todas = APP.cameras, vis = camerasVisiveis();
  const celulas = APP.mosaico;

  m.className = 'mosaic g' + APP.mosaico;
  m.style.gridTemplateColumns = '';
  m.style.gridTemplateRows = '';

  const paginas = Math.max(1, Math.ceil(todas.length / APP.mosaico));
  $('#pgInfo').textContent = `${APP.paginaMosaico+1} / ${paginas}`;
  $('#pgPrev').disabled = APP.paginaMosaico === 0;
  $('#pgNext').disabled = APP.paginaMosaico >= paginas - 1;

  FEED.clear();
  if(!APP.camSelecionada || !vis.find(c => c.codigo === APP.camSelecionada)){
    const primeira = vis.find(c => c.status === 'ONLINE') || vis[0];
    APP.camSelecionada = primeira ? primeira.codigo : null;
  }

  m.innerHTML = Array.from({length: celulas}).map((_, i) => {
    const c = vis[i];
    if(!c) return `<div class="tile"><div class="tile-empty">Sem sinal atribuído</div></div>`;
    const off = c.status !== 'ONLINE';
    return `<div class="tile ${off ? 'offline' : ''} ${c.codigo === APP.camSelecionada ? 'sel' : ''}" data-cam="${esc(c.codigo)}">
      ${off ? `<div class="tile-off">${ico('alert',22)}<span>${c.status === 'OFFLINE' ? 'Offline' : 'Em manutenção'}</span>
               <span style="color:var(--txt-4);font-size:9.5px">${esc(c.codigo)}</span></div>`
            : `<canvas data-feed="${esc(c.codigo)}"></canvas>`}
    </div>`;
  }).join('');

  $$('#mosaic canvas[data-feed]').forEach(cv => {
    const cam = camPorCodigo(cv.dataset.feed);
    // Zona e ruído sobre uma célula pequena viram sujeira: quem decide é o
    // tamanho real da célula, e com o parque inteiro na tela ela é mínima.
    // `hd` segue o tamanho real da célula, não o gosto: no 2×2 cada tile tem
    // perto de 640px e o fluxo padrão já o cobre pixel a pixel. No 1×1 a
    // célula é a tela inteira, e aí o padrão chegava como 640 de largura
    // esticado ao dobro — e com o canvas limitado a 900px, esticado duas
    // vezes. Pedir nativo só aqui custa um encode a mais, de um espectador.
    if(cam) FEED.register(cv, cam,
      {zonas: celulas <= 4, noise: celulas <= 9, hd: celulas === 1});
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
  // A câmera em foco pode ter mudado acima. Em 1×1 ela muda a cada página,
  // porque a anterior sai da tela — e sem este aviso o painel ao lado segue
  // descrevendo uma câmera que ninguém está mais vendo.
  renderPTZ();
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
    // Sem resposta visual imediata: o movimento aparece quando a câmera
    // entregar o próximo quadro. O que havia aqui era a cena sintética
    // reagindo ao botão — um retorno que não vinha da câmera e dizia que o
    // comando funcionou mesmo quando o ONVIF recusava.
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
    ctx.fillText(cam.status === 'OFFLINE' ? 'OFFLINE' : 'MANUTENÇÃO', w/2, h/2 + 4);
    return;
  }
  // Preto até o quadro real chegar. Antes entrava aqui uma cena inventada —
  // pessoas, carros e postes desenhados — e o operador via movimento onde a
  // câmera não tinha entregue nada. Numa tela de videomonitoramento isso não
  // é enfeite: é imagem que não veio de lente nenhuma.
  ctx.fillStyle = '#0A0E15';
  ctx.fillRect(0, 0, w, h);
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
        <option value="OFFLINE" ${CAM_FILTRO.status==='OFFLINE'?'selected':''}>Offline</option>
        <option value="MANUTENCAO" ${CAM_FILTRO.status==='MANUTENCAO'?'selected':''}>Manutenção</option>
      </select>
      ${API.pode('edit') && API.pode('cams') ? `<button class="btn btn-primary btn-sm" id="camNova">${ico('plus',14)} Nova câmera</button>` : ''}
    </div>
  </div>
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-green"><div class="kpi-top"><span class="kpi-lbl">On-line</span><span class="kpi-ico">${ico('cam',15)}</span></div><div class="kpi-val">${cont('ONLINE')}</div><div class="kpi-delta nt">transmitindo agora</div></div>
    <div class="kpi k-red"><div class="kpi-top"><span class="kpi-lbl">Offline</span><span class="kpi-ico">${ico('alert',15)}</span></div><div class="kpi-val">${cont('OFFLINE')}</div><div class="kpi-delta dn">requer verificação em campo</div></div>
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
      img.onerror = () => {};                    // câmera muda: a miniatura fica preta
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

/* ==========================================================
   CADASTRO E CONFIGURAÇÃO DE CÂMERA
   ========================================================== */
async function editarCamera(codigo){
  const novo = !codigo;
  let c = null;
  if(!novo){
    try{ c = await API.camera(codigo); }
    catch(e){ return erroToast(e); }
  }

  let setores = [];
  try{ setores = (await API.setores()).setores || []; }catch(e){ setores = []; }

  // Valor do campo: o da câmera quando editando, o padrão do schema quando nova.
  const v = (campo, padrao) => (c && c[campo] != null && c[campo] !== '') ? c[campo] : padrao;
  const pct = (campo, padrao) => Math.round((c && c[campo] != null ? c[campo] : padrao) * 100);

  const flags = {
    ptz:              c ? !!c.ptz : false,
    infravermelho:    c ? !!c.infravermelho : true,
    analitico_facial: c ? !!c.analitico_facial : true,
    analitico_objeto: c ? !!c.analitico_objeto : true,
    analitico_acao:   c ? !!c.analitico_acao : false,
    alerta_presenca:  c ? !!c.alerta_presenca : false,
    detecta_veiculo:  c ? !!c.detecta_veiculo : false,
    detecta_bagagem:  c ? !!c.detecta_bagagem : false,
    detecta_animal:   c ? !!c.detecta_animal : false,
    detecta_capacete: c ? !!c.detecta_capacete : false,
    detecta_arma:     c ? !!c.detecta_arma : false
  };
  const chave = k => `<span class="sw ${flags[k]?'on':''}"></span>`;

  /* Capacete e arma de fogo não existem no modelo do parque — dependem de um
     detector próprio, apontado na configuração. Sem ele a chave não liga nada.

     A linha aparece mesmo assim, e desligada, dizendo o que falta. Esconder a
     opção mandaria o operador procurá-la; oferecê-la como se funcionasse o
     faria ligar e esperar por um registro que nunca vem — que é exatamente o
     que esta tela fazia com as classes do filtro. */
  const linhaDetectorExtra = (k, titulo, ajuda) => {
    const pronto = ((APP.cfg || {}).detectores_extras || {})[k];
    const campo = 'detecta_' + k;
    return `<div class="sw-row ${pronto ? '' : 'sem-detector'}"
                 ${pronto ? `data-csw="${campo}"` : ''}>
      <div class="bd"><b>${esc(titulo)}</b>
        <span>${pronto ? esc(ajuda)
                       : 'Sem detector instalado — nenhum registro desta classe será gerado'}</span></div>
      ${pronto ? chave(campo) : '<span class="chip c-gray">indisponível</span>'}</div>`;
  };

  /* Ações que esta câmera vigia.

     O catálogo vem do servidor (/api/info), e não de uma lista escrita aqui:
     é ele que sabe quais ações têm detector por trás. Repetir a lista no
     front faria a tela oferecer "Fogo" como se funcionasse no dia em que o
     servidor mudasse de ideia.

     Sem catálogo — /api/info fora do ar, versão antiga — o painel não é
     desenhado e `acoes_ativas` não vai no salvamento: o servidor mantém o que
     já tinha. Melhor não mexer do que gravar uma lista inventada. */
  const catalogoAcoes = (APP.cfg && Array.isArray(APP.cfg.acoes)) ? APP.cfg.acoes : [];
  const padraoAcoes = catalogoAcoes.filter(a => a.disponivel).map(a => a.codigo);
  /* O que abre marcado é o que está ativo *agora*: câmera com a análise
     desligada abre sem nada marcado, mesmo tendo lista gravada. Mostrar seis
     ações marcadas sob um interruptor apagado era dizer duas coisas opostas
     na mesma linha. A lista gravada não se perde — volta no religar. */
  const guardadasAcoes = c ? (c.acoes_ativas || []) : padraoAcoes;
  const acoesSel = new Set(flags.analitico_acao ? guardadasAcoes : []);

  /* A ressalva de uma ação que funciona vira etiqueta curta; o motivo de uma
     que não funciona continua por extenso, porque ali a frase é a informação. */
  // O tipo da zona vem como valor de enum (LINHA, POLIGONO). Minusculizar
  // não basta: "poligono" sem acento é erro de português na tela.
  const NOME_ZONA = {LINHA: 'linha', POLIGONO: 'polígono'};
  const marcasAcao = a => {
    if(!a.disponivel) return [];
    const m = [];
    if(a.exige_zona) m.push(`exige zona ${NOME_ZONA[a.exige_zona] || a.exige_zona.toLowerCase()}`);
    // Não é mais "não funciona": desde 15/10 a câmera recebe uma rajada de
    // quadros quando há movimento em cena. Mas a rajada tem teto por volta,
    // então é amostragem — e dizer "contínua" prometeria mais que isso.
    if(a.exige_continua) m.push('por amostragem de movimento');
    // A ação existe e o detector roda; o que pode faltar é a classe chegar
    // até ele. Dito aqui, quem liga sabe que precisa ligar bagagem junto.
    if(a.exige_bagagem && !flags.detecta_bagagem) m.push('exige bagagem ligada');
    return m;
  };

  /* Guarda a seleção entre um desligar e o religar seguinte, dentro desta
     janela. Desligar a análise para conferir outra coisa não pode custar a
     escolha de ações que levou tempo para montar. */
  let acoesLembradas = [...(guardadasAcoes.length ? guardadasAcoes : padraoAcoes)];

  const resumoAcoes = () => {
    if(!catalogoAcoes.length) return 'Corrida, queda, permanência e aglomeração';
    if(!acoesSel.size) return 'Nenhuma ação ativada — esta câmera não vigia comportamento';
    const nomes = catalogoAcoes.filter(a => acoesSel.has(a.codigo)).map(a => a.rotulo);
    return nomes.length <= 2 ? nomes.join(' e ')
                             : `${nomes.length} ações: ${nomes.slice(0, 2).join(', ')} e mais ${nomes.length - 2}`;
  };

  const linhaAcao = a => {
    const ligada = acoesSel.has(a.codigo) && a.disponivel;
    const marcas = marcasAcao(a);
    return `<div class="acao-item ${a.disponivel ? '' : 'indisponivel'}"
                 ${a.disponivel ? `data-acao="${esc(a.codigo)}"` : ''}>
      <div class="bd"><b>${esc(a.rotulo)}</b>
        ${a.disponivel
            ? (a.detalhe ? `<span>${esc(a.detalhe)}</span>` : '')
            : `<span>${esc(a.indisponivel || 'Indisponível neste sistema')}</span>`}
        ${marcas.length ? `<div class="marcas">${
          marcas.map(m => `<span class="chip c-gray">${esc(m)}</span>`).join('')}</div>` : ''}
      </div>
      <span class="sw ${ligada ? 'on' : ''}"></span>
    </div>
    ${a.codigo === 'fora_de_horario' && a.disponivel ? editorHorario(ligada) : ''}
    ${a.codigo === 'aglomeracao' && a.disponivel ? editorAglomeracao(ligada) : ''}`;
  };

  /* Pelo mesmo motivo do horário: ligar "Aglomeração" sem dizer quantas
     pessoas são demais *aqui* não configura nada. Oito num corredor de
     passagem é ocorrência; na Produção, onde treze trabalham, é terça-feira.
     Em branco vale o do sistema, e o campo abre vazio de propósito — vir
     preenchido com o padrão gravaria na câmera uma escolha que ninguém fez. */
  const editorAglomeracao = ligada => `
    <div class="acao-horario ${ligada ? '' : 'oculta'}" id="cAglomeracao">
      <label for="cAglomN">Alerta a partir de</label>
      <input class="inp mono" type="number" id="cAglomN" min="2" max="200"
             style="width:72px" value="${esc(v('limite_aglomeracao',''))}">
      <label for="cAglomN">pessoas juntas</label>
      <span class="hint" id="cAglomDica"></span>
    </div>`;

  /* A janela de horário mora dentro da própria ação, e não numa seção à
     parte: ligar "Entrada em horário não permitido" sem dizer qual é o
     horário não configura nada, e separar as duas coisas na tela faz a
     metade ficar para trás. Em branco, vale a do sistema — por isso o campo
     abre vazio em vez de já vir preenchido com o padrão, que gravaria na
     câmera uma escolha que ninguém fez. */
  const editorHorario = ligada => `
    <div class="acao-horario ${ligada ? '' : 'oculta'}" id="cHorario">
      <label for="cHoraIni">Permitido das</label>
      <input class="inp mono" type="time" id="cHoraIni" value="${esc(v('horario_inicio',''))}">
      <label for="cHoraFim">às</label>
      <input class="inp mono" type="time" id="cHoraFim" value="${esc(v('horario_fim',''))}">
      <span class="hint" id="cHorarioDica"></span>
    </div>`;

  const paineisDeAcoes = () => {
    if(!catalogoAcoes.length) return '';
    const podem = catalogoAcoes.filter(a => a.disponivel);
    const naoPodem = catalogoAcoes.filter(a => !a.disponivel);
    return `<div class="acoes-sel ${flags.analitico_acao ? '' : 'travada'}" id="cAcoes">
      <div class="topo"><div class="eyebrow">Ações vigiadas por esta câmera</div>
        <button type="button" data-acoes-todas>Ativar todas</button>
        <button type="button" data-acoes-nenhuma>Desativar</button></div>
      ${podem.map(linhaAcao).join('')}
      ${naoPodem.length ? `<div class="acoes-grupo"><b>Sem detector neste sistema</b><i></i></div>
        ${naoPodem.map(linhaAcao).join('')}` : ''}
    </div>`;
  };

  const box = abrirModal(`
    <div class="modal-hd"><h3>${novo ? 'Nova câmera' : 'Configurar câmera'}</h3>
      ${novo ? '' : `<span class="chip c-gray mono">${esc(c.codigo)}</span>`}
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="eyebrow" style="margin-bottom:8px">Identificação</div>
      <div class="form-grid">
        <div class="field full"><label for="cNome">Nome da câmera</label>
          <input class="inp" id="cNome" value="${esc(v('nome',''))}" placeholder="Ex.: Recepção, Pátio dos fundos"></div>
        ${novo ? `<div class="field"><label for="cCodigo">Código (opcional)</label>
          <input class="inp mono" id="cCodigo" placeholder="deixe vazio para gerar">
          <span class="hint">Em branco, o servidor emite o próximo da sequência.</span></div>` : ''}
        <div class="field"><label for="cSetor">Setor</label>
          <input class="inp" id="cSetor" list="cSetores" value="${esc(v('setor',''))}" placeholder="Ex.: Recepção, Externa">
          <datalist id="cSetores">${setores.map(s => `<option value="${esc(s)}"></option>`).join('')}</datalist></div>
        <div class="field"><label for="cCena">Tipo de cena</label>
          <select class="inp" id="cCena">${Object.entries(CENAS)
            .map(([k,o]) => `<option value="${k}" ${v('cena','rua')===k?'selected':''}>${esc(o.lbl)}</option>`).join('')}</select>
          <span class="hint">Rótulo de classificação do ponto. Não altera a análise nem a imagem.</span></div>
        <div class="field"><label for="cStatus">Status operacional</label>
          <select class="inp" id="cStatus">${Object.entries(STATUS_CAMERA)
            .map(([k,o]) => `<option value="${k}" ${v('status','ONLINE')===k?'selected':''}>${esc(o.lbl)}</option>`).join('')}</select>
          <span class="hint">O worker só processa câmeras on-line.</span></div>
      </div>

      <div class="eyebrow" style="margin:17px 0 8px">Conexão</div>
      <div class="form-grid">
        <div class="field"><label for="cIp">Endereço IP</label>
          <input class="inp mono" id="cIp" value="${esc(v('ip',''))}" placeholder="192.168.3.27">
          <span class="hint">O servidor monta o endereço RTSP a partir dele.</span></div>
        <div class="field"><label for="cOnvif">ONVIF (opcional)</label>
          <input class="inp mono" id="cOnvif" value="${esc(v('onvif_url',''))}" placeholder="http://192.168.0.10:80/onvif/device_service">
          <span class="hint">Necessário só para comando PTZ.</span></div>
      </div>

      <div class="eyebrow" style="margin:17px 0 8px">Equipamento</div>
      <div class="form-grid">
        <div class="field"><label for="cFab">Fabricante</label>
          <input class="inp" id="cFab" value="${esc(v('fabricante',''))}" placeholder="Intelbras"></div>
        <div class="field"><label for="cMod">Modelo</label>
          <input class="inp" id="cMod" value="${esc(v('modelo',''))}"></div>
        <div class="field"><label for="cRes">Resolução</label>
          <input class="inp mono" id="cRes" value="${esc(v('resolucao','1920x1080'))}" placeholder="1920x1080">
          <span class="hint">Usada para a proporção da pré-visualização.</span></div>
        <div class="field"><label for="cFps">Quadros por segundo</label>
          <input class="inp mono" id="cFps" type="number" min="1" max="120" value="${v('fps',25)}"></div>
        <div class="field"><label for="cCodec">Codec</label>
          <select class="inp" id="cCodec">${['H.264','H.265','MJPEG']
            .map(x => `<option ${v('codec','H.265')===x?'selected':''}>${x}</option>`).join('')}</select></div>
        <div class="field"><label for="cBitrate">Bitrate (kbps)</label>
          <input class="inp mono" id="cBitrate" type="number" min="128" max="65536" value="${v('bitrate_kbps',4096)}"></div>
      </div>

      <div class="eyebrow" style="margin:17px 0 8px">Analíticos e limiares</div>
      <div class="sw-row" data-csw="analitico_facial"><div class="bd"><b>Reconhecimento facial</b>
        <span>Extrai template e consulta a base biométrica</span></div>${chave('analitico_facial')}</div>
      <div class="sw-row" data-csw="analitico_objeto"><div class="bd"><b>Detecção de objetos</b>
        <span>Pessoas sempre; veículo só com o item abaixo ligado</span></div>${chave('analitico_objeto')}</div>
      <div class="sw-row" data-csw="detecta_veiculo"><div class="bd"><b>Alcança via, pátio ou garagem</b>
        <span>Desligado, veículo é ignorado — num hall ele só vem da janela. Ligado, carro, caminhão, ônibus, moto e bicicleta contam, e a placa é lida</span></div>${chave('detecta_veiculo')}</div>
      <div class="sw-row" data-csw="detecta_bagagem"><div class="bd"><b>Bagagem — mochila, bolsa, mala</b>
        <span>Vista do teto, essa silhueta costuma ser saco de lixo ou caixa: em seis semanas, dos 15 registros nenhum era bagagem. Ligue na revista de entrada ou no guarda-volumes</span></div>${chave('detecta_bagagem')}</div>
      <div class="sw-row" data-csw="detecta_animal"><div class="bd"><b>Animal</b>
        <span>O modelo não tem rótulo para pessoa agachada e escolhe o bicho de silhueta parecida: das 10 detecções deste parque, as 10 eram pessoas. Ligue em pátio, perímetro ou via</span></div>${chave('detecta_animal')}</div>
      ${linhaDetectorExtra('capacete', 'Capacete', 'Não existe no modelo do parque: exige um detector próprio, apontado nas configurações')}
      ${linhaDetectorExtra('arma', 'Arma de fogo', 'Não existe no modelo do parque: exige um detector próprio, apontado nas configurações')}
      <div class="sw-row" data-csw="alerta_presenca"><div class="bd"><b>Alertar toda presença</b>
        <span>Sala vigiada: qualquer pessoa aqui gera alerta, autorizada ou não. Numa sala de trabalho vira enxurrada</span></div>${chave('alerta_presenca')}</div>
      <div class="sw-row" data-csw="analitico_acao"><div class="bd"><b>Análise de ações</b>
        <span id="cAcoesResumo">${esc(resumoAcoes())}</span></div>${chave('analitico_acao')}</div>
      ${paineisDeAcoes()}
      <div class="sw-row" data-csw="ptz"><div class="bd"><b>PTZ</b>
        <span>Habilita os comandos de movimento, via ONVIF</span></div>${chave('ptz')}</div>
      <div class="sw-row" data-csw="infravermelho"><div class="bd"><b>Infravermelho</b>
        <span>A câmera tem iluminação própria para operar no escuro</span></div>${chave('infravermelho')}</div>

      <div class="form-grid" style="margin-top:14px">
        <div class="field"><label for="cLimFace">Limiar facial (%)</label>
          <input class="inp mono" id="cLimFace" type="number" min="50" max="100" value="${pct('limiar_facial',0.88)}">
          <span class="hint">Abaixo disto a captura não é atribuída a ninguém. Baixar aumenta o falso positivo.</span></div>
        <div class="field"><label for="cLimObj">Limiar de objeto (%)</label>
          <input class="inp mono" id="cLimObj" type="number" min="10" max="100" value="${pct('limiar_objeto',0.65)}"></div>
        <div class="field"><label for="cRet">Retenção (dias)</label>
          <input class="inp mono" id="cRet" type="number" min="1" max="3650" value="${v('retencao_dias',90)}"></div>
        <div class="field"><label for="cLat">Latitude</label>
          <input class="inp mono" id="cLat" type="number" step="0.00001" value="${c&&c.latitude!=null?c.latitude:''}" placeholder="-1.45502"></div>
        <div class="field"><label for="cLon">Longitude</label>
          <input class="inp mono" id="cLon" type="number" step="0.00001" value="${c&&c.longitude!=null?c.longitude:''}" placeholder="-48.49017"></div>
      </div>
      <div class="eyebrow" style="margin:17px 0 8px">Zonas e perímetros</div>
      <div class="zone-list">${(c && c.zonas || []).map(z => `<div class="zone-item">
        <span class="zone-swatch" style="background:${esc(z.cor)}"></span><b>${esc(z.nome)}</b>
        <span class="chip c-gray">${z.tipo === 'LINHA' ? 'Linha' : 'Polígono'}</span>
        <span class="mono" style="font-size:10.5px;color:var(--txt-4)">${(z.pontos||[]).length} pts</span></div>`).join('')
        || `<p class="hint">Nenhuma zona desenhada. Invasão de perímetro, área
             restrita, veículo em área proibida e contrafluxo não têm onde
             disparar sem uma região marcada na imagem.</p>`}</div>
      <button class="btn btn-ghost btn-sm" id="cZonas" style="width:100%;margin-top:8px">
        ${ico('edit',13)} ${novo ? 'Cadastrar e desenhar na imagem' : 'Desenhar na imagem'}</button>
      <span class="hint" style="display:block;margin-top:4px">Abre o editor sobre
        o vídeo desta câmera. ${novo ? 'A câmera é cadastrada primeiro — o editor precisa dela gravada.'
          : 'As alterações desta tela são salvas antes de abrir.'}</span>
      ${novo ? '' : `<div class="note" style="margin-top:15px">${ico('alert',15)}<div>Mudança de limiar facial fica registrada com destaque na trilha de auditoria. O worker relê a configuração no próximo ciclo.</div></div>`}
    </div>
    <div class="modal-ft">
      ${novo ? '' : `<button class="btn btn-danger" id="cDel">${ico('trash',14)} Remover câmera</button>`}
      <span style="flex:1"></span>
      <button class="btn btn-ghost" id="cZonasFt">${ico('edit',14)} Desenhar zonas</button>
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="cSave">${ico('check',14)} ${novo ? 'Cadastrar câmera' : 'Salvar alterações'}</button>
    </div>`);

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);

  /* Interruptor e lista são o mesmo controle, e é isso que evita o estado
     morto de "análise ligada sem nenhuma ação" — que não vigia nada e não
     diz por quê. A chave vale como atalho de tudo/nada; a lista é onde a
     escolha mora. */
  // Ligado pelo botão de zonas: diz ao salvamento que, em vez de voltar
  // para a lista, ele deve abrir o editor de desenho. Zerado em todo
  // caminho que não chega lá, senão um salvamento comum depois de uma
  // tentativa falha abriria o editor sem ninguém ter pedido.
  let abrirZonasDepois = false;
  const btTodas = $('[data-acoes-todas]', box), btNenhuma = $('[data-acoes-nenhuma]', box);
  ['#cHoraIni', '#cHoraFim', '#cAglomN'].forEach(sel => {
    const el = $(sel, box);
    // `change` só dispara ao sair do campo; a dica precisa acompanhar quem
    // ainda está escolhendo a hora.
    if(el) el.oninput = () => {
      sincronizarHorario(); sincronizarAglomeracao();
    };
  });

  const sincronizarAcoes = () => {
    // Sem catálogo não há painel, e a chave volta a ser o controle único:
    // deduzi-la de uma seleção que a tela nunca mostrou desligaria a análise
    // de uma câmera sem ninguém ter pedido.
    if(!catalogoAcoes.length) return;
    flags.analitico_acao = acoesSel.size > 0;
    const linha = $('[data-csw="analitico_acao"]', box);
    if(linha) linha.querySelector('.sw').classList.toggle('on', flags.analitico_acao);
    const painel = $('#cAcoes', box);
    if(painel) painel.classList.toggle('travada', !flags.analitico_acao);
    const resumo = $('#cAcoesResumo', box);
    if(resumo) resumo.textContent = resumoAcoes();
    // Atalho que não faz nada fica apagado em vez de enganar o clique.
    const possiveis = catalogoAcoes.filter(a => a.disponivel).length;
    if(btTodas) btTodas.disabled = acoesSel.size >= possiveis;
    if(btNenhuma) btNenhuma.disabled = acoesSel.size === 0;
  };

  const pintarAcao = codigo => {
    const linha = $(`[data-acao="${codigo}"]`, box);
    if(linha) linha.querySelector('.sw').classList.toggle('on', acoesSel.has(codigo));
    if(codigo === 'fora_de_horario') sincronizarHorario();
    if(codigo === 'aglomeracao') sincronizarAglomeracao();
  };

  /* A dica diz o número que vai valer, inclusive quando o campo está em
     branco — descobrir qual era o limite pelo alerta que nunca chegou é
     tarde. */
  const sincronizarAglomeracao = () => {
    const cx = $('#cAglomeracao', box);
    if(!cx) return;
    cx.classList.toggle('oculta', !acoesSel.has('aglomeracao'));
    const dica = $('#cAglomDica', box);
    if(!dica) return;
    const n = parseInt($('#cAglomN', box).value, 10);
    const padrao = (APP.cfg && APP.cfg.aglomeracao_padrao) || 8;
    dica.textContent = n >= 2
      ? `Dispara com ${n} pessoas agrupadas por tempo suficiente.`
      : `Em branco: vale o limite do sistema, ${padrao} pessoas.`;
  };

  /* A dica diz o que vai acontecer com o que está digitado agora — inclusive
     nos dois casos em que a configuração é válida mas não vigia nada: campo
     em branco (vale a do sistema) e início igual ao fim (24 h permitidas).
     Descobrir isso depois, pelo alerta que nunca chega, é tarde. */
  const sincronizarHorario = () => {
    const cx = $('#cHorario', box);
    if(!cx) return;
    cx.classList.toggle('oculta', !acoesSel.has('fora_de_horario'));
    const ini = $('#cHoraIni', box).value, fim = $('#cHoraFim', box).value;
    const padrao = (APP.cfg && APP.cfg.horario_padrao) || {inicio: '07:00', fim: '19:00'};
    const dica = $('#cHorarioDica', box);
    if(!dica) return;
    if(!ini && !fim){
      dica.textContent = `Em branco: vale a janela do sistema, ${padrao.inicio} às ${padrao.fim}.`;
    }else if(ini && fim && ini === fim){
      dica.textContent = 'Início igual ao fim são 24 horas permitidas — assim a ação nunca dispara.';
    }else{
      const a = ini || padrao.inicio, b = fim || padrao.fim;
      const vira = a > b ? ' A janela atravessa a meia-noite.' : '';
      dica.textContent = `Fora de ${a} às ${b}, qualquer pessoa vista aqui vira alerta.${vira}`;
    }
  };

  const marcarAcoes = ligar => {
    // Só as que têm detector: "ativar todas" não liga o que não existe.
    catalogoAcoes.filter(a => a.disponivel).forEach(a => {
      if(ligar) acoesSel.add(a.codigo); else acoesSel.delete(a.codigo);
      pintarAcao(a.codigo);
    });
    sincronizarAcoes();
  };

  $$('[data-csw]', box).forEach(r => r.onclick = () => {
    const k = r.dataset.csw;
    if(k === 'analitico_acao' && catalogoAcoes.length){
      if(acoesSel.size){
        // Desligar não joga fora a escolha: ela volta inteira no religar,
        // enquanto esta janela estiver aberta.
        acoesLembradas = [...acoesSel];
        marcarAcoes(false);
      }else{
        const voltar = acoesLembradas.length
          ? acoesLembradas
          : catalogoAcoes.filter(a => a.disponivel).map(a => a.codigo);
        catalogoAcoes.filter(a => a.disponivel).forEach(a => {
          if(voltar.includes(a.codigo)) acoesSel.add(a.codigo); else acoesSel.delete(a.codigo);
          pintarAcao(a.codigo);
        });
        sincronizarAcoes();
      }
      return;
    }
    flags[k] = !flags[k];
    r.querySelector('.sw').classList.toggle('on', flags[k]);
  });

  $$('[data-acao]', box).forEach(linha => linha.onclick = () => {
    const codigo = linha.dataset.acao;
    if(acoesSel.has(codigo)) acoesSel.delete(codigo); else acoesSel.add(codigo);
    pintarAcao(codigo);
    // Ativar a primeira ação liga a análise sozinha. Escolher o que a câmera
    // vigia *é* ligar a vigilância dela — não são dois passos.
    sincronizarAcoes();
  });

  if(btTodas) btTodas.onclick = () => marcarAcoes(true);
  if(btNenhuma) btNenhuma.onclick = () => marcarAcoes(false);
  sincronizarAcoes();   // acerta o estado dos atalhos já na abertura
  sincronizarHorario(); // e a dica da janela, que depende do que está gravado
  sincronizarAglomeracao();  // idem, para o limite de pessoas juntas

  const bdel = $('#cDel');
  if(bdel) bdel.onclick = async () => {
    const motivo = await confirmar('Remover câmera',
      `Remover <b>${esc(c.codigo)}</b> — ${esc(c.nome)} do parque?<br><br>` +
      `As detecções já registradas por ela <b>são apagadas junto</b>: o vínculo é ` +
      `<span class="mono">ON DELETE CASCADE</span>. Para só parar de processar, ` +
      `mude o status para <b>Manutenção</b>.`,
      {rotulo:'Remover', perigo:true, exigeTexto:'Motivo da remoção'});
    if(!motivo) return;
    try{
      await API.removerCamera(c.codigo, motivo);
      fecharModal();
      toast('Câmera removida', `${c.codigo} saiu do parque.`, 'warn');
      await carregarCatalogoCameras();
      irPara('cams');
    }catch(e){ erroToast(e); }
  };

  $('#cSave').onclick = async () => {
    const num = (id, padrao) => {
      const n = parseFloat($(id).value);
      return Number.isFinite(n) ? n : padrao;
    };
    const texto = id => $(id).value.trim();

    const dados = {
      nome: texto('#cNome'),
      setor: texto('#cSetor'),
      cena: $('#cCena').value,
      status: $('#cStatus').value,
      ip: texto('#cIp'),
      onvif_url: texto('#cOnvif'),
      fabricante: texto('#cFab'),
      modelo: texto('#cMod'),
      resolucao: texto('#cRes') || '1920x1080',
      fps: Math.round(num('#cFps', 25)),
      codec: $('#cCodec').value,
      bitrate_kbps: Math.round(num('#cBitrate', 4096)),
      retencao_dias: Math.round(num('#cRet', 90)),
      limiar_facial: Math.round(num('#cLimFace', 88)) / 100,
      limiar_objeto: Math.round(num('#cLimObj', 65)) / 100,
      latitude: texto('#cLat') === '' ? null : num('#cLat', null),
      longitude: texto('#cLon') === '' ? null : num('#cLon', null),
      ...flags
    };
    // Só vai quando a tela teve o catálogo para desenhar o painel. Sem ele o
    // campo fica de fora do PATCH e o servidor preserva o que já estava.
    //
    // Com a análise desligada grava a lista lembrada, e não vazio: quem
    // montou uma seleção de duas ações e desligou a análise para outra coisa
    // não deve reencontrá-la em branco. O que vale é sempre `analitico_acao`,
    // que o servidor consulta antes de olhar a lista.
    if(catalogoAcoes.length){
      dados.acoes_ativas = flags.analitico_acao ? [...acoesSel] : acoesLembradas;
      // Vazio vira `null`, e não '': é assim que o servidor entende "sem
      // janela própria, use a do sistema". String vazia ele leria como
      // meia-noite e a câmera passaria a acusar o dia inteiro.
      const hi = $('#cHoraIni'), hf = $('#cHoraFim');
      if(hi) dados.horario_inicio = hi.value || null;
      if(hf) dados.horario_fim = hf.value || null;
      // Mesmo acordo para o limite de aglomeração: vazio é `null`, que o
      // servidor lê como "sem limite próprio, use o do sistema".
      const an = $('#cAglomN');
      if(an) dados.limite_aglomeracao = parseInt(an.value, 10) || null;
    }

    // Validar aqui evita um 422 que a interface teria de traduzir depois.
    const erro =
      dados.nome.length < 3       ? ['Nome inválido', 'Informe ao menos 3 caracteres.'] :
      !dados.setor                ? ['Setor obrigatório', 'Informe o setor onde a câmera está.'] :
      !dados.ip                   ? ['IP obrigatório', 'Sem o endereço da câmera não há imagem para processar.'] :
      !/^(\d{1,3}\.){3}\d{1,3}$/.test(dados.ip) ? ['IP inválido', 'Use o formato 192.168.3.27.'] :
      dados.ip.split('.').some(o => +o > 255) ? ['IP inválido', 'Cada parte vai de 0 a 255.'] :
      !/^\d+x\d+$/.test(dados.resolucao) ? ['Resolução inválida', 'Use o formato 1920x1080.'] :
      (dados.fps < 1 || dados.fps > 120) ? ['FPS fora da faixa', 'Informe entre 1 e 120.'] :
      (dados.limiar_facial < 0.5 || dados.limiar_facial > 1) ? ['Limiar facial fora da faixa', 'Informe entre 50% e 100%.'] :
      (dados.limiar_objeto < 0.1 || dados.limiar_objeto > 1) ? ['Limiar de objeto fora da faixa', 'Informe entre 10% e 100%.'] :
      null;
    if(erro){ abrirZonasDepois = false; toast(erro[0], erro[1], 'err'); return; }

    const btn = $('#cSave');
    btn.disabled = true;

    // A gravação é um passo; recarregar a lista e navegar são outro. Juntos
    // num `try` só, qualquer tropeço ao redesenhar a tela saía como "não foi
    // possível concluir" — e o operador tentava de novo algo que o servidor
    // já tinha aceitado.
    let salva;
    try{
      if(novo){
        const cod = texto('#cCodigo');
        if(cod) dados.codigo = cod;
        salva = await API.criarCamera(dados);
      }else{
        salva = await API.salvarCamera(c.codigo, dados);
      }
    }catch(e){
      btn.disabled = false;
      abrirZonasDepois = false;
      erroToast(e, novo ? 'Câmera não foi cadastrada' : 'Configuração não foi salva');
      return;
    }

    fecharModal();
    toast(novo ? 'Câmera cadastrada' : 'Configuração salva',
      `${salva.codigo} — ${salva.nome}.`, 'ok');

    // O editor de zonas desenha sobre o vídeo e mora no mesmo `#modalBox`
    // desta tela — abri-lo direto apagaria o formulário e o que estivesse
    // digitado nele. Por isso o caminho é gravar primeiro e abrir depois,
    // com a câmera que o servidor devolveu: é ela que tem código (a nova
    // acabou de ganhar um) e as zonas já normalizadas.
    if(abrirZonasDepois){
      abrirZonasDepois = false;
      abrirEditorZonas(salva);
      return;
    }
    try{
      await carregarCatalogoCameras();
      irPara('cams');
    }catch(e){
      erroToast(e, 'Salvo, mas a lista não recarregou');
    }
  };

  /* Dois caminhos para a mesma coisa, de propósito: o da seção fica junto da
     lista de zonas; o do rodapé não exige rolar o modal, que é longo.

     Abrir o editor **não** salva a configuração. A primeira versão salvava
     antes de abrir, para não perder o que estivesse digitado — mas isso
     obriga quem só quer desenhar uma linha a gravar a tela inteira, e um
     salvamento recusado por qualquer outro campo passa a impedir o desenho.
     Foi o que aconteceu: o servidor recusava `limite_aglomeracao` e o botão
     parava junto. Agora só se pergunta quando há mesmo o que perder.

     Câmera nova é a exceção: o editor precisa dela gravada para ter código. */
  /* Preso ao corpo do modal, e não à caixa: a caixa é reaproveitada por
     todos os modais do sistema, e ouvintes presos a ela sobreviveriam a esta
     tela. O corpo é recriado a cada abertura e leva os ouvintes junto. */
  let mexeu = false;
  const corpo = $('.modal-bd', box);
  if(corpo){
    const marcar = () => { mexeu = true; };
    corpo.addEventListener('input', marcar);
    corpo.addEventListener('change', marcar);
    // Os interruptores desta tela são `span` com `onclick`, não controles de
    // formulário: não emitem `change`, e sem isto trocar um analítico ou uma
    // ação vigiada passaria por "nada mudou" — e seria perdido em silêncio.
    corpo.addEventListener('click', e => {
      if(e.target.closest('[data-csw],[data-acao],[data-acoes-todas],[data-acoes-nenhuma],[data-sentido]'))
        marcar();
    });
  }

  ['#cZonas', '#cZonasFt'].forEach(sel => {
    const bt = $(sel, box);
    if(bt) bt.onclick = async () => {
      if(novo){
        abrirZonasDepois = true;
        $('#cSave', box).click();
        return;
      }
      if(mexeu){
        const segue = await confirmar('Alterações não salvas',
          'O editor de zonas abre no lugar desta tela, e o que você mudou aqui '
          + 'será perdido. Salve antes, ou abra o editor assim mesmo.',
          {rotulo: 'Abrir o editor'});
        if(!segue) return;
      }
      fecharModal();
      abrirEditorZonas(c);
    };
  });
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
  box.className = 'modal theater';
  // Proporção da própria câmera, não um 16/9 presumido.
  const [rw, rh] = String(c.resolucao || '').split('x').map(Number);
  const razaoInicial = (rw > 0 && rh > 0) ? rw / rh : 16 / 9;
  const ALTURA_MAX_VH = 68;

  // Imagem inteira e sem tarja preta só coexistem de um jeito: a caixa tem de
  // ter exatamente a proporção do quadro. `aspect-ratio` sozinho não garante
  // isso — com um `max-height` que chega a valer, a caixa fica mais larga que
  // a proporção e o quadro passa a flutuar entre duas faixas pretas.
  // Amarrar a largura ao teto de altura resolve nos dois sentidos: na coluna
  // larga manda o teto, na estreita manda a coluna, e em ambos a caixa sai na
  // proporção exata.
  const larguraCaixa = razao =>
    `min(100%, calc(${ALTURA_MAX_VH}vh * ${razao.toFixed(4)}))`;
  const medidaCaixa = razao =>
    `width:${larguraCaixa(razao)};aspect-ratio:${razao.toFixed(4)};margin-inline:auto`;
  const aplicarMedida = (el, razao) => {
    el.style.width = larguraCaixa(razao);
    el.style.aspectRatio = String(razao.toFixed(4));
  };
  box.innerHTML = `
    <div class="modal-hd"><h3>${esc(c.nome)}</h3><span class="chip ${st.cls}">${esc(c.codigo)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="cam-ficha">
        <div style="min-width:0">
          <div id="camQuadro" style="position:relative;${medidaCaixa(razaoInicial)};background:#000;border-radius:var(--r-m);overflow:hidden;border:1px solid var(--line)">
            <canvas id="camModalFeed" style="position:absolute;inset:0;width:100%;height:100%"></canvas>
            <div id="camNomeOverlay" style="position:absolute;bottom:12px;left:12px;background:rgba(0,0,0,0.6);color:#fff;padding:6px 10px;border-radius:4px;font-size:13px;font-weight:500;backdrop-filter:blur(4px)">${esc(c.nome)}</div>
          </div>
        </div>
        <div class="cam-ficha-lado">
          <div>
            <div class="eyebrow" style="margin-bottom:8px">Situação</div>
            <dl class="kv">
              <dt>Status</dt><dd><span class="chip ${st.cls}">${esc(st.lbl || c.status)}</span></dd>
              <dt>Latência</dt><dd class="mono">${c.status === 'ONLINE' ? (c.latencia_ms || 0) + ' ms' : '—'}</dd>
              <dt>Analíticos</dt><dd><div style="display:flex;gap:3px;flex-wrap:wrap">
                ${c.analitico_facial ? '<span class="chip c-amber">Facial</span>' : ''}
                ${c.analitico_objeto ? '<span class="chip c-green">Objetos</span>' : ''}
                ${c.analitico_acao ? '<span class="chip c-violet">Ações</span>' : ''}
                ${c.ptz ? '<span class="chip c-blue">PTZ</span>' : ''}
                ${c.alerta_presenca ? '<span class="chip c-red">Alerta por presença</span>' : ''}
                ${!c.analitico_facial && !c.analitico_objeto && !c.analitico_acao ? '<span class="chip c-gray">Nenhum</span>' : ''}</div></dd>
              ${c.analitico_acao ? `<dt>Ações vigiadas</dt><dd>${
                (c.acoes_ativas && c.acoes_ativas.length)
                  ? c.acoes_ativas.map(k => esc(ROTULO_ACAO_UI[k] || k)).join(' · ')
                  // Análise ligada sem nenhuma ação marcada é um estado válido
                  // e silencioso. Dito assim, quem lê a ficha entende por que
                  // a câmera não produz ocorrência de ação.
                  : '<span class="hint">Nenhuma marcada — a análise não procura nada nesta câmera.</span>'
              }</dd>` : ''}
            </dl>
          </div>
          <div>
            <div class="eyebrow" style="margin-bottom:8px">Equipamento</div>
            <dl class="kv">
              <dt>Código</dt><dd class="mono">${esc(c.codigo)}</dd>
              <dt>Setor</dt><dd>${esc(c.setor)}</dd>
              <dt>Fabricante</dt><dd>${esc(c.fabricante)} ${esc(c.modelo)}</dd>
              <dt>Resolução</dt><dd class="mono">${esc(c.resolucao)} @ ${c.fps}fps</dd>
              <dt>Codec</dt><dd>${esc(c.codec)} · ${fmtNum(c.bitrate_kbps)} kbps</dd>
            </dl>
          </div>
          <div>
            <div class="eyebrow" style="margin-bottom:8px">Rede</div>
            <dl class="kv">
              <dt>IP</dt><dd class="mono">${esc(c.ip) || '—'}</dd>
              <dt>Coordenadas</dt><dd class="mono">${c.latitude != null ? c.latitude.toFixed(5) + ', ' + c.longitude.toFixed(5) : '—'}</dd>
            </dl>
          </div>
          <div>
            <div class="eyebrow" style="margin-bottom:8px">Análise</div>
            <dl class="kv">
              <dt>Limiar facial</dt><dd class="mono">${Math.round(c.limiar_facial*100)}%</dd>
              <dt>Limiar objeto</dt><dd class="mono">${Math.round(c.limiar_objeto*100)}%</dd>
              <dt>Retenção</dt><dd>${c.retencao_dias} dias</dd>
            </dl>
          </div>
          <div>
            <div class="eyebrow" style="margin-bottom:8px;display:flex;align-items:center;gap:8px">
              <span>Zonas configuradas</span><span class="sp" style="flex:1"></span>
              ${API.pode('cams') ? `<button class="btn btn-ghost btn-sm" id="btZonas">${ico('edit',13)} Desenhar</button>` : ''}
            </div>
            <div class="zone-list">${(c.zonas||[]).map(z => `<div class="zone-item">
              <span class="zone-swatch" style="background:${esc(z.cor)}"></span><b>${esc(z.nome)}</b>
              <span class="chip c-gray">${z.tipo === 'LINHA' ? 'Linha' : 'Polígono'}</span>
              <span class="mono" style="font-size:10.5px;color:var(--txt-4)">${(z.pontos||[]).length} pts</span></div>`).join('')
              || `<p class="hint">Nenhuma zona definida. Sem zona, as ações de
                   invasão de perímetro, área restrita, veículo em área proibida e
                   contrafluxo não têm onde disparar.</p>`}</div>
          </div>
        </div>
      </div>
      <div class="eyebrow" style="margin:18px 0 8px">Últimos eventos desta câmera</div>
      ${itens.length ? `<div class="rank" style="border:1px solid var(--line-soft);border-radius:var(--r-m)">
        ${itens.map(d => `<div class="rank-row clickable" data-det="${esc(d.codigo)}" style="cursor:pointer">
          <canvas data-snap="${snapAttr(d)}" width="46"></canvas>
          <div class="rank-bd"><b>${esc(tituloDeteccao(d))}</b><span class="sub">${fmtDT(d.ocorrido_em)}</span></div>
          <span class="chip ${d.tipo === 'FACIAL' ? 'c-blue' : d.tipo === 'OBJETO' ? 'c-green' : 'c-violet'}">${d.tipo}</span>
        </div>`).join('')}</div>` : '<p class="hint">Sem eventos registrados.</p>'}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarSnaps(box);
  $$('[data-det]', box).forEach(r => r.onclick = () => { fecharModal(); abrirDeteccao(r.dataset.det); });

  const cv = $('#camModalFeed');
  if(c.status === 'ONLINE'){
    // `hd`: pede o quadro em resolução nativa à ponte RTSP.
    // `conter`: mostra o campo de visão inteiro, que é o que se precisa ver
    // para ajustar a câmera — o corte do mosaico escondia as bordas.
    FEED.register(cv, c, {zonas: true, hd: true, conter: true});
    FEED.start();
    // A resolução da ficha é a cadastrada, e ela pode divergir do que a
    // câmera entrega. Quem decide a proporção da caixa é o quadro que chegou:
    // vindo do próprio JPEG, não sobra tarja nem com o cadastro errado.
    const quadro = $('#camQuadro');
    const relogio = setInterval(() => {
      if(!cv.isConnected){ clearInterval(relogio); return; }
      const t = FEED.tiles.find(x => x.canvas === cv);
      const img = t && t.img;
      if(img && img.naturalWidth){
        const real = img.naturalWidth / img.naturalHeight;
        // Propriedade por propriedade, e nao `replace` sobre o cssText: o
        // navegador normaliza o texto do atributo (espacos, ordem), e a
        // substituicao textual passaria batido sem erro nenhum.
        if(quadro && Math.abs(real - razaoInicial) > 0.01){
          aplicarMedida(quadro, real);
        }
        clearInterval(relogio);
      }
    }, 400);
  }
  else renderOnce(cv, c);

  const bz = $('#btZonas');
  if(bz) bz.onclick = () => abrirEditorZonas(c);
}


/* ==========================================================
   EDITOR DE ZONAS
   ==========================================================

   Quatro das quinze ações — invasão de perímetro, pessoa em área restrita,
   veículo em área proibida e contrafluxo — só têm sentido dentro de uma
   região marcada: sem ela não há "dentro" nem "fora" para comparar. O banco
   guardava zonas desde o começo, o vídeo já sabia desenhá-las e a ficha já as
   listava, mas não havia por onde criar uma. Eram zero no parque inteiro, e
   as quatro ações ficavam ligáveis e mudas.

   O ponto é gravado normalizado em 0..1, e não em pixel: a mesma zona vale
   quando a câmera troca de resolução, e é assim que o detector a compara com
   a caixa do objeto, que também é normalizada. */
function abrirEditorZonas(cam){
  const zonas = (cam.zonas || []).map(z => ({
    nome: z.nome, tipo: z.tipo, cor: z.cor,
    pontos: (z.pontos || []).map(p => [p[0], p[1]]),
    regras: z.regras || {},
  }));
  let atual = zonas.length ? 0 : -1;

  const PALETA = ['#35A7DE', '#E5484D', '#F5A524', '#30A46C', '#8E4EC6'];

  /* O que cada tipo de zona pode vigiar — e a chave que o detector consulta.
     Desenhar a região não basta: o analisador pergunta à zona *o que* ela
     guarda, e uma zona sem regra é consultada por ninguém. Era o que faltava
     para as quatro ações saírem de ligadas-e-mudas. */
  const REGRAS_ZONA = {
    POLIGONO: [
      ['restrita',         'Pessoa em área restrita'],
      ['veiculo_proibido', 'Veículo em área proibida'],
      ['contrafluxo',      'Deslocamento em contrafluxo'],
    ],
    // Perímetro é cruzamento de linha: é a única coisa que uma linha decide.
    LINHA: [['invasao', 'Invasão de perímetro']],
  };

  /* O sentido permitido do corredor, em coordenadas da imagem — y cresce
     para baixo, como no canvas e como o detector compara. Marcar
     "contrafluxo" sem dizer a direção deixa a zona sem contra o quê
     comparar: ela grava, e fica muda. */
  const SENTIDOS = [
    [[0, -1], 'Para cima',    '↑'],
    [[0,  1], 'Para baixo',   '↓'],
    [[-1, 0], 'Para a esquerda', '←'],
    [[1,  0], 'Para a direita',  '→'],
  ];

  const editorSentido = z => {
    if(!z.regras.contrafluxo) return '';
    const atual = z.regras.direcao || [];
    return `<div class="field" id="zSentido">
      <label>Sentido permitido do fluxo</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${SENTIDOS.map(([v, rot, seta]) => `
        <button type="button" class="btn btn-sm ${atual[0]===v[0]&&atual[1]===v[1] ? '' : 'btn-ghost'}"
          data-sentido="${v[0]},${v[1]}" title="${esc(rot)}">${seta} ${esc(rot)}</button>`).join('')}
      </div>
      <span class="hint" style="margin-top:4px">${atual.length
        ? 'Quem andar contra este sentido, e insistir, vira ocorrência.'
        : 'Sem sentido escolhido a zona grava e fica muda — não há contra o quê comparar.'}</span>
    </div>`;
  };

  abrirModal(`
    <div class="modal-hd"><h3>Zonas de ${esc(cam.nome)}</h3><span class="sp" style="flex:1"></span>
      <button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <p class="hint" style="margin:0 0 10px">Clique sobre a imagem para marcar os vértices.
        O polígono fecha sozinho; a linha usa dois pontos. Pelo menos 3 pontos para
        polígono e 2 para linha.</p>
      <div class="zona-edit">
        <div class="zona-tela">
          <canvas id="zonaCv"></canvas>
          <div class="zona-vazia" id="zonaAviso" hidden></div>
        </div>
        <div class="zona-lado">
          <div class="eyebrow" style="margin-bottom:6px">Zonas desta câmera</div>
          <div id="zonaLista"></div>
          <button class="btn btn-ghost btn-sm" id="zonaNova" style="width:100%;margin-top:8px">
            ${ico('plus',13)} Nova zona</button>
          <div id="zonaForm" style="margin-top:12px"></div>
        </div>
      </div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="zonaSalvar">Salvar zonas</button>
    </div>`, true);

  const cv = $('#zonaCv'), ctx = cv.getContext('2d');
  let fundo = null;

  /* O quadro da câmera por baixo. Sem ele o operador desenharia no escuro —
     uma zona só faz sentido em relação ao que a câmera vê. Se a imagem não
     vier (câmera offline, sem captura), o desenho continua possível sobre uma
     grade, porque a zona é geometria e não depende da foto. */
  const img = new Image();
  img.onload = () => { fundo = img; medir(); pintar(); };
  img.onerror = () => { $('#zonaAviso').hidden = false;
    $('#zonaAviso').textContent = 'Sem imagem desta câmera — o desenho vale assim mesmo.';
    medir(); pintar(); };
  img.src = `${API.base}/stream/${encodeURIComponent(cam.codigo)}/snapshot`
          + `?token=${encodeURIComponent(API.access)}&t=${Date.now()}`;

  function medir(){
    const larg = cv.parentElement.clientWidth || 520;
    const prop = fundo && fundo.naturalWidth ? fundo.naturalHeight / fundo.naturalWidth : 9 / 16;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const alt = Math.round(larg * prop);
    cv.width = larg * dpr; cv.height = alt * dpr;
    cv.style.width = larg + 'px'; cv.style.height = alt + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function pintar(){
    const W = parseFloat(cv.style.width), H = parseFloat(cv.style.height);
    ctx.clearRect(0, 0, W, H);
    if(fundo) ctx.drawImage(fundo, 0, 0, W, H);
    else {
      ctx.fillStyle = '#0A1218'; ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = 'rgba(255,255,255,.06)'; ctx.lineWidth = 1;
      for(let i = 1; i < 8; i++){
        ctx.beginPath(); ctx.moveTo(W*i/8, 0); ctx.lineTo(W*i/8, H); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, H*i/8); ctx.lineTo(W, H*i/8); ctx.stroke();
      }
    }
    zonas.forEach((z, i) => {
      const pts = z.pontos;
      if(!pts.length) return;
      const ativo = i === atual;
      ctx.save();
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      ctx.beginPath();
      pts.forEach((p, k) => { const x = p[0]*W, y = p[1]*H; k ? ctx.lineTo(x,y) : ctx.moveTo(x,y); });
      if(z.tipo === 'POLIGONO' && pts.length > 2){
        ctx.closePath();
        ctx.fillStyle = z.cor + (ativo ? '2A' : '1F');
        ctx.fill();
      }
      ctx.setLineDash(z.tipo === 'LINHA' ? [] : [6, 4]);
      // Mesmo contorno escuro do mosaico: aqui se desenha sobre o quadro
      // real da câmera, então o fundo é tão claro quanto lá — e é pior
      // errar o traçado por não enxergá-lo do que por descuido.
      ctx.strokeStyle = 'rgba(0,0,0,0.60)';
      ctx.lineWidth = ativo ? 5 : 4;
      ctx.stroke();
      ctx.strokeStyle = z.cor;
      ctx.lineWidth = ativo ? 2.4 : 1.8;
      ctx.stroke(); ctx.setLineDash([]);
      // Alça em cada vértice, só na zona em edição: é o que mostra onde se
      // clicou e permite conferir a forma antes de salvar.
      if(ativo) pts.forEach(p => {
        ctx.beginPath(); ctx.arc(p[0]*W, p[1]*H, 4, 0, Math.PI*2);
        ctx.fillStyle = z.cor; ctx.fill();
        ctx.lineWidth = 1.5; ctx.strokeStyle = '#0A1218'; ctx.stroke();
      });
      ctx.restore();
    });
  }

  function listar(){
    $('#zonaLista').innerHTML = zonas.length ? zonas.map((z, i) => `
      <div class="zona-item ${i === atual ? 'on' : ''}" data-z="${i}">
        <span class="zone-swatch" style="background:${esc(z.cor)}"></span>
        <b>${esc(z.nome)}</b>
        <span class="chip c-gray">${z.tipo === 'LINHA' ? 'Linha' : 'Polígono'}</span>
        <span class="mono" style="font-size:10px;color:var(--txt-4)">${z.pontos.length}</span>
        <button class="x" data-rm="${i}" title="Remover">${ico('trash',13)}</button>
      </div>`).join('') : '<p class="hint" style="margin:0">Nenhuma ainda.</p>';

    $$('[data-z]', $('#zonaLista')).forEach(el => el.onclick = e => {
      if(e.target.closest('[data-rm]')) return;
      atual = +el.dataset.z; listar(); formulario(); pintar();
    });
    $$('[data-rm]', $('#zonaLista')).forEach(b => b.onclick = () => {
      zonas.splice(+b.dataset.rm, 1);
      atual = Math.min(atual, zonas.length - 1);
      listar(); formulario(); pintar();
    });
  }

  function formulario(){
    const alvo = $('#zonaForm');
    if(atual < 0 || !zonas[atual]){ alvo.innerHTML = ''; return; }
    const z = zonas[atual];
    const minimo = z.tipo === 'LINHA' ? 2 : 3;
    alvo.innerHTML = `
      <div class="field"><label for="zNome">Nome</label>
        <input class="inp" id="zNome" value="${esc(z.nome)}" maxlength="120"></div>
      <div class="field"><label for="zTipo">Tipo</label>
        <select class="inp" id="zTipo">
          <option value="POLIGONO" ${z.tipo==='POLIGONO'?'selected':''}>Polígono — área</option>
          <option value="LINHA" ${z.tipo==='LINHA'?'selected':''}>Linha — cruzamento</option>
        </select></div>
      <div class="field"><label>Cor</label>
        <div style="display:flex;gap:6px">${PALETA.map(c => `
          <span class="zona-cor ${z.cor===c?'on':''}" data-cor="${c}" style="background:${c}"></span>`).join('')}
        </div></div>
      <div class="field"><label>O que esta zona vigia</label>
        ${REGRAS_ZONA[z.tipo].map(([k, rot]) => `
          <label class="zona-regra"><input type="checkbox" data-regra="${k}"
            ${z.regras[k] ? 'checked' : ''}><span>${esc(rot)}</span></label>`).join('')}
        <span class="hint" style="margin-top:4px">Sem nenhuma marcada, a zona é
          só desenho: nenhuma ação consulta a região.</span></div>
      ${editorSentido(z)}
      <div class="field"><label for="zAutorizados">Quem pode estar aqui</label>
        <input class="inp mono" id="zAutorizados" placeholder="BIO-M-000012, BIO-F-000004"
               value="${esc((z.regras.autorizados || []).join(', '))}">
        <span class="hint">Identificadores biométricos separados por vírgula.
          Em branco, qualquer pessoa na região vira ocorrência. Quem o
          reconhecimento ainda não identificou conta como não autorizado.</span></div>
      <p class="hint" style="margin:8px 0 0">
        ${z.pontos.length} de ${minimo} ponto(s) mínimos.
        ${z.pontos.length >= minimo ? 'Pronta.' : 'Clique na imagem para marcar.'}</p>
      <button class="btn btn-ghost btn-sm" id="zLimpar" style="width:100%;margin-top:8px"
        ${z.pontos.length ? '' : 'disabled'}>Apagar pontos</button>`;

    $('#zNome').oninput = e => { z.nome = e.target.value; listar(); };
    $('#zTipo').onchange = e => {
      z.tipo = e.target.value;
      // Linha é um segmento: guardar seis vértices e desenhar dois enganaria
      // quem conferisse a contagem na lista.
      if(z.tipo === 'LINHA' && z.pontos.length > 2) z.pontos = z.pontos.slice(0, 2);
      // Regra de polígono não vale em linha, e vice-versa: deixá-la gravada
      // criaria uma zona que diz vigiar algo que o detector nunca pergunta
      // àquele tipo.
      // `autorizados` sobrevive à troca: é a lista de quem pode estar ali, e
      // vale tanto para a linha quanto para o polígono. Perdê-la ao alternar
      // o tipo apagaria sem avisar uma decisão de acesso.
      // `direcao` acompanha `contrafluxo`, e some com ele — mas não pode
      // sumir por ser desconhecida do filtro, que a apagava em silêncio.
      const validas = REGRAS_ZONA[z.tipo].map(([k]) => k)
        .concat('autorizados', 'direcao');
      z.regras = Object.fromEntries(
        Object.entries(z.regras).filter(([k, v]) => v && validas.includes(k)));
      listar(); formulario(); pintar();
    };
    $$('[data-cor]', alvo).forEach(s => s.onclick = () => {
      z.cor = s.dataset.cor; listar(); formulario(); pintar();
    });
    $$('[data-regra]', alvo).forEach(cx => cx.onchange = () => {
      z.regras = {...z.regras, [cx.dataset.regra]: cx.checked};
      // O seletor de sentido só existe enquanto contrafluxo está marcado.
      if(cx.dataset.regra === 'contrafluxo') formulario();
      listar();
    });
    $$('[data-sentido]', alvo).forEach(bt => bt.onclick = () => {
      z.regras = {...z.regras,
                  direcao: bt.dataset.sentido.split(',').map(Number)};
      formulario(); listar();
    });
    const aut = $('#zAutorizados', alvo);
    if(aut) aut.oninput = () => {
      // Lista vazia some da regra em vez de ficar como `[]`: é assim que o
      // detector lê "esta zona não libera ninguém".
      const ids = aut.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
      z.regras = {...z.regras, autorizados: ids.length ? ids : undefined};
      listar();
    };
    $('#zLimpar').onclick = () => { z.pontos = []; formulario(); listar(); pintar(); };
  }

  cv.onclick = e => {
    if(atual < 0 || !zonas[atual]) return;
    const z = zonas[atual];
    if(z.tipo === 'LINHA' && z.pontos.length >= 2) z.pontos = [];
    const r = cv.getBoundingClientRect();
    // Preso ao intervalo 0..1: o servidor recusa ponto fora dele, e um clique
    // na borda pode render 1,0003 por arredondamento do navegador.
    const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
    z.pontos.push([+x.toFixed(4), +y.toFixed(4)]);
    formulario(); listar(); pintar();
  };

  $('#zonaNova').onclick = () => {
    zonas.push({nome: `Zona ${zonas.length + 1}`, tipo: 'POLIGONO',
                cor: PALETA[zonas.length % PALETA.length], pontos: [], regras: {}});
    atual = zonas.length - 1;
    listar(); formulario(); pintar();
  };

  $('#zonaSalvar').onclick = async () => {
    // Zona com pontos de menos não é zona: o polígono não fecha área e a
    // linha não tem direção. Recusar aqui é melhor que gravar uma região que
    // o detector vai ignorar em silêncio.
    const incompletas = zonas.filter(z => z.pontos.length < (z.tipo === 'LINHA' ? 2 : 3));
    if(incompletas.length){
      toast('Zona incompleta',
            `${incompletas.map(z => z.nome).join(', ')} — polígono precisa de 3 pontos, linha de 2.`,
            'err', 6000);
      return;
    }
    // Zona sem regra é gravável e inútil: o detector não a consulta. Avisar
    // é melhor que recusar — pode ser uma região desenhada agora para marcar
    // a regra depois —, mas calar seria deixar o operador achar que ligou
    // uma vigilância que não existe.
    // Só as chaves de vigilância contam. `autorizados` diz *quem escapa* de
    // uma regra, não cria regra nenhuma: uma zona que lista autorizados e não
    // marca nada segue sem ninguém a consultando, e o aviso tem de sair.
    const mudas = zonas.filter(z => !REGRAS_ZONA[z.tipo]
      .some(([k]) => (z.regras || {})[k]));
    // Contrafluxo sem sentido declarado é o mesmo caso, um nível adiante: a
    // regra está marcada e o detector não tem contra o quê comparar.
    const semSentido = zonas.filter(
      z => z.regras && z.regras.contrafluxo && !(z.regras.direcao || []).length);
    /* Um aviso só, e que não destrói o editor.

       `confirmar` reaproveita a mesma caixa de modal do editor: ao abrir,
       apaga o desenho da tela. Enquanto cada checagem perguntava por sua
       conta, cancelar qualquer uma delas levava junto o traçado que a
       pessoa acabara de fazer — e, como a maioria das câmeras está com a
       análise de ação desligada, o aviso que eu mesmo acrescentei fazia
       isso acontecer quase sempre. Era o "não está salvando".

       Agora as três checagens viram um texto só, e cancelar devolve o
       editor com o desenho intacto. */
    const ACAO_DA_REGRA = {restrita: 'area_restrita', invasao: 'invasao',
                           veiculo_proibido: 'veiculo_proibido',
                           contrafluxo: 'contrafluxo'};
    const ligadas = new Set(cam.analitico_acao ? (cam.acoes_ativas || []) : []);
    const desligadas = [...new Set(zonas.flatMap(
      z => Object.entries(ACAO_DA_REGRA)
        .filter(([regra, acao]) => z.regras[regra] && !ligadas.has(acao))
        .map(([, acao]) => ROTULO_ACAO_UI[acao] || acao)))];

    /* Gravar zonas substitui a lista inteira da câmera: o que não for
       enviado deixa de existir. Hoje a Garagem perdeu duas zonas e o
       corredor do 4º andar perdeu a dele por esse caminho, e os pontos não
       voltam — a auditoria guarda que a câmera mudou, não o traçado. Dizer
       o nome do que vai sumir é barato e teria evitado isso. */
    const originais = (cam.zonas || []).map(z => z.nome);
    const removidas = originais.filter(n => !zonas.some(z => z.nome === n));

    const avisos = [];
    if(removidas.length)
      avisos.push(`<b>${removidas.map(esc).join('</b>, <b>')}</b>
         ${removidas.length > 1 ? 'serão apagadas' : 'será apagada'} desta
         câmera. O traçado não volta depois.`);
    if(semSentido.length)
      avisos.push(`${semSentido.map(z => `<b>${esc(z.nome)}</b>`).join(', ')} vigia
         contrafluxo mas não diz o sentido permitido — sem isso a regra nunca
         dispara.`);
    if(mudas.length)
      avisos.push(`${mudas.map(z => `<b>${esc(z.nome)}</b>`).join(', ')} não vigia
         nada: nenhuma ação consulta a região.`);
    if(desligadas.length)
      avisos.push(`A zona pede <b>${desligadas.join('</b>, <b>')}</b>, mas
         ${cam.analitico_acao
            ? 'essa ação não está marcada na configuração desta câmera'
            : '<b>a análise de ação está desligada</b> nesta câmera'} — o
         desenho fica gravado e nenhum alerta nasce.`);

    if(avisos.length){
      const segue = await confirmar('Confira antes de salvar',
        avisos.join('<br><br>') + '<br><br>Salvar assim mesmo?',
        {rotulo: 'Salvar assim'});
      if(!segue){
        // O aviso ocupou a caixa do editor. Devolvê-lo com `zonas` — o
        // rascunho em memória, e não `cam.zonas` — é o que preserva o que
        // ainda não foi gravado.
        abrirEditorZonas({...cam, zonas});
        return;
      }
    }
    /* O botão pode já não existir. `confirmar` termina chamando
       `fecharModal`, que esvazia a caixa do modal — a mesma que o editor
       ocupa. Depois de qualquer aviso, `$('#zonaSalvar')` é null, e mexer
       nele lançava TypeError bem aqui: o salvamento morria antes do pedido,
       sem erro na tela e sem zona gravada. Era a causa real do "não salva",
       e valia mesmo para quem clicava em "Salvar assim". */
    const bt = $('#zonaSalvar');
    if(bt){ bt.disabled = true; bt.textContent = 'Salvando…'; }
    try{
      const salva = await API.salvarCamera(cam.codigo, {zonas});
      toast('Zonas salvas', `${zonas.length} zona(s) em ${cam.nome}.`, 'ok');
      // O catálogo em memória é de onde o mosaico tira as zonas que desenha.
      // Sem recarregá-lo, a zona era gravada no banco e o "Ao vivo" seguia
      // mostrando a de antes até a página ser recarregada inteira.
      try{ await carregarCatalogoCameras(); }
      catch(e){ /* a zona está salva; o desenho se corrige na próxima volta */ }
      fecharModal();
      // A ficha reabre com o que foi gravado — inclusive o que o servidor
      // tenha normalizado.
      abrirCamera(salva.codigo || cam.codigo);
    }catch(e){
      toast('Não foi possível salvar', e.message, 'err', 7000);
      // Devolve o editor com o rascunho. Sem isto, uma falha de rede levava
      // junto o desenho que ainda não tinha sido gravado.
      abrirEditorZonas({...cam, zonas});
    }
  };

  window.addEventListener('resize', () => { medir(); pintar(); }, {once: true});
  medir(); listar(); formulario(); pintar();
}
