/* ==========================================================
   SEGURANÇA — Cliente da API

   Um único ponto de contato com o backend. Guarda os tokens,
   renova o access token quando expira e reemite a requisição,
   e traduz erro de HTTP em mensagem legível.
   ========================================================== */
'use strict';

const API = {
  base: '',
  access: null,
  refresh: null,
  usuario: null,
  _renovando: null,

  /* ---------- Sessão ---------- */
  carregarSessao(){
    try{
      const bruto = localStorage.getItem('seguranca.sessao');
      if(!bruto) return false;
      const s = JSON.parse(bruto);
      this.access = s.access; this.refresh = s.refresh; this.usuario = s.usuario;
      return !!this.access;
    }catch(e){ return false; }
  },
  salvarSessao(lembrar){
    const s = {access:this.access, refresh:this.refresh, usuario:this.usuario};
    try{
      if(lembrar) localStorage.setItem('seguranca.sessao', JSON.stringify(s));
      else sessionStorage.setItem('seguranca.sessao', JSON.stringify(s));
    }catch(e){ /* navegação privada: a sessão vive só em memória */ }
  },
  limparSessao(){
    this.access = this.refresh = this.usuario = null;
    try{ localStorage.removeItem('seguranca.sessao'); sessionStorage.removeItem('seguranca.sessao'); }catch(e){}
  },
  restaurar(){
    for(const store of [sessionStorage, localStorage]){
      try{
        const bruto = store.getItem('seguranca.sessao');
        if(bruto){
          const s = JSON.parse(bruto);
          this.access = s.access; this.refresh = s.refresh; this.usuario = s.usuario;
          return true;
        }
      }catch(e){}
    }
    return false;
  },
  pode(permissao){
    return !!(this.usuario && (this.usuario.permissoes||[]).includes(permissao));
  },

  /* ---------- Requisição ---------- */
  // Generoso: envio de foto e busca 1:N passam por aqui e levam segundos
  // legítimos. O que ele corta é a chamada que não volta nunca.
  prazoMs: 45000,

  async _bruto(metodo, rota, {corpo, query, formData, texto, arquivo} = {}){
    let url = this.base + rota;
    if(query){
      const qs = new URLSearchParams();
      Object.entries(query).forEach(([k,v]) => {
        if(v !== undefined && v !== null && v !== '') qs.append(k, v);
      });
      const s = qs.toString();
      if(s) url += '?' + s;
    }

    const opts = {method: metodo, headers: {}};
    if(this.access) opts.headers['Authorization'] = 'Bearer ' + this.access;
    if(formData){ opts.body = formData; }
    else if(corpo !== undefined){
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(corpo);
    }

    // Sem prazo, uma requisição pendurada não falha nunca e a tela que a
    // espera fica no "carregando" para sempre — foi assim que a ficha
    // biométrica parecia "não abrir".
    const relogio = new AbortController();
    const prazo = setTimeout(() => relogio.abort(), this.prazoMs);
    opts.signal = relogio.signal;

    let r;
    try{
      r = await fetch(url, opts);
    }catch(e){
      if(e && e.name === 'AbortError'){
        const estourou = new Error(
          `O servidor não respondeu em ${Math.round(this.prazoMs / 1000)}s. `
          + 'Nada foi gravado — tente de novo.');
        estourou.status = 0;
        estourou.rede = true;
        throw estourou;
      }
      // `fetch` só rejeita quando a requisição não chegou a ter resposta:
      // servidor reiniciando, rede caída, aba aberta há horas. O navegador
      // chama isso de "Failed to fetch", que foi o que a tela mostrou a quem
      // tentava salvar uma câmera — em inglês e sem dizer que nada foi
      // gravado. A distinção importa: erro de validação quer um campo
      // corrigido; este quer uma nova tentativa.
      const falha = new Error(
        'O servidor não respondeu. Nada foi gravado — verifique se o sistema '
        + 'está no ar e tente de novo.');
      falha.status = 0;
      falha.rede = true;
      throw falha;
    }finally{
      clearTimeout(prazo);
    }

    if(r.status === 204) return null;
    if(!r.ok) throw await this._erro(r);
    if(arquivo){
      /* O nome vem do servidor, no Content-Disposition. Sem ele o navegador
         batiza o arquivo com o nome da rota — "exportar" — e o auditor recebe
         um arquivo sem extensão nem data. */
      const cd = r.headers.get('content-disposition') || '';
      const achou = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
      return {blob: await r.blob(),
              nome: achou ? decodeURIComponent(achou[1]) : 'download'};
    }
    if(texto) return await r.text();
    const tipo = r.headers.get('content-type') || '';
    return tipo.includes('application/json') ? await r.json() : await r.text();
  },

  async _erro(r){
    let detalhe = `Erro ${r.status}`;
    try{
      const j = await r.json();
      // `problemas` vem primeiro de propósito. O servidor manda os dois: um
      // `detail` genérico ("Dados inválidos na requisição.") e, ao lado, a
      // lista campo a campo que ele monta justamente para a tela mostrar.
      // Testando `detail` antes, a explicação era descartada sem ser lida, e
      // quem configurava uma câmera via só que não deu — nunca o quê.
      if(Array.isArray(j.problemas) && j.problemas.length){
        detalhe = j.problemas.map(p => `${p.campo}: ${p.problema}`).join(' · ');
      }else if(typeof j.detail === 'string') detalhe = j.detail;
      else if(Array.isArray(j.detail) && j.detail.length){
        // 422 do Pydantic: uma lista de objetos com `loc` e `msg`. Jogado na
        // tela como JSON cru, dizia `[{"type":"value_error","loc":["body",…`,
        // e quem estava configurando a câmera não tinha como saber qual campo
        // recusou. O nome do campo é a única parte que importa aqui.
        detalhe = j.detail.map(p => {
          const campo = Array.isArray(p.loc) ? p.loc.filter(x => x !== 'body').join('.') : '';
          const msg = (p.msg || '').replace(/^Value error,\s*/i, '');
          return campo ? `${campo}: ${msg}` : msg;
        }).filter(Boolean).join(' · ') || JSON.stringify(j.detail);
      }else if(j.detail){ detalhe = JSON.stringify(j.detail); }
    }catch(e){}
    const erro = new Error(detalhe);
    erro.status = r.status;
    return erro;
  },

  async req(metodo, rota, opts = {}){
    try{
      return await this._bruto(metodo, rota, opts);
    }catch(e){
      // 401 em rota autenticada: tenta renovar uma única vez.
      if(e.status === 401 && this.refresh && !rota.startsWith('/auth/')){
        const ok = await this._renovar();
        if(ok) return await this._bruto(metodo, rota, opts);
        this.limparSessao();
        document.dispatchEvent(new CustomEvent('seguranca:sessao-expirada'));
      }
      throw e;
    }
  },

  async _renovar(){
    if(this._renovando) return this._renovando;      // evita corrida de renovações
    this._renovando = (async () => {
      try{
        const d = await this._bruto('POST', '/auth/refresh', {corpo:{refresh_token:this.refresh}});
        this.access = d.access_token; this.refresh = d.refresh_token; this.usuario = d.usuario;
        this.salvarSessao(!!localStorage.getItem('seguranca.sessao'));
        return true;
      }catch(e){ return false; }
      finally{ this._renovando = null; }
    })();
    return this._renovando;
  },

  /* Download de rota autenticada.

     `window.open(url)` não serve aqui: a aba nova não leva o cabeçalho
     `Authorization`, e a rota de exportação exige. O servidor respondia 401,
     a aba mostrava o JSON do erro e a tela já tinha dito "o download começa
     em instantes" — o arquivo nunca chegava e nada explicava por quê.

     Passando por `req`, o download herda o que o resto da aplicação já tem:
     renovação do token em 401, prazo máximo e a tradução dos erros. Em sessão
     longa isso não é luxo — a exportação é justamente o que alguém faz depois
     de uma hora lendo a trilha, quando o token já venceu. */
  async baixar(rota, query){
    const {blob, nome} = await this.req('GET', rota, {query, arquivo: true});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = nome;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Sem revogar, o blob fica na memória da aba até ela fechar — e uma
    // trilha inteira não é um arquivo pequeno.
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    return nome;
  },

  get(rota, query){ return this.req('GET', rota, {query}); },
  post(rota, corpo, query){ return this.req('POST', rota, {corpo, query}); },
  patch(rota, corpo){ return this.req('PATCH', rota, {corpo}); },
  del(rota, query){ return this.req('DELETE', rota, {query}); },

  /* ---------- Autenticação ---------- */
  async login(identificador, senha, lembrar){
    const d = await this._bruto('POST', '/auth/login', {corpo:{identificador, senha}});
    this.access = d.access_token; this.refresh = d.refresh_token; this.usuario = d.usuario;
    this.salvarSessao(lembrar);
    return d.usuario;
  },
  async logout(){
    try{ await this.post('/auth/logout'); }catch(e){}
    this.limparSessao();
  },
  eu(){ return this.get('/auth/eu'); },
  recuperar(email){ return this._bruto('POST', '/auth/recuperar', {corpo:{email}}); },
  // O segredo do link identifica o token e, por ele, o usuário — não é preciso
  // mandar o e-mail junto.
  verificarLink(token){ return this._bruto('POST', '/auth/verificar-link', {corpo:{token}}); },
  redefinirSenha(token, nova_senha){
    return this._bruto('POST', '/auth/redefinir-senha', {corpo:{token, nova_senha}});
  },

  /* ---------- Câmeras ---------- */
  cameras(q){ return this.get('/cameras', q); },
  camera(codigo){ return this.get('/cameras/' + codigo); },
  criarCamera(dados){ return this.post('/cameras', dados); },
  salvarCamera(codigo, dados){ return this.patch('/cameras/' + codigo, dados); },
  removerCamera(codigo, motivo){ return this.del('/cameras/' + codigo, {motivo}); },
  // `lotacao` tira os lugares de passagem: serve ao cadastro de pessoa,
  // não à ficha da câmera, que precisa poder apontar para qualquer um deles.
  setores(lotacao){ return this.get('/cameras/setores' + (lotacao ? '?lotacao=true' : '')); },
  ptz(codigo, comando, velocidade){ return this.post(`/cameras/${codigo}/ptz`, {comando, velocidade:velocidade||0.5}); },
  saudeCamera(codigo){ return this.get(`/cameras/${codigo}/saude`); },
  streams(setor){ return this.get('/stream', {setor}); },

  /* ---------- Base biométrica ---------- */
  pessoas(q){ return this.get('/pessoas', q); },
  pessoa(id){ return this.get('/pessoas/' + id); },
  criarPessoa(dados){ return this.post('/pessoas', dados); },
  salvarPessoa(id, dados){ return this.patch('/pessoas/' + id, dados); },
  excluirPessoa(id, motivo){ return this.del('/pessoas/' + id, {motivo}); },
  esquemaIds(){ return this.get('/pessoas/esquema-ids'); },
  proximoId(sexo){ return this.get('/pessoas/proximo-id', {sexo}); },
  // Passagens, não capturas: quem fica diante da câmera gera uma captura por
  // volta do worker, e lidas uma a uma não dizem por onde a pessoa andou.
  trajetoriaDaPessoa(id, q){ return this.get(`/pessoas/${id}/trajetoria`, q || {}); },
  // Os registros que a trajetória resume, um por linha. `tipo` separa os dois
  // modos de reconhecer: 'rosto' identifica, 'corpo' prova presença.
  registrosDaPessoa(id, tipo, limite){
    return this.get(`/pessoas/${id}/capturas`, {tipo, limite: limite || 200});
  },
  filaConferencia(q){ return this.get('/pessoas/conferencia', q || {}); },
  // A outra metade da triagem: rosto que não se parece com ninguém. A fila
  // de conferência pergunta "é ela?"; esta, "quem é?".
  filaDesconhecidos(q){ return this.get('/pessoas/desconhecidos', q || {}); },
  abrirNaoIdentificado(codigos){ return this.post('/pessoas/nao-identificado', codigos); },
  // Pessoas novas encontradas na triagem, para aprovação humana.
  triagemNovas(){ return this.get('/pessoas/triagem-novas'); },
  triagemAprovar(corpo){ return this.post('/pessoas/triagem-novas/aprovar', corpo); },
  triagemDescartar(chave){ return this.post('/pessoas/triagem-novas/descartar', {chave}); },
  amostrasDeCapturas(id, codigos){
    return this.post(`/pessoas/${id}/amostras-de-capturas`, codigos);
  },
  async enviarAmostra(id, arquivo){
    const fd = new FormData();
    fd.append('imagem', arquivo);
    return this.req('POST', `/pessoas/${id}/amostras`, {formData: fd});
  },
  /* Capturas da própria câmera viram amostra do template: é o material que
     mais se parece com o que se vai reconhecer depois. */
  amostrasDeCapturas(id, codigos){
    return this.post(`/pessoas/${id}/amostras-de-capturas`, codigos);
  },
  async buscarPorImagem(arquivo, limiar){
    const fd = new FormData();
    fd.append('imagem', arquivo);
    return this.req('POST', '/pessoas/buscar', {formData: fd, query:{limiar}});
  },

  /* ---------- Detecções ---------- */
  deteccoes(q){ return this.get('/deteccoes', q); },
  /* Contagem por classe no período, numa requisição só — o gráfico antes
     pedia uma por classe, e as excedentes ficavam na fila de seis conexões
     do navegador, atrás do vídeo. */
  deteccoesPorClasse(q){ return this.get('/deteccoes/por-classe', q); },
  deteccao(codigo){ return this.get('/deteccoes/' + codigo); },
  /* Número: janela de N horas até agora. Objeto {desde, ate}: intervalo
     fechado, que é como o painel olha um dia já encerrado. */
  resumoDeteccoes(q){
    const query = (q && typeof q === 'object') ? q : {horas: q || 24};
    return this.get('/deteccoes/resumo', query);
  },
  revisar(codigo, veredito, observacao){
    return this.post(`/deteccoes/${codigo}/revisao`, {veredito, observacao:observacao||''});
  },
  removerDeteccao(codigo, motivo){
    return this.del(`/deteccoes/${codigo}`, {motivo: motivo || ''});
  },
  expurgar(){ return this.post('/deteccoes/expurgo'); },

  /* ---------- Alertas ---------- */
  alertas(q){ return this.get('/alertas', q); },
  alerta(codigo){ return this.get('/alertas/' + codigo); },
  resumoAlertas(){ return this.get('/alertas/resumo'); },
  assumirAlerta(codigo){ return this.post(`/alertas/${codigo}/assumir`); },
  encerrarAlerta(codigo, resolucao){ return this.post(`/alertas/${codigo}/encerrar`, {resolucao}); },
  encerrarAlertasLote(codigos, resolucao){ return this.post('/alertas/encerrar-lote', {codigos, resolucao}); },
  // O motivo vai no contrato, não só na tela: o servidor recusa 422
  // sem ele, então nenhuma chamada futura apaga sem justificar.
  excluirAlerta(codigo, motivo){ return this.del(`/alertas/${codigo}`, {motivo}); },

  /* ---------- Usuários ---------- */
  usuarios(q, incluirExcluidos){
    return this.get('/usuarios', {q, incluir_excluidos: incluirExcluidos ? 'true' : ''});
  },
  perfis(){ return this.get('/usuarios/perfis'); },
  criarUsuario(dados){ return this.post('/usuarios', dados); },
  salvarUsuario(id, dados){ return this.patch('/usuarios/' + id, dados); },
  bloquearUsuario(id, motivo){ return this.post(`/usuarios/${id}/bloqueio`, undefined, {motivo}); },
  linkRedefinicao(id, motivo){ return this.post(`/usuarios/${id}/link-redefinicao`, undefined, {motivo}); },
  desbloquearUsuario(id){ return this.del(`/usuarios/${id}/bloqueio`); },
  excluirUsuario(id, motivo){ return this.del('/usuarios/' + id, {motivo}); },

  /* ---------- Configuração ---------- */
  configuracao(){ return this.get('/configuracao'); },
  salvarConfiguracao(valores){ return this.req('PUT', '/configuracao', {corpo:{valores}}); },

  /* ---------- Auditoria ---------- */
  auditoria(q){ return this.get('/auditoria', q); },
  acoesAuditoria(){ return this.get('/auditoria/acoes'); },
  verificarCadeia(){ return this.get('/auditoria/verificar'); },
  exportarAuditoria(finalidade, filtro){
    return this.baixar('/auditoria/exportar', {finalidade, ...(filtro || {})});
  },

  /* ---------- Sistema ---------- */
  saude(){ return this.get('/saude'); },
  info(){ return this.get('/api/info'); }
};


