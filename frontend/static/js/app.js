/* ==========================================================
   SEGURANÇA — Shell da aplicação
   Autenticação, recuperação de senha, navegação e barra de status.
   ========================================================== */
'use strict';

const APP = {
  view: 'dash',
  cfg: {},          // preenchido por /api/info
  mosaico: 9,
  paginaMosaico: 0,
  camSelecionada: null,
  pausado: false,
  cameras: [],      // catálogo carregado uma vez por sessão
  relogio: null,
  periodoDesde: null,
  periodoAte: null
};

/* ==========================================================
   AUTENTICAÇÃO
   ========================================================== */
let REC = {email:'', token:'', expira:0, timer:null};

function authAlert(alvo, tipo, msg){
  const el = $(alvo);
  if(!msg){ el.innerHTML = ''; return; }
  const icone = tipo === 'ok' ? 'i-check'
              : (tipo === 'err' || tipo === 'warn') ? 'i-alert' : 'i-info';
  el.innerHTML = `<div class="alert alert-${tipo}">${ico(icone,15)}<div>${msg}</div></div>`;
}

async function fazerLogin(){
  const u = $('#loginUser').value.trim();
  const p = $('#loginPass').value;
  if(!u || !p){ authAlert('#loginAlert', 'err', 'Informe e-mail/matrícula e senha.'); return; }

  const btn = $('#btnLogin');
  btn.disabled = true;
  btn.innerHTML = ico('lock',16) + ' Verificando…';
  authAlert('#loginAlert', '', '');

  try{
    // Sem "manter conectado": a sessão vive só na aba (sessionStorage).
    const usuario = await API.login(u, p, false);
    await entrarNoSistema(usuario);
  }catch(e){
    if(e.status === 428){
      authAlert('#loginAlert', 'info', 'Sua senha precisa ser redefinida. Redirecionando…');
      setTimeout(() => { $('#recEmail').value = u.includes('@') ? u : ''; mostrarRecover(); }, 900);
    }else{
      authAlert('#loginAlert', 'err', esc(e.message));
    }
  }finally{
    btn.disabled = false;
    btn.innerHTML = ico('lock',16) + ' Entrar no sistema';
  }
}

async function entrarNoSistema(usuario){
  $('#authScreen').hidden = true;
  $('#app').hidden = false;
  AUTHFX.parar();
  await montarApp(usuario);
  toast('Sessão iniciada', `${usuario.nome} · ${rotuloPerfil(usuario.perfil)}`, 'ok');
}

async function sair(){
  await API.logout();
  EventosAoVivo.desconectar();
  FEED.stop(); FEED.clear();
  clearInterval(APP.relogio);
  $('#app').hidden = true;
  $('#authScreen').hidden = false;
  $('#loginPass').value = '';
  mostrarLogin();
  AUTHFX.iniciar();
  toast('Sessão encerrada', 'Suas credenciais foram liberadas deste terminal.', 'info');
}

const rotuloPerfil = p => ({
  ADMIN:'Administrador', SUPER:'Supervisor', OPERADOR:'Operador',
  ANALISTA:'Analista', AUDITOR:'Auditor'
}[p] || p);

function mostrarLogin(){
  $('#viewLogin').hidden = false;
  $('#viewRecover').hidden = true;
  authAlert('#loginAlert', '', '');
}
function mostrarRecover(){
  $('#viewLogin').hidden = true;
  $('#viewRecover').hidden = false;
  recStep(1);
  authAlert('#recAlert', '', '');
  if(!$('#recEmail').value && $('#loginUser').value.includes('@')){
    $('#recEmail').value = $('#loginUser').value;
  }
}
function recStep(n){
  [1,2,3].forEach(i => $('#recStep' + i).hidden = i !== n);
  $$('#recStepper .step').forEach(s => {
    const i = +s.dataset.step;
    s.classList.toggle('active', i === n);
    s.classList.toggle('done', i < n);
    s.querySelector('.step-dot').innerHTML = i < n ? ico('check',12) : i;
  });
  $$('#recStepper .step-bar').forEach((b, i) => b.classList.toggle('done', i < n - 1));
}

/* ---------- Etapa 1 ---------- */
async function enviarCodigo(){
  const e = $('#recEmail').value.trim().toLowerCase();
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e)){
    authAlert('#recAlert', 'err', 'Informe um e-mail funcional válido.'); return;
  }
  const btn = $('#btnRecSend');
  btn.disabled = true;
  try{
    const r = await API.recuperar(e);
    REC.email = e;
    $('#recMailEcho').textContent = e;

    /* Sempre a etapa 2, com o mesmo texto, exista ou não a conta.

       A tela já parou na etapa 1 dizendo "não há conta", e já mostrou o link
       direto como atalho. As duas coisas entregavam a quem estivesse na rede
       quem tem cadastro — e o atalho, a própria conta. O servidor agora
       responde igual nos dois casos, e a tela não tem o que distinguir. */
    REC.expira = Date.now() + (r.expira_em_segundos || 1800) * 1000;
    recStep(2);
    iniciarTimerOTP();
    authAlert('#recAlert', 'info', '');
  }catch(err){
    authAlert('#recAlert', 'err', esc(err.message));
  }finally{ btn.disabled = false; }
}

function iniciarTimerOTP(){
  clearInterval(REC.timer);
  REC.timer = setInterval(() => {
    const s = Math.max(0, Math.floor((REC.expira - Date.now()) / 1000));
    const el = $('#otpTimer');
    if(el) el.textContent = `${pad(Math.floor(s/60))}:${pad(s%60)}`;
    if(s <= 0){ clearInterval(REC.timer); if(el){ el.textContent = '00:00'; el.style.color = 'var(--red)'; } }
  }, 1000);
}

/* ---------- Abertura pelo link do e-mail ---------- */
/* O segredo vem no fragmento da URL (`#redefinir=...`), não na query.

   O fragmento não é enviado ao servidor em requisição nenhuma: não entra em
   log de acesso nem em cabeçalho de referência, que é o que aconteceria com
   `?token=`. Para um segredo que dá acesso à conta, é a diferença entre ficar
   só no navegador de quem abriu o e-mail e ficar gravado em cada intermediário
   do caminho. */
/* Um link abre o formulário uma vez por aba.

   Guardar isso é o que impede a etapa 3 de voltar sozinha num F5. O segredo
   sai da barra de endereço assim que é lido, então em tese o recarregamento
   já cairia no login — mas "em tese" depende de o `replaceState` ter valido,
   e basta um app.js velho em cache para a página voltar ao formulário de
   senha. Aqui a resposta não depende da URL: a aba lembra que já abriu aquele
   link, e recarregar passa a dizer isso em vez de reabrir a troca de senha.

   Fica em `sessionStorage` e não em `localStorage` de propósito: morre com a
   aba, então reabrir o e-mail numa aba nova continua funcionando — que é o
   caso de quem fechou a página sem querer. */
const LINKS_ABERTOS = 'seguranca.links_abertos';
function linkJaAberto(token){
  try{
    return (JSON.parse(sessionStorage.getItem(LINKS_ABERTOS)) || []).includes(token);
  }catch(e){ return false; }
}
function marcarLinkAberto(token){
  try{
    const vistos = JSON.parse(sessionStorage.getItem(LINKS_ABERTOS)) || [];
    vistos.push(token);
    // Só os últimos: a lista não precisa crescer, e o que interessa é o link
    // recém-aberto, não o histórico da aba.
    sessionStorage.setItem(LINKS_ABERTOS, JSON.stringify(vistos.slice(-5)));
  }catch(e){}
}

async function abrirPorLink(token){
  mostrarRecover();
  authAlert('#recAlert', 'info', 'Validando o link…');
  marcarLinkAberto(token);
  try{
    const r = await API.verificarLink(token);
    REC.token = token;
    REC.email = r.email;
    REC.expira = Date.now() + (r.expira_em_segundos || 1800) * 1000;
    const eco = $('#recMailEcho'); if(eco) eco.textContent = r.email;
    // A etapa 3 só aparece depois de o servidor aceitar o link. Mostrá-la
    // antes fazia um link vencido piscar o formulário de nova senha para só
    // então recuar — e quem viu o formulário entende que ele valia.
    recStep(3);
    authAlert('#recAlert', 'ok',
      `Link validado. Defina a nova senha de <b>${esc(r.email)}</b>.`);
    iniciarTimerOTP();
    setTimeout(() => $('#recPass').focus(), 120);
  }catch(e){
    recStep(1);
    authAlert('#recAlert', 'err', esc(e.message));
  }
}

/* ---------- Etapa 3 ---------- */
function forcaSenha(p){
  const r = {
    len: p.length >= 10, upper: /[A-Z]/.test(p), lower: /[a-z]/.test(p),
    num: /[0-9]/.test(p), sym: /[^A-Za-z0-9]/.test(p)
  };
  let s = Object.values(r).filter(Boolean).length;
  if(p.length >= 14) s = Math.min(5, s + 1);
  return {score: Math.max(0, s - 1), regras: r};
}
function atualizarMedidor(){
  const p = $('#recPass').value;
  const {score, regras} = forcaSenha(p);
  const cores = ['var(--red)','var(--amber)','var(--amber)','var(--green)'];
  $$('#pwMeter i').forEach((b, i) =>
    b.style.background = (p && i < score) ? cores[Math.min(score-1, 3)] : 'var(--ink-700)');
  $$('#pwRules .pw-rule').forEach(r => r.classList.toggle('ok', !!regras[r.dataset.rule]));
}
async function salvarSenha(){
  const p = $('#recPass').value, p2 = $('#recPass2').value;
  const {score, regras} = forcaSenha(p);
  if(!Object.values(regras).every(Boolean)){
    authAlert('#recAlert', 'err', 'A senha não atende a todos os requisitos de complexidade.'); return;
  }
  if(p !== p2){ authAlert('#recAlert', 'err', 'As senhas não conferem.'); return; }
  if(score < 3){ authAlert('#recAlert', 'err', 'Escolha uma senha mais forte.'); return; }

  const btn = $('#btnRecSave');
  btn.disabled = true;
  try{
    await API.redefinirSenha(REC.token, p);
    authAlert('#recAlert', 'ok', 'Senha redefinida com sucesso. Voltando ao login…');
    // Volta ao login para fazer o processo completo: e-mail + senha + enter
    setTimeout(() => { mostrarLogin(); $('#loginUser').focus(); }, 1200);
  }catch(e){
    authAlert('#recAlert', 'err', esc(e.message));
  }finally{ btn.disabled = false; }
}