/* ==========================================================
   WebSocket de eventos ao vivo
   ========================================================== */
const EventosAoVivo = {
  ws: null,
  ouvintes: [],
  tentativas: 0,
  pingTimer: null,
  fechandoDeProposito: false,

  conectar(){
    if(!API.access) return;
    this.fechandoDeProposito = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/eventos?token=${encodeURIComponent(API.access)}`;

    try{ this.ws = new WebSocket(url); }
    catch(e){ return this._reagendar(); }

    this.ws.onopen = () => {
      this.tentativas = 0;
      this._emitir({tipo:'conexao', dados:{estado:'conectado'}});
      clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => {
        if(this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send('ping');
      }, 25000);
    };
    this.ws.onmessage = (ev) => {
      if(ev.data === 'pong') return;
      try{ this._emitir(JSON.parse(ev.data)); }catch(e){}
    };
    this.ws.onclose = () => {
      clearInterval(this.pingTimer);
      this._emitir({tipo:'conexao', dados:{estado:'desconectado'}});
      if(!this.fechandoDeProposito) this._reagendar();
    };
    this.ws.onerror = () => { try{ this.ws.close(); }catch(e){} };
  },

  _reagendar(){
    // Espera crescente até 30s — reconectar em rajada não ajuda ninguém.
    this.tentativas++;
    const espera = Math.min(1000 * Math.pow(1.6, this.tentativas), 30000);
    setTimeout(() => { if(API.access) this.conectar(); }, espera);
  },

  desconectar(){
    this.fechandoDeProposito = true;
    clearInterval(this.pingTimer);
    if(this.ws){ try{ this.ws.close(); }catch(e){} this.ws = null; }
  },

  ao(fn){ this.ouvintes.push(fn); return () => {
    const i = this.ouvintes.indexOf(fn);
    if(i >= 0) this.ouvintes.splice(i, 1);
  }; },

  _emitir(msg){ this.ouvintes.forEach(fn => { try{ fn(msg); }catch(e){ console.error(e); } }); }
};