$('#btnLogin').onclick = fazerLogin;
$('#loginPass').addEventListener('keydown', e => { if(e.key === 'Enter') fazerLogin(); });
$('#loginUser').addEventListener('keydown', e => { if(e.key === 'Enter') $('#loginPass').focus(); });
$('#goRecover').onclick = mostrarRecover;
$('#btnRecBack1').onclick = mostrarLogin;
$('#btnRecBack2').onclick = () => { clearInterval(REC.timer); recStep(1); };
$('#btnRecSend').onclick = enviarCodigo;
$('#recEmail').addEventListener('keydown', e => { if(e.key === 'Enter') enviarCodigo(); });
/* Campos de senha sem gerenciador de senhas do navegador.

   Com `type="password"` o Chrome oferece gerar uma senha forte, guardá-la na
   conta Google de quem está na máquina e preenchê-la sozinho depois — num
   posto compartilhado, isso deixa a senha de um operador ao alcance do
   próximo. `autocomplete="off"` não impede: em campo de senha ele é ignorado.

   O que impede é o campo deixar de ser de senha para o navegador. Vira texto,
   mascarado por CSS com as mesmas bolinhas. Só onde o navegador sabe mascarar
   (`-webkit-text-security`); nos outros fica `password`, porque mostrar a
   senha em claro seria pior que o aviso. Copiar e recortar ficam bloqueados,
   como num campo de senha comum. */
function protegerCamposDeSenha(){
  const sabeMascarar = window.CSS && CSS.supports &&
    CSS.supports('-webkit-text-security', 'disc');
  if(!sabeMascarar) return;
  $$('input[data-senha]').forEach(el => {
    el.type = 'text';
    el.classList.add('senha-mascarada');
    el.setAttribute('autocorrect', 'off');
    ['copy', 'cut', 'dragstart'].forEach(ev =>
      el.addEventListener(ev, e => e.preventDefault()));
  });
}
protegerCamposDeSenha();

$('#btnResend').onclick = enviarCodigo;
$('#btnRecSave').onclick = salvarSenha;
$('#recPass').addEventListener('input', atualizarMedidor);
$('#recPass2').addEventListener('keydown', e => { if(e.key === 'Enter') salvarSenha(); });
/* ---------- Fundo animado do login ----------
   Um <canvas> aceita um único tipo de contexto: com o 2D tomado aqui, o
   WebGL do Three.js voltaria null. O desenho fica em three-auth.js. */
const AUTHFX = {
  iniciar(){ window.AuthFX3D && window.AuthFX3D.iniciar(); indicadoresDaMarca(); },
  parar(){ window.AuthFX3D && window.AuthFX3D.parar(); }
};

/* Os três números da coluna da marca.
   São indicadores do serviço — disponibilidade, componentes de pé e prazo de
   retenção. Aqui ficava a contagem de registros biométricos, que dizia a quem
   ainda não autenticou quantas pessoas a base monitora; a capacidade instalada
   do sistema é o que cabe nesta tela, não o volume de gente dentro dele. */
async function indicadoresDaMarca(){
  const alvo = $('#statUptime');
  if(!alvo) return;
  const pôr = (id, v) => { const el = $('#' + id); if(el) el.textContent = v; };
  try{
    const [info, saude] = await Promise.all([
      fetch('/api/info', {cache:'no-store'}).then(r => r.json()),
      fetch('/saude',    {cache:'no-store'}).then(r => r.json())
    ]);
    const comps = Object.values(saude.componentes || {});
    const oks = comps.filter(v => /^(ok|mem)/i.test(String(v))).length;
    pôr('statUptime', (info.estatisticas || {}).disponibilidade || '—');
    pôr('statComp', comps.length ? `${oks}/${comps.length}` : '—');
    pôr('statRet', (info.config || {}).retencao_dias ?? '—');
  }catch(e){
    ['statUptime','statComp','statRet'].forEach(id => pôr(id, '—'));
  }
}

/* ==========================================================
   SHELL
   ========================================================== */
const VIEWS = {
  dash:       {t:'Dashboard operacional', s:'Visão geral em tempo real',              perm:'live'},
  live:       {t:'Ao vivo', s:'Mosaico de câmeras e eventos',           perm:'live'},
  cams:       {t:'Câmeras',               s:'Cadastro, status e parâmetros de análise',perm:'live'},
  facial:     {t:'Reconhecimento facial', s:'Registros de identificação biométrica',  perm:'facial'},
  objects:    {t:'Detecção de objetos',   s:'Classificação de objetos por câmera',    perm:'facial'},
  actions:    {t:'Análise de ações',      s:'Eventos comportamentais detectados',     perm:'facial'},
  biometrics: {t:'Base biométrica',       s:'Registros com identificador permanente', perm:'biometrics'},
  alerts:     {t:'Alertas',               s:'Ocorrências que exigem tratamento',      perm:'alerts'},
  users:      {t:'Usuários e perfis',     s:'Controle de acesso e permissões',        perm:'users'},
  audit:      {t:'Trilha de auditoria',   s:'Registro imutável de operações',         perm:'audit'},
  settings:   {t:'Configurações',         s:'Parâmetros do sistema e da análise',     perm:'settings'}
};

async function montarApp(usuario){
  $('#usrName').textContent = usuario.nome;
  $('#usrRole').textContent = rotuloPerfil(usuario.perfil);
  $('#usrAvatar').textContent = iniciais(usuario.nome);

  $$('.nav-item').forEach(b => {
    const v = b.dataset.view;
    const perm = VIEWS[v] ? VIEWS[v].perm : null;
    b.style.display = (perm && !API.pode(perm)) ? 'none' : '';
    b.onclick = () => { irPara(v); document.body.classList.remove('nav-open'); };
  });

  try{ APP.cfg = await API.info(); }catch(e){ APP.cfg = {}; }

  FEED.aoMedirFps = fps => { const e = $('#mFps'); if(e) e.textContent = fps; };
  // O token vai na URL porque <img> não envia cabeçalho. Se a câmera não
  // responder, o onerror do próprio <img> devolve o tile à cena sintética.
  FEED.fonteAoVivo = (cam, opts) => cam.status === 'ONLINE'
    ? `${API.base}/stream/${encodeURIComponent(cam.codigo)}/mjpeg?token=${encodeURIComponent(API.access)}`
      + ((opts && opts.hd) ? '&hd=1' : '')
    : null;
  FEED.fonteSnapshot = cam => cam.status === 'ONLINE'
    ? `${API.base}/stream/${encodeURIComponent(cam.codigo)}/snapshot?token=${encodeURIComponent(API.access)}`
    : null;

  await carregarCatalogoCameras();
  iniciarRelogio();
  EventosAoVivo.conectar();

  const inicial = API.pode('live') ? 'dash' : (API.pode('audit') ? 'audit' : 'alerts');
  await irPara(inicial);
  atualizarContadores();
}

const iniciais = nome => String(nome || '').split(' ')
  .filter(w => w.length > 2).slice(-2).map(w => w[0]).join('').toUpperCase().slice(0, 2) || '–';

async function carregarCatalogoCameras(){
  try{ APP.cameras = await API.cameras(); }
  catch(e){ APP.cameras = []; }
  /* Os tiles no ar guardam o objeto de câmera que receberam ao nascer. Sem
     repontá-los, o mosaico segue desenhando a zona de antes depois de ela
     ter sido salva — a gravação deu certo, a tela continuou mentindo.

     Trocar a referência basta, e é melhor que redesenhar a tela: cada tile
     é uma conexão MJPEG viva, e recriá-los derrubaria e reconectaria o
     vídeo de todas as câmeras para mostrar uma linha nova. */
  if(typeof FEED !== 'undefined' && FEED.tiles){
    FEED.tiles.forEach(t => {
      const atual = t.cam && camPorCodigo(t.cam.codigo);
      if(atual) t.cam = atual;
    });
  }
}
const camPorCodigo = c => APP.cameras.find(x => x.codigo === c);

async function irPara(v){
  if(!VIEWS[v]) v = 'dash';
  if(VIEWS[v].perm && !API.pode(VIEWS[v].perm)){
    toast('Acesso negado',
      `Seu perfil (${rotuloPerfil(API.usuario.perfil)}) não possui esta permissão.`, 'err');
    return;
  }
  APP.view = v;
  FEED.clear();
  $$('.nav-item').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  $('#tbTitle').textContent = VIEWS[v].t;
  $('#tbSub').textContent = VIEWS[v].s;
  $('#main').scrollTop = 0;
  location.hash = v;

  const render = {
    dash: renderPainel, live: renderAoVivo, cams: renderCameras,
    facial: renderFacial, objects: renderObjetos, actions: renderAcoes,
    biometrics: renderBiometria, alerts: renderAlertas, users: renderUsuarios,
    audit: renderAuditoria, settings: renderConfiguracoes
  }[v];

  const alvo = $('#v-' + v);
  alvo.innerHTML = carregando();
  try{ await render(alvo); }
  catch(e){
    alvo.innerHTML = erroBloco(e, 'btnRetry');
    const r = $('#btnRetry'); if(r) r.onclick = () => irPara(v);
  }
}

/* ---------- Barra superior ---------- */
function iniciarRelogio(){
  const tick = () => {
    const d = new Date();
    $('#tbTime').textContent = fmtTime(d);
    $('#tbDate').textContent = d.toLocaleDateString('pt-BR',
      {weekday:'short', day:'2-digit', month:'short'}).replace('.', '');
  };
  tick();
  clearInterval(APP.relogio);
  APP.relogio = setInterval(tick, 1000);
}

async function atualizarContadores(){
  try{
    const [resumoA, resumoD, bio] = await Promise.all([
      API.pode('alerts') ? API.resumoAlertas() : Promise.resolve(null),
      API.pode('facial') ? API.resumoDeteccoes(24) : Promise.resolve(null),
      API.pode('biometrics') ? API.pessoas({por_pagina: 1}) : Promise.resolve(null)
    ]);
    const online = APP.cameras.filter(c => c.status === 'ONLINE').length;
    $('#mCams').textContent = online;
    $('#mCamsTot').textContent = APP.cameras.length;
    $('#navLive').textContent = online;
    $('#navCams').textContent = APP.cameras.length;

    if(resumoA){
      const abertos = (resumoA.abertos || 0) + (resumoA.em_andamento || 0);
      $('#mAlerts').textContent = abertos;
      $('#navAlerts').textContent = abertos;
      const badge = $('#bellBadge');
      badge.textContent = abertos;
      badge.style.display = abertos ? 'grid' : 'none';
    }
    if(bio) $('#navBio').textContent = fmtNum(bio.total);
    if(resumoD){
      const t = resumoD.por_tipo || {};
      $('#navFacial').textContent = fmtNum(t.FACIAL || 0);
      $('#navObj').textContent = fmtNum(t.OBJETO || 0);
      $('#navAct').textContent = fmtNum(t.ACAO || 0);
    }
  }catch(e){ /* contadores são informativos: falha não interrompe a operação */ }
}

/* ==========================================================
   PAINEL DE ALERTA

   Um alerta de um sistema integrado a presídio não pode terminar numa linha
   de lista que alguém abre depois. Ele interrompe: aparece acima de tudo,
   apita, e só sai quando uma pessoa confirma que viu.

   Não cobre a tela inteira de propósito — quem recebe o alerta pode precisar
   mexer no sistema antes de encerrá-lo, e um bloqueio total atrapalharia
   justamente no momento em que a resposta importa.
   ========================================================== */
const ALERTA = {
  fila: [],
  atual: null,
  audio: null,

  /* Dois bipes curtos, sintetizados — sem arquivo para baixar, sem depender
     de o navegador ter carregado um asset antes do primeiro alerta. O
     AudioContext só é criado depois do login, que já é o gesto do usuário
     que as políticas de autoplay exigem. */
  apitar(critico){
    try{
      this.audio = this.audio || new (window.AudioContext || window.webkitAudioContext)();
      if(this.audio.state === 'suspended') this.audio.resume();
      const t0 = this.audio.currentTime;
      [0, 0.22].forEach((atraso, i) => {
        const osc = this.audio.createOscillator(), g = this.audio.createGain();
        osc.type = 'sine';
        osc.frequency.value = critico ? (i ? 760 : 980) : 620;
        g.gain.setValueAtTime(0.0001, t0 + atraso);
        g.gain.exponentialRampToValueAtTime(0.16, t0 + atraso + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + atraso + 0.17);
        osc.connect(g).connect(this.audio.destination);
        osc.start(t0 + atraso); osc.stop(t0 + atraso + 0.2);
      });
    }catch(e){ /* som é reforço, não o aviso: sem ele o painel continua */ }
  },

  enfileirar(a){
    this.fila.push(a);
    this.apitar(a.severidade >= 4);
    // Já há cartão na tela: o novo espera, mas o contador precisa subir na
    // hora. Sem isto uma rajada aparecia como um alerta só — o cartão fora
    // desenhado quando a fila ainda estava vazia e nunca mais se atualizava.
    if(this.atual) this.atualizarFila(); else this.mostrar();
  },

  /* "2 alertas aguardando", com o número em destaque. Vazio quando não há
     ninguém atrás deste — o espaço continua lá, para os botões não pularem. */
  textoFila(n){
    return n ? `<b>${n}</b> ${n === 1 ? 'alerta aguardando' : 'alertas aguardando'}` : '';
  },

  atualizarFila(){
    const marca = $('#alertaFaixa .alerta-fila');
    if(marca) marca.innerHTML = this.textoFila(this.fila.length);
  },

  mostrar(){
    const a = this.atual = this.fila.shift();
    const faixa = $('#alertaFaixa');
    if(!a){ faixa.hidden = true; faixa.innerHTML = ''; return; }

    const critico = a.severidade >= 4;
    const restam = this.fila.length;
    const img = a.deteccao_codigo ? {codigo: a.deteccao_codigo, fonte: 'deteccoes'}
              : a.tem_imagem      ? {codigo: a.codigo, fonte: 'alertas'} : null;
    const podeAgir = API.pode('edit');
    // O vídeo de teste não é câmera do parque: não há para onde ir.
    const podeVerCam = API.pode('live') && a.camera_codigo && a.camera_codigo !== 'VIDEO-TESTE';
    // "Alerta · gravidade média": a gravidade é feminina, o alerta não — o
    // "Alerta média" que saía da junção direta lia como erro.
    const nivel = critico ? 'Alerta crítico'
                : `Alerta · gravidade ${(SEVERIDADE[a.severidade] || '').toLowerCase()}`;

    faixa.hidden = false;
    faixa.innerHTML = `
      <div class="alerta-veu"></div>
      <section class="alerta-cartao ${critico ? '' : 'sev-media'}" role="alertdialog"
               aria-modal="true" aria-live="assertive" aria-labelledby="alertaTitulo">
        <header class="alerta-topo">
          <span class="alerta-sev"><span class="pulso"></span>${esc(nivel)}</span>
          <span class="alerta-topo-dir">
            <span class="alerta-fila">${this.textoFila(restam)}</span>
            ${a.ocorrido_em ? `<span class="alerta-hora">${ico('clock', 13)}${fmtTime(a.ocorrido_em)}</span>` : ''}
          </span>
        </header>
        <div class="alerta-conteudo">
          ${img ? `<div class="alerta-foto"><canvas data-al-img></canvas>
                     <span class="rotulo-foto">Captura</span></div>` : ''}
          <div class="alerta-info">
            <h2 id="alertaTitulo">${esc(a.titulo)}</h2>
            <div class="alerta-local">
              ${a.camera_nome ? `<span class="etiqueta">${ico('cam', 13)}${esc(a.camera_nome)}</span>` : ''}
              ${a.camera_codigo ? `<span class="etiqueta mono">${esc(a.camera_codigo)}</span>` : ''}
            </div>
            ${a.detalhe ? `<p class="alerta-texto">${esc(a.detalhe)}</p>` : ''}
            <dl class="alerta-dados">
              <div><dt>Registro</dt><dd class="mono">${esc(a.codigo)}</dd></div>
              <div><dt>Ocorrido em</dt><dd class="mono">${a.ocorrido_em ? fmtDT(a.ocorrido_em) : '—'}</dd></div>
              <div><dt>Setor</dt><dd data-al-setor>…</dd></div>
              <div><dt>Situação</dt><dd data-al-status>…</dd></div>
              <div class="largo" data-al-pessoa-bloco hidden><dt>Pessoa identificada</dt><dd data-al-pessoa></dd></div>
            </dl>
          </div>
        </div>
        ${podeAgir ? `
        <div class="alerta-encerrar" data-al-painel hidden>
          <span class="titulo-enc">Encerrar o alerta — qual foi o desfecho?</span>
          <div class="motivos" role="radiogroup">
            ${ALERTA.MOTIVOS.map((m, i) => `<button type="button" class="motivo" role="radio"
                 aria-checked="false" data-motivo="${i}">${esc(m.rotulo)}</button>`).join('')}
          </div>
          <textarea class="inp" data-al-obs rows="2"
            placeholder="Observação (obrigatória em “Outro”): o que foi feito, quem foi acionado…"></textarea>
          <div class="acoes-enc">
            <button class="btn btn-ghost btn-sm" data-al-enc-cancelar>Cancelar</button>
            <button class="btn btn-primary btn-sm" data-al-enc-confirmar disabled>${ico('check', 13)} Confirmar encerramento</button>
          </div>
        </div>` : ''}
        <footer class="alerta-rodape">
          <button class="btn btn-ghost" data-al-ciente title="Dispensa o aviso; o alerta continua aberto na lista">Ciente</button>
          <div class="alerta-botoes">
            ${podeVerCam ? `<button class="btn btn-soft" data-al-cam>${ico('live', 14)} Ver câmera</button>` : ''}
            ${podeAgir ? `<button class="btn btn-soft" data-al-assumir>${ico('users', 14)} Assumir</button>
                          <button class="btn btn-soft" data-al-encerrar>${ico('check', 14)} Encerrar</button>` : ''}
            <button class="btn btn-primary" data-al-ver>${ico('eye', 14)} Abrir alerta</button>
          </div>
        </footer>
      </section>`;

    const cv = $('[data-al-img]', faixa);
    /* A moldura do cartão tem tamanho fixo e o recorte se encaixa dentro dela,
       inteiro e sem esticar — em pé ou deitado. Cortar para preencher seria
       pior: no recorte de 201x570 só caberia 26% da pessoa, sem cabeça nem
       pés. A moldura fixa é o que dá ao cartão a mesma forma em todo alerta. */
    if(cv && img){
      molduraVazia(cv, 196, 216);
      carregarRecorte(cv, img.codigo, 196, img.fonte, 216);
    }
    // O foco vai para a ação principal: Enter abre o alerta sem precisar do mouse.
    setTimeout(() => { const b = $('[data-al-ver]', faixa); if(b) b.focus(); }, 60);

    /* O evento traz o essencial; setor, situação e pessoa vêm da ficha. A
       busca é depois do desenho para o aviso não esperar a rede para aparecer,
       e os campos mostram "…" até lá. */
    const campo = sel => $(sel, faixa);
    API.alerta(a.codigo).then(f => {
      if(this.atual !== a) return;                    // já passou para o próximo
      campo('[data-al-setor]').textContent = (f.camera && f.camera.setor) || '—';
      const st = STATUS_ALERTA[f.status] || {};
      campo('[data-al-status]').innerHTML =
        `<span class="chip ${st.cls || ''}">${esc(st.lbl || f.status || '—')}</span>`
        + (f.atribuido_nome ? ` <span class="resp">com ${esc(f.atribuido_nome)}</span>` : '');
      if(f.pessoa){
        campo('[data-al-pessoa-bloco]').hidden = false;
        campo('[data-al-pessoa]').innerHTML =
          `${esc(f.pessoa.nome)} <span class="mono">${esc(f.pessoa.id_biometrico)}</span>`;
      }
      const b = campo('[data-al-assumir]');
      if(b && f.status !== 'ABERTO'){ b.disabled = true; b.innerHTML = `${ico('check', 14)} Assumido`; }
    }).catch(() => {
      if(this.atual !== a) return;
      campo('[data-al-setor]').textContent = '—';
      campo('[data-al-status]').textContent = '—';
    });

    $('[data-al-ciente]', faixa).onclick = () => this.mostrar();
    $('[data-al-ver]', faixa).onclick = () => {
      const codigo = a.codigo;
      this.mostrar();
      if(API.pode('alerts')) abrirAlerta(codigo);
    };

    const bCam = $('[data-al-cam]', faixa);
    if(bCam) bCam.onclick = () => { this.mostrar(); abrirCamera(a.camera_codigo); };

    /* Assumir fica no próprio aviso: é a resposta mais comum, e é ela que
       impede duas pessoas de despacharem guarnição para a mesma coisa. O
       aviso continua aberto — quem assume costuma querer ver a câmera em
       seguida. */
    const bAss = $('[data-al-assumir]', faixa);
    if(bAss) bAss.onclick = async () => {
      bAss.disabled = true;
      try{
        await API.assumirAlerta(a.codigo);
        bAss.innerHTML = `${ico('check', 14)} Assumido`;
        const st = STATUS_ALERTA.EM_ANDAMENTO;
        campo('[data-al-status]').innerHTML =
          `<span class="chip ${st.cls}">${esc(st.lbl)}</span> <span class="resp">com você</span>`;
        toast('Alerta assumido', `${a.codigo} atribuído a ${API.usuario.nome}.`, 'ok');
        atualizarContadoresDebounced();
      }catch(err){ bAss.disabled = false; erroToast(err); }
    };

    /* Encerrar direto do aviso, com o desfecho escolhido entre os mais comuns.
       O desfecho continua obrigatório — é o que vai para a trilha —, só que
       sem abrir a ficha para os casos em que a resposta é óbvia. */
    const painel = $('[data-al-painel]', faixa);
    const bEnc = $('[data-al-encerrar]', faixa);
    if(painel && bEnc){
      let escolhido = null;
      const obs = $('[data-al-obs]', painel);
      const confirmarBtn = $('[data-al-enc-confirmar]', painel);
      const validar = () => {
        const m = ALERTA.MOTIVOS[escolhido];
        confirmarBtn.disabled = !m || (m.exigeTexto && obs.value.trim().length < 8);
      };
      bEnc.onclick = () => {
        painel.hidden = !painel.hidden;
        bEnc.classList.toggle('ativo', !painel.hidden);
        if(!painel.hidden) $('.motivo', painel).focus();
      };
      $$('.motivo', painel).forEach(b => b.onclick = () => {
        escolhido = +b.dataset.motivo;
        $$('.motivo', painel).forEach(x => x.setAttribute('aria-checked', String(x === b)));
        validar();
      });
      obs.oninput = validar;
      $('[data-al-enc-cancelar]', painel).onclick = () => {
        painel.hidden = true; bEnc.classList.remove('ativo');
      };
      confirmarBtn.onclick = async () => {
        const m = ALERTA.MOTIVOS[escolhido];
        const nota = obs.value.trim();
        const resolucao = m.exigeTexto ? nota : `${m.desfecho}${nota ? ` Observação: ${nota}` : ''}`;
        confirmarBtn.disabled = true;
        try{
          await API.encerrarAlerta(a.codigo, resolucao);
          toast('Alerta encerrado', `${a.codigo} — ${m.rotulo.toLowerCase()}. Desfecho na trilha de auditoria.`, 'ok');
          atualizarContadoresDebounced();
          if(APP.view === 'alerts') irPara('alerts');
          this.mostrar();
        }catch(err){ validar(); erroToast(err); }
      };
    }
  },

  /* Desfechos mais comuns, para encerrar sem abrir a ficha. O texto vai
     inteiro para a trilha — por isso diz o que o operador concluiu, e não só
     um rótulo. "Outro" exige que a pessoa escreva. */
  MOTIVOS: [
    {rotulo: 'Falso positivo',
     desfecho: 'Falso positivo: o operador conferiu a imagem e não houve a ocorrência.'},
    {rotulo: 'Pessoa autorizada',
     desfecho: 'Pessoa autorizada no local: verificado pelo operador, sem providência.'},
    {rotulo: 'Ocorrência atendida',
     desfecho: 'Ocorrência confirmada e atendida pela equipe no local.'},
    {rotulo: 'Outro', exigeTexto: true}
  ]
};

/* ---------- Eventos ao vivo ---------- */
EventosAoVivo.ao(msg => {
  if(msg.tipo === 'conexao'){
    const d = $('#wsDot');
    if(d) d.className = 'dot ' + (msg.dados.estado === 'conectado' ? 'g pulse' : 'r');
    return;
  }
  if(msg.tipo === 'deteccao'){
    const d = msg.dados;
    // Sem toast aqui: o alerta desta detecção chega pelo evento `alerta` e
    // vira painel. Avisar duas vezes o mesmo fato só ensina a ignorar.
    if(APP.view === 'live')  aoVivoNovoEvento(d);
    if(APP.view === 'dash')  painelNovoEvento(d);
    atualizarContadoresDebounced();
  }
  if(msg.tipo === 'quadro_ao_vivo'){
    // Sobreposição do mosaico: a cena atual, que não vira registro.
    FEED.definirCaixas(msg.dados.camera_codigo, msg.dados.caixas, msg.dados.validade_s);
  }
  /* Todo alerta — de detecção ou de sala — entra na fila do painel. */
  if(msg.tipo === 'alerta'){
    ALERTA.enfileirar(msg.dados);
    atualizarContadoresDebounced();
    if(APP.view === 'alerts') irPara('alerts');
  }
  if(msg.tipo === 'alerta_atualizado'){
    atualizarContadoresDebounced();
    if(APP.view === 'alerts') irPara('alerts');
  }
});
const atualizarContadoresDebounced = debounce(atualizarContadores, 2500);

document.addEventListener('seguranca:sessao-expirada', () => {
  toast('Sessão expirada', 'Autentique-se novamente para continuar.', 'warn', 6000);
  EventosAoVivo.desconectar();
  FEED.stop();
  $('#app').hidden = true;
  $('#authScreen').hidden = false;
  mostrarLogin();
  AUTHFX.iniciar();
});

/* ---------- Controles do shell ---------- */
$('#navToggle').onclick = () => document.body.classList.toggle('nav-open');
$('#navScrim').onclick  = () => document.body.classList.remove('nav-open');
$('#btnLogout').onclick = sair;
$('#bellBtn').onclick   = () => irPara('alerts');
$('#usrBtn').onclick    = abrirPerfil;

async function abrirPerfil(){
  const u = API.usuario;
  abrirModal(`
    <div class="modal-hd"><h3>Sessão ativa</h3><span class="sp" style="flex:1"></span>
      <button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:14px;align-items:center;margin-bottom:16px">
        <span class="avatar" style="width:52px;height:52px;font-size:18px">${esc(iniciais(u.nome))}</span>
        <div><b style="font-size:16px;display:block">${esc(u.nome)}</b>
        <span class="chip c-blue" style="margin-top:4px">${esc(rotuloPerfil(u.perfil))}</span></div>
      </div>
      <dl class="kv">
        <dt>Matrícula</dt><dd class="mono">${esc(u.matricula)}</dd>
        <dt>E-mail</dt><dd class="mono">${esc(u.email)}</dd>
        <dt>Setor</dt><dd>${esc(u.unidade || '—')}</dd>
        <dt>MFA</dt><dd>${u.mfa_ativo ? '<span class="chip c-green">Ativo</span>' : '<span class="chip c-amber">Inativo</span>'}</dd>
        <dt>Último acesso</dt><dd class="mono">${u.ultimo_acesso ? fmtDT(u.ultimo_acesso) : '—'}</dd>
        <dt>Permissões</dt><dd style="display:flex;gap:4px;flex-wrap:wrap">${(u.permissoes||[]).map(p => `<span class="chip c-gray">${esc(p)}</span>`).join('')}</dd>
      </dl>
      <div class="note" style="margin-top:16px">${ico('info',15)}<div>Toda consulta a registro biométrico feita nesta sessão fica vinculada à sua matrícula na trilha de auditoria.</div></div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      <button class="btn btn-danger" id="mLogout">${ico('logout',15)} Encerrar sessão</button>
    </div>`);
  $('#mLogout').onclick = () => { fecharModal(); sair(); };
}

/* ---------- Atalhos ---------- */
document.addEventListener('keydown', e => {
  if(!API.usuario || $('#app').hidden) return;
  if(e.target.matches('input, textarea, select')) return;
  const mapa = {'1':'dash','2':'live','3':'cams','4':'facial','5':'objects',
                '6':'actions','7':'biometrics','8':'alerts'};
  if(mapa[e.key]) irPara(mapa[e.key]);
  if(e.key === '/'){
    e.preventDefault();
    const s = document.querySelector('.view.on .search .inp');
    if(s) s.focus();
  }
});

document.addEventListener('visibilitychange', () => {
  if(!document.hidden && API.usuario && !APP.pausado && APP.view === 'live') FEED.start();
});

/* ==========================================================
   BOOT
   ========================================================== */
(async function iniciar(){
  AUTHFX.iniciar();

  /* Link de redefinição no endereço? Ele manda em tudo o mais.

     Vem antes da sessão salva de propósito: quem clica no link do e-mail quer
     trocar a senha, e entrar direto com uma sessão antiga numa máquina
     compartilhada abriria a conta de outra pessoa. */
  const deLink = /[#&]redefinir=([\w\-]+)/.exec(location.hash || '');
  if(deLink){
    const token = deLink[1];
    // Some da barra de endereço antes de qualquer outra coisa: o segredo não
    // precisa continuar visível, nem ir parar no histórico de quem usar a
    // máquina depois. Fica aqui, e não em `abrirPorLink`, porque quem lê o
    // fragmento é quem deve apagá-lo — inclusive no caminho em que o link já
    // foi aberto e `abrirPorLink` nem chega a rodar.
    try{ history.replaceState(null, '', location.pathname + location.search); }catch(e){}
    API.limparSessao();
    if(linkJaAberto(token)){
      mostrarRecover();
      authAlert('#recAlert', 'warn',
        'Este link já foi aberto nesta aba. Se ainda precisar trocar a senha, ' +
        'peça um novo abaixo.');
    }else{
      await abrirPorLink(token);
    }
    return;
  }

  // Sessão salva? Valida contra o servidor antes de confiar nela.
  if(API.restaurar()){
    try{
      const u = await API.eu();
      API.usuario = u;
      await entrarNoSistema(u);
      const h = location.hash.replace('#','');
      if(h && VIEWS[h]) await irPara(h);
      return;
    }catch(e){ API.limparSessao(); }
  }

  mostrarLogin();
  setTimeout(() => $('#loginUser').focus(), 300);
})();

